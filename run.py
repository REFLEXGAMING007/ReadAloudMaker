#!/usr/bin/env python3
"""Unified ReadAloudMaker launcher.

    run.py                     start the web UI (http://127.0.0.1:9459)
    run.py --ui [options]      same, explicitly
    run.py --v1 [options]      classic aeneas + syncabook pipeline (CLI)
    run.py --v2 [options]      Qwen3-ForcedAligner pipeline (CLI)

The launchers (run.bat / run.sh) only have to find *a* Python; this script then
selects the interpreter that actually has the requested engine installed. That
is how both pipelines can live behind one command.
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def _usage():
    print(__doc__.strip())


def _missing(engine):
    print("")
    print("ReadAloudMaker: %s is not ready on this machine." % engine.get("name", "engine"))
    missing = engine.get("missing") or []
    if missing:
        print("Missing: " + ", ".join(missing))
    print("")
    if engine.get("key") == "qwen":
        print("Qwen3 setup:")
        print("  py -3.12 -m venv .venv-qwen")
        print("  .venv-qwen\\Scripts\\python.exe -m pip install torch "
              "--index-url https://download.pytorch.org/whl/cu124")
        print("  .venv-qwen\\Scripts\\python.exe -m pip install -r requirements-qwen.txt")
        print("")
        print("See docs/QWEN3_PIPELINE.md for details.")
    else:
        print("Classic setup:")
        print("  py -3.9 -m pip install -r requirements.txt")
        print("  Install FFmpeg and eSpeak NG, then add both to PATH.")
        print("")
        print("See README.md for details.")
    print("")


def _run_cli(mode, arguments):
    from readaloud_ui import environment

    key = "classic" if mode == "v1" else "qwen"
    data = environment.detect()
    engine = next((item for item in data.get("engines", [])
                   if item.get("key") == key), None)
    if engine is None or not engine.get("available"):
        _missing(engine or {"key": key, "name": "the engine"})
        return 1

    if key == "classic":
        command = list(engine["command"]) + [os.path.join(ROOT, "make_readaloud.py")]
        cwd = ROOT
    else:
        command = list(engine["command"]) + ["-m", "qwen_pipeline"]
        cwd = ROOT

    environment_variables = os.environ.copy()
    environment_variables["PYTHONIOENCODING"] = "utf-8"
    environment_variables["PYTHONUNBUFFERED"] = "1"
    environment_variables.setdefault("PYTHONUTF8", "1")

    print("[readaloud] engine: %s (%s)" % (engine.get("name"), engine.get("pythonLabel")))
    try:
        return subprocess.call(command + list(arguments), cwd=cwd,
                               env=environment_variables)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print("Could not start the build: %s" % exc, file=sys.stderr)
        return 1


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)

    if arguments and arguments[0] in ("-h", "--help", "/?"):
        _usage()
        return 0

    mode = "ui"
    if arguments:
        head = arguments[0].lower()
        if head in ("--v1", "--classic"):
            mode = "v1"
            arguments = arguments[1:]
        elif head in ("--v2", "--qwen"):
            mode = "v2"
            arguments = arguments[1:]
        elif head in ("--ui", "--web"):
            mode = "ui"
            arguments = arguments[1:]

    if mode == "ui":
        from readaloud_ui.cli import main as ui_main
        return ui_main(arguments)

    return _run_cli(mode, arguments)


if __name__ == "__main__":
    sys.exit(main())
