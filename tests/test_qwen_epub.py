"""Tests for ZIP-level EPUB reading and writing."""

import os
import shutil
import tempfile
import unittest
import zipfile

from qwen_pipeline.epub import (
    EpubArchive,
    find_audio_files,
    find_epub_file,
    write_epub,
)
from qwen_pipeline.errors import PipelineError
from qwen_pipeline.xmlutil import parse_xml, serialize_xml

try:
    from .qwen_fixtures import make_epub
except ImportError:  # discover -s tests imports test modules as top-level
    from qwen_fixtures import make_epub


class EpubFixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.epub = make_epub(os.path.join(self.folder, "book.epub"))

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)


class ReadingTests(EpubFixture):
    def test_finds_package_document_and_spine(self):
        with EpubArchive(self.epub) as archive:
            self.assertEqual(archive.opf_name, "OEBPS/content.opf")
            self.assertEqual(archive.opf_dir, "OEBPS")
            self.assertTrue(archive.is_epub3())
            self.assertEqual(archive.metadata["title"], "Test Book")
            self.assertEqual(archive.metadata["language"], "en")
            self.assertEqual(archive.metadata["creator"], "Nobody")
            self.assertEqual([document.item.id for document in archive.spine],
                             ["cover-doc", "ch1", "ch2"])

    def test_default_exclusion_drops_cover_and_nav(self):
        with EpubArchive(self.epub) as archive:
            documents = archive.text_documents(
                r"(?i)(^|/)(nav|toc|cover|titlepage|title-page)(\.xhtml|\.html)?$"
            )
            self.assertEqual([document.item.id for document in documents],
                             ["ch1", "ch2"])

    def test_missing_epub_is_reported(self):
        with self.assertRaises(PipelineError):
            EpubArchive(os.path.join(self.folder, "nope.epub"))

    def test_find_epub_rejects_ambiguity(self):
        make_epub(os.path.join(self.folder, "second.epub"))
        with self.assertRaises(PipelineError):
            find_epub_file(self.folder)

    def test_audio_files_sort_naturally(self):
        names = ["chapter10.mp3", "chapter2.MP3", "notes.txt", "chapter1.mp3"]
        for name in names:
            with open(os.path.join(self.folder, name), "wb") as handle:
                handle.write(b"x")
        found = [os.path.basename(path) for path in find_audio_files(self.folder)]
        self.assertEqual(found, ["chapter1.mp3", "chapter2.MP3", "chapter10.mp3"])


class WritingTests(EpubFixture):
    def test_replacement_and_addition_preserve_untouched_entries(self):
        destination = os.path.join(self.folder, "out.epub")
        replacement = b"<?xml version='1.0'?><html><body><p>new</p></body></html>"
        write_epub(
            self.epub,
            destination,
            replacements={"OEBPS/chapter1.xhtml": replacement},
            additions=[("OEBPS/audio/one.mp3", b"audio-bytes")],
        )

        with zipfile.ZipFile(self.epub) as source, zipfile.ZipFile(destination) as result:
            self.assertEqual(result.namelist()[0], "mimetype")
            self.assertEqual(result.getinfo("mimetype").compress_type,
                             zipfile.ZIP_STORED)
            self.assertEqual(result.read("mimetype"), b"application/epub+zip")
            self.assertEqual(result.read("OEBPS/chapter1.xhtml"), replacement)
            self.assertEqual(result.read("OEBPS/style.css"),
                             source.read("OEBPS/style.css"))
            self.assertEqual(result.read("OEBPS/cover.png"),
                             source.read("OEBPS/cover.png"))
            self.assertEqual(result.read("OEBPS/audio/one.mp3"), b"audio-bytes")
            self.assertEqual(result.getinfo("OEBPS/audio/one.mp3").compress_type,
                             zipfile.ZIP_STORED)

    def test_duplicate_addition_is_rejected(self):
        with self.assertRaises(PipelineError):
            write_epub(
                self.epub,
                os.path.join(self.folder, "out.epub"),
                additions=[("OEBPS/style.css", b"no")],
            )

    def test_epub_without_mimetype_still_gets_one(self):
        source = os.path.join(self.folder, "bare.zip")
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("META-INF/container.xml", b"<container/>")

        destination = os.path.join(self.folder, "fixed.epub")
        write_epub(source, destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(archive.namelist()[0], "mimetype")
            self.assertEqual(archive.read("mimetype"), b"application/epub+zip")


class XmlTests(unittest.TestCase):
    def test_doctype_and_namespaces_survive(self):
        raw = (
            b'<?xml version="1.0" encoding="utf-8"?>\n'
            b"<!DOCTYPE html>\n"
            b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            b"<p>Hi&nbsp;there.</p></body></html>"
        )
        tree = parse_xml(raw, "test.xhtml")
        output = serialize_xml(tree)
        self.assertIn(b"<!DOCTYPE html>", output)
        self.assertIn(b"http://www.w3.org/1999/xhtml", output)
        self.assertIn(b"\xc2\xa0", output)
        self.assertEqual(tree.getroot().tag,
                         "{http://www.w3.org/1999/xhtml}html")


if __name__ == "__main__":
    unittest.main()
