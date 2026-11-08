"""Orchestration for the Qwen3 pipeline."""

import json
import os
import posixpath
import re
import shutil
import subprocess
from dataclasses import dataclass, field

from .align import ProportionalAligner, QwenAligner, language_name
from .chunk import (
    HARD_LIMIT_SECONDS,
    detect_silences,
    plan_chunks,
    probe_duration,
)
from .epub import (
    EpubArchive,
    find_audio_files,
    find_epub_file,
    write_epub,
)
from .errors import PipelineError
from .overlay import (
    Overlay,
    apply_overlays,
    build_smil,
    relative_href,
)
from .text import (
    analyse_document,
    assign_fragment_times,
    ensure_positive_durations,
    map_tokens_to_text,
    normalize_stream,
    token_key,
)
from .validate import validate_epub
from .xmlutil import parse_xml, serialize_xml

WORK_DIR = "_qwen_work"
OUTPUT_DIR = "output"
DEFAULT_EXCLUDE = r"(?i)(^|/)(nav|toc|cover|titlepage|title-page)(\.xhtml|\.html)?$"
COPYABLE_AUDIO = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".m4b": "audio/mp4"}


@dataclass
class BuildOptions:
    folder: str
    epub: str = None
    audio_dir: str = None
    out: str = None
    lang: str = None
    exclude: str = DEFAULT_EXCLUDE
    max_chunk_seconds: float = 150.0
    min_coverage: float = 0.98
    device: str = "auto"
    dtype: str = "auto"
    model: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    compile_model: bool = False
    keep_work: bool = False
    dry_run: bool = False
    fake_aligner: bool = False
    epubcheck: str = None


@dataclass
class ChapterBuild:
    index: int
    document: object
    chapter: object
    audio_path: str
    duration: float = 0.0
    chunks: list = field(default_factory=list)
    coverage: float = 1.0
    unmatched: int = 0
    max_drift: float = 0.0
    audio_zip: str = ""
    media_type: str = "audio/mpeg"
    smil_zip: str = ""


def log(message):
    print("[readaloud-qwen] " + message, flush=True)


def clean_filename(value, fallback="Read Aloud Book"):
    value = (value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:150]


def unique_path(path):
    if not os.path.exists(str(path)):
        return str(path)
    stem, extension = os.path.splitext(str(path))
    counter = 2
    while os.path.exists("%s (%d)%s" % (stem, counter, extension)):
        counter += 1
    return "%s (%d)%s" % (stem, counter, extension)


def _resolve_audio(input_path, folder):
    if os.path.isabs(input_path):
        return input_path
    candidate = os.path.join(folder, input_path)
    return candidate if os.path.exists(candidate) else input_path


def _format_chapter_list(chapters, audio_paths):
    lines = ["Chapter documents found in the EPUB (spine order):"]
    for index, document in enumerate(chapters, start=1):
        lines.append("  %2d. %s" % (index, document.href))
    lines.append("")
    lines.append("Audio files found (natural sort order):")
    for index, path in enumerate(audio_paths, start=1):
        lines.append("  %2d. %s" % (index, os.path.basename(path)))
    lines.append("")
    lines.append("There must be exactly one audio file per chapter.")
    return "\n".join(lines)


def _title_of(chapter):
    text = getattr(chapter, "chapter", None)
    if text is not None and text.fragments:
        first = text.fragments[0].text.strip()
        if first:
            return first[:60]
    return ""


def _unique_item_id(archive, base):
    used = set(archive.manifest)
    if base not in used:
        return base
    counter = 2
    while "%s-%d" % (base, counter) in used:
        counter += 1
    return "%s-%d" % (base, counter)


def _opf_relative(zip_name, opf_dir):
    """Path of a ZIP entry relative to the OPF directory.

    ``posixpath.relpath(name, "")`` trips over Windows path rules, so an OPF at
    the archive root is handled explicitly.
    """
    if not opf_dir:
        return zip_name
    return posixpath.relpath(zip_name, opf_dir)


