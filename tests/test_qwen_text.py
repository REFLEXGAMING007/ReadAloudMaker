"""Tests for flat-text extraction, sentence splitting and span injection."""

import unittest
from types import SimpleNamespace

from qwen_pipeline.text import (
    analyse_document,
    assign_fragment_times,
    cover_ranges,
    counted_words,
    ensure_positive_durations,
    map_tokens_to_text,
    normalize_stream,
    split_sentences,
    token_key,
)
from qwen_pipeline.xmlutil import XHTML_NS, parse_xml, qname


def fragments_of(text):
    return [text[start:end] for start, end in split_sentences(text)]


class SentenceSplitTests(unittest.TestCase):
    def test_plain_sentences(self):
        self.assertEqual(
            fragments_of("Hello world. This is a test!"),
            ["Hello world.", " This is a test!"],
        )

    def test_abbreviations_do_not_split(self):
        self.assertEqual(len(split_sentences("Mr. Smith went home. Then he slept.")), 2)
        self.assertEqual(len(split_sentences("Dr. Jones paid 3.14 dollars. Fine.")), 2)

    def test_ellipsis_and_quotes_stay_together(self):
        pieces = fragments_of('Wait... "What?" she asked.')
        self.assertEqual(pieces, ["Wait...", ' "What?"', " she asked."])

    def test_cjk_terminators(self):
        self.assertEqual(fragments_of("你好。世界！"), ["你好。", "世界！"])

    def test_cover_ranges_cover_every_character(self):
        text = "  One.   Two! Three?  "
        covered = cover_ranges(text, split_sentences(text))
        joined = "".join(text[start:end] for start, end in covered)
        self.assertEqual(joined, text)
        for previous, current in zip(covered, covered[1:]):
            self.assertEqual(previous[1], current[0])

    def test_empty_text(self):
        self.assertEqual(split_sentences(""), [])
        self.assertEqual(cover_ranges("", []), [])


class TokenMappingTests(unittest.TestCase):
    def test_punctuation_free_tokens_map_back(self):
        text = "Hello, world! It's 3.14 today."
        tokens = ["Hello", "world", "It's", "314", "today"]
        spans, unmatched = map_tokens_to_text(
            text,
            [type("T", (), {"text": token, "start_time": 0.0, "end_time": 1.0})()
             for token in tokens],
        )
        self.assertEqual(unmatched, [])
        self.assertEqual([text[start:end] for _, start, end in spans],
                         ["Hello", "world", "It's", "3.14", "today"])

    def test_token_key_matches_aligner_normalisation(self):
        self.assertEqual(token_key("well-known,"), "wellknown")
        self.assertEqual(token_key("It's"), "it's")
        self.assertEqual(normalize_stream("A\u2014B!")[0], "ab")

    def test_missing_token_is_reported(self):
        text = "one two"
        token = type("T", (), {"text": "three", "start_time": 0.0, "end_time": 1.0})()
        spans, unmatched = map_tokens_to_text(text, [token])
        self.assertEqual(spans, [])
        self.assertEqual(len(unmatched), 1)

    def test_word_count_tracks_speech_units(self):
        # Digits are counted separately, like the syllables a narrator says.
        self.assertEqual(counted_words("It's a 3.14 test."), 5)


