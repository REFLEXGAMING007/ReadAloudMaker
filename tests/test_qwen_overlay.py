"""Tests for SMIL generation and OPF media-overlay wiring."""

import unittest

from qwen_pipeline.errors import PipelineError
from qwen_pipeline.overlay import (
    Overlay,
    apply_overlays,
    build_smil,
    format_clock,
    media_type_for,
    relative_href,
)
from qwen_pipeline.validate import _clock_seconds
from qwen_pipeline.xmlutil import find_first, localname, parse_xml

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">x</dc:identifier>
    <dc:title>Book</dc:title>
    <dc:language>en</dc:language>
    <meta property="media:duration">9:99:99.999</meta>
  </metadata>
  <manifest>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>
"""


class ClockTests(unittest.TestCase):
    def test_format_clock(self):
        self.assertEqual(format_clock(0), "0:00:00.000")
        self.assertEqual(format_clock(1.2345), "0:00:01.234")
        self.assertEqual(format_clock(3661.5), "1:01:01.500")
        self.assertEqual(format_clock(-3), "0:00:00.000")

    def test_parse_clock_roundtrip(self):
        for value in (0.0, 1.234, 61.5, 3661.5):
            self.assertAlmostEqual(_clock_seconds(format_clock(value)), value, places=3)
        self.assertAlmostEqual(_clock_seconds("12.5s"), 12.5)
        self.assertAlmostEqual(_clock_seconds("250ms"), 0.25)


class SmilTests(unittest.TestCase):
    def test_build_smil(self):
        fragment = type("F", (), {"id": "ra-1", "start_time": 1.5, "end_time": 2.5})()
        data = build_smil("../chapter1.xhtml", "../audio/ch1.mp3", [fragment])
        tree = parse_xml(data, "chapter1.smil")
        root = tree.getroot()
        self.assertEqual(root.tag, "{http://www.w3.org/ns/SMIL}smil")
        pars = [element for element in root.iter()
                if localname(element) == "par"]
        self.assertEqual(len(pars), 1)
        text = [child for child in pars[0] if localname(child) == "text"][0]
        audio = [child for child in pars[0] if localname(child) == "audio"][0]
        self.assertEqual(text.get("src"), "../chapter1.xhtml#ra-1")
        self.assertEqual(audio.get("src"), "../audio/ch1.mp3")
        self.assertEqual(audio.get("clipBegin"), "1.500s")
        self.assertEqual(audio.get("clipEnd"), "2.500s")

    def test_relative_href(self):
        self.assertEqual(
            relative_href("OEBPS/chapter1.xhtml", "OEBPS/overlays/ch1.smil"),
            "../chapter1.xhtml",
        )
        self.assertEqual(
            relative_href("OEBPS/audio/ch1.mp3", "OEBPS/overlays/ch1.smil"),
            "../audio/ch1.mp3",
        )

    def test_media_types(self):
        self.assertEqual(media_type_for("a.MP3"), "audio/mpeg")
        self.assertEqual(media_type_for("a.m4b"), "audio/mp4")
        self.assertEqual(media_type_for("a.weird"), "audio/mpeg")


class OpfTests(unittest.TestCase):
    def test_apply_overlays(self):
        tree = parse_xml(OPF.encode("utf-8"), "content.opf")
        overlay = Overlay(
            xhtml_item_id="ch1",
            xhtml_href="chapter1.xhtml",
            smil_item_id="ra-smil-0001",
            smil_href="overlays/chapter1.smil",
            audio_item_id="ra-audio-0001",
            audio_href="audio/chapter1.mp3",
            media_type="audio/mpeg",
            duration=61.5,
        )
        apply_overlays(tree, [overlay], 61.5)
        root = tree.getroot()

        manifest = find_first(root, "manifest")
        items = {item.get("id"): item for item in manifest
                 if localname(item) == "item"}
        self.assertEqual(items["ch1"].get("media-overlay"), "ra-smil-0001")
        self.assertEqual(items["ra-smil-0001"].get("href"),
                         "overlays/chapter1.smil")
        self.assertEqual(items["ra-smil-0001"].get("media-type"),
                         "application/smil+xml")
        self.assertEqual(items["ra-audio-0001"].get("href"),
                         "audio/chapter1.mp3")
        self.assertEqual(items["ra-audio-0001"].get("media-type"), "audio/mpeg")

        metadata = find_first(root, "metadata")
        durations = [element for element in metadata
                     if localname(element) == "meta"
                     and element.get("property") == "media:duration"]
        self.assertEqual(len(durations), 2)
        refined = [element for element in durations if element.get("refines")]
        book = [element for element in durations if not element.get("refines")]
        self.assertEqual(refined[0].get("refines"), "#ra-smil-0001")
        self.assertEqual(refined[0].text, "0:01:01.500")
        self.assertEqual(book[0].text, "0:01:01.500")

    def test_missing_text_item_is_reported(self):
        tree = parse_xml(OPF.encode("utf-8"), "content.opf")
        overlay = Overlay(
            xhtml_item_id="gone",
            xhtml_href="gone.xhtml",
            smil_item_id="s",
            smil_href="s.smil",
            audio_item_id="a",
            audio_href="a.mp3",
            media_type="audio/mpeg",
            duration=1.0,
        )
        with self.assertRaises(PipelineError):
            apply_overlays(tree, [overlay], 1.0)


if __name__ == "__main__":
    unittest.main()