def _asset_dir(archive, preferred, fallback):
    """Pick a new asset directory inside the OPF directory.

    Keeping generated files next to the package document is what readers (and
    epubcheck) expect; putting them at the ZIP root works but is unusual.
    """

    def inside(name):
        return posixpath.join(archive.opf_dir, name) if archive.opf_dir else name

    def is_free(name):
        prefix = inside(name) + "/"
        return not any(entry.startswith(prefix) for entry in archive.names)

    if is_free(preferred):
        return inside(preferred)
    if is_free(fallback):
        return inside(fallback)
    counter = 2
    while not is_free("%s-%d" % (fallback, counter)):
        counter += 1
    return inside("%s-%d" % (fallback, counter))


def _transcode_to_mp3(source, destination):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PipelineError(
            "FFmpeg is required to convert %s to MP3" % os.path.basename(source)
        )
    result = subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-vn", "-ac", "1", "-ar", "22050",
            "-b:a", "96k", str(destination),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise PipelineError(
            "FFmpeg could not convert %s:\n%s"
            % (source, result.stderr.decode("utf-8", "replace")[-800:])
        )


def _prepare_audio(chapter, work_dir):
    extension = posixpath.splitext(chapter.audio_path)[1].lower()
    if extension in COPYABLE_AUDIO:
        return chapter.audio_path, COPYABLE_AUDIO[extension], extension
    target = os.path.join(work_dir, "audio", "chapter%04d.mp3" % chapter.index)
    _transcode_to_mp3(chapter.audio_path, target)
    return target, "audio/mpeg", ".mp3"


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def build(options, aligner=None):
    folder = os.path.abspath(options.folder)
    if not os.path.isdir(folder):
        raise PipelineError("folder not found: " + folder)
    work_dir = os.path.join(folder, WORK_DIR)
    epub_path = options.epub or find_epub_file(folder)
    if not os.path.isabs(epub_path):
        candidate = os.path.join(folder, epub_path)
        epub_path = candidate if os.path.exists(candidate) else epub_path
    audio_dir = options.audio_dir or folder
    if not os.path.isabs(audio_dir):
        audio_dir = os.path.join(folder, audio_dir)

    archive = EpubArchive(epub_path)
    try:
        if not archive.is_epub3():
            raise PipelineError(
                "this EPUB is version %s. Media Overlays need EPUB 3. "
                "Convert it first (for example with Calibre)." % archive.version
            )

        documents = archive.text_documents(options.exclude)
        if not documents:
            raise PipelineError(
                "no narrated spine documents were found. "
                "Check --exclude or the EPUB's spine."
            )
        audio_paths = find_audio_files(audio_dir)

        log("EPUB:    " + os.path.basename(epub_path))
        log("chapters: %d   audio files: %d" % (len(documents), len(audio_paths)))

        if len(documents) != len(audio_paths):
            raise PipelineError(_format_chapter_list(documents, audio_paths))

        language = language_name(options.lang or archive.metadata.get("language"))

        chapters = []
        for index, document in enumerate(documents, start=1):
            raw = archive.read(document.zip_name)
            tree = parse_xml(raw, document.href)
            chapter_text = analyse_document(tree, index)
            if not chapter_text.fragments:
                raise PipelineError(
                    "no speakable text was found in %s" % document.href
                )
            chapters.append(
                ChapterBuild(
                    index=index,
                    document=document,
                    chapter=chapter_text,
                    audio_path=audio_paths[index - 1],
                )
            )

        total_fragments = sum(len(chapter.chapter.fragments) for chapter in chapters)
        log("language: %s   fragments: %d" % (language, total_fragments))

        for chapter in chapters:
            chapter.duration = probe_duration(chapter.audio_path)
            silences = []
            if chapter.duration > options.max_chunk_seconds + 5.0:
                silences = detect_silences(chapter.audio_path)
            chapter.chunks = plan_chunks(
                chapter.duration,
                chapter.chapter.fragments,
                max_seconds=options.max_chunk_seconds,
                silences=silences,
            )

        if options.dry_run:
            return _dry_run(archive, chapters, work_dir, language)

        if aligner is None:
            if options.fake_aligner:
                aligner = ProportionalAligner()
            else:
                aligner = QwenAligner(
                    model_id=options.model,
                    device=options.device,
                    compile_model=options.compile_model,
                    dtype=options.dtype,
                )
                log("loading %s (device=%s) ..." % (options.model, options.device))
                aligner.load()

        for chapter in chapters:
            _align_chapter(chapter, aligner, language, options)
            log(
                "[%d/%d] %s: %d chunks, %d fragments, coverage %.1f%%"
                % (
                    chapter.index,
                    len(chapters),
                    _title_of(chapter) or chapter.document.href,
                    len(chapter.chunks),
                    len(chapter.chapter.fragments),
                    chapter.coverage * 100.0,
                )
            )

        return _package(archive, chapters, epub_path, folder, work_dir,
                        options, language)
    finally:
        archive.close()


