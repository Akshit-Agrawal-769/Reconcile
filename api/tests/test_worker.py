"""The background worker. These tests commit (the worker does), so they use the separate
`committed` database, which is emptied before each test."""
import hashlib
import json

import psycopg
import pytest

from api.sources import FileUploadSource, GspSandboxSource
from db.tests.conftest import as_role
from db.tests.factories import (assign, build_2b_payload, make_client, make_invoice,
                                make_party, make_period, make_reg, make_user)
from worker import run as worker

BOOK_COLS = ("gstin_of_supplier, supplier_name, document_type, invoice_number, invoice_date, "
             "place_of_supply, reverse_charge, hsn_sac, rate, taxable_value, "
             "integrated_tax_paid, central_tax_paid, state_ut_tax_paid, cess_paid, "
             "eligible_for_itc")


def as_user(conn, w, who, sql, params=()):
    role = "partner" if who == "partner" else "staff"
    with conn.transaction():
        as_role(conn, role, w[who])
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
        conn.execute("RESET ROLE")
    return rows


@pytest.fixture
def db(committed):
    """(setup connection, worker connection, world) on the committed database."""
    setup = psycopg.connect(committed, autocommit=False)
    wconn = psycopg.connect(committed, autocommit=False)
    c1 = make_client(setup, pan="AAACA1111A", name="One")
    w = dict(c1=c1, g1=make_reg(setup, c1), partner=make_user(setup, "partner"),
             staff_a=make_user(setup, "staff"), staff_b=make_user(setup, "staff"))
    assign(setup, w["staff_a"], c1)
    w["sup"] = make_party(setup, "ABCDE1234F")
    w["pid"] = make_period(setup, w["g1"], "092026")
    setup.commit()
    yield setup, wconn, w
    for c in (setup, wconn):
        c.rollback()
        c.close()


def upload_2b(conn, w, docs, rtnprd="092026", source="DRAFT_2B"):
    """As staff A: store a 2B upload and enqueue IMPORT_2B, then commit."""
    content = json.dumps(build_2b_payload(w["g1"], rtnprd, docs))
    sha = hashlib.sha256(content.encode()).hexdigest()
    up = as_user(conn, w, "staff_a",
                 "INSERT INTO upload(period_id, kind, filename, content, sha256, uploaded_by) "
                 "VALUES (%s, 'GSTR2B', '2b.json', %s, %s, app_user_id()) RETURNING upload_id",
                 (w["pid"], content, sha))[0][0]
    jid = as_user(conn, w, "staff_a", "SELECT sp_enqueue_job('IMPORT_2B', %s, %s, NULL, %s)",
                  (w["pid"], up, json.dumps({"source": source})))[0][0]
    conn.commit()
    return jid


def drain(wconn, limit=20):
    n = 0
    while worker.run_once(wconn):
        n += 1
        assert n < limit, "worker did not settle"
    return n


def jobs(conn):
    conn.rollback()
    rows = conn.execute("SELECT job_type, status, attempts, last_error, result FROM job_queue "
                        "ORDER BY job_id").fetchall()
    conn.rollback()
    return rows


def books_invoice(setup, w, no="INV-1"):
    make_invoice(setup, w["g1"], w["sup"], no, "2026-09-10",
                 [dict(taxable_value=100000, cgst=9000, sgst=9000)], pos="07")
    setup.commit()


DOC = dict(inum="INV-1", dt="10-09-2026", txval=100000, cgst=9000, sgst=9000)


def test_import_2b_chain_runs_to_done(db):
    setup, wconn, w = db
    books_invoice(setup, w)
    upload_2b(setup, w, [dict(DOC, ctin=w["sup"]),
                         dict(DOC, ctin=w["sup"], inum="INV-9", txval=5000, cgst=450, sgst=450)])
    assert drain(wconn) == 3
    rows = jobs(setup)
    assert [(t, s) for t, s, *_ in rows] == [
        ("IMPORT_2B", "DONE"), ("RECONCILE", "DONE"), ("REFRESH_SCORE", "DONE")]
    assert rows[0][4]["import_id"] and rows[1][4]["rows"] == 2
    statuses = sorted(r[0] for r in setup.execute(
        "SELECT status::text FROM recon_result").fetchall())
    assert statuses == ["EXACT", "ONLY_IN_2B"]
    q = as_user(setup, w, "staff_a", "SELECT count(*) FROM v_queue WHERE period_id = %s",
                (w["pid"],))[0][0]
    assert q == 2
    src = setup.execute("SELECT source, imported_by FROM gstr2b_import").fetchone()
    assert src == ("DRAFT_2B", w["staff_a"])


