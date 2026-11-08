"""Flatten XHTML text, split sentences, and inject fragment spans.

The central idea is the "flat string + index map": the visible text of a block
is flattened into one string, sentence boundaries are computed on that string,
and then the original DOM is rebuilt with one ``<span id="...">`` per sentence.
Inline markup (``<em>``, ``<a>``, ``<span class=...>``) is preserved, including
when a sentence starts or ends in the middle of it.
"""

import re
from dataclasses import dataclass

from lxml import etree

from .errors import PipelineError
from .xmlutil import XML_NS, localname, namespace_of, qname

BLOCK_TAGS = frozenset(
    (
        "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd",
        "td", "th", "blockquote", "figcaption", "div", "pre", "address",
        "caption", "legend",
    )
)

SKIP_TEXT_TAGS = frozenset(
    ("script", "style", "head", "title", "noscript", "template")
)

VOID_TAGS = frozenset(
    ("br", "img", "hr", "wbr", "source", "track", "area", "base", "col",
     "embed", "input", "link", "meta", "param")
)

TERMINATORS = ".!?\u2026\u3002\uff01\uff1f"
CLOSERS = "\"'\u201d\u2019\u00bb)]}\uff09\u3011\u300d\u300f"

_ABBREVIATIONS = frozenset(
    (
        "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
        "fig", "no", "vol", "ch", "chap", "ed", "al", "inc", "ltd", "co",
        "corp", "dept", "univ", "approx", "est", "min", "max", "sec", "hr",
        "mt", "ft", "ave", "blvd", "rd", "gen", "col", "capt", "lt", "sgt",
    )
)


@dataclass
class Fragment:
    """One highlightable unit of text."""

    id: str
    start: int          # character offset in ChapterText.text
    end: int            # exclusive
    text: str
    block: int = 0
    words: int = 0
    start_time: float = 0.0
    end_time: float = 0.0

    @property
    def duration(self):
        return self.end_time - self.start_time


@dataclass
class ChapterText:
    """A parsed XHTML chapter plus its fragments and flat text."""

    tree: object
    text: str
    fragments: list
    block_count: int
    word_count: int

    def serialized(self):
        from .xmlutil import serialize_xml

        return serialize_xml(self.tree)


def counted_words(text):
    """Count runs of word characters, roughly matching an aligner's tokens."""
    total = 0
    inside = False
    for char in text:
        kept = char.isalnum() or char == "'"
        if kept and not inside:
            total += 1
        inside = kept
    return total


def _is_abbreviation(text, dot_index):
    start = dot_index - 1
    while start >= 0 and (text[start].isalnum() or text[start] in ".-"):
        start -= 1
    word = text[start + 1:dot_index].strip(".").lower()
    if not word:
        return False
    if word in _ABBREVIATIONS:
        return True
    if len(word) == 1 and word.isalpha() and word.isupper():
        return True
    return False


def split_sentences(text):
    """Return sentence ranges as ``(start, end)`` over ``text``.

    The ranges do not overlap but may not cover whitespace between sentences;
    use :func:`cover_ranges` when every character must belong to a fragment.
    """
    length = len(text)
    if not text.strip():
        return []

    bounds = []
    index = 0
    while index < length:
        char = text[index]
        if char not in TERMINATORS:
            index += 1
            continue

        if (char == "." and 0 < index < length - 1
                and text[index - 1].isdigit() and text[index + 1].isdigit()):
            index += 1
            continue

        end = index + 1
        while end < length and text[end] in TERMINATORS:
            end += 1
        while end < length and text[end] in CLOSERS:
            end += 1

        if char == "." and _is_abbreviation(text, index):
            index = end
            continue

        if end >= length or text[end].isspace() or char in "\u3002\uff01\uff1f":
            bounds.append(end)

        index = end

    ranges = []
    start = 0
    for boundary in bounds:
        if boundary > start:
            ranges.append((start, boundary))
            start = boundary
    if start < length:
        ranges.append((start, length))

    return [item for item in ranges if text[item[0]:item[1]].strip()]


def cover_ranges(text, ranges):
    """Extend sentence ranges so they cover every character exactly once."""
    ranges = [item for item in ranges if text[item[0]:item[1]].strip()]
    if not ranges:
        return []

    covered = []
    for index, (start, end) in enumerate(ranges):
        if index == 0:
            start = 0
        covered.append([start, end])
    for index in range(len(covered) - 1):
        covered[index][1] = covered[index + 1][0]
    covered[-1][1] = len(text)
    return [(start, end) for start, end in covered if end > start]


