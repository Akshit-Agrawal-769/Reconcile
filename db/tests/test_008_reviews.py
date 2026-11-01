from decimal import Decimal as D

import psycopg
import pytest

from db.tests.conftest import as_role
from db.tests.factories import (build_2b_payload, import_2b, make_client, make_invoice,
                                make_party, make_period, make_reg, make_user, reconcile)

SUP = "27"  # supplier in another state -> igst


def setup(conn, period="092026"):
    cid = make_client(conn)
    g = make_reg(conn, cid)
    staff = make_user(conn, "staff")
    partner = make_user(conn, "partner")
    return g, make_period(conn, g, period), staff, partner


def books(conn, g, sup, no, date="2026-09-10", igst=1800, **kw):
    return make_invoice(conn, g, sup, no, date, [dict(taxable_value=10000, igst=igst)],
                        pos="07", **kw)


def doc(sup, no, date="10-09-2026", igst=1800, **kw):
    return dict(ctin=sup, inum=no, dt=date, txval=10000, igst=igst, **kw)


def portal(conn, g, pid, uid, docs, rtnprd="092026"):
    return import_2b(conn, pid, build_2b_payload(g, rtnprd, docs), uid)


def set_user(conn, uid):
    conn.execute("SELECT set_config('app.user_id', %s, true)", (str(uid),))


def result_id(conn, imp, invoice_no):
    return conn.execute(
        "SELECT r.result_id FROM recon_result r "
        "LEFT JOIN invoice i ON i.invoice_id = r.invoice_id AND i.fin_year = r.invoice_fy "
        "LEFT JOIN gstr2b_entry e ON e.entry_id = r.entry_id AND e.fin_year = r.entry_fy "
        "WHERE r.import_id = %s AND coalesce(i.invoice_no, e.invoice_no) = %s",
        (imp, invoice_no),
    ).fetchone()[0]


def propose(conn, rid, decision="ACCEPT", ims="ACCEPT", pct=None, remark=None):
    return conn.execute(
        "SELECT sp_propose(%s, %s::review_decision, %s::ims_action, %s, %s)",
        (rid, decision, ims, pct, remark),
    ).fetchone()[0]


def review(conn, rev):
    return conn.execute(
        "SELECT state::text, proposed_by, approved_by, tax_at_risk FROM review "
        "WHERE review_id = %s", (rev,)).fetchone()


def one_mismatch(conn, books_igst, portal_igst, no="INV-1"):
    g, pid, staff, partner = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, no, igst=books_igst)
    imp = portal(conn, g, pid, staff, [doc(s, no, igst=portal_igst)])
    reconcile(conn, imp, staff)
    return g, pid, imp, staff, partner, result_id(conn, imp, no)


def test_propose_small_auto_approves(conn):
    _, _, _, staff, _, rid = one_mismatch(conn, 1800, 1600)
    set_user(conn, staff)
    rev = propose(conn, rid)
    assert review(conn, rev) == ("APPROVED", staff, None, D("200.00"))


def test_propose_requires_app_user(conn):
    _, _, _, _, _, rid = one_mismatch(conn, 1800, 1600)
    conn.execute("SELECT set_config('app.user_id', '', true)")
    with pytest.raises(psycopg.errors.RaiseException, match="app.user_id not set"):
        with conn.transaction():
            propose(conn, rid)


def test_propose_upserts_on_natural_key(conn):
    _, _, _, staff, _, rid = one_mismatch(conn, 1800, 1600)
    set_user(conn, staff)
    rev1 = propose(conn, rid)
    rev2 = propose(conn, rid, decision="CHASE", ims="PENDING", remark="call supplier")
    assert rev1 == rev2
    assert conn.execute("SELECT count(*), max(decision::text) FROM review").fetchone() == (
        1, "CHASE")


def test_propose_large_needs_partner(conn):
    _, _, _, staff, partner, rid = one_mismatch(conn, 30000, 10000)
    with conn.transaction():
        as_role(conn, "staff", staff)
        rev = propose(conn, rid)
        conn.execute("RESET ROLE")
    assert review(conn, rev) == ("PROPOSED", staff, None, D("20000.00"))

    with pytest.raises(psycopg.errors.RaiseException, match="only a partner can approve"):
        with conn.transaction():
            as_role(conn, "staff", staff)
            conn.execute("SELECT sp_approve(%s)", (rev,))
    assert review(conn, rev)[0] == "PROPOSED"

    with conn.transaction():
        as_role(conn, "partner", partner)
        conn.execute("SELECT sp_approve(%s)", (rev,))
        conn.execute("RESET ROLE")
    assert review(conn, rev) == ("APPROVED", staff, partner, D("20000.00"))