def _align_with_growth(aligner, audio_path, start, end, piece, language,
                       is_last, duration, label):
    """Align one text chunk, widening the audio window if it ends too early.

    A window that ends just after the last aligned word means the model may not
    have heard the rest of the sentence, so the same text is aligned again with
    more audio. Windows are capped at the model limit and the file length.
    """
    edge = 3.0
    expansion = 60.0
    limit = start + HARD_LIMIT_SECONDS

    while True:
        tokens = aligner.align_file_slice(
            audio_path, start, end - start, piece, language
        )
        if not tokens or is_last or end >= duration - 0.001:
            return tokens, end

        last_end = max(token.end_time for token in tokens)
        if last_end <= end - edge:
            return tokens, end

        wider = min(duration, end + expansion, limit)
        if wider <= end + 0.5:
            raise PipelineError(
                "%s: the text of this alignment pass needs more than %.0f "
                "seconds of audio, which is past the model limit.\n"
                "Lower --max-chunk-seconds (for example 90) and try again."
                % (label, HARD_LIMIT_SECONDS)
            )
        log(
            "  %s: window ended at %.1fs but the text reached %.1fs; "
            "re-aligning with %.1fs of audio"
            % (label, end, last_end, wider - start)
        )
        end = wider


def _align_chapter(chapter, aligner, language, options):
    text = chapter.chapter.text
    fragments = chapter.chapter.fragments
    duration = chapter.duration
    spans = []
    cursor = 0.0
    total_unmatched = 0
    expected_characters = 0
    matched_characters = 0
    last_index = len(chapter.chunks) - 1

    for position, chunk in enumerate(chapter.chunks):
        piece = text[chunk.text_start:chunk.text_end]
        if not piece.strip():
            raise PipelineError("empty text chunk in " + chapter.document.href)

        start = min(max(cursor, 0.0), max(0.0, duration - 0.2))
        if position == last_index:
            end = duration
        else:
            end = min(duration, start + max(1.0, chunk.end - chunk.start))
        if end - start <= 0.2:
            raise PipelineError(
                "chunk %d of %s has no audio left; a previous chunk overran."
                % (position + 1, chapter.document.href)
            )

        try:
            tokens, end = _align_with_growth(
                aligner,
                chapter.audio_path,
                start,
                end,
                piece,
                language,
                is_last=(position == last_index),
                duration=duration,
                label="%s chunk %d" % (chapter.document.href, position + 1),
            )
        except PipelineError:
            raise
        except Exception as exc:
            raise PipelineError(
                "alignment failed on %s, chunk %d: %s"
                % (chapter.document.href, position + 1, exc)
            )

        mapped, unmatched = map_tokens_to_text(piece, tokens)
        total_unmatched += len(unmatched)
        expected = len(normalize_stream(piece)[0])
        matched = sum(len(token_key(token.text)) for token, _, _ in mapped)
        expected_characters += expected
        matched_characters += matched

        if expected and matched / float(expected) < options.min_coverage:
            raise PipelineError(
                "only %.1f%% of chunk %d of %s aligned (minimum %.0f%%).\n"
                "The EPUB text does not match the narration at that point."
                % (
                    matched / float(expected) * 100.0,
                    position + 1,
                    chapter.document.href,
                    options.min_coverage * 100.0,
                )
            )

        for token, token_start, token_end in mapped:
            token.char_start = chunk.text_start + token_start
            token.char_end = chunk.text_start + token_end
            spans.append((token, token.char_start, token.char_end))

        if not tokens:
            cursor = end
            continue

        last_end = max(token.end_time for token in tokens)
        if last_end > duration + 2.0:
            raise PipelineError(
                "the aligned narration for %s ends after the audio "
                "(%.1fs > %.1fs). The text does not match the audio."
                % (chapter.document.href, last_end, duration)
            )

        # Slight overlap so a word at the seam is never cut in half.
        cursor = max(start, last_end - 0.25)

    spans.sort(key=lambda item: item[1])

    if expected_characters:
        chapter.coverage = min(1.0, matched_characters / float(expected_characters))
    chapter.unmatched = total_unmatched
    if chapter.coverage < options.min_coverage:
        raise PipelineError(
            "only %.1f%% of the words in %s could be matched to the audio "
            "(minimum %.0f%%).\n"
            "The EPUB text probably differs from what the narration says "
            "(skipped front matter, numbers read differently, ad-libs).\n"
            "Try --exclude to drop non-narrated chapters, or check the audio order."
            % (chapter.coverage * 100.0, chapter.document.href, options.min_coverage * 100.0)
        )

    assign_fragment_times(fragments, spans)
    ensure_positive_durations(fragments, duration=duration)

    previous = 0.0
    drift = 0.0
    for fragment in fragments:
        if fragment.start_time + 0.0001 < previous:
            drift = max(drift, previous - fragment.start_time)
        previous = max(previous, fragment.end_time)
    chapter.max_drift = drift


