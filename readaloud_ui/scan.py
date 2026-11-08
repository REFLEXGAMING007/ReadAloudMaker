"""Light-weight folder inspection for the UI.

The scan never loads the alignment model and never runs FFmpeg. It reads the
EPUB spine so the UI can show how many chapters and sentences were found and
warn about the classic "one audio file per chapter" mismatch before a build
starts.
"""

from __future__ import annotations

import os
import re

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".m4b", ".wav", ".ogg", ".flac", ".aac", ".opus",
}


class ScanError(ValueError):
    def __init__(self, message, hint=""):
        super().__init__(message)
        self.hint = hint


def natural_key(value):
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r"(\d+)", value)]


def _list_audio(folder):
    found = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
            found.append(path)
    found.sort(key=lambda path: natural_key(os.path.basename(path)))
    return found


def _list_epubs(folder):
    found = [
        os.path.join(folder, name)
        for name in os.listdir(folder)
        if name.lower().endswith(".epub") and os.path.isfile(os.path.join(folder, name))
    ]
    found.sort(key=natural_key)
    return found


def _file_info(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {"name": os.path.basename(path), "path": path, "size": size}


def _qwen_details(epub_path, exclude):
    from qwen_pipeline.align import SUPPORTED_LANGUAGES, language_name
    from qwen_pipeline.epub import EpubArchive
    from qwen_pipeline.text import analyse_document
    from qwen_pipeline.xmlutil import parse_xml

    archive = EpubArchive(epub_path)
    try:
        documents = archive.text_documents(exclude)
        chapters = []
        fragments = 0
        for index, document in enumerate(documents, start=1):
            tree = parse_xml(archive.read(document.zip_name), document.href)
            chapter = analyse_document(tree, index)
            chapters.append({
                "href": document.href,
                "fragments": len(chapter.fragments),
            })
            fragments += len(chapter.fragments)

        language = (archive.metadata.get("language") or "").strip()
        title = (archive.metadata.get("title") or "").strip()
        author = (archive.metadata.get("author") or "").strip() or ""
        language_ok = True
        if language:
            try:
                language_name(language)
            except Exception:
                language_ok = False
        return {
            "chapters": len(chapters),
            "chapterList": chapters,
            "fragments": fragments,
            "language": language,
            "languageOk": language_ok,
            "supported": list(SUPPORTED_LANGUAGES),
            "title": title,
            "author": author,
        }
    finally:
        archive.close()


def _classic_details(epub_path, exclude):
    import make_readaloud

    chapters, metadata, _cover = make_readaloud.read_epub(epub_path, exclude or
                                                          make_readaloud.DEFAULT_EXCLUDE)
    return {
        "chapters": len(chapters),
        "chapterList": [
            {"href": chapter.get("zip_name", ""), "title": chapter.get("title", "")}
            for chapter in chapters
        ],
        "fragments": None,
        "language": (metadata.get("language") or "").strip(),
        "languageOk": True,
        "supported": [],
        "title": (metadata.get("title") or "").strip(),
        "author": (metadata.get("author") or "").strip(),
    }


def scan_folder(folder, engine="qwen", exclude=""):
    """Inspect ``folder`` and return a JSON-friendly summary."""
    folder = (folder or "").strip().strip('"')
    if not folder:
        raise ScanError("Choose a folder that contains the EPUB and audio files.")
    folder = os.path.abspath(os.path.expandvars(os.path.expanduser(folder)))
    if not os.path.isdir(folder):
        raise ScanError("Folder not found: " + folder,
                        "Check the path or drag the files into the window.")

    epubs = _list_epubs(folder)
    audio = _list_audio(folder)

    result = {
        "folder": folder,
        "engine": engine,
        "epub": os.path.basename(epubs[0]) if epubs else "",
        "epubPath": epubs[0] if epubs else "",
        "epubs": [os.path.basename(path) for path in epubs],
        "audio": [_file_info(path) for path in audio],
        "output": os.path.join(folder, "output"),
        "chapters": None,
        "chapterList": [],
        "fragments": None,
        "language": "",
        "languageOk": True,
        "supported": [],
        "title": "",
        "author": "",
        "warnings": [],
        "ready": False,
    }

    if not epubs:
        result["warnings"].append("No .epub file in this folder.")
    elif len(epubs) > 1:
        result["warnings"].append(
            "%d EPUB files found. Pick the one to build below." % len(epubs)
        )

    if not audio:
        result["warnings"].append("No chapter audio files in this folder.")

    if epubs:
        default_exclude = exclude
        if not default_exclude:
            try:
                if engine == "classic":
                    import make_readaloud
                    default_exclude = make_readaloud.DEFAULT_EXCLUDE
                else:
                    from qwen_pipeline.build import DEFAULT_EXCLUDE
                    default_exclude = DEFAULT_EXCLUDE
            except Exception:
                default_exclude = r"(?i)(^|/)(nav|toc|cover|titlepage|title-page)(\.xhtml|\.html)?$"

        try:
            details = (_classic_details(epubs[0], default_exclude) if engine == "classic"
                       else _qwen_details(epubs[0], default_exclude))
            result.update(details)
        except BaseException as exc:  # scripts raise SystemExit on bad input
            result["warnings"].append(
                "Could not read the EPUB: %s" % (str(exc).strip() or exc.__class__.__name__)
            )

    if result["chapters"] is not None:
        if result["chapters"] and len(audio) != result["chapters"]:
            result["warnings"].append(
                "%d chapter(s) in the EPUB but %d audio file(s). "
                "There must be one audio file per chapter."
                % (result["chapters"], len(audio))
            )
        elif result["chapters"] and not audio:
            result["warnings"].append("Chapter audio is missing.")

    if engine == "qwen" and result["language"] and not result.get("languageOk", True):
        result["warnings"].append(
            "Qwen3 does not support %r. Choose a supported language in Advanced settings."
            % result["language"]
        )

    result["ready"] = (
        len(epubs) == 1
        and bool(audio)
        and (result["chapters"] is None or result["chapters"] == len(audio))
        and result.get("languageOk", True)
    )
    return result
