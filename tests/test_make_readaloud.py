"""Unit tests for ReadAloudMaker.

These tests use only the Python standard library (the module itself imports
only stdlib at module level), so they run without aeneas, FFmpeg or eSpeak.
"""

import codecs
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import make_readaloud as m  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CONTAINER_XML = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0"
           xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="EPUB/content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

LONG_TEXT = "This is a reasonably long paragraph so the chapter is not " \
            "treated as empty by the EPUB reader. "


def long_html(title, paragraphs=3, extra=""):
    body = "".join("<p>%s %d</p>" % (LONG_TEXT, i)
                   for i in range(paragraphs))
    return ("<?xml version='1.0' encoding='utf-8'?>\n"
            "<html xmlns='http://www.w3.org/1999/xhtml'><head>"
            "<title>%s</title></head><body><h1>%s</h1>%s%s</body></html>"
            % (title, title, body, extra))


def build_epub(path, documents, title="Test Book", author="Test Author",
               language="en", cover=None):
    """Write a minimal EPUB3 for read_epub() tests.

    documents: list of dicts with name (inside EPUB/), html, linear.
    """
    items = []
    refs = []
    for index, doc in enumerate(documents):
        items.append('<item id="c%d" href="%s" '
                     'media-type="application/xhtml+xml"/>'
                     % (index, doc["name"]))
        linear = "" if doc.get("linear", True) else ' linear="no"'
        refs.append('<itemref idref="c%d"%s/>' % (index, linear))

    if cover is not None:
        items.append('<item id="cover-img" href="cover.png" '
                     'media-type="image/png" properties="cover-image"/>')

    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"
         unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">test-id</dc:identifier>
    <dc:title>%s</dc:title>
    <dc:creator>%s</dc:creator>
    <dc:language>%s</dc:language>
  </metadata>
  <manifest>
    %s
  </manifest>
  <spine>
    %s
  </spine>
</package>
""" % (title, author, language, "\n    ".join(items), "\n    ".join(refs))

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        z.writestr("EPUB/content.opf", opf)
        for doc in documents:
            z.writestr("EPUB/" + doc["name"], doc["html"])
        if cover is not None:
            z.writestr("EPUB/cover.png", cover)


def build_overlay_epub(path, *, smil=True, pars=True, overlay_attr=True,
                       chapter_id="f000001", fragment="f000001",
                       audio_exists=True, begin="00:00:00.000",
                       end="00:00:10.000", opf_bytes=None):
    """Write a tiny EPUB3 with one chapter and (optionally broken) overlays."""
    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"
         unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">test-id</dc:identifier>
    <dc:title>Overlay Test</dc:title>
    <dc:language>en</dc:language>
  </metadata>
  <manifest>
    <item id="textchapter0001" href="text/chapter0001.xhtml"
          media-type="application/xhtml+xml"%s/>
    <item id="audiochapter0001" href="audio/chapter0001.mp3"
          media-type="audio/mpeg"/>
    <item id="smilchapter0001" href="smil/chapter0001.smil"
          media-type="application/smil+xml"/>
  </manifest>
  <spine>
    <itemref idref="textchapter0001"/>
  </spine>
</package>
""" % (' media-overlay="smilchapter0001"' if overlay_attr else "")

    chapter = ("<?xml version='1.0' encoding='utf-8'?>\n"
               "<html xmlns='http://www.w3.org/1999/xhtml'><body>"
               "<p id=\"%s\">Hello world.</p></body></html>" % chapter_id)

    par = """      <par id="par000001">
        <text src="../text/chapter0001.xhtml#%s"/>
        <audio src="../audio/chapter0001.mp3"
               clipBegin="%s" clipEnd="%s"/>
      </par>
""" % (fragment, begin, end)
    body = par if pars else ""
    smil_xml = """<smil xmlns="http://www.w3.org/ns/SMIL"
      xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">
  <body>
    <seq id="seq1" epub:textref="../text/chapter0001.xhtml">
%s    </seq>
  </body>
</smil>
""" % body

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER_XML)
        if opf_bytes is None:
            z.writestr("EPUB/content.opf", opf)
        else:
            z.writestr("EPUB/content.opf", opf_bytes)
        z.writestr("EPUB/text/chapter0001.xhtml", chapter)
        if smil:
            z.writestr("EPUB/smil/chapter0001.smil", smil_xml)
        if audio_exists:
            z.writestr("EPUB/audio/chapter0001.mp3", b"ID3 fake audio")