def document_ids(root):
    """All existing XML IDs in a document, so generated IDs never collide."""
    found = set()
    for element in root.iter():
        value = element.get("id")
        if value:
            found.add(value)
        xml_id = element.get(qname(XML_NS, "id"))
        if xml_id:
            found.add(xml_id)
    return found


def block_events(block):
    """Flatten a block into text/open/close/atom events, in document order."""
    events = []

    def walk(element):
        if element.text:
            events.append(("text", element.text))
        for child in element:
            name = localname(child)
            if name in SKIP_TEXT_TAGS:
                pass
            elif name in VOID_TAGS or not isinstance(child.tag, str):
                events.append(("atom", child))
            else:
                events.append(("open", child))
                walk(child)
                events.append(("close", child))
            if child.tail:
                events.append(("text", child.tail))

    walk(block)
    return events


def flat_text_of_events(events):
    return "".join(value for kind, value in events if kind == "text")


def _has_text(element):
    for text in element.itertext():
        if text.strip():
            return True
    return False


def _has_block_descendant(element):
    for child in element.iterdescendants():
        if localname(child) in BLOCK_TAGS:
            return True
    return False


def find_leaf_blocks(root):
    """Innermost text blocks, in document order.

    A ``<div>`` that only wraps ``<p>`` elements is skipped; the paragraphs are
    used instead. A ``<div class="box-title">`` with direct text is a block.
    """
    blocks = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        if localname(element) in SKIP_TEXT_TAGS:
            continue
        if localname(element) not in BLOCK_TAGS:
            continue
        if _has_block_descendant(element):
            continue
        if not _has_text(element):
            continue
        blocks.append(element)
    return blocks


def _copy_attributes(source, destination, emitted_ids):
    for key, value in source.attrib.items():
        if key == "id" or key == qname(XML_NS, "id"):
            if value in emitted_ids:
                continue
            emitted_ids.add(value)
        destination.set(key, value)


def _emit_span(block, events, start, end, span_id, span_tag, emitted_ids):
    span = etree.Element(span_tag)
    span.set("id", span_id)
    emitted_ids.add(span_id)

    source_stack = []
    output_stack = []
    cursor = 0
    emitted = False

    def container():
        return output_stack[-1][1] if output_stack else span

    def append_text(value):
        node = container()
        if len(node):
            node[-1].tail = (node[-1].tail or "") + value
        else:
            node.text = (node.text or "") + value

    def sync():
        common = 0
        while (common < len(output_stack) and common < len(source_stack)
               and output_stack[common][0] is source_stack[common]):
            common += 1
        del output_stack[common:]
        for source in source_stack[common:]:
            created = etree.SubElement(container(), source.tag)
            _copy_attributes(source, created, emitted_ids)
            output_stack.append((source, created))

    for kind, value in events:
        if kind == "text":
            segment_start = cursor
            segment_end = cursor + len(value)
            if segment_end > start and segment_start < end:
                sync()
                first = max(start, segment_start) - segment_start
                last = min(end, segment_end) - segment_start
                append_text(value[first:last])
                emitted = True
            cursor = segment_end
        elif kind == "open":
            source_stack.append(value)
        elif kind == "close":
            if output_stack and output_stack[-1][0] is value:
                output_stack.pop()
            if source_stack and source_stack[-1] is value:
                source_stack.pop()
        elif kind == "atom":
            if start <= cursor < end:
                sync()
                clone = etree.SubElement(container(), value.tag)
                _copy_attributes(value, clone, emitted_ids)
                emitted = True

    return span if emitted else None


def wrap_block(block, flat, ranges, fragment_id, emitted_ids):
    """Wrap each sentence range in the block, preserving inline markup.

    Returns a list of ``(start, end)`` local ranges for the fragments that were
    actually created.
    """
    if not ranges:
        return []

    span_tag = qname(namespace_of(block), "span") if namespace_of(block) else "span"
    events = block_events(block)

    if len(ranges) == 1:
        start, end = ranges[0]
        existing = block.get("id")
        if existing and existing not in emitted_ids:
            emitted_ids.add(existing)
            return [(existing, start, end)]
        new_id = fragment_id()
        block.set("id", new_id)
        emitted_ids.add(new_id)
        return [(new_id, start, end)]

    created = []
    for start, end in ranges:
        identifier = fragment_id()
        span = _emit_span(block, events, start, end, identifier, span_tag, emitted_ids)
        if span is None:
            continue
        created.append((identifier, span, start, end))

    if not created:
        return []

    block.text = None
    for child in list(block):
        block.remove(child)
    for _, span, _, _ in created:
        block.append(span)

    return [(identifier, start, end) for identifier, _, start, end in created]