def _package(archive, chapters, epub_path, folder, work_dir, options, language):
    os.makedirs(os.path.join(work_dir, "align"), exist_ok=True)
    os.makedirs(os.path.join(work_dir, "audio"), exist_ok=True)
    os.makedirs(os.path.join(work_dir, "text"), exist_ok=True)

    audio_dir_name = _asset_dir(archive, "audio", "ra-audio")
    overlay_dir_name = _asset_dir(archive, "overlays", "ra-overlays")

    replacements = {}
    additions = []
    overlays = []
    audio_durations = {}
    report_chapters = []
    total_duration = 0.0

    for chapter in chapters:
        audio_path, media_type, extension = _prepare_audio(chapter, work_dir)
        chapter.audio_zip = "%s/chapter%04d%s" % (audio_dir_name, chapter.index, extension)
        chapter.media_type = media_type
        chapter.smil_zip = "%s/chapter%04d.smil" % (overlay_dir_name, chapter.index)

        audio_item_id = _unique_item_id(archive, "ra-audio-%04d" % chapter.index)
        smil_item_id = _unique_item_id(archive, "ra-smil-%04d" % chapter.index)

        text_href, audio_href = _relative_refs(chapter, archive)
        smil_bytes = build_smil(
            text_href,
            audio_href,
            chapter.chapter.fragments,
            sequence_id="ra-seq-%04d" % chapter.index,
        )

        overlay = Overlay(
            xhtml_item_id=chapter.document.item.id,
            xhtml_href=chapter.document.item.href,
            smil_item_id=smil_item_id,
            smil_href=_opf_relative(chapter.smil_zip, archive.opf_dir),
            audio_item_id=audio_item_id,
            audio_href=_opf_relative(chapter.audio_zip, archive.opf_dir),
            media_type=media_type,
            duration=chapter.duration,
            smil_bytes=smil_bytes,
        )

        replacements[chapter.document.zip_name] = chapter.chapter.serialized()
        additions.append((chapter.audio_zip, audio_path))
        additions.append((chapter.smil_zip, smil_bytes))
        overlays.append(overlay)
        audio_durations[chapter.audio_zip] = chapter.duration
        total_duration += chapter.duration

        _write_json(
            os.path.join(work_dir, "align", "chapter%04d.json" % chapter.index),
            {
                "chapter_id": chapter.index,
                "xhtml": chapter.document.href,
                "audio": os.path.basename(chapter.audio_path),
                "duration": chapter.duration,
                "coverage": chapter.coverage,
                "fragments": [
                    {
                        "id": fragment.id,
                        "text": fragment.text,
                        "start": round(fragment.start_time, 3),
                        "end": round(fragment.end_time, 3),
                    }
                    for fragment in chapter.chapter.fragments
                ],
            },
        )
        report_chapters.append(
            {
                "index": chapter.index,
                "xhtml": chapter.document.href,
                "audio": os.path.basename(chapter.audio_path),
                "duration": round(chapter.duration, 3),
                "chunks": len(chapter.chunks),
                "fragments": len(chapter.chapter.fragments),
                "coverage": round(chapter.coverage, 4),
                "unmatched_words": chapter.unmatched,
                "max_drift": round(chapter.max_drift, 4),
            }
        )

    apply_overlays(archive.opf_tree, overlays, total_duration)
    replacements[archive.opf_name] = serialize_xml(archive.opf_tree, pretty=True)

    output_dir = options.out or os.path.join(folder, OUTPUT_DIR)
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    title = clean_filename(archive.metadata.get("title") or os.path.splitext(os.path.basename(epub_path))[0])
    destination = unique_path(os.path.join(output_dir, title.replace(" ", "_") + "_readaloud.epub"))

    log("packaging EPUB3 with Media Overlays ...")
    write_epub(epub_path, destination, replacements, additions)

    report = validate_epub(destination, audio_durations)
    log("check:   EPUB OK (%d media overlays, %d <par>)" % (report["overlays"], report["pars"]))

    if options.epubcheck:
        _run_epubcheck(destination, options.epubcheck)

    payload = {
        "book": title,
        "language": language,
        "total_duration": round(total_duration, 3),
        "chapters": report_chapters,
        "validation": report,
        "epubcheck": "passed" if options.epubcheck else "not run",
    }
    _write_json(os.path.join(work_dir, "report.json"), payload)
    _write_json(os.path.join(output_dir, "report.json"), payload)

    if not options.keep_work:
        shutil.rmtree(work_dir, ignore_errors=True)

    log("DONE: " + destination)
    return destination


