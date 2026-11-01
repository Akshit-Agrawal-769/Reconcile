import psycopg
import pytest

from db.tests.conftest import as_role
from db.tests.factories import (assign, build_2b_payload, make_client, make_invoice,
                                make_party, make_period, make_reg, make_user)

SUP = "27"  # supplier in another state -> igst


def world(conn):
    """Firm with 3 clients; staff A on clients 1 & 2, staff B on client 3."""
    c1 = make_client(conn, pan="AAACA1111A", name="One")
    c2 = make_client(conn, pan="AAACB2222B", name="Two")
    c3 = make_client(conn, pan="AAACC3333C", name="Three")
    w = dict(c1=c1, c2=c2, c3=c3,
             g1=make_reg(conn, c1), g2=make_reg(conn, c2), g3=make_reg(conn, c3),
             partner=make_user(conn, "partner"),
             staff_a=make_user(conn, "staff"), staff_b=make_user(conn, "staff"),
             client1=make_user(conn, "client", c1))
    assign(conn, w["staff_a"], c1)
    assign(conn, w["staff_a"], c2)
    assign(conn, w["staff_b"], c3)
    w["sup"] = make_party(conn, "AAAPA1111A", SUP)
    return w


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def as_(conn, w, who):
    role = {"partner": "partner", "staff_a": "staff", "staff_b": "staff",
            "client1": "client"}[who]
    as_role(conn, role, w[who])


def reconciled(conn, w):
    """Client 1: a September period with one books invoice and one 2B import, reconciled
    (as superuser). Returns (period_id, import_id, result_id)."""
    pid = make_period(conn, w["g1"])
    make_invoice(conn, w["g1"], w["sup"], "INV-1", "2026-09-10",
                 [dict(taxable_value=10000, igst=30000)], pos="07")
    payload = build_2b_payload(w["g1"], "092026", [dict(
        ctin=w["sup"], inum="INV-1", dt="10-09-2026", txval=10000, igst=10000)])
    import json
    imp = scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
                 (pid, json.dumps(payload), w["staff_a"]))
    scalar(conn, "SELECT sp_reconcile(%s, %s)", (imp, w["staff_a"]))
    rid = scalar(conn, "SELECT result_id FROM recon_result WHERE import_id = %s", (imp,))
    return pid, imp, rid


# --- brief tests -----------------------------------------------------------------------

def test_staff_sees_assigned_only(conn):
    w = world(conn)
    counts = {}
    for who in ("staff_a", "staff_b", "partner", "client1"):
        with conn.transaction():
            as_(conn, w, who)
            counts[who] = scalar(conn, "SELECT count(*) FROM client")
            conn.execute("RESET ROLE")
    assert counts == {"staff_a": 2, "staff_b": 1, "partner": 3, "client1": 1}


def test_staff_b_no_rows_client1(conn):
    w = world(conn)
    make_invoice(conn, w["g1"], w["sup"], "INV-1", "2026-09-10",
                 [dict(taxable_value=10000, igst=1800)], pos="07")
    with conn.transaction():
        as_(conn, w, "staff_b")
        assert conn.execute("SELECT * FROM invoice WHERE gstin = %s",
                            (w["g1"],)).fetchall() == []
        assert scalar(conn, "SELECT count(*) FROM invoice_line") == 0
        conn.execute("RESET ROLE")
    with conn.transaction():
        as_(conn, w, "staff_a")
        assert scalar(conn, "SELECT count(*) FROM invoice WHERE gstin = %s", (w["g1"],)) == 1
        assert scalar(conn, "SELECT count(*) FROM invoice_line") == 1
        conn.execute("RESET ROLE")


def test_client_reads_own_summary_only(conn):
    w = world(conn)
    pid, _, _ = reconciled(conn, w)
    other = make_period(conn, w["g2"])
    with conn.transaction():
        as_(conn, w, "client1")
        assert conn.execute("SELECT DISTINCT period_id FROM v_queue").fetchall() == [(pid,)]
        assert scalar(conn, "SELECT count(*) FROM v_queue WHERE period_id = %s", (other,)) == 0
        assert scalar(conn, "SELECT count(*) FROM return_period") == 1
        conn.execute("RESET ROLE")