def test_upload_source_param_is_used(db):
    setup, wconn, w = db
    upload_2b(setup, w, [dict(DOC, ctin=w["sup"])], source="IMS")
    drain(wconn)
    assert setup.execute("SELECT source FROM gstr2b_import").fetchone()[0] == "IMS"


def stage_books(setup, w, rows):
    for r in rows:
        as_user(setup, w, "staff_a", f"INSERT INTO books_staging(period_id, {BOOK_COLS}) "
                f"VALUES (%s, {', '.join(['%s'] * 15)})", (w["pid"], *r))
    jid = as_user(setup, w, "staff_a", "SELECT sp_enqueue_job('IMPORT_BOOKS', %s)",
                  (w["pid"],))[0][0]
    setup.commit()
    return jid


ROW = ("07ABCDE1234F1Z2", "Alpha", "Invoice", "INV-1", "10-09-2026", "07", "N", "8471",
       "18", "100000", "0", "9000", "9000", "0", "Yes")


def test_books_before_2b_waits(db):
    setup, wconn, w = db
    stage_books(setup, w, [ROW])
    assert drain(wconn) == 1
    rows = jobs(setup)
    assert [(t, s) for t, s, *_ in rows] == [("IMPORT_BOOKS", "DONE")]
    assert rows[0][4] == {"invoices": 1, "reconcile": "waiting for 2B"}


def test_books_after_2b_reconciles_again(db):
    setup, wconn, w = db
    upload_2b(setup, w, [dict(DOC, ctin=w["sup"])])
    drain(wconn)
    stage_books(setup, w, [ROW])
    drain(wconn)
    types = [(t, s) for t, s, *_ in jobs(setup)]
    assert types[3:] == [("IMPORT_BOOKS", "DONE"), ("RECONCILE", "DONE"), ("REFRESH_SCORE", "DONE")]
    assert setup.execute("SELECT status::text FROM recon_result r JOIN gstr2b_import i "
                         "USING (import_id) WHERE i.is_current").fetchall() == [("EXACT",)]


def test_job_runs_as_enqueuer(db):
    """A job row naming a user who cannot see the client fails exactly as that user's
    direct call would: the worker borrows the enqueuer's role, never more."""
    setup, wconn, w = db
    upload_2b(setup, w, [dict(DOC, ctin=w["sup"])])
    drain(wconn)
    imp = setup.execute("SELECT import_id FROM gstr2b_import").fetchone()[0]
    setup.execute("INSERT INTO job_queue(job_type, period_id, import_id, created_by, max_attempts) "
                  "VALUES ('RECONCILE', %s, %s, %s, 2)", (w["pid"], imp, w["staff_b"]))
    setup.commit()
    assert worker.run_once(wconn)
    setup.execute("UPDATE job_queue SET run_after = now() WHERE status = 'QUEUED'")
    setup.commit()
    assert worker.run_once(wconn)
    t, status, attempts, err, _ = jobs(setup)[-1]
    assert (t, status, attempts, err) == ("RECONCILE", "FAILED", 2, "not your client")


def test_failure_retries_then_failed(db):
    setup, wconn, w = db
    upload_2b(setup, w, [dict(DOC, ctin=w["sup"])], rtnprd="082026")
    assert worker.run_once(wconn)
    t, status, attempts, err, _ = jobs(setup)[0]
    assert (status, attempts) == ("QUEUED", 1) and "rtnprd mismatch" in err
    assert not worker.run_once(wconn)  # backoff: not ready yet
    for _ in range(2):
        setup.execute("UPDATE job_queue SET run_after = now() WHERE status = 'QUEUED'")
        setup.commit()
        assert worker.run_once(wconn)
    t, status, attempts, err, _ = jobs(setup)[0]
    assert (status, attempts) == ("FAILED", 3) and "rtnprd mismatch" in err
    assert setup.execute("SELECT count(*) FROM gstr2b_import").fetchone()[0] == 0


def test_failed_job_leaves_no_partial_work(db, monkeypatch):
    """Work, the chained enqueue and the DONE mark share one transaction: if the step
    after the import fails, the import itself is rolled back too."""
    setup, wconn, w = db
    upload_2b(setup, w, [dict(DOC, ctin=w["sup"])])

    def broken_enqueue(*a, **k):
        raise RuntimeError("queue unavailable")
    monkeypatch.setattr(worker, "_enqueue", broken_enqueue)
    assert worker.run_once(wconn)
    assert setup.execute("SELECT count(*) FROM gstr2b_import").fetchone()[0] == 0
    t, status, attempts, err, _ = jobs(setup)[0]
    assert (status, attempts, err) == ("QUEUED", 1, "queue unavailable")


