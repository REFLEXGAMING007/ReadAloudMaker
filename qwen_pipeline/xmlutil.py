"""XML helpers for EPUB XHTML and OPF documents.

Everything here uses lxml's XML parser (never an HTML parser) so namespaces and
self-closing tags survive. Real-world EPUBs occasionally contain HTML named
entities that are not predefined in XML (``&nbsp;``, ``&mdash;``, ...), so those
are converted to numeric references before parsing.
"""

import codecs
import re

from lxml import etree

from .errors import PipelineError

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
SMIL_NS = "http://www.w3.org/ns/SMIL"
EPUB_NS = "http://www.idpf.org/2007/ops"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
XML_NS = "http://www.w3.org/XML/1998/namespace"

_XML_ENTITIES = frozenset(("amp", "lt", "gt", "quot", "apos"))
_NAMED_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
_ENCODING_RE = re.compile(br"""encoding\s*=\s*["']([^"']+)["']""", re.I)
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.I | re.S)

# A compact subset of HTML5 named entities. Using the full table costs nothing
# at runtime, but importing html.entities keeps this file short.
try:
    from html.entities import html5 as _HTML5

    _ENTITY_TABLE = dict(
        (name[:-1], value) for name, value in _HTML5.items() if name.endswith(";")
    )
except Exception:  # pragma: no cover - html.entities always exists on 3.9+
    _ENTITY_TABLE = {}


def qname(namespace, tag):
    """Return a Clark-notation tag name."""
    return "{%s}%s" % (namespace, tag)


def namespace_of(element):
    """Return the namespace URI of an element, or "" when it has none."""
    tag = element.tag
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def localname(element):
    """Return the local tag name of an element."""
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def decode_xml_bytes(data):
    """Decode XML bytes using BOM, declared encoding, then UTF-8/cp1252."""
    if isinstance(data, str):
        return data

    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", "replace")
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16", "replace")

    match = _ENCODING_RE.search(data[:200])
    if match:
        name = match.group(1).decode("ascii", "ignore")
        try:
            return data.decode(name)
        except (LookupError, UnicodeDecodeError):
            pass

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def _numericise_entities(text):
    def replace(match):
        name = match.group(1)
        if name in _XML_ENTITIES:
            return match.group(0)
        value = _ENTITY_TABLE.get(name)
        if value is None:
            return match.group(0)
        return "".join("&#%d;" % ord(char) for char in value)

    return _NAMED_ENTITY_RE.sub(replace, text)


def parse_xml(data, what="XML"):
    """Parse XML bytes or text into an lxml ElementTree.

    Strict parsing is attempted first. If the document is malformed only enough
    that a recoverable parse succeeds, that result is returned; the caller can
    inspect ``tree.docinfo`` but we do not silently rewrite content.
    """
    text = decode_xml_bytes(data)
    text = _numericise_entities(text)
    raw = text.encode("utf-8")

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=True,
        recover=False,
        strip_cdata=False,
    )
    try:
        root = etree.fromstring(raw, parser)
    except etree.XMLSyntaxError as strict_error:
        lenient = etree.XMLParser(
            resolve_entities=False,
            no_network=True,
            huge_tree=True,
            recover=True,
            strip_cdata=False,
        )
        try:
            root = etree.fromstring(raw, lenient)
        except etree.XMLSyntaxError:
            raise PipelineError("%s could not be parsed: %s" % (what, strict_error))
        if root is None:
            raise PipelineError("%s could not be parsed: %s" % (what, strict_error))

    tree = root.getroottree()
    return tree


def serialize_xml(tree, xml_declaration=True, pretty=False):
    """Serialise an ElementTree as UTF-8 XML, preserving the original doctype."""
    return etree.tostring(
        tree,
        encoding="utf-8",
        xml_declaration=xml_declaration,
        pretty_print=pretty,
    )


def find_first(root, local_name):
    """Find the first descendant (including root) with this local name."""
    for element in root.iter():
        if localname(element) == local_name:
            return element
    return None


def find_children(element, local_name):
    """Direct children with this local name."""
    return [child for child in element if localname(child) == local_name]
