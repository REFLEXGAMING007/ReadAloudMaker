# Contributing

Thanks for taking a look. This is a small, single-file tool; keep changes
focused and easy to review.

## Ground rules

- **Do not commit books or audio.** `.gitignore` blocks `*.epub` and all common
  audio extensions on purpose. Use a synthetic EPUB in tests instead.
- **Keep `make_readaloud.py` importable without external packages.** Only the
  Python standard library may be imported at module level. `aeneas`,
  `syncabook`, FFmpeg and eSpeak are runtime dependencies and are invoked as
  subprocesses. This is what lets the unit tests run in CI without installing
  the heavy stack.
- **Keep the pipeline dependencies at the edges.** Pure helpers
  (`inject_ids`, `natural_key`, `clean_title`, `decode_html`, `clock_seconds`,
  `validate_epub`, …) should stay testable in isolation.
- **Use Python 3.9-compatible syntax.** No `match`, no `X | Y` type unions in
  annotations.

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

The suite runs on Linux and Windows in GitHub Actions with Python 3.9.

## Testing a real book

```bash
python3 make_readaloud.py --folder /path/to/book --prepare-only
python3 make_readaloud.py --folder /path/to/book
```

Then open `output/*_readaloud.epub` in
[Thorium Reader](https://www.edrlab.org/software/thorium-reader/) and confirm
that highlighting tracks the narration.

## Reporting bugs

Please include:

- operating system and Python version,
- `python -m pip show aeneas syncabook`,
- the full console output (the tool prints the chapter/audio lists and the
  failing chapter),
- which reader you tested the EPUB in.

Do not attach copyrighted books or audio. A small synthetic EPUB that
reproduces the problem is ideal.

## Code style

- 4-space indentation, lines under ~90 characters where practical.
- Keep messages user-facing and actionable: say what failed and how to fix it.
- Update `README.md` when you add or change a command-line option.
