"""Migration 014: upload store, job queue procedures, login, UI views."""
import hashlib
import json

import psycopg
import pytest

from db.tests.conftest import as_role
from db.tests.factories import (assign, build_2b_payload, import_2b, make_client,
                                make_invoice, make_party, make_period, make_reg,
                                make_user, reconcile)

SUP_PAN = "AAAPA1111A"


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def world(conn):
    """Client 1 with staff A, client 2 with staff B, a partner, a client user, a period."""
    c1 = make_client(conn, pan="AAACA1111A", name="One")
    c2 = make_client(conn, pan="AAACB2222B", name="Two")
    w = dict(c1=c1, c2=c2, g1=make_reg(conn, c1), g2=make_reg(conn, c2),
             partner=make_user(conn, "partner"),
             staff_a=make_user(conn, "staff"), staff_b=make_user(conn, "staff"),
             client1=make_user(conn, "client", c1))
    assign(conn, w["staff_a"], c1)
    assign(conn, w["staff_b"], c2)
    w["sup"] = make_party(conn, SUP_PAN)
    w["pid"] = make_period(conn, w["g1"], "092026")
    return w


ROLE = {"partner": "partner", "staff_a": "staff", "staff_b": "staff", "client1": "client"}


def run(conn, w, who, sql, params=()):
    with conn.transaction():
        as_role(conn, ROLE[who], w[who])
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
        conn.execute("RESET ROLE")
    return rows


def refused(conn, w, who, sql, params=(), exc=psycopg.errors.RaiseException, match=None):
    with pytest.raises(exc, match=match):
        with conn.transaction():
            as_role(conn, ROLE[who], w[who])
            conn.execute(sql, params)


def force_closed(conn, pid):
    """Test shortcut: skip the workflow triggers and mark the period CLOSED."""
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("UPDATE return_period SET state = 'CLOSED' WHERE period_id = %s", (pid,))
        conn.execute("SET LOCAL session_replication_role = origin")


def job(conn, job_id):
    return conn.execute(
        "SELECT job_type, status, attempts, created_by, run_after > now(), last_error, "
        "lease_until > now() FROM job_queue WHERE job_id = %s", (job_id,)).fetchone()


# --- enqueue -------------------------------------------------------------------------

def test_enqueue_as_assigned_staff(conn):
    w = world(conn)
    jid = run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
    jt, status, attempts, created_by, *_ = job(conn, jid)
    assert (jt, status, attempts, created_by) == ("RECONCILE", "QUEUED", 0, w["staff_a"])


def test_enqueue_other_client_refused(conn):
    w = world(conn)
    refused(conn, w, "staff_b", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],),
            match="not your client")


def test_enqueue_closed_period_refused(conn):
    w = world(conn)
    force_closed(conn, w["pid"])
    refused(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],),
            match="period is closed")


def test_enqueue_foreign_import_refused(conn):
    """An import id from another client's period cannot ride on your period's job."""
    w = world(conn)
    pid2 = make_period(conn, w["g2"], "092026")
    imp2 = import_2b(conn, pid2, build_2b_payload(w["g2"], "092026", []), w["partner"])
    refused(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s, NULL, %s)",
            (w["pid"], imp2), match="import does not belong to this period")


def test_client_cannot_enqueue_or_read_jobs(conn):
    w = world(conn)
    refused(conn, w, "client1", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],),
            exc=psycopg.errors.InsufficientPrivilege)
    refused(conn, w, "client1", "SELECT * FROM job_queue",
            exc=psycopg.errors.InsufficientPrivilege)


def test_staff_cannot_write_job_queue_directly(conn):
    w = world(conn)
    refused(conn, w, "staff_a",
            "INSERT INTO job_queue(job_type, period_id, created_by) VALUES ('RECONCILE', %s, %s)",
            (w["pid"], w["staff_a"]), exc=psycopg.errors.InsufficientPrivilege)
    refused(conn, w, "staff_a", "SELECT * FROM sp_claim_job()",
            exc=psycopg.errors.InsufficientPrivilege)


