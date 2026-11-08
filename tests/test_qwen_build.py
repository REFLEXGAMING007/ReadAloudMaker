"""End-to-end tests for the Qwen3 pipeline using the deterministic aligner."""

import json
import os
import shutil
import tempfile
import unittest
import zipfile

from lxml import etree

from qwen_pipeline.align import ProportionalAligner
from qwen_pipeline.build import BuildOptions, build
from qwen_pipeline.errors import PipelineError
from qwen_pipeline.validate import validate_epub

try:
    from .qwen_fixtures import make_audio_folder, make_epub
except ImportError:  # discover -s tests imports test modules as top-level
    from qwen_fixtures import make_audio_folder, make_epub


class PipelineFixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.epub = make_epub(os.path.join(self.folder, "book.epub"))
        make_audio_folder(self.folder)

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def options(self, **overrides):
        values = dict(
            folder=self.folder,
            fake_aligner=True,
            out=os.path.join(self.folder, "result"),
        )
        values.update(overrides)
        return BuildOptions(**values)

    def build(self, **overrides):
        from unittest import mock

        with mock.patch("qwen_pipeline.build.probe_duration", return_value=30.0), \
                mock.patch("qwen_pipeline.build.detect_silences", return_value=[]):
            return build(self.options(**overrides), aligner=ProportionalAligner())


class BuildTests(PipelineFixture):
    def test_end_to_end_epub(self):
        destination = self.build()
        self.assertTrue(os.path.isfile(destination))

        with zipfile.ZipFile(destination) as archive:
            infos = archive.infolist()
            self.assertEqual(infos[0].filename, "mimetype")
            self.assertEqual(infos[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.read("mimetype"), b"application/epub+zip")
            self.assertIn("OEBPS/audio/chapter0001.mp3", archive.namelist())
            self.assertIn("OEBPS/overlays/chapter0001.smil", archive.namelist())
            self.assertIn("OEBPS/audio/chapter0002.mp3", archive.namelist())

            opf = etree.fromstring(archive.read("OEBPS/content.opf"))
            namespace = {"opf": "http://www.idpf.org/2007/opf"}
            items = {item.get("id"): item
                     for item in opf.findall(".//opf:manifest/opf:item", namespace)}
            self.assertEqual(items["ch1"].get("media-overlay"), "ra-smil-0001")
            self.assertEqual(items["ch2"].get("media-overlay"), "ra-smil-0002")
            self.assertEqual(items["ra-smil-0001"].get("href"),
                             "overlays/chapter0001.smil")
            self.assertEqual(items["ra-audio-0001"].get("media-type"), "audio/mpeg")

            chapter = archive.read("OEBPS/chapter1.xhtml").decode("utf-8")
            self.assertIn("ra0001-", chapter)

            smil = archive.read("OEBPS/overlays/chapter0001.smil").decode("utf-8")
            self.assertIn("../chapter1.xhtml#ra0001-", smil)
            self.assertIn("../audio/chapter0001.mp3", smil)

        with open(os.path.join(self.folder, "result", "report.json"),
                  encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["book"], "Test Book")
        self.assertEqual(report["language"], "English")
        self.assertEqual(report["chapters"][0]["coverage"], 1.0)
        self.assertEqual(report["validation"]["overlays"], 2)
        self.assertFalse(os.path.exists(os.path.join(self.folder, "_qwen_work")))

        validate_epub(destination)

    def test_keep_work_writes_timings(self):
        self.build(keep_work=True)
        work = os.path.join(self.folder, "_qwen_work")
        self.assertTrue(os.path.isfile(os.path.join(work, "align", "chapter0001.json")))
        with open(os.path.join(work, "align", "chapter0001.json"),
                  encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertGreater(len(payload["fragments"]), 0)
        self.assertTrue(payload["fragments"][0]["id"])

    def test_dry_run_needs_no_model(self):
        from unittest import mock

        with mock.patch("qwen_pipeline.build.probe_duration", return_value=30.0), \
                mock.patch("qwen_pipeline.build.detect_silences", return_value=[]):
            result = build(self.options(dry_run=True))
        self.assertIsNone(result)
        with open(os.path.join(self.folder, "_qwen_work", "plan.json"),
                  encoding="utf-8") as handle:
            plan = json.load(handle)
        self.assertEqual(len(plan["chapters"]), 2)
        self.assertEqual(plan["chapters"][0]["chunks"][0]["end"], 30.0)

    def test_audio_count_must_match_chapters(self):
        os.remove(os.path.join(self.folder, "chapter02.mp3"))
        with self.assertRaises(PipelineError) as caught:
            self.build()
        self.assertIn("exactly one audio file", str(caught.exception))

    def test_epub2_is_rejected(self):
        episode = make_epub(os.path.join(self.folder, "old.epub"), version="2.0")
        with self.assertRaises(PipelineError) as caught:
            self.build(epub=episode)
        self.assertIn("EPUB 3", str(caught.exception))

    def test_exclude_empty_string_keeps_cover(self):
        from unittest import mock

        with mock.patch("qwen_pipeline.build.probe_duration", return_value=30.0), \
                mock.patch("qwen_pipeline.build.detect_silences", return_value=[]):
            with self.assertRaises(PipelineError):
                build(self.options(exclude=""), aligner=ProportionalAligner())


if __name__ == "__main__":
    unittest.main()