def test_maker_checker(conn):
    _, _, _, _, partner, rid = one_mismatch(conn, 30000, 10000)
    with conn.transaction():
        as_role(conn, "partner", partner)
        rev = propose(conn, rid)
        conn.execute("RESET ROLE")
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            as_role(conn, "partner", partner)
            conn.execute("SELECT sp_approve(%s)", (rev,))


def test_partial_needs_pct(conn):
    _, _, _, staff, _, rid = one_mismatch(conn, 1800, 1600)
    set_user(conn, staff)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            propose(conn, rid, decision="PARTIAL", pct=None)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            propose(conn, rid, decision="PARTIAL", pct=120)
    rev = propose(conn, rid, decision="PARTIAL", pct=50)
    assert conn.execute("SELECT claim_pct FROM review WHERE review_id = %s",
                        (rev,)).fetchone()[0] == D("50")


def test_rcm_ims_none(conn):
    g, pid, staff, _ = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute(
                "INSERT INTO review(gstin, party_gstin, doc_type, invoice_no_norm, fin_year, "
                "invoice_no, decision, ims_action, is_rcm, proposed_by, last_period_id) "
                "VALUES (%s, %s, 'INV', 'RCM1', '2026-27', 'RCM-1', 'ACCEPT', 'ACCEPT', true, "
                "%s, %s)", (g, s, staff, pid))
    conn.execute(
        "INSERT INTO review(gstin, party_gstin, doc_type, invoice_no_norm, fin_year, "
        "invoice_no, decision, ims_action, is_rcm, proposed_by, last_period_id) "
        "VALUES (%s, %s, 'INV', 'RCM1', '2026-27', 'RCM-1', 'ACCEPT', 'NONE', true, %s, %s)",
        (g, s, staff, pid))


def test_mark_done_requires_approved(conn):
    _, _, _, staff, partner, rid = one_mismatch(conn, 30000, 10000)
    set_user(conn, staff)
    rev = propose(conn, rid)
    with pytest.raises(psycopg.errors.RaiseException, match="review must be approved first"):
        with conn.transaction():
            conn.execute("SELECT sp_mark_done(%s, '2026-10-12')", (rev,))
    set_user(conn, partner)
    conn.execute("SELECT sp_approve(%s)", (rev,))
    conn.execute("SELECT sp_mark_done(%s, '2026-10-12')", (rev,))
    assert conn.execute("SELECT state::text, executed_on::text FROM review WHERE review_id = %s",
                        (rev,)).fetchone() == ("DONE", "2026-10-12")


def queue(conn, pid):
    return conn.execute(
        "SELECT invoice_no, status::text, review_id, changed_since_review, carried_from_period, "
        "tax_at_risk, priority FROM v_queue WHERE period_id = %s "
        "ORDER BY priority, tax_at_risk DESC NULLS LAST, invoice_no", (pid,)).fetchall()


def test_queue_orders_exact_last(conn):
    g, pid, staff, _ = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "OK-1")
    books(conn, g, s, "BIG-1", igst=9000)
    books(conn, g, s, "SMALL-1", igst=1800)
    imp = portal(conn, g, pid, staff, [doc(s, "OK-1"), doc(s, "BIG-1", igst=1000),
                                      doc(s, "SMALL-1", igst=1700)])
    reconcile(conn, imp, staff)
    assert [(r[0], r[1], r[5], r[6]) for r in queue(conn, pid)] == [
        ("BIG-1", "MISMATCH", D("8000.00"), 0),
        ("SMALL-1", "MISMATCH", D("100.00"), 0),
        ("OK-1", "EXACT", D("0.00"), 1),
    ]


