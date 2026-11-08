"""Command line interface for the Qwen3 forced-alignment pipeline."""

import argparse
import sys

from . import __version__
from .build import BuildOptions, build
from .errors import PipelineError


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run2",
        description=(
            "Build an EPUB 3 with Media Overlays using Qwen3-ForcedAligner. "
            "Existing EPUB formatting and assets are preserved."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  run2 --folder .\n"
            "  run2 --folder . --dry-run\n"
            "  run2 --folder . --lang en --device cuda:0\n"
            "  run2 --folder . --exclude \"(?i)(nav|cover)\"\n"
        ),
    )
    parser.add_argument(
        "--folder", default=".",
        help="folder holding the EPUB and the per-chapter audio (default: .)",
    )
    parser.add_argument(
        "--epub", default=None,
        help="EPUB file (default: the only .epub in --folder)",
    )
    parser.add_argument(
        "--audio-dir", default=None,
        help="folder holding the chapter audio (default: --folder)",
    )
    parser.add_argument(
        "--out", default=None,
        help="output folder (default: <folder>/output)",
    )
    parser.add_argument(
        "--lang", default=None,
        help="language name or code, e.g. en, eng, English (default: EPUB dc:language)",
    )
    parser.add_argument(
        "--exclude", default=None,
        help="regex of EPUB document paths to skip; pass an empty string to keep all",
    )
    parser.add_argument(
        "--max-chunk-seconds", type=float, default=150.0,
        help="target narration length per alignment pass (default: 150, max: 170)",
    )
    parser.add_argument(
        "--min-coverage", type=float, default=0.98,
        help="fail if less than this fraction of characters aligns (default: 0.98)",
    )
    parser.add_argument(
        "--device", default="auto",
        help="auto, cpu, cuda:0, cuda:1, ... (default: auto)",
    )
    parser.add_argument(
        "--dtype", default="auto",
        choices=("auto", "bfloat16", "float16", "float32"),
        help="model precision (default: auto; bfloat16 on Ampere+, float16 on older GPUs)",
    )
    parser.add_argument(
        "--model", default="Qwen/Qwen3-ForcedAligner-0.6B",
        help="model id or local folder (default: Qwen/Qwen3-ForcedAligner-0.6B)",
    )
    parser.add_argument(
        "--compile", dest="compile_model", action="store_true",
        help="ask torch.compile to optimise the model (slower first run)",
    )
    parser.add_argument(
        "--keep-work", action="store_true",
        help="keep the _qwen_work folder (text dumps, timings, report)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="parse the EPUB, plan chunks and write a plan without loading the model",
    )
    parser.add_argument(
        "--fake-aligner", action="store_true",
        help="use a deterministic proportion-based aligner instead of Qwen3 (testing)",
    )
    parser.add_argument(
        "--epubcheck", nargs="?", const="epubcheck", default=None,
        metavar="PATH",
        help="also run EPUBCheck (looked up on PATH unless a path is given)",
    )
    parser.add_argument(
        "--version", action="version", version="%(prog)s " + __version__,
    )
    return parser


def options_from_args(args):
    from .build import DEFAULT_EXCLUDE

    exclude = DEFAULT_EXCLUDE if args.exclude is None else args.exclude
    return BuildOptions(
        folder=args.folder,
        epub=args.epub,
        audio_dir=args.audio_dir,
        out=args.out,
        lang=args.lang,
        exclude=exclude,
        max_chunk_seconds=args.max_chunk_seconds,
        min_coverage=args.min_coverage,
        device=args.device,
        dtype=args.dtype,
        model=args.model,
        compile_model=args.compile_model,
        keep_work=args.keep_work,
        dry_run=args.dry_run,
        fake_aligner=args.fake_aligner,
        epubcheck=args.epubcheck,
    )


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not 0.0 < args.min_coverage <= 1.0:
        parser.error("--min-coverage must be greater than 0 and at most 1")

    try:
        result = build(options_from_args(args))
    except PipelineError as exc:
        print("", file=sys.stderr)
        print("ERROR: " + str(exc), file=sys.stderr)
        if getattr(exc, "hint", None):
            print("", file=sys.stderr)
            print("Hint: " + exc.hint, file=sys.stderr)
        print("", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130

    if result:
        print("", file=sys.stdout)
        print("Created: " + str(result), file=sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