def replace_member(path, name, data):
    temp = path.with_suffix(".tmp")
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(temp, "w") as dst:
        for info in src.infolist():
            payload = data if info.filename == name else src.read(info.filename)
            dst.writestr(info, payload)
    temp.replace(path)


def quietly(function, *args, **kwargs):
    """Run a function and return (result, printed output)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = function(*args, **kwargs)
    return result, buffer.getvalue()


def prepare_args(**overrides):
    values = {
        "folder": None,
        "epub": None,
        "lang": None,
        "title": None,
        "author": None,
        "exclude": m.DEFAULT_EXCLUDE,
        "prepare_only": True,
        "keep_work": False,
    }
    values.update(overrides)
    return Namespace(**values)


# ---------------------------------------------------------------------------
# inject_ids
# ---------------------------------------------------------------------------

class InjectIdTests(unittest.TestCase):
    def test_paragraphs_get_sequential_ids(self):
        html, count = m.inject_ids("<p>A</p><p>B</p>")
        self.assertEqual(count, 2)
        self.assertIn('<p id="f000001">A</p>', html)
        self.assertIn('<p id="f000002">B</p>', html)

    def test_headings_h1_to_h6(self):
        source = "".join("<h%d>H%d</h%d>" % (i, i, i) for i in range(1, 7))
        html, count = m.inject_ids(source)
        self.assertEqual(count, 6)
        self.assertIn('<h1 id="f000001">H1</h1>', html)
        self.assertIn('<h6 id="f000006">H6</h6>', html)

    def test_existing_ids_are_preserved_and_not_counted(self):
        html, count = m.inject_ids('<p id="keep">A</p><p>B</p>')
        self.assertEqual(count, 1)
        self.assertIn('<p id="keep">A</p>', html)
        self.assertIn('<p id="f000001">B</p>', html)

    def test_pre_and_picture_are_not_treated_as_p(self):
        source = "<pre>code</pre><picture><source/></picture>"
        html, count = m.inject_ids(source)
        self.assertEqual(html, source)
        self.assertEqual(count, 0)

    def test_title_divs_double_and_single_quotes(self):
        source = ('<div class="box-title">A</div>'
                  "<div class='blue-title'>B</div>"
                  '<div class="novel-title extra">C</div>')
        html, count = m.inject_ids(source)
        self.assertEqual(count, 3)
        self.assertIn('<div id="f000001" class="box-title">A</div>', html)
        self.assertIn("<div id=\"f000002\" class='blue-title'>B</div>", html)
        self.assertIn('<div id="f000003" class="novel-title extra">C</div>',
                      html)

    def test_text_center_divs(self):
        html, count = m.inject_ids('<div class="text-center">* * *</div>')
        self.assertEqual(count, 1)
        self.assertIn('<div id="f000001" class="text-center">', html)

    def test_plain_div_untouched(self):
        source = '<div class="narration"><p>A</p></div>'
        html, count = m.inject_ids(source)
        self.assertEqual(count, 1)
        self.assertIn('<div class="narration"><p id="f000001">A</p></div>',
                      html)

    def test_numbering_order_is_paragraphs_then_divs(self):
        # The injector numbers all <p>/<hN> first, then the div passes. This is
        # exactly why the aligner must use document order, not numeric order.
        html, count = m.inject_ids(
            '<div class="box-title">Title</div><p>Paragraph</p>')
        self.assertEqual(count, 2)
        self.assertIn('<p id="f000001">Paragraph</p>', html)
        self.assertIn('<div id="f000002" class="box-title">Title</div>', html)

    def test_attributes_are_preserved(self):
        html, _ = m.inject_ids('<p class="dialogue" data-x="1">Hi</p>')
        self.assertIn('<p id="f000001" class="dialogue" data-x="1">Hi</p>',
                      html)

    def test_self_closing_and_uppercase_tags(self):
        html, count = m.inject_ids("<P>A</P><H2>B</H2>")
        self.assertEqual(count, 2)
        self.assertIn('<P id="f000001">A</P>', html)
        self.assertIn('<H2 id="f000002">B</H2>', html)


# ---------------------------------------------------------------------------
# Sorting and naming
# ---------------------------------------------------------------------------

class NaturalSortTests(unittest.TestCase):
    def test_numbers_sort_numerically(self):
        names = ["chapter 10.mp3", "chapter 2.mp3", "chapter 1.mp3"]
        ordered = sorted((Path(n) for n in names), key=m.natural_key)
        self.assertEqual([p.name for p in ordered],
                         ["chapter 1.mp3", "chapter 2.mp3", "chapter 10.mp3"])

    def test_numeric_prefix_and_suffix(self):
        names = ["03 - x.mp3", "1 - x.mp3", "02 - x.mp3"]
        ordered = sorted((Path(n) for n in names), key=m.natural_key)
        self.assertEqual([p.name for p in ordered],
                         ["1 - x.mp3", "02 - x.mp3", "03 - x.mp3"])

    def test_mixed_letters_and_numbers_do_not_crash(self):
        names = ["a1.mp3", "a.mp3", "1.mp3", "b2.mp3"]
        ordered = sorted((Path(n) for n in names), key=m.natural_key)
        self.assertEqual(len(ordered), 4)

    def test_case_insensitive(self):
        names = ["Chapter 2.mp3", "chapter 1.MP3"]
        ordered = sorted((Path(n) for n in names), key=m.natural_key)
        self.assertEqual(ordered[0].name, "chapter 1.MP3")


class FindAudioTests(unittest.TestCase):
    def test_only_audio_files_and_natural_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("b10.mp3", "b2.mp3", "b1.MP3", "notes.txt",
                         "book.epub", "x.wav"):
                (root / name).write_bytes(b"x")
            (root / "sub").mkdir()
            (root / "sub" / "nested.mp3").write_bytes(b"x")

            found = m.find_audio_files(root)
            self.assertEqual([p.name for p in found],
                             ["b1.MP3", "b2.mp3", "b10.mp3", "x.wav"])


class CleanTitleTests(unittest.TestCase):
    def test_invalid_filename_characters_replaced(self):
        self.assertEqual(m.clean_title('a/b:c*d?e"f<g>h|i'), "a-b-c-d-e-f-g-h-i")

    def test_whitespace_collapsed(self):
        self.assertEqual(m.clean_title("  a   b  "), "a b")

    def test_empty_falls_back(self):
        self.assertEqual(m.clean_title(""), "Read Aloud Book")
        self.assertEqual(m.clean_title(None), "Read Aloud Book")

    def test_truncated(self):
        self.assertEqual(len(m.clean_title("x" * 500)), 150)

    def test_trailing_dot_and_space_removed(self):
        self.assertEqual(m.clean_title("Book. "), "Book")


class UniquePathTests(unittest.TestCase):
    def test_appends_number(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book_readaloud.epub"
            self.assertEqual(m.unique_path(path), path)
            path.write_text("x")
            second = m.unique_path(path)
            self.assertEqual(second.name, "book_readaloud (2).epub")
            second.write_text("x")
            third = m.unique_path(path)
            self.assertEqual(third.name, "book_readaloud (3).epub")


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

class DecodeHtmlTests(unittest.TestCase):
    def test_utf8(self):
        self.assertEqual(m.decode_html("caf\u00e9".encode("utf-8")),
                         "caf\u00e9")

    def test_utf8_bom(self):
        raw = codecs.BOM_UTF8 + "caf\u00e9".encode("utf-8")
        self.assertEqual(m.decode_html(raw), "caf\u00e9")

    def test_utf16_bom(self):
        self.assertEqual(m.decode_html("caf\u00e9".encode("utf-16")),
                         "caf\u00e9")

    def test_cp1252_fallback(self):
        # 0x92 is a right single quotation mark in cp1252, invalid UTF-8.
        self.assertEqual(m.decode_html(b"caf\x92"), "caf\u2019")

    def test_declared_encoding(self):
        raw = (b'<?xml version="1.0" encoding="ISO-8859-1"?>'
               b"<p>caf\xe9</p>")
        self.assertIn("caf\u00e9", m.decode_html(raw))

    def test_unknown_declared_encoding_falls_back(self):
        raw = b'<?xml version="1.0" encoding="not-a-real-codec"?><p>x</p>'
        self.assertIn("<p>x</p>", m.decode_html(raw))


class TextHelperTests(unittest.TestCase):
    def test_strip_tags_removes_style_and_script(self):
        html = ("<style>p{color:red}</style><script>x()</script>"
                "<p>Hello &amp; bye</p>")
        self.assertEqual(m.strip_tags(html), "Hello & bye")

    def test_guess_title_prefers_title_tag(self):
        html = "<html><head><title>Real Title</title></head><body>" \
               "<h1>Heading</h1></body></html>"
        self.assertEqual(m.guess_title(html), "Real Title")

    def test_guess_title_falls_back_to_heading(self):
        self.assertEqual(m.guess_title("<body><h1>Heading</h1></body>"),
                         "Heading")

    def test_guess_title_empty(self):
        self.assertEqual(m.guess_title("<body><p>text</p></body>"), "")


class ClockSecondsTests(unittest.TestCase):
    def test_full_clock(self):
        self.assertAlmostEqual(m.clock_seconds("00:21:06.720"), 1266.72)

    def test_short_clock(self):
        self.assertAlmostEqual(m.clock_seconds("1:02:03.5"), 3723.5)
        self.assertAlmostEqual(m.clock_seconds("0:10"), 10.0)

    def test_seconds_value(self):
        self.assertAlmostEqual(m.clock_seconds("12.5s"), 12.5)

    def test_invalid_value_fails(self):
        with self.assertRaises(SystemExit):
            m.clock_seconds("not-a-time")


# ---------------------------------------------------------------------------
# EPUB reading
# ---------------------------------------------------------------------------

class ReadEpubTests(unittest.TestCase):
    def test_reads_chapters_in_spine_order_and_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_epub(path, [
                {"name": "second.xhtml", "html": long_html("Second")},
                {"name": "first.xhtml", "html": long_html("First")},
            ], title="My Book", author="Me", language="de",
                cover=b"\x89PNG fake")

            chapters, metadata, cover = m.read_epub(path, m.DEFAULT_EXCLUDE)

            self.assertEqual([c["title"] for c in chapters], ["Second", "First"])
            self.assertEqual([c["zip_name"] for c in chapters],
                             ["EPUB/second.xhtml", "EPUB/first.xhtml"])
            self.assertEqual(metadata["title"], "My Book")
            self.assertEqual(metadata["author"], "Me")
            self.assertEqual(metadata["language"], "de")
            self.assertEqual(cover[0], b"\x89PNG fake")
            self.assertEqual(cover[1], ".png")

    def test_skips_nav_cover_short_and_non_linear(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_epub(path, [
                {"name": "nav.xhtml", "html": long_html("Navigation")},
                {"name": "cover.xhtml", "html": long_html("Cover")},
                {"name": "tiny.xhtml", "html": "<p>short</p>"},
                {"name": "appendix.xhtml", "html": long_html("Appendix"),
                 "linear": False},
                {"name": "chapter.xhtml", "html": long_html("Chapter")},
            ])

            chapters, _, _ = m.read_epub(path, m.DEFAULT_EXCLUDE)
            self.assertEqual([c["title"] for c in chapters], ["Chapter"])

    def test_custom_exclude(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_epub(path, [
                {"name": "front.xhtml", "html": long_html("Front")},
                {"name": "chapter.xhtml", "html": long_html("Chapter")},
            ])
            chapters, _, _ = m.read_epub(path, r"(?i)(^|/)front\.xhtml$")
            self.assertEqual([c["title"] for c in chapters], ["Chapter"])

    def test_empty_spine_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_epub(path, [{"name": "tiny.xhtml", "html": "<p>x</p>"}])
            with self.assertRaises(SystemExit):
                m.read_epub(path, m.DEFAULT_EXCLUDE)

    def test_bad_zip_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            path.write_text("not a zip")
            with self.assertRaises(SystemExit):
                m.read_epub(path, m.DEFAULT_EXCLUDE)

    def test_missing_container_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr("mimetype", "application/epub+zip")
            with self.assertRaises(SystemExit):
                m.read_epub(path, m.DEFAULT_EXCLUDE)


# ---------------------------------------------------------------------------
# Generated navigation / colophon
# ---------------------------------------------------------------------------

class NavigationTests(unittest.TestCase):
    def test_nav_links_and_escaping(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "nav.xhtml"
            m.write_nav(path, ["A & B", "Chapter 2"])
            text = path.read_text(encoding="utf-8")
            self.assertIn('href="chapter0001.xhtml"', text)
            self.assertIn("A &amp; B", text)
            self.assertIn('href="chapter0002.xhtml"', text)

    def test_colophon_escaping(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "colophon.xhtml"
            m.write_colophon(path, "A & B", "<Author>")
            text = path.read_text(encoding="utf-8")
            self.assertIn("A &amp; B", text)
            self.assertIn("&lt;Author&gt;", text)


# ---------------------------------------------------------------------------
# EPUB validation
# ---------------------------------------------------------------------------

class ValidateEpubTests(unittest.TestCase):
    def test_valid_overlay_book(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_overlay_epub(path)
            _, output = quietly(m.validate_epub, path)
            self.assertIn("EPUB OK (1 media overlays)", output)

    def test_invalid_utf8_opf(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_overlay_epub(path)
            good = zipfile.ZipFile(path).read("EPUB/content.opf")
            bad = good.replace(b"Overlay Test", b"Overlay \x92Test")
            replace_member(path, "EPUB/content.opf", bad)
            with self.assertRaises(SystemExit):
                quietly(m.validate_epub, path)

    def test_missing_smil(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_overlay_epub(path, smil=False)
            with self.assertRaises(SystemExit):
                quietly(m.validate_epub, path)

    def test_smil_without_par(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_overlay_epub(path, pars=False)
            with self.assertRaises(SystemExit):
                quietly(m.validate_epub, path)

    def test_no_media_overlay_attribute(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_overlay_epub(path, overlay_attr=False)
            with self.assertRaises(SystemExit):
                quietly(m.validate_epub, path)

    def test_text_fragment_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_overlay_epub(path, fragment="f999999")
            with self.assertRaises(SystemExit):
                quietly(m.validate_epub, path)

    def test_audio_file_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_overlay_epub(path, audio_exists=False)
            with self.assertRaises(SystemExit):
                quietly(m.validate_epub, path)

    def test_clip_ends_before_it_begins(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_overlay_epub(path, begin="00:00:10.000",
                               end="00:00:05.000")
            with self.assertRaises(SystemExit):
                quietly(m.validate_epub, path)

    def test_zero_length_clip_is_allowed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "book.epub"
            build_overlay_epub(path, begin="00:00:05.000",
                               end="00:00:05.000")
            _, output = quietly(m.validate_epub, path)
            self.assertIn("EPUB OK", output)


# ---------------------------------------------------------------------------
# End-to-end preparation (no aeneas / FFmpeg needed)
# ---------------------------------------------------------------------------

class PrepareOnlyTests(unittest.TestCase):
    def make_book(self, folder, chapters=2, audio=2):
        root = Path(folder)
        documents = [
            {"name": "chapter%04d.xhtml" % i,
             "html": long_html("Chapter %d" % i)}
            for i in range(1, chapters + 1)
        ]
        build_epub(root / "book.epub", documents,
                   title="Integration Book", author="Tester")
        for i in range(1, audio + 1):
            (root / ("Chapter %02d.mp3" % i)).write_bytes(b"ID3 %d" % i)
        return root

    def test_prepare_only_builds_expected_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.make_book(folder)
            work = root / m.WORK_DIR_NAME
            args = prepare_args()

            with mock.patch.object(m, "check_engine", return_value=None):
                _, output = quietly(m.run, root, work, args)

            self.assertIn("chapters: 2", output)
            self.assertIn("8 fragment IDs injected", output)

            text1 = (work / "sync_text" / "chapter0001.xhtml").read_text(
                encoding="utf-8")
            self.assertIn('id="f000001"', text1)
            self.assertTrue((work / "audio" / "chapter0001.mp3").exists())
            self.assertTrue((work / "audio" / "chapter0002.mp3").exists())

            metadata = json.loads((work / "metadata.json").read_text(
                encoding="utf-8"))
            self.assertEqual(metadata["title"], "Integration Book")
            self.assertEqual(metadata["author"], "Tester")
            # metadata.json must be pure ASCII so syncabook reads it safely.
            raw = (work / "metadata.json").read_bytes()
            raw.decode("ascii")

            nav = (work / "no_sync_text" / "nav.xhtml").read_text(
                encoding="utf-8")
            self.assertIn("chapter0001.xhtml", nav)
            self.assertIn("chapter0002.xhtml", nav)
            self.assertTrue((work / "no_sync_text" / "colophon.xhtml").exists())

    def test_count_mismatch_fails_before_work(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.make_book(folder, chapters=2, audio=1)
            args = prepare_args()
            with self.assertRaises(SystemExit) as ctx:
                quietly(m.run, root, root / m.WORK_DIR_NAME, args)
            message = str(ctx.exception)
            self.assertIn("Chapter documents found", message)
            self.assertIn("Audio files found", message)
            self.assertFalse((root / m.WORK_DIR_NAME).exists())

    def test_no_audio_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.make_book(folder, chapters=1, audio=0)
            with self.assertRaises(SystemExit):
                quietly(m.run, root, root / m.WORK_DIR_NAME,
                        prepare_args())

    def test_no_epub_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.mp3").write_bytes(b"x")
            with self.assertRaises(SystemExit):
                quietly(m.run, root, root / m.WORK_DIR_NAME,
                        prepare_args())

    def test_two_epubs_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.make_book(folder)
            (root / "second.epub").write_bytes(b"x")
            with self.assertRaises(SystemExit):
                quietly(m.run, root, root / m.WORK_DIR_NAME,
                        prepare_args())


if __name__ == "__main__":
    unittest.main()