def test_jobs_visible_only_to_own_tenant(conn):
    w = world(conn)
    run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))
    assert run(conn, w, "staff_a", "SELECT count(*) FROM job_queue")[0][0] == 1
    assert run(conn, w, "staff_b", "SELECT count(*) FROM job_queue")[0][0] == 0


# --- claim / finish / fail ---------------------------------------------------------

def test_claim_order_and_lease(conn):
    w = world(conn)
    first = run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
    run(conn, w, "staff_a", "SELECT sp_enqueue_job('REFRESH_SCORE', %s)", (w["pid"],))
    row = conn.execute("SELECT job_id, status, attempts, lease_until > now() "
                       "FROM sp_claim_job()").fetchone()
    assert row == (first, "RUNNING", 1, True)


def test_claim_skips_future_run_after(conn):
    w = world(conn)
    jid = run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
    conn.execute("UPDATE job_queue SET run_after = now() + interval '1 hour' WHERE job_id = %s",
                 (jid,))
    assert conn.execute("SELECT * FROM sp_claim_job()").fetchall() == []


def test_expired_lease_is_reclaimed(conn):
    w = world(conn)
    jid = run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
    conn.execute("UPDATE job_queue SET status = 'RUNNING', attempts = 1, "
                 "lease_until = now() - interval '1 minute' WHERE job_id = %s", (jid,))
    row = conn.execute("SELECT job_id, status, attempts FROM sp_claim_job()").fetchone()
    assert row == (jid, "RUNNING", 2)


def test_running_job_with_live_lease_is_not_reclaimed(conn):
    w = world(conn)
    jid = run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
    conn.execute("SELECT * FROM sp_claim_job()")
    assert conn.execute("SELECT * FROM sp_claim_job()").fetchall() == []
    assert job(conn, jid)[2] == 1


def test_expired_lease_on_last_attempt_fails(conn):
    """A worker that died on the final attempt does not earn the job a fourth run."""
    w = world(conn)
    jid = run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
    conn.execute("UPDATE job_queue SET status = 'RUNNING', attempts = 3, "
                 "lease_until = now() - interval '1 minute' WHERE job_id = %s", (jid,))
    assert conn.execute("SELECT * FROM sp_claim_job()").fetchall() == []
    jt, status, attempts, _, _, err, _ = job(conn, jid)
    assert (status, attempts) == ("FAILED", 3)
    assert "worker stopped" in err


def test_finish_job(conn):
    w = world(conn)
    jid = run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
    conn.execute("SELECT * FROM sp_claim_job()")
    conn.execute("SELECT sp_finish_job(%s, %s::jsonb)", (jid, json.dumps({"rows": 3})))
    row = conn.execute("SELECT status, result, lease_until, finished_at IS NOT NULL "
                       "FROM job_queue WHERE job_id = %s", (jid,)).fetchone()
    assert row == ("DONE", {"rows": 3}, None, True)


def test_fail_then_retry_then_failed(conn):
    w = world(conn)
    jid = run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
    conn.execute("SELECT * FROM sp_claim_job()")
    conn.execute("SELECT sp_fail_job(%s, 'boom 1')", (jid,))
    _, status, attempts, _, later, err, _ = job(conn, jid)
    assert (status, attempts, later, err) == ("QUEUED", 1, True, "boom 1")
    conn.execute("UPDATE job_queue SET status = 'RUNNING', attempts = 3, "
                 "lease_until = now() + interval '1 minute' WHERE job_id = %s", (jid,))
    conn.execute("SELECT sp_fail_job(%s, 'boom 3')", (jid,))
    _, status, attempts, _, _, err, _ = job(conn, jid)
    assert (status, attempts, err) == ("FAILED", 3, "boom 3")


