#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-click read-aloud EPUB3 maker.

Put this script (and run.bat) in a folder with:
  - exactly one .epub
  - one audio file per chapter (.mp3, .m4a, .m4b, .wav, .ogg, .flac, .aac, .opus)

Then double-click run.bat, or run:
    python make_readaloud.py

It produces:
    output/<book title>_readaloud.epub

Pipeline:
  EPUB -> chapter XHTML in spine order -> inject id="f000001"...
  audio -> chapter0001.mp3 ...
  aeneas -> smil/chapter0001.smil ...
  syncabook create -> EPUB3 with Media Overlays

No Docker and no afaligner are needed. Aeneas must be installed for the Python
that runs this script (Python 3.9 on this machine).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
import posixpath
import xml.etree.ElementTree as ET
from html import escape as html_escape
from html import unescape
from pathlib import Path

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"

VERSION = "1.0.0"

AUDIO_EXTS = (".mp3", ".m4a", ".m4b", ".wav", ".ogg", ".flac", ".aac", ".opus")
DEFAULT_EXCLUDE = r"(?i)(^|/)(cover|nav|toc)\.x?html?$"
MIN_TEXT_CHARS = 50

WORK_DIR_NAME = "_readaloud_work"
OUTPUT_DIR_NAME = "output"


def log(message):
    print("[readaloud] " + message, flush=True)


def fail(message):
    raise SystemExit("\n[readaloud] ERROR: " + message + "\n")


# ---------------------------------------------------------------------------
# EPUB input
# ---------------------------------------------------------------------------

def strip_tags(html):
    html = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ", html)
    text = unescape(re.sub(r"(?s)<[^>]+>", " ", html))
    return re.sub(r"\s+", " ", text).strip()


