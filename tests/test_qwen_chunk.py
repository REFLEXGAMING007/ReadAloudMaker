"""Tests for chunk planning and the alignment-window safety margin."""

import unittest
from types import SimpleNamespace

from qwen_pipeline.chunk import (
    DEFAULT_MAX_SECONDS,
    HARD_LIMIT_SECONDS,
    plan_chunks,
)
from qwen_pipeline.errors import PipelineError


def make_fragments(count=100, words=10):
    fragments = []
    for index in range(count):
        start = index * 11
        fragments.append(SimpleNamespace(start=start, end=start + 10, words=words))
    return fragments


class ChunkPlanTests(unittest.TestCase):
    def test_chunks_are_contiguous_and_cover_every_fragment(self):
        fragments = make_fragments()
        chunks = plan_chunks(1000.0, fragments, max_seconds=150.0)
        self.assertEqual(chunks[0].first, 0)
        self.assertEqual(chunks[-1].last, len(fragments))
        for previous, current in zip(chunks, chunks[1:]):
            self.assertEqual(previous.last, current.first)
            self.assertEqual(current.text_start, fragments[current.first].start)
        for chunk in chunks:
            self.assertEqual(chunk.text_end, fragments[chunk.last - 1].end)

    def test_estimation_and_margin(self):
        chunks = plan_chunks(1000.0, make_fragments(), max_seconds=150.0)
        self.assertEqual(len(chunks), 7)
        self.assertAlmostEqual(chunks[0].start, 0.0, places=3)
        self.assertAlmostEqual(chunks[0].end, 180.0, places=3)
        self.assertAlmostEqual(chunks[1].end, 330.0, places=3)
        self.assertAlmostEqual(chunks[-1].end, 1000.0, places=3)

    def test_default_matches_the_documented_value(self):
        self.assertEqual(DEFAULT_MAX_SECONDS, 150.0)
        chunks = plan_chunks(1000.0, make_fragments())
        self.assertEqual(len(chunks), 7)
        self.assertEqual(chunks[0].end, 180.0)

    def test_short_chapter_is_one_chunk(self):
        fragments = make_fragments(count=10)
        chunks = plan_chunks(90.0, fragments, max_seconds=150.0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].end, 90.0)

    def test_silence_snaps_window_without_cutting_text(self):
        chunks = plan_chunks(
            1000.0, make_fragments(), max_seconds=150.0,
            silences=[155.0, 175.0],
        )
        self.assertEqual(chunks[0].end, 175.0)

    def test_silence_before_the_estimate_is_ignored(self):
        chunks = plan_chunks(
            1000.0, make_fragments(), max_seconds=150.0,
            silences=[100.0, 200.0],
        )
        self.assertEqual(chunks[0].end, 180.0)

    def test_long_fragment_still_progresses(self):
        fragments = [SimpleNamespace(start=0, end=500, words=1000)]
        chunks = plan_chunks(500.0, fragments, max_seconds=150.0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].last, 1)

    def test_zero_word_fragment_is_kept(self):
        fragments = make_fragments(count=3)
        fragments.insert(1, SimpleNamespace(start=11, end=12, words=0))
        chunks = plan_chunks(33.0, fragments, max_seconds=150.0)
        self.assertEqual(chunks[0].last, len(fragments))

    def test_max_allowed_value_is_accepted(self):
        fragments = [SimpleNamespace(start=0, end=500, words=1000)]
        chunks = plan_chunks(
            500.0, fragments, max_seconds=HARD_LIMIT_SECONDS - 10.0
        )
        self.assertEqual(len(chunks), 1)

    def test_rejects_bad_input(self):
        self.assertEqual(plan_chunks(10.0, []), [])
        with self.assertRaises(PipelineError):
            plan_chunks(0.0, make_fragments())
        with self.assertRaises(PipelineError):
            plan_chunks(10.0, make_fragments(), max_seconds=HARD_LIMIT_SECONDS + 1)
        with self.assertRaises(PipelineError):
            plan_chunks(10.0, [SimpleNamespace(start=0, end=1, words=0)])


if __name__ == "__main__":
    unittest.main()