def analyse_document(tree, chapter_index, id_prefix="ra"):
    """Parse one XHTML tree into flat text, fragments, and modified markup."""
    root = tree.getroot()
    body = None
    for element in root.iter():
        if localname(element) == "body":
            body = element
            break
    if body is None:
        body = root

    used = document_ids(root)
    counter = [0]

    def next_id():
        while True:
            counter[0] += 1
            candidate = "%s%04d-%05d" % (id_prefix, chapter_index, counter[0])
            if candidate not in used:
                used.add(candidate)
                return candidate

    blocks = find_leaf_blocks(body)
    fragments = []
    texts = []
    offset = 0
    emitted_ids = set()

    for block_index, block in enumerate(blocks):
        events = block_events(block)
        flat = flat_text_of_events(events)
        if not flat.strip():
            continue
        ranges = cover_ranges(flat, split_sentences(flat))
        if not ranges:
            continue

        local = wrap_block(block, flat, ranges, next_id, emitted_ids)
        for identifier, start, end in local:
            fragment = Fragment(
                id=identifier,
                start=offset + start,
                end=offset + end,
                text=flat[start:end],
                block=block_index,
                words=counted_words(flat[start:end]),
            )
            fragments.append(fragment)

        texts.append(flat)
        offset += len(flat) + 1

    text = "\n".join(texts)
    return ChapterText(
        tree=tree,
        text=text,
        fragments=fragments,
        block_count=len(blocks),
        word_count=counted_words(text),
    )


def normalize_stream(text):
    """Build a punctuation-free comparison string plus original indices."""
    characters = []
    indices = []
    for index, char in enumerate(text):
        if char.isalnum() or char == "'":
            lowered = char.lower()
            characters.append(lowered if len(lowered) == 1 else lowered[0])
            indices.append(index)
    return "".join(characters), indices


def token_key(token_text):
    return "".join(
        char for char in token_text if char.isalnum() or char == "'"
    ).lower()


def map_tokens_to_text(text, tokens):
    """Locate aligner tokens inside ``text``.

    Returns ``(spans, unmatched)`` where ``spans`` is a list of
    ``(token, start_char, end_char)``. Tokens produced by Qwen3 have had all
    punctuation removed, so matching happens on a normalized stream while the
    returned offsets still refer to the original text.
    """
    normalized, original_indices = normalize_stream(text)
    spans = []
    unmatched = []
    cursor = 0

    for token in tokens:
        key = token_key(token.text)
        if not key:
            continue
        position = normalized.find(key, cursor)
        if position < 0:
            position = normalized.find(key)
        if position < 0:
            unmatched.append(token)
            continue
        start = original_indices[position]
        end = original_indices[position + len(key) - 1] + 1
        spans.append((token, start, end))
        cursor = position + len(key)

    return spans, unmatched


def assign_fragment_times(fragments, spans):
    """Attach start/end times to fragments from matched token spans."""
    span_index = 0
    previous_end = 0.0

    for fragment in fragments:
        while span_index < len(spans) and spans[span_index][2] <= fragment.start:
            span_index += 1

        inside = []
        probe = span_index
        while probe < len(spans) and spans[probe][1] < fragment.end:
            inside.append(spans[probe])
            probe += 1

        if inside:
            start = min(item[0].start_time for item in inside)
            end = max(item[0].end_time for item in inside)
        else:
            start = end = previous_end

        start = max(start, previous_end)
        end = max(end, start)
        fragment.start_time = start
        fragment.end_time = end
        previous_end = end


def ensure_positive_durations(fragments, duration=None, minimum=0.05):
    """Give every fragment a non-zero, non-overlapping time range.

    Qwen returns zero-length tokens for text that is not in the recording
    (scene separators, status-box labels the narrator skipped). A SMIL clip
    with ``clipBegin == clipEnd`` is useless and some readers reject it, so
    those fragments are stretched by a few milliseconds. The stretch is capped
    by the audio length (plus the validator's tolerance), so a subsequent
    fragment is never pushed past the end of the chapter.
    """
    previous_end = 0.0
    for fragment in fragments:
        start = max(fragment.start_time, previous_end)
        end = max(fragment.end_time, start)
        if end - start < minimum:
            end = start + minimum
        if duration is not None and end > duration + 0.2:
            end = duration + 0.2
            if end - start < minimum:
                start = max(previous_end, end - minimum)
        fragment.start_time = start
        fragment.end_time = end
        previous_end = end


def fragment_at(fragments, char_offset):
    """Find the fragment containing a character offset (or the nearest one)."""
    for fragment in fragments:
        if fragment.start <= char_offset < fragment.end:
            return fragment
    return None
