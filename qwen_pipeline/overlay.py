"""Generate SMIL overlays and surgically update the OPF package document."""

import posixpath
from dataclasses import dataclass

from lxml import etree

from .errors import PipelineError
from .xmlutil import (
    EPUB_NS,
    SMIL_NS,
    find_first,
    localname,
    namespace_of,
    qname,
    serialize_xml,
)

MEDIA_NS = "http://www.idpf.org/2007/ops"


@dataclass
class Overlay:
    xhtml_item_id: str
    xhtml_href: str
    smil_item_id: str
    smil_href: str          # relative to the OPF directory
    audio_item_id: str
    audio_href: str         # relative to the OPF directory
    media_type: str
    duration: float
    smil_bytes: bytes = b""


def format_clock(seconds):
    """EPUB clock value: ``0:00:12.345`` (hours are not zero padded)."""
    seconds = max(0.0, float(seconds))
    milliseconds = int(round(seconds * 1000.0))
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return "%d:%02d:%02d.%03d" % (hours, minutes, whole_seconds, milliseconds)


def build_smil(text_href, audio_href, fragments, sequence_id=None):
    """Return SMIL bytes for one chapter.

    ``text_href`` and ``audio_href`` must already be relative to the SMIL file.
    """
    root = etree.Element(
        qname(SMIL_NS, "smil"),
        nsmap={None: SMIL_NS, "epub": EPUB_NS},
    )
    root.set("version", "3.0")

    body = etree.SubElement(root, qname(SMIL_NS, "body"))
    sequence = etree.SubElement(body, qname(SMIL_NS, "seq"))
    sequence.set("id", sequence_id or "seq")
    sequence.set(qname(EPUB_NS, "textref"), text_href)

    for index, fragment in enumerate(fragments, start=1):
        par = etree.SubElement(sequence, qname(SMIL_NS, "par"))
        par.set("id", "par%05d" % index)

        text = etree.SubElement(par, qname(SMIL_NS, "text"))
        text.set("src", "%s#%s" % (text_href, fragment.id))

        audio = etree.SubElement(par, qname(SMIL_NS, "audio"))
        audio.set("src", audio_href)
        audio.set("clipBegin", "%.3fs" % max(0.0, fragment.start_time))
        audio.set("clipEnd", "%.3fs" % max(fragment.start_time, fragment.end_time))

    return serialize_xml(root.getroottree(), pretty=True)


def relative_href(target_zip_name, from_zip_name):
    """Path of ``target`` relative to the directory containing ``from``."""
    return posixpath.relpath(target_zip_name, posixpath.dirname(from_zip_name))


def find_manifest_item(manifest_element, item_id):
    for item in manifest_element:
        if localname(item) == "item" and item.get("id") == item_id:
            return item
    return None


def _subelement(parent, local_name):
    """Create a child in the parent's namespace (usually the OPF namespace)."""
    namespace = namespace_of(parent)
    tag = qname(namespace, local_name) if namespace else local_name
    return etree.SubElement(parent, tag)


def ensure_manifest_item(manifest_element, item_id, href, media_type,
                         properties=None):
    existing = find_manifest_item(manifest_element, item_id)
    if existing is not None:
        existing.set("href", href)
        existing.set("media-type", media_type)
        if properties:
            existing.set("properties", properties)
        return existing

    item = _subelement(manifest_element, "item")
    item.set("id", item_id)
    item.set("href", href)
    item.set("media-type", media_type)
    if properties:
        item.set("properties", properties)
    return item


def apply_overlays(opf_tree, overlays, total_duration):
    """Register overlays in the OPF and set ``media-overlay`` on text items."""
    root = opf_tree.getroot()
    manifest = find_first(root, "manifest")
    metadata = find_first(root, "metadata")
    if manifest is None or metadata is None:
        raise PipelineError("package document has no manifest or metadata")

    for overlay in overlays:
        text_item = find_manifest_item(manifest, overlay.xhtml_item_id)
        if text_item is None:
            raise PipelineError(
                "manifest item %s disappeared while packaging"
                % overlay.xhtml_item_id
            )

        ensure_manifest_item(
            manifest,
            overlay.audio_item_id,
            overlay.audio_href,
            overlay.media_type,
        )
        ensure_manifest_item(
            manifest,
            overlay.smil_item_id,
            overlay.smil_href,
            "application/smil+xml",
        )
        text_item.set("media-overlay", overlay.smil_item_id)

    # Replace stale durations from a previous run.
    for element in list(metadata):
        if localname(element) == "meta" and element.get("property") == "media:duration":
            metadata.remove(element)

    for overlay in overlays:
        meta = _subelement(metadata, "meta")
        meta.set("property", "media:duration")
        meta.set("refines", "#" + overlay.smil_item_id)
        meta.text = format_clock(overlay.duration)

    total = _subelement(metadata, "meta")
    total.set("property", "media:duration")
    total.text = format_clock(total_duration)


def smil_paths(smil_zip_name, text_zip_name, audio_zip_name):
    return (
        relative_href(text_zip_name, smil_zip_name),
        relative_href(audio_zip_name, smil_zip_name),
    )


def media_type_for(path):
    extension = posixpath.splitext(path)[1].lower()
    return {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".m4b": "audio/mp4",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
        ".opus": "audio/opus",
    }.get(extension, "audio/mpeg")
