"""Tests for the web UI job runner (no model, no build tools needed)."""

import os
import shutil
import sys
import tempfile
import time
import unittest

from readaloud_ui.jobs import (
    Job,
    OptionError,
    build_command,
    normalize_options,
)


QWEN_ENGINE = {
    "key": "qwen",
    "name": "Qwen3 Forced Aligner",
    "available": True,
    "command": [sys.executable],
    "pythonLabel": "test python",
}

CLASSIC_ENGINE = {
    "key": "classic",
    "name": "Classic",
    "available": True,
    "command": [sys.executable],
    "pythonLabel": "test python",
}


def wait_for(job, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.status in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish (status=%s)" % job.status)


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_qwen_defaults(self):
        options = normalize_options({"folder": self.folder}, "qwen")
        self.assertEqual(options["out"], os.path.join(self.folder, "output"))
        self.assertEqual(options["max_chunk_seconds"], 150.0)
        self.assertAlmostEqual(options["min_coverage"], 0.98)
        self.assertEqual(options["dtype"], "auto")
        self.assertFalse(options["dry_run"])

    def test_relative_output_is_inside_the_source_folder(self):
        options = normalize_options({"folder": self.folder, "out": "my out"}, "qwen")
        self.assertEqual(options["out"], os.path.join(self.folder, "my out"))

    def test_classic_options(self):
        options = normalize_options(
            {"folder": self.folder, "title": "Book", "prepare_only": True},
            "classic",
        )
        self.assertEqual(options["title"], "Book")
        self.assertTrue(options["prepare_only"])
        self.assertNotIn("max_chunk_seconds", options)

    def test_missing_folder_is_rejected(self):
        with self.assertRaises(OptionError):
            normalize_options({"folder": os.path.join(self.folder, "nope")}, "qwen")

    def test_coverage_range_is_enforced(self):
        with self.assertRaises(OptionError):
            normalize_options({"folder": self.folder, "min_coverage": 1.5}, "qwen")

    def test_dry_run_and_fake_aligner_are_exclusive(self):
        with self.assertRaises(OptionError):
            normalize_options(
                {"folder": self.folder, "dry_run": True, "fake_aligner": True},
                "qwen",
            )

    def test_unknown_engine_is_rejected(self):
        with self.assertRaises(OptionError):
            normalize_options({"folder": self.folder}, "magic")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.options = normalize_options({"folder": self.folder}, "qwen")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_qwen_command(self):
        command, cwd = build_command(QWEN_ENGINE, self.options)
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:3], ["-m", "qwen_pipeline"])
        self.assertIn("--folder", command)
        self.assertIn(self.folder, command)
        self.assertIn("--out", command)
        self.assertIn("150", command)
        self.assertIn("--min-coverage", command)

    def test_classic_command(self):
        options = normalize_options({"folder": self.folder}, "classic")
        command, cwd = build_command(CLASSIC_ENGINE, options)
        self.assertTrue(any(part.endswith("make_readaloud.py") for part in command))
        self.assertIn("--folder", command)
        self.assertEqual(cwd, self.folder)

    def test_dry_run_switch_is_forwarded(self):
        options = normalize_options(
            {"folder": self.folder, "dry_run": True}, "qwen")
        command, _ = build_command(QWEN_ENGINE, options)
        self.assertIn("--dry-run", command)


class JobLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.out = os.path.join(self.folder, "output")
        os.makedirs(self.out)
        self.epub = os.path.join(self.out, "book_readaloud.epub")
        with open(self.epub, "wb") as handle:
            handle.write(b"PK\x03\x04fake")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def _options(self):
        return {
            "folder": self.folder,
            "out": self.out,
            "keep_work": False,
        }

    def test_successful_job_collects_the_epub_and_classifies_lines(self):
        script = (
            "import sys\n"
            "print('[readaloud-qwen] EPUB:    book.epub', flush=True)\n"
            "print('[C:/x/layer3.c:INT123_do_layer3():1880] error: "
            "part2_3_length (3808) too large for available bit count (3736)', flush=True)\n"
            "print('[readaloud-qwen] [1/2] Chapter 1', flush=True)\n"
            "print('[readaloud-qwen] packaging EPUB3 with Media Overlays ...', flush=True)\n"
            "print('[readaloud-qwen] check:   EPUB OK (2 media overlays, 10 <par>)', flush=True)\n"
            "print('[readaloud-qwen] DONE: ' + sys.argv[1], flush=True)\n"
        )
        job = Job(QWEN_ENGINE, self._options(),
                  [sys.executable, "-u", "-c", script, self.epub])
        job.start()
        wait_for(job)

        self.assertEqual(job.status, "succeeded", job.error)
        self.assertEqual(job.progress, 1.0)
        self.assertEqual(len(job.outputs), 1)
        self.assertEqual(os.path.normcase(job.outputs[0]["path"]),
                         os.path.normcase(self.epub))
        levels = [line["level"] for line in job.to_dict(0)["lines"]]
        self.assertIn("decoder", levels)
        self.assertIn("success", levels)
        self.assertEqual(job.error, "")

    def test_failure_reports_the_error_line(self):
        script = (
            "import sys\n"
            "print('[readaloud-qwen] aligning', flush=True)\n"
            "print('ERROR: the audio does not match the text', file=sys.stderr, flush=True)\n"
            "sys.exit(3)\n"
        )
        job = Job(QWEN_ENGINE, self._options(),
                  [sys.executable, "-u", "-c", script])
        job.start()
        wait_for(job)

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.exit_code, 3)
        self.assertIn("does not match", job.error)

    def test_cancel_stops_the_process(self):
        script = (
            "import time\n"
            "print('[readaloud-qwen] starting', flush=True)\n"
            "time.sleep(60)\n"
        )
        job = Job(QWEN_ENGINE, self._options(),
                  [sys.executable, "-u", "-c", script])
        job.start()
        deadline = time.time() + 10
        while time.time() < deadline and not job.to_dict(0)["lines"]:
            time.sleep(0.05)
        job.cancel()
        wait_for(job, timeout=20)
        self.assertEqual(job.status, "cancelled")

    def test_since_returns_only_new_lines(self):
        script = (
            "print('one', flush=True)\n"
            "print('two', flush=True)\n"
        )
        job = Job(QWEN_ENGINE, self._options(),
                  [sys.executable, "-u", "-c", script])
        job.start()
        wait_for(job)
        payload = job.to_dict(0)
        total = payload["next"]
        self.assertGreaterEqual(total, 2)
        tail = job.to_dict(total - 1)
        self.assertEqual(len(tail["lines"]), 1)
        self.assertEqual(tail["lines"][0]["text"], "two")


if __name__ == "__main__":
    unittest.main()
