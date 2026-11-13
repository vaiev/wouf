#!/bin/sh
# Fast check of an agent branch before review (team flow's pre-gate): the helpers compile and the test suite
# passes. Container tests skip themselves without an engine. WOUF_BASE and WOUF_HEAD name the task's commits.
set -eu
export PYTHONDONTWRITEBYTECODE=1
cd "$(dirname "$0")/.."
python3 - .agents/bin/* <<'PY'
import ast, os, sys
for path in sys.argv[1:]:
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            ast.parse(fh.read(), path)
PY
if [ -n "${WOUF_BASE:-}" ] && [ -n "${WOUF_HEAD:-}" ]; then
    python3 scripts/complexity.py "$WOUF_BASE" "$WOUF_HEAD"
else
    echo "Complexity ratchet skipped: WOUF_BASE and WOUF_HEAD are not set (local run)."
fi
python3 scripts/mdlint.py
python3 scripts/test-parallel.py