def test_worker_connection_is_clean_after_a_job(db):
    setup, wconn, w = db
    upload_2b(setup, w, [dict(DOC, ctin=w["sup"])])
    worker.run_once(wconn)
    assert wconn.execute("SELECT current_user = session_user, "
                         "current_setting('app.user_id', true)").fetchone() in (
        (True, ""), (True, None))


def test_main_once_drains_queue(db, monkeypatch, committed):
    setup, wconn, w = db
    books_invoice(setup, w)
    upload_2b(setup, w, [dict(DOC, ctin=w["sup"])])
    monkeypatch.setenv("DATABASE_URL", committed)
    assert worker.main(["--once"]) == 0
    assert {s for _, s, *_ in jobs(setup)} == {"DONE"}


def test_file_upload_source_reads_the_jobs_upload(db):
    setup, wconn, w = db
    upload_2b(setup, w, [dict(DOC, ctin=w["sup"])])
    up = setup.execute("SELECT upload_id FROM upload").fetchone()[0]
    payload = FileUploadSource(setup).fetch_2b(w["pid"], up)
    assert payload["data"]["rtnprd"] == "092026"


def test_gsp_source_not_implemented():
    with pytest.raises(NotImplementedError, match="licensed provider"):
        GspSandboxSource().fetch_2b(1)


# --- final review fixes -----------------------------------------------------------------

def test_worker_survives_losing_its_job(db, monkeypatch, committed):
    """Finding #3: if the job was reclaimed while this worker ran it, finishing and
    failing are both refused; the worker logs it and keeps going instead of crashing."""
    setup, wconn, w = db
    upload_2b(setup, w, [dict(DOC, ctin=w["sup"])])
    real = worker.HANDLERS["IMPORT_2B"]

    def stolen(conn, job, source):
        other = psycopg.connect(committed, autocommit=True)
        try:
            other.execute("UPDATE job_queue SET attempts = attempts + 1 WHERE job_id = %s",
                          (job["job_id"],))
        finally:
            other.close()
        return real(conn, job, source)
    monkeypatch.setitem(worker.HANDLERS, "IMPORT_2B", stolen)
    assert worker.run_once(wconn) is True          # no exception escapes
    t, status, attempts, err, _ = jobs(setup)[0]
    assert (status, attempts) == ("RUNNING", 2)    # left to the worker that holds it
    assert setup.execute("SELECT count(*) FROM gstr2b_import").fetchone()[0] == 0


def test_main_reconnects_after_lost_connection(monkeypatch, committed):
    monkeypatch.setenv("DATABASE_URL", committed)
    calls = {"connect": 0, "run": 0}
    real_connect = worker.psycopg.connect

    def connect(*a, **k):
        calls["connect"] += 1
        return real_connect(*a, **k)

    def run_once(conn, *a, **k):
        calls["run"] += 1
        if calls["run"] == 1:
            raise psycopg.errors.OperationalError("server closed the connection")
        return False
    monkeypatch.setattr(worker.psycopg, "connect", connect)
    monkeypatch.setattr(worker, "run_once", run_once)
    assert worker.main(["--once", "--sleep", "0"]) == 0
    assert calls == {"connect": 2, "run": 2}


def test_reexported_books_import_cleanly_end_to_end(db, committed):
    """Finding #1, the demo path: upload the month's books, then an updated export that
    repeats INV-1 and adds INV-2. Both imports finish; INV-1 is not imported twice."""
    from api.app import create_app
    from api.tests.asgi import Client
    from api.tests.conftest import set_login, settings_for
    from api.uploads import BOOKS_HEADER
    setup, wconn, w = db
    set_login(setup, w["staff_a"], "a@e2e.test")
    setup.commit()
    c = Client(create_app(settings_for(committed)))
    token = c.post("/api/login", json={"email": "a@e2e.test", "password": "demo123"}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    hdr = ",".join(BOOKS_HEADER)
    row = ",".join(ROW)
    for body in (hdr + "\n" + row + "\n", hdr + "\n" + row + "\n" + row.replace("INV-1", "INV-2") + "\n"):
        r = c.post(f"/api/periods/{w['pid']}/uploads/books", headers=h,
                   files={"file": ("books.csv", body, "text/csv")})
        assert r.status_code == 201, r
        drain(wconn)
    assert {s for _, s, *_ in jobs(setup)} == {"DONE"}
    setup.rollback()
    assert sorted(r[0] for r in setup.execute(
        "SELECT invoice_no FROM invoice WHERE gstin = %s", (w["g1"],)).fetchall()) == ["INV-1", "INV-2"]