def _relative_refs(chapter, archive):
    text_href = relative_href(chapter.document.zip_name, chapter.smil_zip)
    audio_href = relative_href(chapter.audio_zip, chapter.smil_zip)
    return text_href, audio_href


def _dry_run(archive, chapters, work_dir, language):
    os.makedirs(os.path.join(work_dir, "text"), exist_ok=True)
    log("dry run: no model will be loaded")
    payload = []
    for chapter in chapters:
        with open(
            os.path.join(work_dir, "text", "chapter%04d.txt" % chapter.index),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(chapter.chapter.text)
        plan = {
            "index": chapter.index,
            "xhtml": chapter.document.href,
            "audio": os.path.basename(chapter.audio_path),
            "duration": round(chapter.duration, 3),
            "fragments": len(chapter.chapter.fragments),
            "words": chapter.chapter.word_count,
            "chunks": [
                {
                    "start": round(chunk.start, 3),
                    "end": round(chunk.end, 3),
                    "fragments": chunk.last - chunk.first,
                    "text_chars": chunk.text_end - chunk.text_start,
                }
                for chunk in chapter.chunks
            ],
        }
        payload.append(plan)
        log(
            "[%d/%d] %s: %.1fs, %d fragments, %d chunk(s)"
            % (
                chapter.index,
                len(chapters),
                chapter.document.href,
                chapter.duration,
                len(chapter.chapter.fragments),
                len(chapter.chunks),
            )
        )
    _write_json(os.path.join(work_dir, "plan.json"), {"language": language, "chapters": payload})
    log("plan written to " + os.path.join(work_dir, "plan.json"))
    return None


def _run_epubcheck(path, executable):
    command = [executable, str(path)]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise PipelineError("could not run epubcheck: %s" % exc)
    if result.returncode != 0:
        output = (result.stdout + result.stderr).decode("utf-8", "replace")
        raise PipelineError(
            "epubcheck reported problems:\n" + output[-4000:]
        )
    log("epubcheck: passed")
