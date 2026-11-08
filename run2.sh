#!/usr/bin/env bash
#
# Launcher for the Qwen3 pipeline (v2). On Windows use run2.bat instead.
#
#   ./run2.sh [options]
#
# Options are passed straight to `python -m qwen_pipeline` (see --help).

set -u
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8

PY=""
if [ -x ".venv-qwen/bin/python" ] &&
   ".venv-qwen/bin/python" -c "import lxml" >/dev/null 2>&1; then
    PY=".venv-qwen/bin/python"
else
    for candidate in python3.12 python3.11 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 &&
           "$candidate" -c "import lxml" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
fi

if [ -z "$PY" ]; then
    cat <<'EOF'

ERROR: no Python with the v2 requirements was found.

  1. Install Python 3.12.
  2. python3.12 -m venv .venv-qwen
  3. .venv-qwen/bin/pip install torch
     .venv-qwen/bin/pip install -r requirements-qwen.txt

FFmpeg and ffprobe must also be on PATH.
See docs/QWEN3_PIPELINE.md for details.

EOF
    exit 1
fi

"$PY" -m qwen_pipeline "$@"
status=$?

echo
if [ "$status" -eq 0 ]; then
    echo 'Finished. The read-aloud EPUB is in the "output" folder.'
else
    echo "Something went wrong. Read the messages above."
    if [ -d _qwen_work ]; then
        echo "The _qwen_work folder was kept so the generated files can be inspected."
    fi
fi

exit "$status"
