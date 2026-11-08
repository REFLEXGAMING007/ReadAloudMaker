# Qwen3 pipeline (v2)

ReadAloudMaker has two independent pipelines:

| | v1 (default) | v2 (this document) |
|---|---|---|
| Launcher | `run.bat` / `run.sh` | `run2.bat` / `run2.sh` |
| Alignment | [aeneas](https://www.readbeyond.it/aeneas/) (DTW) | [Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B) |
| Packaging | [syncabook](https://github.com/r4victor/syncabook) | built-in `zipfile` + `lxml` |
| EPUB handling | unpacked and rebuilt | modified at ZIP level |
| Python | 3.9 | 3.10+ (3.12 recommended) |
| GPU | not required | strongly recommended |

v1 is unchanged and remains the default. v2 is an additional pipeline for
maximum synchronization accuracy: Qwen3 predicts a timestamp for every word
instead of matching an estimated speech signal with DTW. The model card reports
an average alignment error in the 32–43 ms range depending on language; that is
a benchmark figure, not a guarantee for every recording.

---

## What v2 does differently

- **Surgical EPUB packaging.** Untouched entries (fonts, images, CSS, the
  navigation document, the cover) are copied byte for byte; only the chapter
  XHTML, the package document, and the new SMIL/audio entries change. The
  `mimetype` entry stays first and uncompressed.
- **Sentence-level fragments.** Text is flattened, split at sentence
  boundaries, and re-serialized with one `<span id="...">` per sentence.
  Inline markup (`<em>`, `<strong>`, `<a>`, nested spans) is preserved, even
  when a sentence boundary falls inside it.
- **Word-level alignment.** Qwen3 returns `(text, start, end)` for every word
  or CJK character. Fragment times are the min start / max end of the words
  that fall inside the fragment, then clamped to stay monotonic.
- **Windowed long chapters.** A chapter can be much longer than one alignment
  call. qwen-asr caps forced alignment at 180 seconds of audio per pass (the
  model card advertises up to five minutes; the pipeline uses the conservative
  package limit). Chapters are split at sentence boundaries, and each pass gets
  a 30-second audio tail. If the aligned text reaches that tail, the pass is
  repeated with a larger window (up to 180 s). The next pass starts where the
  previous narration actually ended, so errors do not accumulate.
- **Fail fast.** A chapter is rejected when too little of its text can be
  matched, when clips are out of bounds, or when the timeline is not monotonic.
  It will not silently produce a drifting book.

---

## Install

Use a dedicated environment; v2 does **not** need aeneas/syncabook.

### 1. Python 3.12 environment

```bat
py -3.12 -m venv .venv-qwen
.venv-qwen\Scripts\python.exe -m pip install --upgrade pip
.venv-qwen\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu124
.venv-qwen\Scripts\python.exe -m pip install -r requirements-qwen.txt
```

On Linux/macOS:

```bash
python3.12 -m venv .venv-qwen
.venv-qwen/bin/python -m pip install --upgrade pip
.venv-qwen/bin/python -m pip install torch
.venv-qwen/bin/python -m pip install -r requirements-qwen.txt
```

Pick the PyTorch CUDA wheel that matches your driver. The package index above
is for CUDA 12.4; see <https://pytorch.org/get-started/locally/>.

`run2.bat` and `run2.sh` automatically prefer `.venv-qwen` in the repository
folder, then a normal `py -3.12` / `python3.12`, then `python`. Only FFmpeg
(`ffmpeg` + `ffprobe`) has to be on `PATH`.

### 2. Model download

The first run downloads `Qwen/Qwen3-ForcedAligner-0.6B` from Hugging Face
(about 1.2 GB). Set `HF_HOME` if you want the cache somewhere else. To use a
local copy, pass `--model C:\models\Qwen3-ForcedAligner-0.6B`.

---

## Quick start

Put the EPUB and one audio file per chapter in a folder (same rules as v1),
then:

```bat
run2.bat --folder "C:\books\my book"
```

```bash
./run2.sh --folder "/books/my book"
```

The result is written to `<folder>/output/<title>_readaloud.epub`. Media
Overlays need a reader that implements them; **Thorium Reader** is the easiest
choice.

### Check the setup first

```bat
run2.bat --folder "C:\books\my book" --dry-run
```

`--dry-run` only needs `lxml` and FFmpeg. It parses the EPUB, counts
fragments, plans audio windows, and writes `_qwen_work/plan.json` — without
loading torch or the model.

To produce a real EPUB without a GPU (useful for testing the packager), use
`--fake-aligner`. Timings are proportional and **not** accurate, but the EPUB
structure and overlays are real.

---

## Options

| Option | Meaning |
|---|---|
| `--folder PATH` | Folder with the EPUB and audio (default: current folder). |
| `--epub PATH` | Use a specific EPUB. |
| `--audio-dir PATH` | Read audio from another folder. |
| `--out PATH` | Output folder (default: `<folder>/output`). |
| `--lang CODE` | Language name or code: `en`, `eng`, `English`, … Default: EPUB `dc:language`. |
| `--exclude REGEX` | Skip matching EPUB documents. Default skips `cover`, `nav`, `toc`, `titlepage`. Pass `""` to keep everything. |
| `--max-chunk-seconds N` | Planned narration length per alignment pass (default 150). Maximum 170: with the safety tail, no pass exceeds qwen-asr's 180-second audio limit. |
| `--min-coverage F` | Reject a chapter when less than this fraction of characters align (default 0.98). |
| `--device DEV` | `auto`, `cpu`, `cuda:0`, `cuda:1`, … |
| `--dtype TYPE` | `auto`, `bfloat16`, `float16`, `float32`. `auto` picks bfloat16 on Ampere+ and float16 on older NVIDIA cards. |
| `--model ID` | Hugging Face id or local folder. |
| `--compile` | Try `torch.compile` for a slower first run and faster later runs. |
| `--keep-work` | Keep `_qwen_work/` (text dumps, per-fragment timings, report). |
| `--dry-run` | Plan without loading the model. |
| `--fake-aligner` | Deterministic proportional aligner, for tests. |
| `--epubcheck [PATH]` | Also run EPUBCheck. |
| `--version`, `-h` | Version and help. |

Supported languages: Chinese, English, Cantonese, French, German, Italian,
Japanese, Korean, Portuguese, Russian, Spanish.

---

## How a build works

1. `qwen_pipeline.epub` reads `META-INF/container.xml`, the OPF, the manifest,
   and the spine. Cover and nav documents are skipped.
2. Each spine document is parsed with lxml. Text blocks are flattened into a
   string plus an index map, split into sentences, and wrapped in spans.
3. Audio and chapters are paired by natural sort order, exactly as in v1.
4. `qwen_pipeline.chunk` plans alignment windows from the chapter's
   speech-rate estimate, with a 30-second tail and optional snapping to
   detected silences.
5. `qwen_pipeline.align` reads 16 kHz mono audio slices with librosa and calls
   `Qwen3ForcedAligner.align(audio=(wave, sr), text=..., language=...)`.
6. Aligner tokens are matched back to the flattened text on a
   punctuation-free character stream. Matched characters become fragment
   timestamps.
7. `qwen_pipeline.overlay` writes one SMIL file per chapter, adds the audio and
   SMIL items to the OPF manifest, sets `media-overlay` on each chapter item,
   and records `media:duration`.
8. `qwen_pipeline.epub.write_epub` copies the original archive entry by entry,
   replacing only the changed documents and adding the new files.
9. `qwen_pipeline.validate` re-opens the result and checks the mimetype, every
   fragment reference, every audio reference, clip bounds, and monotonicity.

Per-chapter timings and a JSON report are written to
`<out>/report.json`; the detailed per-fragment timings live in
`_qwen_work/align/` when `--keep-work` is used.

---

## Accuracy notes

- Qwen3 timestamps are rounded to milliseconds by the model itself.
- Sentence highlights use the first word's start and the last word's end, so
  a highlight never starts late or ends early inside its sentence.
- Windows overlap slightly (`last word end - 0.25s` starts the next pass), so
  a word at a boundary is never cut. Text chunks do not overlap, so no word is
  highlighted twice.
- If the narration is slower than estimated and the text reaches the audio
  tail, the pass is re-run with up to 60 more seconds, never more than 180
  seconds per call. If that is still not enough the build stops with a clear
  message instead of producing a drifting book.
- `--min-coverage` is checked per chunk *and* per chapter. A chapter with
  skipped front matter, ad-libs, or audio in the wrong order fails loudly.

### GPUs with 4–6 GB

The model is small, but a two-and-a-half-minute window is not. On a GTX 1050 Ti
(4 GB) or GTX 1060 (6 GB):

- use the default `float16` (selected automatically on these cards);
- lower `--max-chunk-seconds` to `90` if you see out-of-memory errors;
- close other GPU applications.

CPU works with `--device cpu`, but is much slower.

---

## Limitations

- **EPUB 3 only.** EPUB 2 has no Media Overlays. Convert old books first.
- One audio file per spine document, in natural sort order. It does not split
  a single full-book audio file.
- The EPUB text must match the narration. If the TTS skipped or added text,
  alignment coverage drops and the build stops instead of guessing.
- DRM-protected EPUBs cannot be opened.
- The model does not return timestamps for pure punctuation; those fragments
  inherit the previous fragment's end time.
- `epubcheck` is optional and not bundled. Structural checks run every time.

---

## Tests

The v2 tests do not need torch, a GPU, or FFmpeg:

```bash
python -m unittest discover -s tests -v
```

They cover EPUB read/write preservation, sentence splitting, nested inline
markup, token mapping, chunk planning, SMIL/OPF generation, structural
validation, and an end-to-end build with the deterministic aligner.

---

## Package layout

```text
qwen_pipeline/
    __init__.py      version
    __main__.py      python -m qwen_pipeline
    cli.py           argument parsing
    build.py         orchestration and self-correction
    epub.py          ZIP/OPF reader and byte-preserving writer
    text.py          flat text, sentences, spans, token mapping
    chunk.py         ffprobe, silences, window planning
    align.py         Qwen3 wrapper (lazy torch import) + test aligner
    overlay.py       SMIL and OPF updates
    validate.py      structural checks
    xmlutil.py       lxml parsing, entity repair, serialization
    errors.py        user-facing exception type
```
