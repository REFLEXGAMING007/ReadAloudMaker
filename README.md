# ReadAloudMaker

Turn **one EPUB** plus **one audio file per chapter** into an **EPUB 3 with Media
Overlays** — the "read-aloud" format where the text is highlighted as the
narration plays.

Put the files in a folder, double-click `run.bat` (Windows) or run `./run.sh`
(Linux/macOS), and get `output/<book title>_readaloud.epub`.

> **Two pipelines.** `run.bat` / `run.sh` keep the original aeneas + syncabook
> pipeline. `run2.bat` / `run2.sh` use **Qwen3-ForcedAligner-0.6B** and a
> built-in EPUB packager instead. The second pipeline is independent, needs
> Python 3.12 + PyTorch, and is documented in
> [docs/QWEN3_PIPELINE.md](docs/QWEN3_PIPELINE.md).

![Python](https://img.shields.io/badge/python-3.9-blue)
![EPUB](https://img.shields.io/badge/EPUB-3-green)
![License](https://img.shields.io/badge/license-MIT-blue)
![Qwen3](https://img.shields.io/badge/v2-Qwen3--ForcedAligner-purple)
[![tests](https://github.com/REFLEXGAMING007/ReadAloudMaker/actions/workflows/tests.yml/badge.svg)](https://github.com/REFLEXGAMING007/ReadAloudMaker/actions/workflows/tests.yml)

---

## What it does

```
 my book.epub          chapter 1.mp3
                       chapter 2.mp3
                       chapter 3.mp3
        │                    │
        └────────┬───────────┘
                 ▼
          ReadAloudMaker
                 ▼
   output/my book_readaloud.epub
        (EPUB 3 + Media Overlays)
```

- Reads the EPUB spine, so chapters are used in the book's real order.
- Adds `id="f000001"`-style IDs to every spoken block (headings, paragraphs,
  status-box titles, scene dividers).
- Pairs audio files with chapters by **natural sort order** (`2` before `10`).
  File names do **not** need to match the EPUB's internal names — only the
  count has to match.
- Converts non-MP3 audio to MP3 automatically with FFmpeg.
- Aligns each chapter with [aeneas](https://www.readbeyond.it/aeneas/) and
  packages the result with [syncabook](https://github.com/r4victor/syncabook).
- Carries over the cover, title, author, language, and per-chapter titles.
- Validates the finished EPUB before telling you it worked: OPF is valid UTF-8,
  every media overlay exists, every SMIL `<par>` points at a real fragment and a
  real MP3.
- **No Docker**, no afaligner, no manual project folder. One command.

Works with any text-based EPUB whose chapter count matches the audio count.
It does not split a single full-book audiobook into chapters — see
[Limitations](#limitations).

---

## Requirements

| Requirement | Why |
|---|---|
| **Python 3.9** | aeneas 1.7.3 ships prebuilt C extensions for 3.9. Newer versions often fail to build them. |
| **FFmpeg + ffprobe** | audio conversion and duration probing |
| **eSpeak NG** | speech synthesis used internally by aeneas |
| **aeneas + syncabook** | installed with `pip install -r requirements.txt`. This also installs their dependencies: **lxml**, BeautifulSoup4, Jinja2, numpy and progressbar2. |

> Docker is **not** required. The original syncabook workflow used afaligner in a
> container; this tool calls aeneas directly and hands the SMIL files to
> `syncabook create`.

### Install the Python packages

```bash
python3.9 -m pip install -r requirements.txt
```

On Windows:

```bat
py -3.9 -m pip install -r requirements.txt
```

`lxml` is required, but you do not install it yourself: it is a dependency of
both aeneas and syncabook and comes in automatically with the command above.
The same applies to BeautifulSoup4, Jinja2, numpy and progressbar2.

### Install FFmpeg and eSpeak NG

**Windows**

```powershell
winget install Gyan.FFmpeg
```

eSpeak NG: download the installer from
<https://github.com/espeak-ng/espeak-ng/releases> and add its folder to `PATH`
(the installer usually does this).

**Debian / Ubuntu**

```bash
sudo apt update
sudo apt install ffmpeg espeak-ng python3.9 python3.9-venv
```

If your distribution does not provide Python 3.9, use
[deadsnakes](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa) or `pyenv`.

**macOS (Homebrew)**

```bash
brew install ffmpeg espeak-ng python@3.9
```

Check everything:

```bash
ffmpeg -version
ffprobe -version
espeak-ng --version
python3.9 -c "import aeneas; print(aeneas.__version__)"
python3.9 -c "import lxml; print(lxml.__version__)"
syncabook -h
```

---

## Quick start

1. Clone or download this repository.
2. Put your book and audio **in the repository folder**, next to `run.bat`:

   ```text
   ReadAloudMaker/
       run.bat
       run.sh
       make_readaloud.py
       requirements.txt
       ...
       my book.epub
       chapter 01.mp3
       chapter 02.mp3
       chapter 03.mp3
   ```

   - Exactly **one** `.epub`.
   - Exactly **one audio file per chapter**.
   - Supported audio: `mp3`, `m4a`, `m4b`, `wav`, `ogg`, `flac`, `aac`, `opus`.

3. Double-click `run.bat` (Windows) or run:

   ```bash
   ./run.sh
   ```

   If the executable bit was lost (common when downloading a ZIP), run
   `chmod +x run.sh` once, or use `bash run.sh`.

4. Open `output/<book title>_readaloud.epub` in a reader that supports Media
   Overlays. **Thorium Reader** is the easiest choice on Windows, Linux and
   macOS. Press play and the current sentence is highlighted.

> Calibre's viewer and most generic EPUB readers do **not** implement Media
> Overlays. An EPUB that looks fine there can still be a perfectly good
> read-aloud book elsewhere.

### Test without waiting for alignment

```bash
./run.sh --prepare-only
```

This unpacks the EPUB, injects IDs, copies/converts the audio, writes the
metadata and table of contents, and stops. It prints the chapter list and the
audio list, so it is the fastest way to check that the pairing is right. The
`_readaloud_work/` folder is kept so you can inspect it.

## Qwen3 pipeline (v2)

The second launcher is independent of aeneas and syncabook:

```bat
py -3.12 -m venv .venv-qwen
.venv-qwen\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu124
.venv-qwen\Scripts\python.exe -m pip install -r requirements-qwen.txt
run2.bat --folder "C:\books\my book" --dry-run
run2.bat --folder "C:\books\my book"
```

```bash
python3.12 -m venv .venv-qwen
.venv-qwen/bin/pip install torch
.venv-qwen/bin/pip install -r requirements-qwen.txt
./run2.sh --folder "/books/my book" --dry-run
./run2.sh --folder "/books/my book"
```

`--dry-run` needs only `lxml` and FFmpeg: it parses the EPUB, plans alignment
windows and writes `_qwen_work/plan.json` without downloading the model.
`--fake-aligner` builds a real EPUB with placeholder timings, which is useful
for checking the packager on a machine without a GPU.

See [docs/QWEN3_PIPELINE.md](docs/QWEN3_PIPELINE.md) for requirements,
options, accuracy details and GPU memory tips.

---

## Matching chapters to audio

Audio files are sorted **naturally** and paired with the EPUB spine in order:

```text
Chapter 1.mp3   ->  EPUB chapter 1
Chapter 2.mp3   ->  EPUB chapter 2
Chapter 10.mp3  ->  EPUB chapter 10   (not before chapter 2)
```

Names are only used for sorting. If the counts differ, the tool stops **before
doing any work** and prints both lists:

```text
Chapter documents found in the EPUB (spine order):
   1. EPUB/chapter0001.xhtml    Chapter 1: ...
   2. EPUB/chapter0002.xhtml    Chapter 2: ...

Audio files found (natural sort order):
   1. 01.mp3

There must be exactly one audio file per chapter.
```

Cover, nav and toc documents are skipped automatically, as are spine documents
with fewer than 50 characters of text. If your book has a title page or
copyright page with real text in the spine, either remove it from the EPUB or
exclude it with `--exclude`.

---

## Command-line options

Both launchers pass options through:

```bat
run.bat --lang eng
run.bat --title "My Book" --author "Author Name"
```

```bash
./run.sh --lang eng
./run.sh --title "My Book" --author "Author Name"
```

| Option | Meaning |
|---|---|
| `--folder PATH` | Folder with the EPUB and audio (default: the script's folder). |
| `--epub PATH` | Use a specific EPUB (relative paths are resolved inside `--folder`). |
| `--lang CODE` | aeneas language (`eng`, `deu`, `fra`, …). Default: from the EPUB's `dc:language`, falling back to `eng`. |
| `--title TEXT` | Override the book title. |
| `--author TEXT` | Override the author. |
| `--exclude REGEX` | Skip spine documents whose path matches this regex. Default skips `cover`, `nav`, `toc`. |
| `--prepare-only` | Stop after preparation (no alignment, no EPUB). |
| `--keep-work` | Keep `_readaloud_work/` after a successful build. |
| `--version` | Print the version. |
| `-h`, `--help` | Full help. |

Language codes are passed to aeneas, so any language eSpeak NG supports works.
If the EPUB has no `dc:language`, or the narration is in another language,
**set `--lang` explicitly** — alignment quality depends on it.

---

## How it works

```text
 EPUB ──► spine order ──► inject id="f000001"… ──► sync_text/chapterNNNN.xhtml
                                                             │
 audio ──► natural sort ──► chapterNNNN.mp3 ────────────────┤
                                                             ▼
                                        aeneas (per chapter) ──► smil/chapterNNNN.smil
                                                             │
 metadata.json + nav.xhtml + colophon.xhtml + cover.jpg ─────┤
                                                             ▼
                                             syncabook create ──► EPUB 3 + Media Overlays
                                                             │
                                                             ▼
                                              built-in validation ──► output/
```

A few implementation details that matter:

- **Document order, not numeric ID order.** The ID injector numbers all
  paragraphs first, then status-box titles and scene dividers, so the IDs are
  not in reading order. aeneas is run with
  `is_text_unparsed_id_sort=unsorted`; using `numeric` would move those
  fragments to the end of the chapter and give them the wrong audio.
- **UTF-8 packaging.** `syncabook create` writes `content.opf` with the default
  system encoding. The tool runs it with `PYTHONUTF8=1`, so titles containing
  characters like `’` or `ä` stay valid UTF-8.
- **Silent fragments.** Punctuation-only fragments such as `“…”` may get a
  zero-length clip. That is harmless: they are highlighted instantly.

---

## Output

```text
output/
└── <book title>_readaloud.epub
```

Running again does not overwrite: the second file becomes
`<book title> (2)_readaloud.epub`.

On failure the `_readaloud_work/` folder is kept with:

```text
_readaloud_work/
├── audio/            converted chapterNNNN.mp3
├── sync_text/        chapterNNNN.xhtml with injected IDs
├── smil/             chapterNNNN.smil from aeneas
├── no_sync_text/     generated nav.xhtml and colophon.xhtml
├── images/           cover.jpg
├── metadata.json
└── out/              syncabook's own output
```

On success the folder is deleted (unless `--keep-work` is given).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `no .epub file found` | Put exactly one `.epub` in the folder, or pass `--epub`. |
| `more than one .epub found` | Keep only the book you want, or pass `--epub`. |
| `There must be exactly one audio file per chapter` | Read the two printed lists. Rename, move or add the audio so the counts match. |
| `alignment failed on chapter N` | The audio for that chapter does not match its text, is silent, or the language is wrong. Check the order, then try `--lang eng`/`deu`/… |
| `aeneas is not installed for this Python` | The launcher picked the wrong Python. Install requirements for 3.9: `py -3.9 -m pip install -r requirements.txt`. |
| `missing required program(s): ffmpeg, ffprobe` | Install FFmpeg and make sure `ffmpeg -version` works in a new terminal. |
| `missing required program(s): espeak-ng` | Install eSpeak NG and add it to `PATH`. |
| `syncabook was not found` | `python3.9 -m pip install -r requirements.txt`. |
| `ModuleNotFoundError: No module named 'lxml'` | The install was interrupted or a different Python was used. Re-run `python3.9 -m pip install -r requirements.txt`; lxml comes with aeneas/syncabook. |
| The EPUB opens but there is no highlighting | Your reader does not support Media Overlays. Use Thorium Reader. |
| A few paragraphs highlight instantly | They are punctuation-only fragments (`“…”`, `[…]`). Expected. |
| Alignment is slow | Expected: aeneas synthesizes every fragment with eSpeak and then aligns audio features. A 39-minute sample took about 15–25 s on the development machine; a full-length audiobook can take several minutes. It is CPU-bound and scales roughly with the amount of audio. |
| `generated content.opf is not valid UTF-8` | You are running an old copy of `syncabook` whose default encoding is not UTF-8, or `PYTHONUTF8` was overridden. Update the tool and reinstall `requirements.txt`. |

### Why Python 3.9?

aeneas 1.7.3 is the last release with prebuilt extensions and it needs Python
≤ 3.9. If you must use a newer Python, install a 3.9 interpreter just for this
tool (venv, pyenv, or `py -3.9`).

---

## Limitations

- **One audio file per chapter.** A single full-book MP3 is not split
  automatically. Cut it at chapter boundaries first (for example with
  `ffmpeg -ss <start> -to <end> -i full.mp3 -c copy chapter01.mp3`).
- **Text-based EPUBs only.** No OCR, no PDF input.
- **Audio must actually match the text.** The tool aligns; it cannot invent
  narration for missing or reordered chapters.
- **Windows is the best-tested platform.** The Python pipeline is
  cross-platform and `run.sh` is provided for Linux/macOS, but those two are
  less exercised than Windows.
- **Not DRM-aware.** Encrypted EPUBs cannot be read.

---

## Development

The script uses only the Python standard library at import time; external tools
are only needed when you actually build a book. That means the unit tests run
without aeneas, FFmpeg or eSpeak installed:

```bash
python3 -m unittest discover -s tests -v
```

Tests cover ID injection, natural sorting, title sanitising, SMIL clock parsing,
EPUB reading, SMIL/text/audio reference validation, and a full `--prepare-only`
run on a synthetic EPUB.

Pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License

[MIT](LICENSE).

This project is not affiliated with aeneas, syncabook, FFmpeg, eSpeak NG or any
reading system. Use it only with books and audio you have the right to process.

## Acknowledgements

- [aeneas](https://www.readbeyond.it/aeneas/) — forced alignment
- [syncabook](https://github.com/r4victor/syncabook) — EPUB 3 Media Overlays packaging
- [FFmpeg](https://ffmpeg.org/) — audio conversion
- [eSpeak NG](https://github.com/espeak-ng/espeak-ng) — speech synthesis used by aeneas
- [Thorium Reader](https://www.edrlab.org/software/thorium-reader/) — the reader used to verify the output