def test_skip_locked_two_workers(committed):
    """Two workers polling at once never take the same job (Review Focus 2)."""
    setup = psycopg.connect(committed, autocommit=False)
    a = psycopg.connect(committed, autocommit=False)
    b = psycopg.connect(committed, autocommit=False)
    try:
        w = world(setup)
        j1 = run(setup, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
        j2 = run(setup, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
        setup.commit()
        got_a = a.execute("SELECT job_id FROM sp_claim_job()").fetchone()[0]  # held open
        got_b = b.execute("SELECT job_id FROM sp_claim_job()").fetchone()[0]
        assert {got_a, got_b} == {j1, j2}
        assert b.execute("SELECT job_id FROM sp_claim_job()").fetchall() == []
    finally:
        for c in (a, b, setup):
            c.rollback()
            c.close()


# --- uploads --------------------------------------------------------------------------

def insert_upload(conn, w, who="staff_a", content="{}", kind="GSTR2B"):
    sha = hashlib.sha256(content.encode()).hexdigest()
    return run(conn, w, who,
               "INSERT INTO upload(period_id, kind, filename, content, sha256, uploaded_by) "
               "VALUES (%s, %s, 'f.json', %s, %s, app_user_id()) RETURNING upload_id",
               (w["pid"], kind, content, sha))[0][0]


def test_upload_insert_and_duplicate_sha_refused(conn):
    w = world(conn)
    uid = insert_upload(conn, w)
    assert scalar(conn, "SELECT uploaded_by FROM upload WHERE upload_id = %s", (uid,)) == w["staff_a"]
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_upload(conn, w)
    insert_upload(conn, w, kind="BOOKS")  # same bytes, other kind: allowed


def test_upload_other_tenant_refused_and_invisible(conn):
    w = world(conn)
    insert_upload(conn, w)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        insert_upload(conn, w, who="staff_b", content="other")
    assert run(conn, w, "staff_b", "SELECT count(*) FROM upload")[0][0] == 0


def test_upload_cannot_forge_uploader(conn):
    w = world(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        run(conn, w, "staff_a",
            "INSERT INTO upload(period_id, kind, filename, content, sha256, uploaded_by) "
            "VALUES (%s, 'GSTR2B', 'f', 'x', repeat('a', 64), %s)", (w["pid"], w["partner"]))


def test_enqueue_with_foreign_upload_refused(conn):
    w = world(conn)
    pid2 = make_period(conn, w["g2"], "092026")
    w2 = dict(w, pid=pid2)
    up2 = insert_upload(conn, w2, who="staff_b")
    refused(conn, w, "staff_a", "SELECT sp_enqueue_job('IMPORT_2B', %s, %s)",
            (w["pid"], up2), match="upload does not belong to this period")


# --- login ----------------------------------------------------------------------------

def test_fn_login(conn):
    w = world(conn)
    conn.execute("UPDATE app_user SET email = 'a@x.test', password_hash = crypt('demo123', "
                 "gen_salt('bf')) WHERE user_id = %s", (w["staff_a"],))
    assert conn.execute("SELECT user_id, role::text FROM fn_login('A@x.test', 'demo123')"
                        ).fetchall() == [(w["staff_a"], "staff")]
    assert conn.execute("SELECT * FROM fn_login('a@x.test', 'nope')").fetchall() == []
    refused(conn, w, "staff_a", "SELECT password_hash FROM app_user",
            exc=psycopg.errors.InsufficientPrivilege)
    refused(conn, w, "staff_a", "SELECT * FROM fn_login('a@x.test', 'demo123')",
            exc=psycopg.errors.InsufficientPrivilege)


def test_rc_api_is_login_role_with_pipeline_grants(conn):
    assert scalar(conn, "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'rc_api'") is True
    for fn in ("sp_claim_job(interval)", "sp_finish_job(bigint, jsonb, int)",
               "sp_fail_job(bigint, text, int)", "fn_login(text, text)"):
        assert scalar(conn, "SELECT has_function_privilege('rc_api', %s, 'EXECUTE')", (fn,)), fn
        assert not scalar(conn, "SELECT has_function_privilege('rc_staff', %s, 'EXECUTE')", (fn,)), fn


# --- views ----------------------------------------------------------------------------

def test_client_periods_view(conn):
    w = world(conn)
    rows = run(conn, w, "staff_a",
               "SELECT client_id, client_name, gstin, period_id, period, state::text, fin_year "
               "FROM v_client_periods")
    assert rows == [(w["c1"], "One", w["g1"], w["pid"], "092026", "OPEN", "2026-27")]
    assert run(conn, w, "staff_b", "SELECT count(*) FROM v_client_periods")[0][0] == 0


def test_result_detail_view(conn):
    w = world(conn)
    make_invoice(conn, w["g1"], w["sup"], "INV-1", "2026-09-10",
                 [dict(taxable_value=100000, cgst=9000, sgst=9000)], pos="07")
    imp = import_2b(conn, w["pid"], build_2b_payload(w["g1"], "092026", [
        dict(ctin=w["sup"], inum="INV-1", dt="10-09-2026", txval=100000, cgst=9000, sgst=9000)]),
        w["partner"])
    reconcile(conn, imp, w["partner"])
    rows = run(conn, w, "staff_a",
               "SELECT status::text, matched_by, diff_tax, books_invoice_no, books_date::text, "
               "books_taxable, books_tax, portal_invoice_no, portal_date::text, portal_taxable, "
               "portal_tax, party_gstin, party_name, period_id FROM v_result_detail "
               "WHERE import_id = %s", (imp,))
    assert len(rows) == 1
    st, mb, diff, bno, bdt, btx, btax, pno, pdt, ptx, ptax, pg, pname, pid = rows[0]
    assert (st, mb, bno, bdt, pno, pdt, pg, pid) == (
        "EXACT", "EXACT", "INV-1", "2026-09-10", "INV-1", "2026-09-10", w["sup"], w["pid"])
    assert (float(btx), float(btax), float(ptx), float(ptax), float(diff)) == (
        100000, 18000, 100000, 18000, 0)
    assert pname
    assert run(conn, w, "staff_b", "SELECT count(*) FROM v_result_detail")[0][0] == 0


def test_job_runner_names_the_enqueuer(conn):
    """The worker (rc_api, no table grants) learns whose role to run a claimed job as."""
    w = world(conn)
    jid = run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
    assert conn.execute("SELECT * FROM fn_job_runner(%s)", (jid,)).fetchall() == []  # not claimed
    conn.execute("SELECT * FROM sp_claim_job()")
    assert conn.execute("SELECT user_id, role::text, client_id FROM fn_job_runner(%s)",
                        (jid,)).fetchall() == [(w["staff_a"], "staff", None)]
    assert scalar(conn, "SELECT has_function_privilege('rc_api', 'fn_job_runner(bigint)', 'EXECUTE')")
    refused(conn, w, "staff_a", "SELECT * FROM fn_job_runner(%s)", (jid,),
            exc=psycopg.errors.InsufficientPrivilege)


def test_stale_attempt_cannot_finish_or_fail(conn):
    """Finding #3: a worker whose lease expired (the job was reclaimed, attempt 2 is
    running) must not mark the job DONE or FAILED under the new worker's feet."""
    w = world(conn)
    jid = run(conn, w, "staff_a", "SELECT sp_enqueue_job('RECONCILE', %s)", (w["pid"],))[0][0]
    conn.execute("SELECT * FROM sp_claim_job()")                       # attempt 1
    conn.execute("UPDATE job_queue SET lease_until = now() - interval '1 second' WHERE job_id = %s", (jid,))
    conn.execute("SELECT * FROM sp_claim_job()")                       # attempt 2
    with pytest.raises(psycopg.errors.RaiseException, match="no longer running"):
        with conn.transaction():
            conn.execute("SELECT sp_finish_job(%s, '{}'::jsonb, 1)", (jid,))
    with pytest.raises(psycopg.errors.RaiseException, match="no longer running"):
        with conn.transaction():
            conn.execute("SELECT sp_fail_job(%s, 'late', 1)", (jid,))
    conn.execute("SELECT sp_finish_job(%s, '{}'::jsonb, 2)", (jid,))
    assert job(conn, jid)[1] == "DONE"
