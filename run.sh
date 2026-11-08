#!/usr/bin/env bash
#
# One launcher for both pipelines.
#
#   ./run.sh                    web UI on http://127.0.0.1:9459
#   ./run.sh --ui [options]     same, explicitly
#   ./run.sh --v1 [options]     classic aeneas + syncabook (CLI)
#   ./run.sh --v2 [options]     Qwen3-ForcedAligner (CLI)
#
# run.py picks the interpreter that actually has the requested engine, so the
# only job here is to find some Python 3.9+ and hand over.

set -u
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8

PY=""
for candidate in ".venv-qwen/bin/python" python3.12 python3.11 python3.10 \
                 python3.9 python3 python; do
    if [ -x "$candidate" ]; then
        PY="$candidate"
        break
    fi
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c "import sys" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    cat <<'EOF'

ERROR: no Python was found.

  1. Install Python 3.9 (classic pipeline) and/or Python 3.12 (Qwen3).
  2. Run:  python3 -m pip install -r requirements.txt
  3. Install FFmpeg and eSpeak NG (see README.md).

EOF
    exit 1
fi

exec "$PY" run.py "$@"
