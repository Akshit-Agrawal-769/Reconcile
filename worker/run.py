"""Background worker: takes jobs from job_queue and runs the pipeline.

    IMPORT_2B     -> sp_import_gstr2b   -> enqueue RECONCILE
    IMPORT_BOOKS  -> sp_import_books    -> enqueue RECONCILE (if the period has a 2B import)
    RECONCILE     -> sp_reconcile, sp_refresh_reviews -> enqueue REFRESH_SCORE
    REFRESH_SCORE -> sp_refresh_party_score

Each job runs as the user who enqueued it (SET LOCAL ROLE + app.user_id), so every
tenant, closed-month and approval rule applies exactly as for a direct call. The work,
the next job and the DONE mark commit together; on any error all of it rolls back and
sp_fail_job schedules a retry (or marks the job FAILED after its last attempt).

Run:  python -m worker.run --sleep 1      (loop)
      python -m worker.run --once         (drain the queue, then exit)
"""
import argparse
import json
import logging
import os
import sys
import time

import psycopg

from api.config import CurrentUser
from api.db import ApiError, tx
from api.sources import FileUploadSource

log = logging.getLogger("reconcile.worker")

CLAIM = "SELECT * FROM sp_claim_job()"
RUNNER = "SELECT user_id, role::text, client_id, firm_id FROM fn_job_runner(%s)"
FINISH = "SELECT sp_finish_job(%s, %s::jsonb, %s)"
FAIL = "SELECT sp_fail_job(%s, %s, %s)"
ENQUEUE = "SELECT sp_enqueue_job(%s, %s, NULL, %s)"
IMPORT_2B = "SELECT sp_import_gstr2b(%s, %s::jsonb, %s, %s)"
IMPORT_BOOKS = "SELECT sp_import_books(%s, %s)"
CURRENT_IMPORT = ("SELECT import_id FROM gstr2b_import WHERE period_id = %s AND is_current "
                  "ORDER BY version DESC LIMIT 1")
RECONCILE = "SELECT sp_reconcile(%s, %s)"
REFRESH_REVIEWS = "SELECT sp_refresh_reviews(%s)"
REFRESH_SCORE = "SELECT sp_refresh_party_score()"


def _one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def _enqueue(conn, job_type, period_id, import_id=None):
    return _one(conn, ENQUEUE, (job_type, period_id, import_id))


def _do_import_2b(conn, job, source):
    payload = source.fetch_2b(job["period_id"], job["upload_id"])
    src = (job["params"] or {}).get("source") or "DRAFT_2B"
    imp = _one(conn, IMPORT_2B, (job["period_id"], json.dumps(payload), src, job["created_by"]))
    nxt = _enqueue(conn, "RECONCILE", job["period_id"], imp)
    return {"import_id": imp, "next_job_id": nxt}


def _do_import_books(conn, job, source):
    n = _one(conn, IMPORT_BOOKS, (job["period_id"], job["created_by"]))
    row = conn.execute(CURRENT_IMPORT, (job["period_id"],)).fetchone()
    if row is None:
        return {"invoices": n, "reconcile": "waiting for 2B"}
    nxt = _enqueue(conn, "RECONCILE", job["period_id"], row[0])
    return {"invoices": n, "import_id": row[0], "next_job_id": nxt}


def _do_reconcile(conn, job, source):
    if job["import_id"] is None:
        raise ApiError(400, "reconcile job has no import")
    rows = _one(conn, RECONCILE, (job["import_id"], job["created_by"]))
    reviews = _one(conn, REFRESH_REVIEWS, (job["import_id"],))
    nxt = _enqueue(conn, "REFRESH_SCORE", job["period_id"])
    return {"rows": rows, "reviews_refreshed": reviews, "next_job_id": nxt}


def _do_refresh_score(conn, job, source):
    conn.execute(REFRESH_SCORE)
    return {"refreshed": True}


HANDLERS = {"IMPORT_2B": _do_import_2b, "IMPORT_BOOKS": _do_import_books,
            "RECONCILE": _do_reconcile, "REFRESH_SCORE": _do_refresh_score}


def _error_text(exc):
    """The message staff will see on the job. Prefer the database's own wording."""
    cause = exc.__cause__ if isinstance(exc, ApiError) and exc.__cause__ else exc
    diag = getattr(cause, "diag", None)
    msg = getattr(diag, "message_primary", None) if diag is not None else None
    return (msg or getattr(exc, "message", None) or str(exc) or type(exc).__name__)[:2000]


def run_once(conn, source_factory=FileUploadSource) -> bool:
    """Claim and run one job. Returns False when no job was ready."""
    conn.rollback()
    cur = conn.execute(CLAIM)
    row = cur.fetchone()
    if row is None:
        conn.rollback()
        return False
    job = dict(zip([d[0] for d in cur.description], row))
    conn.commit()  # the lease is now visible to other workers

    started = time.monotonic()
    try:
        r = conn.execute(RUNNER, (job["job_id"],)).fetchone()
        if r is None:
            raise ApiError(409, "job is no longer running")
        user = CurrentUser(user_id=r[0], role=r[1], client_id=r[2], firm_id=r[3])
        handler = HANDLERS[job["job_type"]]
        with tx(conn, user) as c:
            result = handler(c, job, source_factory(c))
        conn.execute(FINISH, (job["job_id"], json.dumps(result), job["attempts"]))
        conn.commit()
        status = "DONE"
    except Exception as exc:  # noqa: BLE001 — every failure is recorded on the job
        conn.rollback()
        msg = _error_text(exc)
        try:
            conn.execute(FAIL, (job["job_id"], msg, job["attempts"]))
            conn.commit()
            status = f"FAILED({msg})"
        except psycopg.errors.RaiseException as lost:
            # The lease ran out and another worker owns this job now: leave it to them.
            conn.rollback()
            status = f"ABANDONED({_error_text(lost)}; first error: {msg})"
    ms = int((time.monotonic() - started) * 1000)
    log.info("job %s %s %s %dms", job["job_id"], job["job_type"], status, ms)
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m worker.run", description=__doc__.split("\n")[0])
    p.add_argument("--once", action="store_true", help="drain the queue, then exit")
    p.add_argument("--sleep", type=float, default=1.0, help="seconds to wait when idle")
    args = p.parse_args(argv)
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("set DATABASE_URL", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    conn = None
    backoff = 1.0
    try:
        while True:
            try:
                if conn is None:
                    conn = psycopg.connect(url, autocommit=False)
                busy = run_once(conn)
                backoff = 1.0
            except psycopg.errors.OperationalError as exc:
                # Database restarted or the network dropped: reconnect, never exit.
                log.warning("database connection lost (%s); reconnecting in %.0fs", exc, backoff)
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:  # noqa: BLE001
                        pass
                conn = None
                time.sleep(min(backoff, 30) if not args.once else 0)
                backoff *= 2
                continue
            if busy:
                continue
            if args.once:
                return 0
            time.sleep(args.sleep)
    except KeyboardInterrupt:
        return 0
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
