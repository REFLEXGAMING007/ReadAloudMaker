"""Shared fixtures for the Qwen3 pipeline tests."""

import os
import zipfile

CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0"
           xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

CHAPTER_ONE = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
  <head><title>One</title><style type="text/css">p { color: red; }</style></head>
  <body>
    <h1 id="existing">Chapter One</h1>
    <p class="first">Before <em>middle sentence. Second <strong>bold</strong> part.</em> after.</p>
    <div class="box">
      <p>Dawn.</p>
      <p>It costs 3.14 dollars &amp; more.</p>
    </div>
  </body>
</html>
"""

CHAPTER_TWO = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
  <head><title>Two</title></head>
  <body>
    <p>Only one sentence here.</p>
  </body>
</html>
"""

COVER = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
  <body><p>Cover</p></body>
</html>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="uid">test-book</dc:identifier>
    <dc:title>Test Book</dc:title>
    <dc:creator>Nobody</dc:creator>
    <dc:language>en</dc:language>
    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="cover-doc" href="cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover-img" href="cover.png" media-type="image/png" properties="cover-image"/>
    <item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
  </manifest>
  <spine>
    <itemref idref="cover-doc"/>
    <itemref idref="ch1"/>
    <itemref idref="ch2"/>
  </spine>
</package>
"""


def make_epub(path, version="3.0"):
    """Write a small but valid EPUB 3 package to ``path``."""
    opf = OPF
    if version != "3.0":
        opf = OPF.replace('version="3.0"', 'version="%s"' % version)

    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            b"application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", CONTAINER.encode("utf-8"))
        archive.writestr("OEBPS/content.opf", opf.encode("utf-8"))
        archive.writestr("OEBPS/cover.xhtml", COVER.encode("utf-8"))
        archive.writestr("OEBPS/chapter1.xhtml", CHAPTER_ONE.encode("utf-8"))
        archive.writestr("OEBPS/chapter2.xhtml", CHAPTER_TWO.encode("utf-8"))
        archive.writestr("OEBPS/cover.png", b"\x89PNG\r\n\x1a\nfake-image-bytes")
        archive.writestr("OEBPS/style.css", b"p { margin: 0; }\n")
    return str(path)


def make_audio_folder(folder, names=("chapter01.mp3", "chapter02.mp3")):
    """Write placeholder MP3 files; tests never decode them."""
    for index, name in enumerate(names, start=1):
        with open(os.path.join(str(folder), name), "wb") as handle:
            handle.write((b"ID3fake-audio-%d" % index) * 8)
    return [os.path.join(str(folder), name) for name in names]