def test_reimport_keeps_reviews_flags_only_changed(conn):
    g, pid, staff, _ = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "A-1")
    books(conn, g, s, "B-1")
    v1 = portal(conn, g, pid, staff, [doc(s, "A-1", igst=1600), doc(s, "B-1", igst=1700)])
    reconcile(conn, v1, staff)
    set_user(conn, staff)
    rev_a = propose(conn, result_id(conn, v1, "A-1"), decision="CHASE", ims="PENDING")
    rev_b = propose(conn, result_id(conn, v1, "B-1"), decision="CHASE", ims="PENDING")
    assert review(conn, rev_a)[0] == review(conn, rev_b)[0] == "APPROVED"

    # v2: supplier amended A-1's tax; B-1 unchanged.
    v2 = import_2b(conn, pid, build_2b_payload(g, "092026", [doc(s, "A-1", igst=1500),
                                                             doc(s, "B-1", igst=1700)]),
                   staff, source="RECOMPUTED_2B")
    reconcile(conn, v2, staff)

    rows = {r[0]: r for r in queue(conn, pid)}
    assert set(rows) == {"A-1", "B-1"}
    assert conn.execute("SELECT count(*) FROM v_queue WHERE period_id = %s AND import_id = %s",
                        (pid, v2)).fetchone()[0] == 2
    assert rows["A-1"][2] == rev_a and rows["A-1"][3] is True
    assert rows["B-1"][2] == rev_b and rows["B-1"][3] is False
    assert conn.execute("SELECT count(*) FROM review").fetchone()[0] == 2

    assert conn.execute("SELECT sp_refresh_reviews(%s)", (v2,)).fetchone()[0] == 1
    assert review(conn, rev_a)[0] == "PROPOSED"
    assert review(conn, rev_a)[2] is None
    assert review(conn, rev_b)[0] == "APPROVED"

    # Re-proposing on the new result clears the flag.
    propose(conn, result_id(conn, v2, "A-1"), decision="CHASE", ims="PENDING")
    assert {r[0]: r[3] for r in queue(conn, pid)} == {"A-1": False, "B-1": False}


def carry_setup(conn):
    g, p08, staff, partner = setup(conn, "082026")
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "AUG-1", date="2026-08-10")
    imp08 = portal(conn, g, p08, staff, [doc(s, "AUG-1", date="10-08-2026", igst=1500)],
                   rtnprd="082026")
    reconcile(conn, imp08, staff)
    set_user(conn, staff)
    rev = propose(conn, result_id(conn, imp08, "AUG-1"), decision="DEFER", ims="PENDING")
    return g, s, p08, staff, rev


def test_carry_forward(conn):
    g, s, p08, staff, rev = carry_setup(conn)
    p09 = make_period(conn, g, "092026")
    books(conn, g, s, "SEP-1")
    imp09 = portal(conn, g, p09, staff, [doc(s, "SEP-1")])
    reconcile(conn, imp09, staff)

    carried = conn.execute(
        "SELECT review_id, carried_from_period, result_id, import_id, status, party_gstin, "
        "invoice_no, tax_at_risk, priority, decision::text FROM v_queue "
        "WHERE period_id = %s AND carried_from_period IS NOT NULL", (p09,)).fetchall()
    assert carried == [(rev, "082026", None, None, None, s, "AUG-1", D("300.00"), 0, "DEFER")]
    # In its own period the review is attached, not carried.
    assert conn.execute("SELECT review_id, carried_from_period FROM v_queue "
                        "WHERE period_id = %s", (p08,)).fetchall() == [(rev, None)]

    conn.execute("SELECT sp_mark_done(%s, '2026-09-20')", (rev,))
    assert conn.execute("SELECT count(*) FROM v_queue WHERE period_id = %s "
                        "AND carried_from_period IS NOT NULL", (p09,)).fetchone()[0] == 0


def test_carry_forward_stops_after_s16_4(conn):
    g, _, _, _, rev = carry_setup(conn)
    # FY 2026-27 -> s.16(4) date 2027-11-30.
    p_nov = make_period(conn, g, "112027")
    p_dec = make_period(conn, g, "122027")
    assert [r[0] for r in conn.execute(
        "SELECT review_id FROM v_queue WHERE period_id = %s", (p_nov,)).fetchall()] == [rev]
    assert conn.execute("SELECT count(*) FROM v_queue WHERE period_id = %s",
                        (p_dec,)).fetchone()[0] == 0


def test_carry_forward_reattaches_when_invoice_reappears(conn):
    g, s, _, staff, rev = carry_setup(conn)
    p09 = make_period(conn, g, "092026")
    # The supplier files AUG-1 late; it shows up in September's 2B.
    imp09 = portal(conn, g, p09, staff, [doc(s, "AUG-1", date="10-08-2026", igst=1500)])
    reconcile(conn, imp09, staff)
    rows = conn.execute("SELECT review_id, carried_from_period, result_id IS NOT NULL "
                        "FROM v_queue WHERE period_id = %s", (p09,)).fetchall()
    assert rows == [(rev, None, True)]


