"""GPU discovery for the UI (nvidia-smi parsing, no torch needed)."""

import os
import unittest
from unittest import mock

from readaloud_ui.environment import (
    _parse_gpus,
    _visible_gpus,
    gpu_name,
    recommended_gpu,
)

SAMPLE = """0, NVIDIA GeForce GTX 1050 Ti, 4096, 3100, 6.1, 582.16
1, NVIDIA GeForce GTX 1060 6GB, 6144, 5700, 6.1, 582.16
2, NVIDIA GeForce GTX 1060 6GB, 6144, 5800, 6.1, 582.16"""


class ParseTests(unittest.TestCase):
    def test_parses_every_gpu(self):
        gpus = _parse_gpus(SAMPLE)
        self.assertEqual([gpu["index"] for gpu in gpus], [0, 1, 2])
        self.assertEqual(gpus[0]["name"], "NVIDIA GeForce GTX 1050 Ti")
        self.assertEqual(gpus[0]["totalMemory"], 4096 * 1024 * 1024)
        self.assertEqual(gpus[0]["freeMemory"], 3100 * 1024 * 1024)
        self.assertEqual(gpus[0]["computeCap"], "6.1")
        self.assertEqual(gpus[0]["driver"], "582.16")

    def test_ignores_junk_rows(self):
        self.assertEqual(_parse_gpus(""), [])
        self.assertEqual(_parse_gpus("No devices were found"), [])
        gpus = _parse_gpus("0, GPU, 1024, 512")
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["computeCap"], "")

    def test_header_row_is_skipped(self):
        text = "index, name, memory.total [MiB], memory.free [MiB]\n" + SAMPLE
        self.assertEqual(len(_parse_gpus(text)), 3)


class RecommendTests(unittest.TestCase):
    def test_recommends_the_best_class_of_card(self):
        # Two 1060s beat the 1050 Ti; free memory is not part of the ranking.
        self.assertEqual(recommended_gpu(_parse_gpus(SAMPLE)), 1)

    def test_prefers_a_newer_architecture(self):
        gpus = _parse_gpus(
            "0, RTX 3090, 24576, 3000, 8.6, 582\n"
            "1, GTX 1060 6GB, 6144, 6000, 6.1, 582"
        )
        self.assertEqual(recommended_gpu(gpus), 0)

    def test_no_gpus_means_no_recommendation(self):
        self.assertIsNone(recommended_gpu([]))
        self.assertEqual(gpu_name([]), "")

    def test_gpu_name_is_the_first_card(self):
        self.assertEqual(gpu_name(_parse_gpus(SAMPLE)), "NVIDIA GeForce GTX 1050 Ti")


class VisibleTests(unittest.TestCase):
    """The UI must number cards the way torch does, not the way nvidia-smi does."""

    def test_no_variable_keeps_every_card(self):
        with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": ""}):
            self.assertEqual(len(_visible_gpus(_parse_gpus(SAMPLE))), 3)

    def test_subset_is_renumbered_from_zero(self):
        with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "1,2"}):
            gpus = _visible_gpus(_parse_gpus(SAMPLE))
            self.assertEqual([gpu["index"] for gpu in gpus], [0, 1])
            self.assertEqual([gpu["physicalIndex"] for gpu in gpus], [1, 2])
            self.assertIn("1060", gpus[0]["name"])

    def test_minus_one_hides_every_card(self):
        with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "-1"}):
            self.assertEqual(_visible_gpus(_parse_gpus(SAMPLE)), [])

    def test_uuid_list_is_left_untouched(self):
        with mock.patch.dict(os.environ, {"CUDA_VISIBLE_DEVICES": "GPU-abc123"}):
            self.assertEqual(len(_visible_gpus(_parse_gpus(SAMPLE))), 3)


if __name__ == "__main__":
    unittest.main()
