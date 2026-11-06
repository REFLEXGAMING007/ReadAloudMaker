#!/usr/bin/env bash
#
# Launcher for Linux and macOS. On Windows use run.bat instead.
#
#   ./run.sh [options]
#
# Options are passed straight to make_readaloud.py (see --help).

set -u
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8

PY=""
for candidate in python3.9 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c "import aeneas" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    cat <<'EOF'

ERROR: no Python with the required packages was found.

  1. Install Python 3.9 (aeneas 1.7.3 needs it).
  2. Run:  python3 -m pip install -r requirements.txt
  3. Install FFmpeg and eSpeak NG (see README.md).

EOF
    exit 1
fi

"$PY" make_readaloud.py "$@"
status=$?

echo
if [ "$status" -eq 0 ]; then
    echo 'Finished. The read-aloud EPUB is in the "output" folder.'
else
    echo "Something went wrong. Read the messages above."
    if [ -d _readaloud_work ]; then
        echo "The _readaloud_work folder was kept so the generated files can be inspected."
    fi
fi

exit "$status"