def decode_html(raw):
    """Decode an XHTML/HTML document, honouring a BOM or an XML declaration."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    match = re.search(rb"""encoding\s*=\s*["']([A-Za-z0-9._-]+)["']""", raw[:200])
    if match:
        try:
            return raw.decode(match.group(1).decode("ascii"))
        except (UnicodeDecodeError, LookupError):
            pass
    # Last resort: cp1252 is a superset of latin-1 for printable characters.
    return raw.decode("cp1252", errors="replace")


def guess_title(html):
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    if match:
        title = strip_tags(match.group(1))
        if title:
            return title
    for tag in ("h1", "h2", "h3"):
        match = re.search(r"(?is)<%s[^>]*>(.*?)</%s>" % (tag, tag), html)
        if match:
            title = strip_tags(match.group(1))
            if title:
                return title
    return ""


def read_epub(epub_path, exclude_regex):
    try:
        archive = zipfile.ZipFile(epub_path)
    except zipfile.BadZipFile:
        fail("not a valid EPUB/ZIP file: " + str(epub_path))

    with archive:
        try:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
        except KeyError:
            fail("EPUB has no META-INF/container.xml")
        except ET.ParseError as exc:
            fail("EPUB container.xml is not well-formed XML: %s" % exc)

        rootfile = container.find(".//{%s}rootfile" % CONTAINER_NS)
        if rootfile is None or not rootfile.get("full-path"):
            fail("EPUB container.xml has no rootfile")

        opf_path = rootfile.get("full-path")
        opf_dir = posixpath.dirname(opf_path)
        try:
            opf_raw = archive.read(opf_path)
        except KeyError:
            fail("EPUB is missing its package document: " + opf_path)
        try:
            opf = ET.fromstring(opf_raw)
        except ET.ParseError as exc:
            fail("EPUB package document %s is not well-formed XML: %s"
                 % (opf_path, exc))

        manifest = {}
        for item in opf.findall(".//{%s}manifest/{%s}item" % (OPF_NS, OPF_NS)):
            manifest[item.get("id")] = {
                "href": item.get("href") or "",
                "media_type": item.get("media-type") or "",
                "properties": item.get("properties") or "",
            }

        chapters = []
        for itemref in opf.findall(".//{%s}spine/{%s}itemref" % (OPF_NS, OPF_NS)):
            if (itemref.get("linear") or "yes").lower() == "no":
                continue
            item = manifest.get(itemref.get("idref"))
            if not item:
                continue
            if item["media_type"] not in ("application/xhtml+xml", "text/html"):
                continue
            zip_name = posixpath.normpath(posixpath.join(opf_dir, item["href"]))
            if re.search(exclude_regex, zip_name):
                continue
            try:
                raw = archive.read(zip_name)
            except KeyError:
                continue
            html = decode_html(raw)
            if html is None:
                continue
            if len(strip_tags(html)) < MIN_TEXT_CHARS:
                continue
            chapters.append({
                "zip_name": zip_name,
                "title": guess_title(html),
                "html": html,
            })

        metadata = {}
        for tag, key in (("title", "title"), ("creator", "author"),
                         ("language", "language"), ("description", "description")):
            element = opf.find(".//{%s}%s" % (DC_NS, tag))
            if element is not None and element.text:
                metadata[key] = element.text.strip()

        cover = None
        for item in manifest.values():
            if "cover-image" in item["properties"]:
                try:
                    cover = (
                        archive.read(posixpath.normpath(
                            posixpath.join(opf_dir, item["href"]))),
                        posixpath.splitext(item["href"])[1].lower(),
                    )
                except KeyError:
                    pass
                break

    if not chapters:
        fail("no chapter XHTML found in the EPUB spine")
    return chapters, metadata, cover


# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

def inject_ids(html):
    """Add id="f000001"... to every spoken block.

    Three passes, matching the original workflow:
      1. h1-h6 and p
      2. div whose class contains "title"  (status-box headers)
      3. div whose class contains "text-center"  (scene dividers)
    """
    counter = [1]

    def replacer(match):
        tag = match.group(1)
        attrs = match.group(2)
        if re.search(r"\bid\s*=", attrs, re.I):
            return match.group(0)
        new_tag = '<%s id="f%06d"%s>' % (tag, counter[0], attrs)
        counter[0] += 1
        return new_tag

    html = re.sub(r"<(h[1-6]|p)(?=[\s/>])([^>]*)>", replacer, html, flags=re.I)
    html = re.sub(
        r"<(div)([^>]*\bclass\s*=\s*(?:\"[^\"]*title[^\"]*\"|'[^']*title[^']*')[^>]*)>",
        replacer, html, flags=re.I)
    html = re.sub(
        r"<(div)([^>]*\bclass\s*=\s*(?:\"[^\"]*text-center[^\"]*\"|'[^']*text-center[^']*')[^>]*)>",
        replacer, html, flags=re.I)
    return html, counter[0] - 1


NAV_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
  <head>
    <meta http-equiv="default-style" content="text/html; charset=utf-8"/>
    <link rel="stylesheet" href="../styles/style.css" type="text/css"/>
    <title>Table of Contents</title>
  </head>
  <body>
    <nav epub:type="toc">
      <h1>Table of Contents</h1>
      <ol>
{items}
      </ol>
    </nav>
  </body>
</html>
"""

COLOPHON_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
  <head>
    <meta http-equiv="default-style" content="text/html; charset=utf-8"/>
    <link rel="stylesheet" href="../styles/style.css" type="text/css"/>
    <title>Colophon</title>
  </head>
  <body>
    <section epub:type="bodymatter chapter">
      <h1>{title}</h1>
      <h2>by {author}</h2>
      <p>This read-aloud edition was produced with
        <a href="https://github.com/r4victor/syncabook">syncabook</a>.</p>
    </section>
  </body>
</html>
"""


def write_nav(path, titles):
    items = []
    for index, title in enumerate(titles, start=1):
        label = html_escape(title or ("Chapter %d" % index))
        items.append('        <li><a href="chapter%04d.xhtml">%s</a></li>'
                     % (index, label))
    path.write_text(NAV_TEMPLATE.format(items="\n".join(items)), encoding="utf-8")


def write_colophon(path, title, author):
    path.write_text(
        COLOPHON_TEMPLATE.format(title=html_escape(title), author=html_escape(author)),
        encoding="utf-8")


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

def natural_key(path):
    name = path.name.lower()
    return [(0, int(part), "") if part.isdigit() else (1, 0, part)
            for part in re.split(r"(\d+)", name)]


def find_audio_files(root):
    files = [p for p in root.iterdir()
             if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    return sorted(files, key=natural_key)


def ensure_mp3(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".mp3":
        try:
            os.link(str(source), str(destination))
        except OSError:
            shutil.copy2(str(source), str(destination))
        return
    log("converting %s to MP3 ..." % source.name)
    result = subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-vn", "-c:a", "libmp3lame", "-q:a", "2",
        str(destination),
    ])
    if result.returncode != 0 or not destination.exists():
        fail("could not convert audio file: " + source.name)


# ---------------------------------------------------------------------------
# Aeneas alignment
# ---------------------------------------------------------------------------

LANGUAGE_MAP = {
    "en": "eng", "eng": "eng",
    "de": "deu", "deu": "deu",
    "fr": "fra", "fra": "fra",
    "es": "spa", "spa": "spa",
    "it": "ita", "ita": "ita",
    "pt": "por", "por": "por",
    "nl": "nld", "nld": "nld",
    "ru": "rus", "rus": "rus",
    "pl": "pol", "pol": "pol",
    "tr": "tur", "tur": "tur",
    "hi": "hin", "hin": "hin",
    "bn": "ben", "ben": "ben",
    "ja": "jpn", "jpn": "jpn",
    "ko": "kor", "kor": "kor",
    "zh": "cmn", "cmn": "cmn",
}


def aeneas_language(code):
    code = (code or "").strip().lower().replace("_", "-")
    primary = code.split("-")[0]
    if code in LANGUAGE_MAP:
        return LANGUAGE_MAP[code]
    if primary in LANGUAGE_MAP:
        return LANGUAGE_MAP[primary]
    if len(primary) == 3 and primary.isalpha():
        return primary
    return "eng"


def check_engine(require_aeneas=True):
    """Check external programs and, unless preparing only, aeneas itself."""
    missing = []
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            missing.append(tool)
    if require_aeneas and not shutil.which("espeak") and not shutil.which("espeak-ng"):
        missing.append("espeak-ng")
    if missing:
        fail("missing required program(s): %s.\n"
             "Install FFmpeg and eSpeak NG, then add them to PATH."
             % ", ".join(missing))

    if not require_aeneas:
        return
    result = subprocess.run(
        [sys.executable, "-c", "import aeneas"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        fail("aeneas is not installed for this Python (%s).\n"
             "Run: \"%s\" -m pip install -r requirements.txt"
             % (sys.executable, sys.executable))


def align_chapter(text_path, audio_path, smil_path, index, language):
    page_ref = "../text/chapter%04d.xhtml" % index
    audio_ref = "../audio/chapter%04d.mp3" % index
    config = (
        "task_language=%s|os_task_file_format=smil|"
        "os_task_file_smil_audio_ref=%s|os_task_file_smil_page_ref=%s|"
        "is_text_type=unparsed|is_text_unparsed_id_regex=f[0-9]+|"
        # Document order, NOT numeric ID order. The injector numbers all <p>/<h1>
        # first, then the status-box divs, so numeric sorting would move those
        # divs to the end of the chapter and misalign them.
        "is_text_unparsed_id_sort=unsorted"
        % (language, audio_ref, page_ref)
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "UTF-8"

    result = subprocess.run([
        sys.executable, "-m", "aeneas.tools.execute_task",
        str(audio_path), str(text_path), config, str(smil_path),
    ], env=env)

    if result.returncode != 0:
        fail("alignment failed on chapter %d.\n"
             "Check that the audio for this chapter matches its text, and that "
             "the EPUB language is correct (use --lang eng/deu/...)." % index)
    if not smil_path.exists():
        fail("aeneas produced no SMIL file for chapter %d" % index)
    content = smil_path.read_text(encoding="utf-8")
    if "<par" not in content:
        fail("aeneas produced an empty SMIL file for chapter %d.\n"
             "The audio and text probably do not match." % index)


# ---------------------------------------------------------------------------
# Packaging with syncabook
# ---------------------------------------------------------------------------

def find_syncabook():
    found = shutil.which("syncabook")
    if found:
        return found
    python_dir = Path(sys.executable).parent
    for folder in ("Scripts", "bin"):
        for name in ("syncabook.exe", "syncabook", "syncabook.sh"):
            candidate = python_dir / folder / name
            if candidate.exists():
                return str(candidate)
    fail("syncabook was not found.\n"
         "Install it with: \"%s\" -m pip install -r requirements.txt"
         % sys.executable)


def package_epub(project_dir):
    executable = find_syncabook()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "UTF-8"
    # syncabook 0.2.0 writes content.opf/nav.xhtml with open(..., 'x') and no
    # explicit encoding, i.e. the Windows ANSI code page. UTF-8 mode makes the
    # default UTF-8, so non-ASCII titles are written correctly.
    env["PYTHONUTF8"] = "1"
    log("packaging EPUB3 with syncabook ...")
    result = subprocess.run([executable, "create", str(project_dir)], env=env)
    if result.returncode != 0:
        fail("syncabook create failed")

    output_dir = project_dir / "out"
    epubs = sorted(output_dir.glob("*.epub"),
                   key=lambda p: p.stat().st_mtime)
    if not epubs:
        fail("syncabook create finished but produced no EPUB in " + str(output_dir))
    return epubs[-1]


SMIL_NS = "http://www.w3.org/ns/SMIL"


def clock_seconds(value):
    """Parse an SMIL clock value like 00:01:02.345 into seconds."""
    hours, minutes, seconds = 0, 0, 0.0
    parts = value.strip().split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
        elif len(parts) == 2:
            minutes, seconds = int(parts[0]), float(parts[1])
        else:
            seconds = float(parts[0].rstrip("s"))
    except ValueError:
        fail("invalid SMIL clock value: " + value)
    return hours * 3600 + minutes * 60 + seconds


def check_smil_ref(names, archive, smil_name, ref, id_cache, kind):
    """Verify a SMIL src points at something that exists inside the EPUB."""
    path, _, fragment = ref.partition("#")
    target = posixpath.normpath(
        posixpath.join(posixpath.dirname(smil_name), path))
    if target not in names:
        fail("%s references missing %s file: %s" % (smil_name, kind, target))
    if kind != "text" or not fragment or id_cache is None:
        return
    if target not in id_cache:
        content = archive.read(target).decode("utf-8", errors="replace")
        id_cache[target] = set(re.findall(r'\bid\s*=\s*["\']([^"\']+)["\']',
                                          content))
    if fragment not in id_cache[target]:
        fail("%s points at #%s, which does not exist in %s"
             % (smil_name, fragment, target))


def validate_epub(epub_path):
    """Sanity-check the generated EPUB3 Media Overlays.

    Catches the failure modes seen in practice: a mangled OPF encoding, missing
    SMIL files, empty SMIL files, and spine items without a media overlay.
    """
    try:
        archive = zipfile.ZipFile(str(epub_path))
    except zipfile.BadZipFile:
        fail("generated EPUB is not a valid ZIP: " + str(epub_path))

    with archive:
        try:
            opf_name = next(n for n in archive.namelist()
                            if n.lower().endswith(".opf"))
        except StopIteration:
            fail("generated EPUB has no OPF package document")

        try:
            opf_text = archive.read(opf_name).decode("utf-8")
        except UnicodeDecodeError as exc:
            fail("generated %s is not valid UTF-8 (%s).\n"
                 "This is the syncabook default-encoding bug; the "
                 "PYTHONUTF8=1 fix did not take effect." % (opf_name, exc))
        try:
            opf = ET.fromstring(opf_text)
        except ET.ParseError as exc:
            fail("generated %s is not well-formed XML: %s" % (opf_name, exc))

        base = posixpath.dirname(opf_name)
        manifest = {}
        for item in opf.findall(".//{%s}manifest/{%s}item" % (OPF_NS, OPF_NS)):
            manifest[item.get("id")] = item

        # EPUB 3 puts media-overlay on the manifest <item>, not on <itemref>.
        spine_items = []
        for itemref in opf.findall(".//{%s}spine/{%s}itemref" % (OPF_NS, OPF_NS)):
            item = manifest.get(itemref.get("idref"))
            if item is None:
                fail("spine references missing manifest item: %s"
                     % itemref.get("idref"))
            spine_items.append(item)

        overlays = 0
        names = set(archive.namelist())
        id_cache = {}
        for item in spine_items:
            overlay_id = item.get("media-overlay")
            if not overlay_id:
                continue
            overlay = manifest.get(overlay_id)
            if overlay is None:
                fail("media-overlay %s is not in the manifest" % overlay_id)
            smil_name = posixpath.normpath(
                posixpath.join(base, overlay.get("href") or ""))
            try:
                smil_text = archive.read(smil_name).decode("utf-8")
            except KeyError:
                fail("generated EPUB is missing " + smil_name)
            except UnicodeDecodeError:
                fail("generated %s is not valid UTF-8" % smil_name)
            try:
                smil = ET.fromstring(smil_text)
            except ET.ParseError as exc:
                fail("generated %s is not well-formed XML: %s"
                     % (smil_name, exc))

            pars = list(smil.iter("{%s}par" % SMIL_NS)) or list(smil.iter("par"))
            if not pars:
                fail("generated %s has no <par> entries" % smil_name)
            for par in pars:
                text = par.find("{%s}text" % SMIL_NS)
                if text is None:
                    text = par.find("text")
                audio = par.find("{%s}audio" % SMIL_NS)
                if audio is None:
                    audio = par.find("audio")
                if audio is None or not audio.get("clipBegin"):
                    fail("%s has a <par> without a timed <audio> clip"
                         % smil_name)
                if text is not None and text.get("src"):
                    check_smil_ref(names, archive, smil_name, text.get("src"),
                                   id_cache, "text")
                if audio.get("src"):
                    check_smil_ref(names, archive, smil_name, audio.get("src"),
                                   None, "audio")
                if audio.get("clipEnd") and audio.get("clipBegin"):
                    # Equal times are legitimate for silent/punctuation-only
                    # fragments, which aeneas gives a zero-length clip.
                    if (clock_seconds(audio.get("clipEnd")) <
                            clock_seconds(audio.get("clipBegin"))):
                        fail("%s has a clip that ends before it begins: %s"
                             % (smil_name, audio.get("src")))
            overlays += 1

        if overlays == 0:
            fail("generated EPUB has no Media Overlays (no media-overlay links)")

    log("check:   EPUB OK (%d media overlays)" % overlays)


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

def clean_title(title):
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", title)
    title = title.strip(" .")
    return title[:150] or "Read Aloud Book"


def unique_path(path):
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for number in range(2, 10000):
        candidate = path.with_name("%s (%d)%s" % (stem, number, suffix))
        if not candidate.exists():
            return candidate
    return path.with_name("%s_%d%s" % (stem, os.getpid(), suffix))


def find_epub(root):
    epubs = [p for p in root.iterdir()
             if p.is_file() and p.suffix.lower() == ".epub"]
    if not epubs:
        fail("no .epub file found in:\n  " + str(root))
    if len(epubs) > 1:
        fail("more than one .epub found. Keep only the book you want:\n  "
             + "\n  ".join(p.name for p in epubs))
    return epubs[0]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(root, work, args):
    if args.epub:
        epub_path = Path(args.epub)
        if not epub_path.is_absolute():
            epub_path = root / epub_path
        epub_path = epub_path.resolve()
    else:
        epub_path = find_epub(root)
    audio_files = find_audio_files(root)
    if not audio_files:
        fail("no audio files found in:\n  " + str(root) +
             "\nSupported: " + ", ".join(AUDIO_EXTS))

    chapters, epub_meta, cover = read_epub(epub_path, args.exclude)

    log("EPUB:    " + epub_path.name)
    log("chapters: %d   audio files: %d" % (len(chapters), len(audio_files)))

    if len(chapters) != len(audio_files):
        lines = ["", "Chapter documents found in the EPUB (spine order):"]
        for index, chapter in enumerate(chapters, start=1):
            lines.append("  %2d. %-45s  %s"
                         % (index, chapter["zip_name"], chapter["title"]))
        lines.append("")
        lines.append("Audio files found (natural sort order):")
        for index, audio in enumerate(audio_files, start=1):
            lines.append("  %2d. %s" % (index, audio.name))
        lines.append("")
        lines.append("There must be exactly one audio file per chapter.")
        fail("\n".join(lines))

    check_engine(require_aeneas=not args.prepare_only)

    if work.exists():
        shutil.rmtree(work)
    for subdir in ("audio", "sync_text", "smil", "no_sync_text", "images"):
        (work / subdir).mkdir(parents=True, exist_ok=True)

    # --- text -------------------------------------------------------------
    titles = []
    total_ids = 0
    for index, chapter in enumerate(chapters, start=1):
        injected, count = inject_ids(chapter["html"])
        total_ids += count
        (work / "sync_text" / ("chapter%04d.xhtml" % index)).write_text(
            injected, encoding="utf-8")
        titles.append(chapter["title"] or ("Chapter %d" % index))

    if total_ids == 0:
        fail("no paragraphs/headings were found in the EPUB chapters")
    log("text:    %d fragment IDs injected" % total_ids)

    # --- audio ------------------------------------------------------------
    for index, source in enumerate(audio_files, start=1):
        ensure_mp3(source, work / "audio" / ("chapter%04d.mp3" % index))
    log("audio:   %d chapter files prepared" % len(audio_files))

    # --- metadata / navigation -------------------------------------------
    title = clean_title(args.title or epub_meta.get("title") or epub_path.stem)
    author = (args.author or epub_meta.get("author") or "Unknown").strip() or "Unknown"
    metadata = {
        "title": title,
        "author": author,
        "description": "",
        "narrator": "",
        "contributor": "",
        "transcriber": "",
    }
    # syncabook 0.2.0 reads this file with the Windows ANSI code page.
    # Escaping non-ASCII keeps titles like "Märchen" intact.
    (work / "metadata.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=True), encoding="utf-8")
    write_nav(work / "no_sync_text" / "nav.xhtml", titles)
    write_colophon(work / "no_sync_text" / "colophon.xhtml", title, author)
    log("meta:    %s  (by %s)" % (title, author))

    # --- cover (optional) -------------------------------------------------
    if cover:
        image_bytes, extension = cover
        destination = work / "images" / "cover.jpg"
        if extension in (".jpg", ".jpeg"):
            destination.write_bytes(image_bytes)
            log("cover:   included")
        else:
            source_image = work / ("cover_source" + (extension or ".img"))
            source_image.write_bytes(image_bytes)
            result = subprocess.run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source_image), "-frames:v", "1", "-q:v", "3",
                str(destination),
            ])
            if result.returncode == 0 and destination.exists():
                log("cover:   included")
            else:
                log("cover:   could not convert, skipping")

    if args.prepare_only:
        log("prepared only. Work folder: " + str(work))
        return

    # --- align ------------------------------------------------------------
    language = args.lang or aeneas_language(epub_meta.get("language"))
    log("aligning with aeneas (language=%s). This is the slow part." % language)
    for index, chapter_title in enumerate(titles, start=1):
        log("[%d/%d] %s" % (index, len(titles), chapter_title))
        align_chapter(
            work / "sync_text" / ("chapter%04d.xhtml" % index),
            work / "audio" / ("chapter%04d.mp3" % index),
            work / "smil" / ("chapter%04d.smil" % index),
            index, language,
        )
    log("alignment finished")

    # --- package ----------------------------------------------------------
    created = package_epub(work)
    validate_epub(created)

    output_dir = root / OUTPUT_DIR_NAME
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = unique_path(
        output_dir / (title.replace(" ", "_") + "_readaloud.epub"))
    shutil.copy2(str(created), str(destination))
    log("DONE: " + str(destination))


def main():
    default_folder = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Turn one EPUB plus one audio per chapter into a read-aloud EPUB3.")
    parser.add_argument("--version", action="version",
                        version="ReadAloudMaker " + VERSION)
    parser.add_argument("--folder", default=str(default_folder),
                        help="folder containing the EPUB and audio files "
                             "(default: this script's folder)")
    parser.add_argument("--epub", help="explicit path to the EPUB")
    parser.add_argument("--lang", help="aeneas language code (default: from EPUB)")
    parser.add_argument("--title", help="override book title")
    parser.add_argument("--author", help="override author")
    parser.add_argument("--exclude", default=DEFAULT_EXCLUDE,
                        help="regex of spine documents to skip")
    parser.add_argument("--prepare-only", action="store_true",
                        help="extract/inject/copy but do not align or build")
    parser.add_argument("--keep-work", action="store_true",
                        help="keep the _readaloud_work folder after success")
    args = parser.parse_args()

    root = Path(args.folder).resolve()
    if not root.is_dir():
        fail("folder not found: " + str(root))
    work = root / WORK_DIR_NAME

    try:
        run(root, work, args)
    except KeyboardInterrupt:
        if work.exists():
            log("interrupted; work folder kept: " + str(work))
        raise SystemExit(130)
    except BaseException:
        if work.exists():
            log("work folder kept for inspection: " + str(work))
        raise

    if not args.keep_work and not args.prepare_only:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