class SpanInjectionTests(unittest.TestCase):
    def analyse(self, body):
        document = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>%s</body></html>'
            % body
        )
        tree = parse_xml(document.encode("utf-8"))
        return tree, analyse_document(tree, 1)

    def test_one_sentence_reuses_block_id(self):
        tree, chapter = self.analyse("<p>Hello.</p>")
        root = tree.getroot()
        paragraph = root.find(".//" + qname(XHTML_NS, "p"))
        self.assertIsNotNone(paragraph.get("id"))
        self.assertEqual(len(paragraph.findall(".//" + qname(XHTML_NS, "span"))), 0)
        self.assertEqual(len(chapter.fragments), 1)

    def test_existing_id_is_kept(self):
        tree, chapter = self.analyse('<p id="mine">Hello.</p>')
        paragraph = tree.getroot().find(".//" + qname(XHTML_NS, "p"))
        self.assertEqual(paragraph.get("id"), "mine")
        self.assertEqual(chapter.fragments[0].id, "mine")

    def test_multiple_sentences_create_spans_and_keep_inline_markup(self):
        tree, chapter = self.analyse(
            "<p>Before <em>middle sentence. Second <strong>bold</strong> part.</em> after.</p>"
        )
        root = tree.getroot()
        paragraph = root.find(".//" + qname(XHTML_NS, "p"))

        spans = paragraph.findall(qname(XHTML_NS, "span"))
        self.assertEqual(len(spans), 3)
        self.assertEqual(chapter.fragments[0].text.strip(),
                         "Before middle sentence.")
        self.assertIsNotNone(paragraph.find(".//" + qname(XHTML_NS, "strong")))
        self.assertEqual(
            "".join(paragraph.itertext()),
            "Before middle sentence. Second bold part. after.",
        )

    def test_ids_are_unique_across_blocks(self):
        tree, chapter = self.analyse(
            "<p>One. Two.</p><p>Three. Four.</p><h2>Five.</h2>"
        )
        identifiers = [
            element.get("id")
            for element in tree.getroot().iter()
            if element.get("id")
        ]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(len(chapter.fragments), 5)

    def test_style_and_script_are_skipped(self):
        tree, chapter = self.analyse(
            "<style>.x{color:red}</style><script>var a = 1;</script><p>Real text.</p>"
        )
        self.assertEqual(chapter.text.strip(), "Real text.")

    def test_flat_offsets_match_fragment_text(self):
        tree, chapter = self.analyse("<p>One. Two.</p><p>Three.</p>")
        for fragment in chapter.fragments:
            self.assertEqual(chapter.text[fragment.start:fragment.end], fragment.text)

    def test_named_entities_are_repaired(self):
        tree, chapter = self.analyse("<p>A&nbsp;B&mdash;C.</p>")
        self.assertIn("\u00a0", chapter.text)
        self.assertIn("\u2014", chapter.text)


class TimeAssignmentTests(unittest.TestCase):
    def test_times_follow_token_order_and_never_go_backwards(self):
        text = "One. Two. Three."
        tree = parse_xml(
            ('<html xmlns="http://www.w3.org/1999/xhtml"><body>'
             "<p>%s</p></body></html>" % text).encode("utf-8")
        )
        chapter = analyse_document(tree, 1)

        tokens = [
            type("T", (), {"text": "One", "start_time": 0.0, "end_time": 1.0})(),
            type("T", (), {"text": "Two", "start_time": 1.0, "end_time": 2.0})(),
            type("T", (), {"text": "Three", "start_time": 2.0, "end_time": 3.0})(),
        ]
        spans = [
            (tokens[index], chapter.fragments[index].start,
             chapter.fragments[index].end)
            for index in range(3)
        ]
        assign_fragment_times(chapter.fragments, spans)
        self.assertEqual([fragment.start_time for fragment in chapter.fragments],
                         [0.0, 1.0, 2.0])
        self.assertEqual([fragment.end_time for fragment in chapter.fragments],
                         [1.0, 2.0, 3.0])


class PositiveDurationTests(unittest.TestCase):
    def test_zero_length_fragments_are_stretched(self):
        first = SimpleNamespace(start_time=1.0, end_time=2.0)
        zero = SimpleNamespace(start_time=2.0, end_time=2.0)
        last = SimpleNamespace(start_time=2.0, end_time=3.0)
        ensure_positive_durations([first, zero, last], duration=10.0)
        self.assertGreater(zero.end_time, zero.start_time)
        self.assertGreaterEqual(last.start_time, zero.end_time - 1e-9)
        self.assertGreater(last.end_time, last.start_time)

    def test_consecutive_zero_length_fragments_stay_monotonic(self):
        fragments = [
            SimpleNamespace(start_time=4.0, end_time=4.0),
            SimpleNamespace(start_time=4.0, end_time=4.0),
            SimpleNamespace(start_time=4.0, end_time=5.0),
        ]
        ensure_positive_durations(fragments, duration=10.0)
        for previous, current in zip(fragments, fragments[1:]):
            self.assertGreater(current.start_time, previous.start_time)
            self.assertGreaterEqual(current.start_time, previous.end_time - 1e-9)

    def test_stretch_stays_near_the_audio_length(self):
        last = SimpleNamespace(start_time=9.98, end_time=9.98)
        ensure_positive_durations([last], duration=10.0)
        self.assertGreater(last.end_time, last.start_time)
        self.assertLessEqual(last.end_time, 10.2)


if __name__ == "__main__":
    unittest.main()
