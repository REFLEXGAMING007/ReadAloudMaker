"""Device selection for the Qwen3 pipeline (no torch, no GPU needed)."""

import unittest
from types import SimpleNamespace

from qwen_pipeline.align import gpu_snapshot, select_device
from qwen_pipeline.errors import PipelineError


class FakeCuda(object):
    def __init__(self, devices):
        self.devices = devices
        self.current = None

    def is_available(self):
        return bool(self.devices)

    def device_count(self):
        return len(self.devices)

    def get_device_name(self, index):
        return self.devices[index]["name"]

    def get_device_capability(self, index):
        return self.devices[index]["capability"]

    def get_device_properties(self, index):
        return SimpleNamespace(total_memory=self.devices[index]["total"])

    def mem_get_info(self, index):
        return (self.devices[index]["free"], self.devices[index]["total"])

    def set_device(self, device):
        self.current = device


class FakeTorch(object):
    def __init__(self, devices=()):
        self.cuda = FakeCuda(list(devices))


def gpu(name, capability=(6, 1), total=6.0, free=None):
    total_bytes = int(total * 1024 ** 3)
    free_bytes = total_bytes if free is None else int(free * 1024 ** 3)
    return {
        "name": name,
        "capability": capability,
        "total": total_bytes,
        "free": free_bytes,
    }


# The machine this was written on: one 1050 Ti, two 1060s, 1050 Ti first.
THREE_CARDS = [
    gpu("NVIDIA GeForce GTX 1050 Ti", total=4.0, free=3.1),
    gpu("NVIDIA GeForce GTX 1060 6GB", total=6.0, free=5.7),
    gpu("NVIDIA GeForce GTX 1060 6GB", total=6.0, free=5.8),
]


class AutoSelectionTests(unittest.TestCase):
    def test_auto_picks_the_best_card_not_cuda_zero(self):
        device, reason = select_device(FakeTorch(THREE_CARDS), "auto")
        self.assertEqual(device, "cuda:2")
        self.assertIn("best of 3", reason)
        self.assertIn("GTX 1060", reason)

    def test_auto_skips_a_card_with_no_free_memory(self):
        cards = [
            gpu("GTX 1050 Ti", total=4.0, free=1.0),
            gpu("GTX 1060 6GB", total=6.0, free=5.0),
        ]
        device, reason = select_device(FakeTorch(cards), "auto")
        self.assertEqual(device, "cuda:1")
        self.assertIn("skipped", reason)

    def test_auto_prefers_a_newer_architecture_over_raw_memory(self):
        cards = [
            gpu("RTX 3090", capability=(8, 6), total=24.0, free=2.6),
            gpu("GTX 1060 6GB", capability=(6, 1), total=6.0, free=5.8),
        ]
        device, _reason = select_device(FakeTorch(cards), "auto")
        self.assertEqual(device, "cuda:0")

    def test_auto_falls_back_when_every_card_is_tight(self):
        cards = [
            gpu("GTX 1050 Ti", total=4.0, free=0.4),
            gpu("GTX 1060 6GB", total=6.0, free=0.9),
        ]
        device, reason = select_device(FakeTorch(cards), "auto")
        self.assertEqual(device, "cuda:1")
        self.assertIn("skipped", reason)

    def test_auto_without_cuda_uses_cpu(self):
        device, reason = select_device(FakeTorch([]), "auto")
        self.assertEqual(device, "cpu")
        self.assertIn("no CUDA", reason)

    def test_auto_defaults_to_auto(self):
        self.assertEqual(select_device(FakeTorch([]), None)[0], "cpu")
        self.assertEqual(select_device(FakeTorch(THREE_CARDS), "")[0], "cuda:2")


class ExplicitSelectionTests(unittest.TestCase):
    def test_explicit_index_is_kept(self):
        for index in (0, 1, 2):
            device, reason = select_device(FakeTorch(THREE_CARDS), "cuda:%d" % index)
            self.assertEqual(device, "cuda:%d" % index)
            self.assertEqual(reason, "requested")

    def test_bare_cuda_means_zero(self):
        self.assertEqual(select_device(FakeTorch(THREE_CARDS), "cuda")[0], "cuda:0")

    def test_cpu_is_kept(self):
        self.assertEqual(select_device(FakeTorch(THREE_CARDS), "CPU")[0], "cpu")

    def test_missing_index_lists_the_real_devices(self):
        with self.assertRaises(PipelineError) as context:
            select_device(FakeTorch(THREE_CARDS), "cuda:7")
        message = str(context.exception)
        self.assertIn("cuda:7 does not exist", message)
        self.assertIn("GTX 1050 Ti", message)
        self.assertIn("GTX 1060", message)

    def test_cuda_without_a_gpu_is_explained(self):
        with self.assertRaises(PipelineError) as context:
            select_device(FakeTorch([]), "cuda:1")
        self.assertIn("CUDA is not available", str(context.exception))

    def test_nonsense_device_is_rejected(self):
        with self.assertRaises(PipelineError):
            select_device(FakeTorch(THREE_CARDS), "rocm:0")
        with self.assertRaises(PipelineError):
            select_device(FakeTorch(THREE_CARDS), "cudafoo")
        with self.assertRaises(PipelineError):
            select_device(FakeTorch(THREE_CARDS), "cuda:first")
        with self.assertRaises(PipelineError):
            select_device(FakeTorch(THREE_CARDS), "cuda:-1")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_reports_name_capability_and_memory(self):
        snapshot = gpu_snapshot(FakeTorch(THREE_CARDS))
        self.assertEqual(len(snapshot), 3)
        self.assertEqual(snapshot[1]["name"], "NVIDIA GeForce GTX 1060 6GB")
        self.assertEqual(snapshot[1]["capability"], 61)
        self.assertEqual(snapshot[1]["total_memory"], int(6 * 1024 ** 3))
        self.assertGreater(snapshot[1]["free_memory"], 0)

    def test_snapshot_survives_a_broken_cuda(self):
        torch = FakeTorch(THREE_CARDS)

        def boom(*args, **kwargs):
            raise RuntimeError("CUDA is on fire")

        torch.cuda.get_device_name = boom
        torch.cuda.get_device_capability = boom
        torch.cuda.mem_get_info = boom
        snapshot = gpu_snapshot(torch)
        self.assertEqual(len(snapshot), 3)
        self.assertEqual(snapshot[0]["name"], "cuda:0")

    def test_snapshot_without_cuda_is_empty(self):
        self.assertEqual(gpu_snapshot(FakeTorch([])), [])


if __name__ == "__main__":
    unittest.main()
