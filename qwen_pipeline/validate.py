"""Structural validation for the generated EPUB.

This complements epubcheck: it verifies the things that break Media Overlays in
practice (dangling fragment IDs, missing audio, bad clip bounds, non-monotonic
timelines) without requiring Java.
"""

import posixpath
import zipfile

from lxml import etree

from .errors import PipelineError
from .xmlutil import (
    localname,
    parse_xml,
    find_first,
)

SMIL_NS = "http://www.w3.org/ns/SMIL"


def _resolve(base_zip_name, href):
    from urllib.parse import unquote

    href = unquote(href.split("#", 1)[0])
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_zip_name), href))


def _clock_seconds(value):
    value = (value or "").strip()
    if value.endswith("ms"):
        return float(value[:-2]) / 1000.0
    if value.endswith("s"):
        value = value[:-1]
    parts = value.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        raise PipelineError("invalid SMIL clock value: %r" % value)
    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    raise PipelineError("invalid SMIL clock value: %r" % value)


def validate_epub(path, audio_durations=None):
    """Validate the finished EPUB and return a report dictionary."""
    audio_durations = audio_durations or {}
    report = {
        "overlays": 0,
        "pars": 0,
        "errors": [],
        "warnings": [],
    }

    try:
        archive = zipfile.ZipFile(str(path), "r")
    except (IOError, OSError, zipfile.BadZipFile) as exc:
        raise PipelineError("output is not a readable ZIP: %s" % exc)

    try:
        with archive:
            bad = archive.testzip()
            if bad is not None:
                raise PipelineError("corrupt ZIP entry: " + bad)

            infos = archive.infolist()
            if not infos or infos[0].filename != "mimetype":
                raise PipelineError("mimetype is not the first EPUB entry")
            if infos[0].compress_type != zipfile.ZIP_STORED:
                raise PipelineError("mimetype must be stored uncompressed")
            if archive.read("mimetype") != b"application/epub+zip":
                raise PipelineError("mimetype content is wrong")

            container = parse_xml(
                archive.read("META-INF/container.xml"), "container.xml"
            )
            opf_name = None
            for element in container.iter():
                if localname(element) == "rootfile" and element.get("full-path"):
                    opf_name = element.get("full-path")
                    break
            if not opf_name:
                raise PipelineError("container.xml has no rootfile")

            opf = parse_xml(archive.read(opf_name), opf_name)
            root = opf.getroot()
            opf_dir = posixpath.dirname(opf_name)

            manifest = _manifest_by_id(find_first(root, "manifest"))
            if not manifest:
                raise PipelineError("package document has no manifest")

            for item_id, item in manifest.items():
                href = item.get("href")
                if not href:
                    continue
                target = posixpath.normpath(posixpath.join(opf_dir, href))
                if item.get("media-type") != "application/smil+xml":
                    if target not in archive.namelist():
                        report["errors"].append(
                            "manifest item %s points at missing %s" % (item_id, target)
                        )

            text_ids = {}
            for item_id, item in manifest.items():
                if item.get("media-type") != "application/xhtml+xml":
                    continue
                href = item.get("href")
                target = posixpath.normpath(posixpath.join(opf_dir, href))
                text_ids[target] = _document_ids(archive, target)

            for item_id, item in manifest.items():
                overlay_id = item.get("media-overlay")
                if not overlay_id:
                    continue
                if overlay_id not in manifest:
                    raise PipelineError(
                        "media-overlay on %s references unknown id %s"
                        % (item_id, overlay_id)
                    )

                smil_item = manifest[overlay_id]
                smil_name = posixpath.normpath(
                    posixpath.join(opf_dir, smil_item.get("href"))
                )
                if smil_name not in archive.namelist():
                    raise PipelineError("missing SMIL file: " + smil_name)

                _validate_smil(
                    archive,
                    smil_name,
                    manifest,
                    opf_dir,
                    text_ids,
                    report,
                    audio_durations,
                )
                report["overlays"] += 1

    finally:
        archive.close()

    if report["errors"]:
        raise PipelineError(
            "EPUB validation failed:\n  " + "\n  ".join(report["errors"])
        )
    return report


def _manifest_by_id(manifest_element):
    result = {}
    if manifest_element is None:
        return result
    for item in manifest_element:
        if localname(item) != "item":
            continue
        item_id = item.get("id")
        if item_id:
            result[item_id] = item
    return result


def _document_ids(archive, name):
    try:
        tree = parse_xml(archive.read(name), name)
    except KeyError:
        return set()
    found = set()
    for element in tree.iter():
        value = element.get("id")
        if value:
            found.add(value)
    return found


def _validate_smil(archive, smil_name, manifest, opf_dir, text_ids, report,
                   audio_durations):
    tree = parse_xml(archive.read(smil_name), smil_name)
    previous_end = -1.0
    pars = 0

    for par in tree.iter():
        if localname(par) != "par":
            continue
        pars += 1
        text = None
        audio = None
        for child in par:
            if localname(child) == "text":
                text = child
            elif localname(child) == "audio":
                audio = child
        if text is None or audio is None:
            report["errors"].append("%s has a <par> without text or audio" % smil_name)
            continue

        src = text.get("src") or ""
        if "#" not in src:
            report["errors"].append("%s has a text reference without a fragment" % smil_name)
        else:
            target, fragment = src.split("#", 1)
            target_name = _resolve(smil_name, target)
            identifiers = text_ids.get(target_name)
            if identifiers is None:
                report["errors"].append(
                    "%s references unknown document %s" % (smil_name, target_name)
                )
            elif fragment not in identifiers:
                report["errors"].append(
                    "%s references missing fragment #%s" % (smil_name, fragment)
                )

        audio_src = audio.get("src") or ""
        audio_name = _resolve(smil_name, audio_src)
        if audio_name not in archive.namelist():
            report["errors"].append("%s references missing audio %s" % (smil_name, audio_name))
            continue

        try:
            begin = _clock_seconds(audio.get("clipBegin"))
            end = _clock_seconds(audio.get("clipEnd"))
        except PipelineError as exc:
            report["errors"].append(str(exc))
            continue

        if begin < 0 or end <= begin:
            report["errors"].append(
                "%s has an invalid clip %.3f -> %.3f" % (smil_name, begin, end)
            )
        if begin + 0.0001 < previous_end:
            report["errors"].append(
                "%s is not monotonic at %.3fs" % (smil_name, begin)
            )
        previous_end = end

        duration = audio_durations.get(audio_name)
        if duration is not None and end > duration + 0.25:
            report["errors"].append(
                "%s clip ends at %.3fs but audio is %.3fs"
                % (smil_name, end, duration)
            )

    report["pars"] += pars
    if pars == 0:
        report["errors"].append("%s contains no <par> entries" % smil_name)


def total_duration_from_opf(opf_tree):
    """Read the book-level ``media:duration`` from an OPF tree."""
    metadata = find_first(opf_tree.getroot(), "metadata")
    if metadata is None:
        return 0.0
    for element in metadata:
        if localname(element) == "meta" and element.get("property") == "media:duration" and not element.get("refines"):
            try:
                return _clock_seconds(element.text)
            except PipelineError:
                return 0.0
    return 0.0
