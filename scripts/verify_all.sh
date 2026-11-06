#!/usr/bin/env bash
# Run every test: database, API and worker (pytest), web unit tests, and the browser
# end-to-end flows. Exits non-zero on the first failure.
#   PYTHON=python3 bash scripts/verify_all.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}

echo "== database, API and worker tests"
"$PY" -m pytest -q

echo "== web build"
node web/build.mjs

echo "== web unit tests"
node --test web/src/*.test.mjs

echo "== browser end-to-end tests"
for f in web/e2e/*.e2e.mjs; do
  echo "-- $f"
  node --test "$f"
done
echo "all green"
