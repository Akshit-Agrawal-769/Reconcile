# Reconcile

GST input-tax-credit reconciliation for CA firms. A firm uploads each client's purchase
register and GSTR-2B; Reconcile matches them, lists the exceptions, and walks the month
through staff review, partner approval and client sign-off. Every business rule (tenant
isolation, maker-checker, closed months) is enforced inside PostgreSQL; the API and the
web app only ask the database and show its answer.

| Client board | Period dashboard |
|---|---|
| ![Client board](docs/screenshots/board.png) | ![Period dashboard](docs/screenshots/period.png) |
| **Review drawer** | **Client view** |
| ![Review drawer](docs/screenshots/drawer.png) | ![Client view](docs/screenshots/client.png) |

## How it fits together

```
browser (React) ──► API (Starlette) ──► PostgreSQL: RLS, procedures, views
                         │ upload                     ▲
                         ▼                            │ runs each job as the user
                    job_queue ◄── worker (claims with FOR UPDATE SKIP LOCKED)
```

- `db/`: migrations 001–014, the whole data model and every rule. See `docs/er-diagram.md`.
- `api/`: the HTTP API. Each request runs as the logged-in user's database role.
- `worker/`: the background pipeline: import 2B or books → match → refresh reviews → supplier scores.
- `web/`: the React app, bundled by esbuild and served by the API.
- `tools/synth.py`: synthetic books and 2B files with known answers, for testing and demos.

## Run the app

You need Python 3.12+, Node 20+, and Docker (or a local PostgreSQL 16).

```bash
docker compose up -d db
pip install -r requirements.txt
cd web && npm install && npm run build && npm run typecheck && cd ..
bash scripts/dev.sh            # add --reset to rebuild the database
```

On Windows, `scripts/dev.sh` runs in Git Bash. In PowerShell the same steps are:

```powershell
$env:DATABASE_URL = "postgresql://reconcile:reconcile@localhost:5432/reconcile"
$env:REC_SECRET = "any-long-random-string"
python scripts/seed_e2e.py --url $env:DATABASE_URL --out data/demo
cd web; node build.mjs; cd ..
Start-Process python "-m worker.run --sleep 1"
python -m uvicorn --factory api.app:app_from_env --port 8000
```

Open http://localhost:8000. Every demo login uses the password `demo123`:

| Login | Who |
|---|---|
| `staff.a@demo.test` | Staff assigned to Acme, Bharat and Delta |
| `staff.b@demo.test` | Staff assigned to Cyan only (cannot see the others) |
| `partner@demo.test` | Partner: approves reviews, closes and reopens months |
| `delta@demo.test` | The owner of Delta Distributors: sees four numbers and approves |

Try a month: log in as staff A, open **Delta Distributors**, upload
`data/demo/books_*.csv` then `data/demo/gstr2b_*.json`, and watch the queue fill. For
bigger files: `python tools/synth.py --clients 1 --docs 2000 --out data/synth/`.

## Tests

```bash
pip install -r requirements-dev.txt
pytest                         # database, API and worker
bash scripts/verify_all.sh     # everything, including the browser tests
```

`verify_all.sh` also runs the web unit tests and the Playwright end-to-end flows in
`web/e2e/` (`npx playwright install chromium` once). Tests need Postgres at
`TEST_DATABASE_URL` (default `postgresql://reconcile:reconcile@localhost:5432/reconcile_test`).

## Configuration

| Variable | Used by | Meaning |
|---|---|---|
| `DATABASE_URL` | API, worker, scripts | Database to use. In production, log in as `rc_api` (`ALTER ROLE rc_api PASSWORD '…'`). |
| `REC_SECRET` | API | Signs session tokens (8 hours). Keep it secret; changing it logs everyone out. |
| `REC_STATIC_DIR` | API | Built web app to serve (default `web/dist`). |
| `TEST_DATABASE_URL` | tests | A database the tests may drop and recreate. |
