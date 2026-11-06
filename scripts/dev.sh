#!/usr/bin/env bash
# Run Reconcile locally: migrate, seed demo data, build the web app, then start the API
# (http://localhost:8000) and the background worker. Ctrl-C stops both.
#
#   bash scripts/dev.sh            # keep the existing database
#   bash scripts/dev.sh --reset    # drop and rebuild the database first
#
# Needs Postgres (docker compose up -d db), `pip install -r requirements.txt`, and
# `npm install` in web/. Reads .env if present.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
PY=${PYTHON:-python}
: "${DATABASE_URL:=postgresql://reconcile:reconcile@localhost:5432/reconcile}"
: "${REC_SECRET:=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')}"
export DATABASE_URL REC_SECRET REC_STATIC_DIR="${REC_STATIC_DIR:-web/dist}"

FRESH=()
[ "${1:-}" = "--reset" ] && FRESH=(--fresh)
"$PY" scripts/seed_e2e.py --url "$DATABASE_URL" --out data/demo "${FRESH[@]}" >/dev/null
echo "demo files to upload: data/demo/books_*.csv and data/demo/gstr2b_*.json"

(cd web && node build.mjs)

"$PY" -m uvicorn --factory api.app:app_from_env --port 8000 &
API=$!
"$PY" -m worker.run --sleep 1 &
WORKER=$!
trap 'kill $API $WORKER 2>/dev/null; wait' INT TERM EXIT
echo "Reconcile is running at http://localhost:8000 (log in as staff.a@demo.test / demo123)"
wait