def test_ims_plan_vs_observed(conn):
    _, pid, imp, staff, _, rid = one_mismatch(conn, 1800, 1600)
    set_user(conn, staff)
    rev = propose(conn, rid, decision="ACCEPT", ims="ACCEPT")
    assert conn.execute(
        "SELECT review_id, planned::text, observed, mismatch FROM v_ims_plan_vs_observed "
        "WHERE period_id = %s", (pid,)).fetchall() == [(rev, "ACCEPT", "NO_ACTION", True)]

    conn.execute("UPDATE gstr2b_entry SET ims_status = 'ACCEPTED' WHERE import_id = %s", (imp,))
    assert conn.execute("SELECT mismatch FROM v_ims_plan_vs_observed WHERE period_id = %s",
                        (pid,)).fetchone()[0] is False

    propose(conn, rid, decision="ACCEPT", ims="NONE")
    assert conn.execute("SELECT mismatch FROM v_ims_plan_vs_observed WHERE period_id = %s",
                        (pid,)).fetchone()[0] is False


def test_duplicate_never_attaches_to_review(conn):
    g, pid, staff, partner = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "DUP-1", igst=30000)
    imp = portal(conn, g, pid, staff, [doc(s, "DUP-1", igst=10000), doc(s, "DUP-1", igst=10000)])
    reconcile(conn, imp, staff)
    dup_rid, real_rid = (conn.execute(
        "SELECT result_id FROM recon_result WHERE import_id = %s AND status = %s",
        (imp, st)).fetchone()[0] for st in ("DUPLICATE", "MISMATCH"))

    set_user(conn, staff)
    rev = propose(conn, real_rid, decision="CHASE", ims="ACCEPT")
    set_user(conn, partner)
    with conn.transaction():
        as_role(conn, "partner", partner)
        conn.execute("SELECT sp_approve(%s)", (rev,))
        conn.execute("RESET ROLE")
    assert review(conn, rev)[0] == "APPROVED"

    rows = conn.execute("SELECT status::text, review_id, changed_since_review FROM v_queue "
                        "WHERE period_id = %s ORDER BY status::text", (pid,)).fetchall()
    assert rows == [("DUPLICATE", None, False), ("MISMATCH", rev, False)]
    assert conn.execute("SELECT sp_refresh_reviews(%s)", (imp,)).fetchone()[0] == 0
    assert review(conn, rev)[0] == "APPROVED"
    assert conn.execute("SELECT review_id FROM v_ims_plan_vs_observed WHERE period_id = %s",
                        (pid,)).fetchall() == [(rev,)]

    set_user(conn, staff)
    with pytest.raises(psycopg.errors.RaiseException, match="cannot review a duplicate entry"):
        with conn.transaction():
            propose(conn, dup_rid)
    assert review(conn, rev)[:3] == ("APPROVED", staff, partner)


def test_refresh_skips_already_proposed(conn):
    g, pid, staff, _ = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "BIG-1", igst=30000)
    v1 = portal(conn, g, pid, staff, [doc(s, "BIG-1", igst=10000)])
    reconcile(conn, v1, staff)
    set_user(conn, staff)
    rev = propose(conn, result_id(conn, v1, "BIG-1"), decision="CHASE", ims="PENDING")
    assert review(conn, rev)[0] == "PROPOSED"
    v2 = portal(conn, g, pid, staff, [doc(s, "BIG-1", igst=9000)])
    reconcile(conn, v2, staff)
    assert conn.execute("SELECT sp_refresh_reviews(%s)", (v2,)).fetchone()[0] == 0


def test_propose_ignores_temp_table_shadowing(conn):
    _, _, _, staff, _, rid = one_mismatch(conn, 30000, 10000)
    cid = conn.execute("SELECT max(client_id) FROM client").fetchone()[0]
    with conn.transaction():
        as_role(conn, "staff", staff)
        can_temp = conn.execute(
            "SELECT has_database_privilege(current_database(), 'TEMP')").fetchone()[0]
        if can_temp:
            conn.execute("CREATE TEMP TABLE client(client_id bigint, "
                         "approval_threshold numeric(14,2)) ON COMMIT DROP")
            conn.execute("INSERT INTO pg_temp.client VALUES (%s, 999999999)", (cid,))
        rev = propose(conn, rid)
        conn.execute("RESET ROLE")
    assert review(conn, rev)[0] == "PROPOSED"
