"""Read and write EPUB containers at ZIP level.

The writer never rebuilds the workbook: every entry that is not explicitly
replaced is copied byte for byte, and the ``mimetype`` entry stays first and
uncompressed.
"""

import os
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from urllib.parse import unquote

from .errors import PipelineError
from .xmlutil import (
    DC_NS,
    find_children,
    find_first,
    localname,
    parse_xml,
    qname,
)

NATURAL_RE = re.compile(r"(\d+)")


def natural_key(path):
    """Sort key that orders ``chapter 2`` before ``chapter 10``."""
    return [
        int(part) if part.isdigit() else part.lower()
        for part in NATURAL_RE.split(str(path))
    ]


@dataclass
class ManifestItem:
    id: str
    href: str
    media_type: str
    properties: str
    zip_name: str
    element: object = field(repr=False, default=None)


@dataclass
class SpineDocument:
    item: ManifestItem
    linear: bool = True
    element: object = field(repr=False, default=None)

    @property
    def href(self):
        return self.item.href

    @property
    def zip_name(self):
        return self.item.zip_name


class EpubArchive(object):
    """A read-only view of an EPUB plus helpers for its OPF."""

    def __init__(self, path):
        self.path = str(path)
        try:
            self._zip = zipfile.ZipFile(self.path, "r")
        except (IOError, OSError, zipfile.BadZipFile) as exc:
            raise PipelineError("cannot open EPUB %s: %s" % (path, exc))

        self.names = self._zip.namelist()
        self.opf_name = self._find_opf()
        self.opf_tree = parse_xml(self._zip.read(self.opf_name),
                                  "package document (%s)" % self.opf_name)
        self.opf_root = self.opf_tree.getroot()
        self.opf_dir = posixpath.dirname(self.opf_name)
        self.version = self.opf_root.get("version", "3.0")
        self.manifest, self.spine = self._read_package()
        self.metadata = self._read_metadata()

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        try:
            self._zip.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def read(self, name):
        return self._zip.read(name)

    def has(self, name):
        return name in self.names

    def getinfo(self, name):
        return self._zip.getinfo(name)

    # -- package parsing ---------------------------------------------------

    def _find_opf(self):
        container_name = "META-INF/container.xml"
        if container_name not in self.names:
            raise PipelineError("not a valid EPUB: META-INF/container.xml is missing")
        container = parse_xml(self._zip.read(container_name), "container.xml")
        for element in container.iter():
            if localname(element) != "rootfile":
                continue
            media_type = element.get("media-type", "")
            if media_type in ("application/oebps-package+xml", ""):
                full_path = element.get("full-path")
                if full_path:
                    full_path = posixpath.normpath(unquote(full_path))
                    if full_path not in self.names:
                        raise PipelineError(
                            "container.xml points at a missing package document: "
                            + full_path
                        )
                    return full_path
        raise PipelineError("container.xml does not list an OPF package document")

    def _resolve(self, href):
        href = unquote(href.split("#", 1)[0])
        if href.startswith("/"):
            href = href[1:]
        return posixpath.normpath(posixpath.join(self.opf_dir, href))

    def _read_package(self):
        manifest_parent = find_first(self.opf_root, "manifest")
        spine_parent = find_first(self.opf_root, "spine")
        if manifest_parent is None or spine_parent is None:
            raise PipelineError("package document has no manifest or spine")

        manifest = {}
        for element in find_children(manifest_parent, "item"):
            item_id = element.get("id")
            href = element.get("href")
            if not item_id or not href:
                continue
            item = ManifestItem(
                id=item_id,
                href=href,
                media_type=element.get("media-type", ""),
                properties=element.get("properties", ""),
                zip_name=self._resolve(href),
                element=element,
            )
            manifest[item_id] = item

        spine = []
        for element in find_children(spine_parent, "itemref"):
            idref = element.get("idref")
            if idref not in manifest:
                continue
            spine.append(
                SpineDocument(
                    item=manifest[idref],
                    linear=element.get("linear", "yes") != "no",
                    element=element,
                )
            )
        self.spine_element = spine_parent
        self.manifest_element = manifest_parent
        return manifest, spine

    def _read_metadata(self):
        metadata_parent = find_first(self.opf_root, "metadata")
        result = {"title": "", "creator": "", "language": "", "identifier": ""}
        if metadata_parent is None:
            return result

        creators = []
        for element in metadata_parent.iter():
            name = localname(element)
            text = (element.text or "").strip()
            if not text:
                continue
            if element.tag == qname(DC_NS, "title") and not result["title"]:
                result["title"] = text
            elif element.tag == qname(DC_NS, "creator"):
                creators.append(text)
            elif element.tag == qname(DC_NS, "language") and not result["language"]:
                result["language"] = text
            elif element.tag == qname(DC_NS, "identifier") and not result["identifier"]:
                result["identifier"] = text
            elif name == "title" and element.tag.startswith("{") is False:
                result["title"] = result["title"] or text
        result["creator"] = ", ".join(creators)
        return result

    # -- selection ---------------------------------------------------------

    def is_epub3(self):
        try:
            return int(self.version.split(".")[0]) >= 3
        except (ValueError, AttributeError):
            return False

    def text_documents(self, exclude=None):
        """Return spine documents that can hold narrated text, in spine order."""
        pattern = re.compile(exclude) if exclude else None
        documents = []
        for document in self.spine:
            item = document.item
            if not document.linear:
                continue
            if "nav" in item.properties.split():
                continue
            if item.media_type and item.media_type != "application/xhtml+xml":
                continue
            if pattern is not None and pattern.search(item.href):
                continue
            if not self.has(item.zip_name):
                continue
            documents.append(document)
        return documents

    def cover_image(self):
        for item in self.manifest.values():
            if "cover-image" in item.properties.split():
                if self.has(item.zip_name):
                    return item
        return None