def test_client_cannot_write(conn):
    w = world(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            as_(conn, w, "client1")
            make_invoice(conn, w["g1"], w["sup"], "INV-1", "2026-09-10",
                         [dict(taxable_value=10000, igst=1800)], pos="07")
    for sql in ("UPDATE return_period SET state = 'CLOSED'",
                "DELETE FROM client",
                "INSERT INTO books_staging(period_id) VALUES (1)"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                as_(conn, w, "client1")
                conn.execute(sql)


def test_staff_cannot_set_state_column(conn):
    w = world(conn)
    _, _, rid = reconciled(conn, w)
    with conn.transaction():
        as_(conn, w, "staff_a")
        rev = scalar(conn, "SELECT sp_propose(%s, 'CHASE', 'PENDING', NULL, NULL)", (rid,))
        # decision columns are updatable
        conn.execute("UPDATE review SET remark = 'called' WHERE review_id = %s", (rev,))
        conn.execute("RESET ROLE")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            as_(conn, w, "staff_a")
            conn.execute("UPDATE review SET state = 'DONE' WHERE review_id = %s", (rev,))
    assert conn.execute("SELECT state::text, remark FROM review WHERE review_id = %s",
                        (rev,)).fetchone() == ("PROPOSED", "called")


def test_audit_append_only(conn):
    w = world(conn)
    reconciled(conn, w)
    assert scalar(conn, "SELECT count(*) FROM audit_log") > 0
    for who in ("staff_a", "partner"):
        for sql in ("UPDATE audit_log SET action = 'DELETE'", "DELETE FROM audit_log",
                    "TRUNCATE audit_log"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    as_(conn, w, who)
                    conn.execute(sql)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            as_(conn, w, "staff_a")
            conn.execute("SELECT count(*) FROM audit_log")
    with conn.transaction():
        as_(conn, w, "partner")
        assert scalar(conn, "SELECT count(*) FROM audit_log") > 0
        conn.execute("RESET ROLE")


# --- additional tests (controller ruling R11) -------------------------------------------

def test_staff_can_import_and_reconcile_own_client(conn):
    import json

    w = world(conn)
    with conn.transaction():
        as_(conn, w, "staff_a")
        pid = make_period(conn, w["g1"])
        assert pid is not None
        payload = build_2b_payload(w["g1"], "092026", [dict(
            ctin=w["sup"], inum="INV-1", dt="10-09-2026", txval=10000, igst=10000)])
        imp = scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
                     (pid, json.dumps(payload), w["staff_a"]))
        conn.execute(
            "INSERT INTO books_staging(period_id, gstin_of_supplier, supplier_name, "
            "document_type, invoice_number, invoice_date, place_of_supply, reverse_charge, "
            "hsn_sac, rate, taxable_value, integrated_tax_paid, central_tax_paid, "
            "state_ut_tax_paid, cess_paid, eligible_for_itc) VALUES "
            "(%s, %s, 'Sup', 'Invoice', 'INV-1', '10-09-2026', '07', 'N', '8471', '18', "
            "'10000', '30000', '0', '0', '0', 'Yes')", (pid, w["sup"]))
        assert scalar(conn, "SELECT sp_import_books(%s, %s)", (pid, w["staff_a"])) == 1
        assert scalar(conn, "SELECT sp_reconcile(%s, %s)", (imp, w["staff_a"])) == 1
        rid = scalar(conn, "SELECT result_id FROM recon_result WHERE import_id = %s", (imp,))
        rev = scalar(conn, "SELECT sp_propose(%s, 'CHASE', 'PENDING', NULL, NULL)", (rid,))
        assert rev is not None
        assert scalar(conn, "SELECT state::text FROM return_period WHERE period_id = %s",
                      (pid,)) == "IN_REVIEW"
        conn.execute("RESET ROLE")
    assert conn.execute("SELECT status::text FROM recon_result WHERE import_id = %s",
                        (imp,)).fetchall() == [("MISMATCH",)]


def test_staff_cannot_touch_other_client(conn):
    import json

    w = world(conn)
    pid, imp, rid = reconciled(conn, w)
    with pytest.raises(psycopg.errors.RaiseException, match="not your client"):
        with conn.transaction():
            as_(conn, w, "staff_b")
            conn.execute("SELECT sp_propose(%s, 'ACCEPT', 'ACCEPT', NULL, NULL)", (rid,))
    payload = build_2b_payload(w["g1"], "092026", [dict(
        ctin=w["sup"], inum="INV-2", dt="10-09-2026", txval=100, igst=18)])
    # 013 (F1): the definer wrapper refuses before the core can report 'unknown period'
    with pytest.raises(psycopg.errors.RaiseException, match="not your client"):
        with conn.transaction():
            as_(conn, w, "staff_b")
            conn.execute("SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
                         (pid, json.dumps(payload), w["staff_b"]))
    # the tenant guard (BEFORE trigger) fires before RLS WITH CHECK would
    with pytest.raises(psycopg.errors.RaiseException, match="not your client"):
        with conn.transaction():
            as_(conn, w, "staff_b")
            make_period(conn, w["g1"], "102026")
    with pytest.raises(psycopg.errors.RaiseException, match="not your client"):
        with conn.transaction():
            as_(conn, w, "staff_b")
            conn.execute("SELECT sp_submit_period(%s)", (pid,))
    assert scalar(conn, "SELECT count(*) FROM gstr2b_import WHERE period_id = %s", (pid,)) == 1
    assert scalar(conn, "SELECT count(*) FROM review") == 0


def test_role_none_is_not_partner_for_non_superuser(conn):
    with conn.transaction():
        conn.execute("SET LOCAL ROLE rc_staff")
        assert scalar(conn, "SELECT app_role_is('rc_partner')") is False
        assert scalar(conn, "SELECT app_role_is('rc_staff')") is True
        conn.execute("RESET ROLE")
    with conn.transaction():
        conn.execute("SET LOCAL ROLE rc_partner")
        assert scalar(conn, "SELECT app_role_is('rc_partner')") is True
        conn.execute("RESET ROLE")
    # rc_api is a (NOINHERIT) member of all three groups; before it SETs ROLE it must
    # count as none of them. SESSION AUTHORIZATION makes it the session user with
    # role = 'none', exactly as a fresh rc_api login.
    with conn.transaction():
        conn.execute("SET LOCAL SESSION AUTHORIZATION rc_api")
        assert scalar(conn, "SELECT current_setting('role')") == "none"
        for r in ("rc_partner", "rc_staff", "rc_client"):
            assert scalar(conn, "SELECT app_role_is(%s)", (r,)) is False
        conn.execute("RESET SESSION AUTHORIZATION")
    assert scalar(conn, "SELECT app_role_is('rc_partner')") is True  # superuser


def test_partner_sees_firm_users_staff_sees_self(conn):
    w = world(conn)
    with conn.transaction():
        as_(conn, w, "staff_a")
        assert conn.execute("SELECT user_id FROM app_user").fetchall() == [(w["staff_a"],)]
        assert scalar(conn, "SELECT count(*) FROM staff_assignment") == 2
        conn.execute("RESET ROLE")
    with conn.transaction():
        as_(conn, w, "partner")
        assert scalar(conn, "SELECT count(*) FROM app_user") == 4
        assert scalar(conn, "SELECT count(*) FROM staff_assignment") == 3
        conn.execute("RESET ROLE")


def test_no_app_user_sees_nothing(conn):
    w = world(conn)
    with conn.transaction():
        conn.execute("SET LOCAL ROLE rc_partner")
        assert scalar(conn, "SELECT count(*) FROM client") == 0
        assert scalar(conn, "SELECT current_app_user()") is None
        conn.execute("RESET ROLE")
    assert w  # world exists for the superuser


# --- fix round 1 ------------------------------------------------------------------------

def _import(conn, pid, g, docs, uid):
    import json

    return scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
                  (pid, json.dumps(build_2b_payload(g, "092026", docs)), uid))


def test_result_bound_to_its_import(conn):
    w = world(conn)
    # 013 (F1) revoked recon_result DML from app roles; grant it back inside this test's
    # transaction so the binding trigger (defence in depth) is still exercised as staff.
    conn.execute("GRANT INSERT, UPDATE ON recon_result TO rc_staff")
    _, imp1, _ = reconciled(conn, w)
    inv1 = conn.execute("SELECT invoice_id, fin_year FROM invoice WHERE gstin = %s",
                        (w["g1"],)).fetchone()
    ent1 = conn.execute("SELECT entry_id, fin_year FROM gstr2b_entry WHERE import_id = %s",
                        (imp1,)).fetchone()
    p3 = make_period(conn, w["g3"])
    d = dict(ctin=w["sup"], inum="B-1", dt="10-09-2026", txval=100, igst=18)
    imp3a = _import(conn, p3, w["g3"], [d], w["staff_b"])
    own_old = conn.execute("SELECT entry_id, fin_year FROM gstr2b_entry WHERE import_id = %s",
                           (imp3a,)).fetchone()
    imp3b = _import(conn, p3, w["g3"], [], w["staff_b"])
    cases = [
        ("invoice_id, invoice_fy", inv1, "ONLY_IN_BOOKS"),   # client 1's invoice
        ("entry_id, entry_fy", ent1, "ONLY_IN_2B"),          # client 1's entry
        ("entry_id, entry_fy", own_old, "ONLY_IN_2B"),       # own entry, other import
    ]
    for cols, ref, status in cases:
        with pytest.raises(psycopg.errors.RaiseException,
                           match="result does not belong to this import"):
            with conn.transaction():
                as_(conn, w, "staff_b")
                conn.execute(f"INSERT INTO recon_result(import_id, {cols}, status, matched_by) "
                             f"VALUES (%s, %s, %s, %s, 'x')", (imp3b, *ref, status))
    # an UPDATE that re-points an existing result is refused too
    rid = scalar(conn, "SELECT result_id FROM recon_result WHERE import_id = %s", (imp1,))
    with pytest.raises(psycopg.errors.RaiseException,
                       match="result does not belong to this import"):
        with conn.transaction():
            conn.execute("UPDATE recon_result SET entry_id = %s, entry_fy = %s "
                         "WHERE result_id = %s", (*own_old, rid))
    # an outward invoice of the right GSTIN is not an ITC candidate either
    out_id, out_fy = make_invoice(conn, w["g3"], w["sup"], "OUT-1", "2026-09-10",
                                  [dict(taxable_value=100, igst=18)], direction="OUTWARD",
                                  pos="27")
    with pytest.raises(psycopg.errors.RaiseException,
                       match="result does not belong to this import"):
        with conn.transaction():
            conn.execute("INSERT INTO recon_result(import_id, invoice_id, invoice_fy, status, "
                         "matched_by) VALUES (%s, %s, %s, 'ONLY_IN_BOOKS', 'x')",
                         (imp3b, out_id, out_fy))


def _awaiting(conn, w):
    pid = make_period(conn, w["g1"])
    conn.execute("UPDATE return_period SET state = 'IN_REVIEW' WHERE period_id = %s", (pid,))
    conn.execute("UPDATE return_period SET state = 'AWAITING_APPROVAL', "
                 "override_reason = 'client away' WHERE period_id = %s", (pid,))
    return pid


def test_partner_closes_and_reopens_only_via_procedures(conn):
    w = world(conn)
    # 013 (F1) revoked UPDATE on return_period; grant it back inside this test's
    # transaction so the transition trigger (defence in depth) is still exercised.
    conn.execute("GRANT UPDATE (state) ON return_period TO rc_partner")
    pid = _awaiting(conn, w)
    st = lambda: scalar(conn, "SELECT state::text FROM return_period WHERE period_id = %s",
                        (pid,))
    with pytest.raises(psycopg.errors.RaiseException,
                       match="use sp_close_period / sp_reopen_period"):
        with conn.transaction():
            as_(conn, w, "partner")
            conn.execute("UPDATE return_period SET state = 'CLOSED' WHERE period_id = %s",
                         (pid,))
    assert st() == "AWAITING_APPROVAL"
    with conn.transaction():
        as_(conn, w, "partner")
        conn.execute("SELECT sp_close_period(%s)", (pid,))
        conn.execute("RESET ROLE")
    assert st() == "CLOSED"
    with pytest.raises(psycopg.errors.RaiseException,
                       match="use sp_close_period / sp_reopen_period"):
        with conn.transaction():
            as_(conn, w, "partner")
            conn.execute("UPDATE return_period SET state = 'IN_REVIEW' WHERE period_id = %s",
                         (pid,))
    assert st() == "CLOSED"
    with conn.transaction():
        as_(conn, w, "partner")
        conn.execute("SELECT sp_reopen_period(%s, 'late invoice')", (pid,))
        conn.execute("RESET ROLE")
    assert st() == "IN_REVIEW"


def test_no_cross_firm_client_users(conn):
    w = world(conn)
    f2 = scalar(conn, "INSERT INTO firm(name) VALUES ('Firm 2') RETURNING firm_id")
    cx = scalar(conn, "INSERT INTO client(firm_id, name, pan) "
                      "VALUES (%s, 'Other', 'AAACX9999X') RETURNING client_id", (f2,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            as_(conn, w, "partner")
            conn.execute("INSERT INTO app_user(firm_id, client_id, email, role, password_hash) "
                         "VALUES (current_app_firm(), %s, 'evil@x', 'client', 'x')", (cx,))
    # a mismatched user created behind the policies still sees nothing
    bad = scalar(conn, "INSERT INTO app_user(firm_id, client_id, email, role, password_hash) "
                       "VALUES (%s, %s, 'bad@x', 'client', 'x') RETURNING user_id",
                 (scalar(conn, "SELECT firm_id FROM client WHERE client_id = %s", (w["c1"],)),
                  cx))
    with conn.transaction():
        as_role(conn, "client", bad)
        assert scalar(conn, "SELECT count(*) FROM client") == 0
        conn.execute("RESET ROLE")
    # own-firm client users are still fine
    with conn.transaction():
        as_(conn, w, "partner")
        conn.execute("INSERT INTO app_user(firm_id, client_id, email, role, password_hash) "
                     "VALUES (current_app_firm(), %s, 'ok@x', 'client', 'x')", (w["c2"],))
        conn.execute("RESET ROLE")