def find_audio_files(folder):
    """Audio files in a folder, in natural sort order."""
    import os

    extensions = {".mp3", ".m4a", ".m4b", ".wav", ".ogg", ".flac", ".aac", ".opus"}
    found = []
    for name in os.listdir(str(folder)):
        path = os.path.join(str(folder), name)
        if not os.path.isfile(path):
            continue
        if os.path.splitext(name)[1].lower() in extensions:
            found.append(path)
    found.sort(key=lambda value: natural_key(os.path.basename(value)))
    return found


def find_epub_file(folder):
    """Return the single EPUB in a folder, or fail with a useful message."""
    import os

    candidates = [
        os.path.join(str(folder), name)
        for name in os.listdir(str(folder))
        if name.lower().endswith(".epub") and os.path.isfile(os.path.join(str(folder), name))
    ]
    candidates.sort(key=natural_key)
    if not candidates:
        raise PipelineError("no .epub file found in " + str(folder))
    if len(candidates) > 1:
        raise PipelineError(
            "more than one .epub found in %s:\n  %s\n"
            "Keep only the book you want, or pass --epub."
            % (str(folder), "\n  ".join(os.path.basename(path) for path in candidates))
        )
    return candidates[0]


def write_epub(source_path, destination_path, replacements=None, additions=None):
    """Copy ``source_path`` to ``destination_path`` applying small changes.

    replacements: mapping of ZIP entry name -> new bytes.
    additions:    iterable of ``(name, bytes)`` or ``(name, bytes, compress_type)``.
    """
    replacements = replacements or {}
    additions = [tuple(entry) for entry in (additions or [])]

    source = zipfile.ZipFile(str(source_path), "r")
    try:
        infos = source.infolist()
        names = set(info.filename for info in infos)

        for entry in additions:
            if entry[0] in names:
                raise PipelineError("cannot add %s: it already exists in the EPUB" % entry[0])

        destination = zipfile.ZipFile(str(destination_path), "w", zipfile.ZIP_DEFLATED)
        try:
            _write_mimetype(source, destination, infos, replacements)

            for info in infos:
                if info.filename == "mimetype":
                    continue
                payload = replacements.get(info.filename)
                if payload is None:
                    payload = source.read(info.filename)
                else:
                    info = _copy_info(info)
                destination.writestr(info, payload)

            for entry in additions:
                name, payload = entry[0], entry[1]
                compress = entry[2] if len(entry) > 2 else None
                if compress is None:
                    compress = _default_compression(name)
                if isinstance(payload, (str, os.PathLike)):
                    # Stream large audio files from disk instead of holding
                    # every chapter in memory at once.
                    destination.write(str(payload), name, compress_type=compress)
                else:
                    destination.writestr(name, payload, compress_type=compress)
        finally:
            destination.close()
    finally:
        source.close()


_AUDIO_EXTENSIONS = (".mp3", ".m4a", ".m4b", ".aac", ".opus", ".ogg", ".flac", ".wav")


def _default_compression(name):
    """Audio is already compressed; storing it is faster and no larger."""
    if name.lower().endswith(_AUDIO_EXTENSIONS):
        return zipfile.ZIP_STORED
    return zipfile.ZIP_DEFLATED


def _copy_info(info):
    """A fresh ZipInfo so a replaced entry can be recompressed safely."""
    clone = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    clone.compress_type = zipfile.ZIP_DEFLATED
    clone.external_attr = info.external_attr
    clone.internal_attr = info.internal_attr
    clone.create_system = info.create_system
    clone.comment = info.comment
    clone.extra = info.extra
    return clone


def _write_mimetype(source, destination, infos, replacements):
    stored = None
    for info in infos:
        if info.filename == "mimetype":
            stored = info
            break

    payload = replacements.get("mimetype")
    if payload is None:
        if stored is not None:
            payload = source.read("mimetype")
        else:
            payload = b"application/epub+zip"

    if stored is not None:
        stored.compress_type = zipfile.ZIP_STORED
        destination.writestr(stored, payload)
    else:
        destination.writestr("mimetype", payload, compress_type=zipfile.ZIP_STORED)


def is_media_overlay_supported(archive):
    return archive.is_epub3()


__all__ = [
    "EpubArchive",
    "ManifestItem",
    "SpineDocument",
    "find_audio_files",
    "find_epub_file",
    "is_media_overlay_supported",
    "natural_key",
    "write_epub",
]
