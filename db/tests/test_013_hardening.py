"""013 hardening (final review F1-F8): every exploit is attempted as the attacking role."""
import datetime as dt
import json
from decimal import Decimal as D

import psycopg
import pytest

from db.tests.conftest import as_role
from db.tests.factories import (assign, build_2b_payload, make_client, make_invoice,
                                make_party, make_period, make_reg, make_user)

SUP = "27"  # supplier in another state -> igst

BOOK_COLS = ("gstin_of_supplier, supplier_name, document_type, invoice_number, invoice_date, "
             "place_of_supply, reverse_charge, hsn_sac, rate, taxable_value, "
             "integrated_tax_paid, central_tax_paid, state_ut_tax_paid, cess_paid, "
             "eligible_for_itc")


def scalar(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def world(conn):
    """Client 1 (staff_a and staff_a2 assigned), client 2 (staff_b assigned)."""
    c1 = make_client(conn, pan="AAACA1111A", name="One")
    c2 = make_client(conn, pan="AAACB2222B", name="Two")
    w = dict(c1=c1, c2=c2, g1=make_reg(conn, c1), g2=make_reg(conn, c2),
             partner=make_user(conn, "partner"),
             staff_a=make_user(conn, "staff"), staff_a2=make_user(conn, "staff"),
             staff_b=make_user(conn, "staff"),
             client1=make_user(conn, "client", c1))
    assign(conn, w["staff_a"], c1)
    assign(conn, w["staff_a2"], c1)
    assign(conn, w["staff_b"], c2)
    w["sup"] = make_party(conn, "AAAPA1111A", SUP)
    return w


ROLE = {"partner": "partner", "staff_a": "staff", "staff_a2": "staff", "staff_b": "staff",
        "client1": "client"}


def run(conn, w, who, sql, params=()):
    with conn.transaction():
        as_role(conn, ROLE[who], w[who])
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else None
        conn.execute("RESET ROLE")
    return rows


def refused(conn, w, who, sql, params=(), exc=psycopg.errors.InsufficientPrivilege,
            match=None):
    with pytest.raises(exc, match=match):
        with conn.transaction():
            as_role(conn, ROLE[who], w[who])
            conn.execute(sql, params)


def payload(g, rtnprd, docs):
    return json.dumps(build_2b_payload(g, rtnprd, docs))


def reconciled(conn, w, g=None, period="092026", books_igst=30000, portal_igst=10000,
               no="INV-1"):
    """As superuser: one books invoice and one 2B doc for g, reconciled. MISMATCH 20000
    by default. Returns (period_id, import_id, result_id)."""
    g = g or w["g1"]
    pid = make_period(conn, g, period)
    mm, yyyy = period[:2], period[2:]
    make_invoice(conn, g, w["sup"], no, f"{yyyy}-{mm}-10",
                 [dict(taxable_value=100000, igst=books_igst)], pos="07")
    imp = scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
                 (pid, payload(g, period, [dict(ctin=w["sup"], inum=no, dt=f"10-{mm}-{yyyy}",
                                                txval=100000, igst=portal_igst)]),
                  w["partner"]))
    scalar(conn, "SELECT sp_reconcile(%s, %s)", (imp, w["partner"]))
    rid = scalar(conn, "SELECT result_id FROM recon_result WHERE import_id = %s", (imp,))
    return pid, imp, rid


def closed(conn, w):
    pid, imp, rid = reconciled(conn, w)
    run(conn, w, "staff_a", "SELECT sp_submit_period(%s)", (pid,))
    run(conn, w, "client1", "SELECT sp_client_approve(%s)", (pid,))
    run(conn, w, "partner", "SELECT sp_close_period(%s)", (pid,))
    assert scalar(conn, "SELECT state::text FROM return_period WHERE period_id = %s",
                  (pid,)) == "CLOSED"
    return pid, imp, rid


def review_row(conn, rev):
    return conn.execute("SELECT state::text, approved_by, proposed_by, approved_at IS NULL, "
                        "executed_on FROM review WHERE review_id = %s", (rev,)).fetchone()


# --- C1: an edited approved decision needs a fresh approval ----------------------------

def test_c1_staff_edit_of_approved_review_drops_approval(conn):
    w = world(conn)
    _, _, rid = reconciled(conn, w)
    rev = run(conn, w, "staff_a", "SELECT sp_propose(%s, 'DEFER', 'NONE', NULL, 'wait')",
              (rid,))[0][0]
    run(conn, w, "partner", "SELECT sp_approve(%s)", (rev,))
    assert review_row(conn, rev)[:3] == ("APPROVED", w["partner"], w["staff_a"])
    run(conn, w, "staff_a2", "UPDATE review SET decision = 'ACCEPT', ims_action = 'ACCEPT' "
                             "WHERE review_id = %s", (rev,))
    assert review_row(conn, rev) == ("PROPOSED", None, w["staff_a2"], True, None)
    # and the partner must approve it again (the editor is now the maker)
    run(conn, w, "partner", "SELECT sp_approve(%s)", (rev,))
    assert review_row(conn, rev)[:3] == ("APPROVED", w["partner"], w["staff_a2"])


def test_c1_edit_of_done_small_review_is_reapproved_by_rule(conn):
    w = world(conn)
    _, _, rid = reconciled(conn, w, books_igst=10100, portal_igst=10000)  # risk 100
    rev = run(conn, w, "staff_a", "SELECT sp_propose(%s, 'ACCEPT', 'ACCEPT', NULL, NULL)",
              (rid,))[0][0]
    run(conn, w, "staff_a", "SELECT sp_mark_done(%s, '2026-10-05')", (rev,))
    assert review_row(conn, rev)[0] == "DONE"
    run(conn, w, "staff_a2", "UPDATE review SET claim_pct = 40, decision = 'PARTIAL' "
                             "WHERE review_id = %s", (rev,))
    st, appr, prop, appr_at_null, executed = review_row(conn, rev)
    assert (st, appr, prop, appr_at_null, executed) == ("APPROVED", None, w["staff_a2"],
                                                        False, None)


def test_c1_remark_edit_keeps_approval(conn):
    w = world(conn)
    _, _, rid = reconciled(conn, w)
    rev = run(conn, w, "staff_a", "SELECT sp_propose(%s, 'DEFER', 'NONE', NULL, NULL)",
              (rid,))[0][0]
    run(conn, w, "partner", "SELECT sp_approve(%s)", (rev,))
    run(conn, w, "staff_a2", "UPDATE review SET remark = 'called' WHERE review_id = %s",
        (rev,))
    assert review_row(conn, rev)[:3] == ("APPROVED", w["partner"], w["staff_a"])


# --- C2: no direct DML on derived / imported data -------------------------------------

def test_c2_staff_direct_dml_refused(conn):
    w = world(conn)
    pid, imp, rid = reconciled(conn, w)
    inv = conn.execute("SELECT invoice_id, fin_year FROM invoice WHERE gstin = %s",
                       (w["g1"],)).fetchone()
    stmts = [
        ("UPDATE recon_result SET status = 'EXACT', diff_tax = 0 WHERE import_id = %s", (imp,)),
        ("DELETE FROM recon_result WHERE import_id = %s", (imp,)),
        ("INSERT INTO recon_result(import_id, invoice_id, invoice_fy, status, matched_by, "
         "diff_tax) VALUES (%s, %s, %s, 'EXACT', 'EXACT', 0)", (imp, *inv)),
        ("UPDATE gstr2b_entry SET igst = 30000 WHERE import_id = %s", (imp,)),
        ("DELETE FROM gstr2b_entry WHERE import_id = %s", (imp,)),
        ("INSERT INTO gstr2b_entry(import_id, fin_year, party_gstin, gstin_valid, section, "
         "doc_type, invoice_no, invoice_date, taxable_value) VALUES "
         "(%s, '2026-27', %s, true, 'b2b', 'INV', 'X-1', '2026-09-01', 1)", (imp, w["sup"])),
        ("UPDATE gstr2b_import SET is_current = false WHERE import_id = %s", (imp,)),
        ("UPDATE invoice SET invoice_no = 'INV-1X' WHERE gstin = %s", (w["g1"],)),
        ("DELETE FROM invoice WHERE gstin = %s", (w["g1"],)),
        ("INSERT INTO invoice(gstin, party_gstin, direction, doc_type, invoice_no, "
         "invoice_date, fin_year, place_of_supply) VALUES "
         "(%s, %s, 'INWARD', 'INV', 'NEW-1', '2026-09-12', '2026-27', '07')",
         (w["g1"], w["sup"])),
        ("UPDATE invoice_line SET igst = 10000 WHERE invoice_id = %s", (inv[0],)),
        ("DELETE FROM invoice_line WHERE invoice_id = %s", (inv[0],)),
        ("INSERT INTO invoice_line(invoice_id, fin_year, hsn_sac, taxable_value, rate, igst) "
         "VALUES (%s, %s, '8471', 10, 18, 1.8)", inv),
        ("UPDATE party SET name = 'HACKED' WHERE gstin = %s", (w["sup"],)),
        ("UPDATE return_period SET state = 'AWAITING_APPROVAL' WHERE period_id = %s", (pid,)),
    ]
    for who in ("staff_a", "partner"):
        for sql, params in stmts:
            refused(conn, w, who, sql, params)
    assert conn.execute("SELECT status::text, diff_tax FROM recon_result WHERE import_id = %s",
                        (imp,)).fetchall() == [("MISMATCH", D("20000.00"))]
    assert scalar(conn, "SELECT igst FROM gstr2b_entry WHERE import_id = %s",
                  (imp,)) == D("10000.00")


def test_c2_cores_not_executable_by_app_roles(conn):
    w = world(conn)
    pid, imp, _ = reconciled(conn, w)
    for who in ("staff_a", "partner"):
        refused(conn, w, who, "SELECT _sp_reconcile_core(%s, %s)", (imp, w[who]))
        refused(conn, w, who, "SELECT _sp_open_period_core(%s, '102026')", (w["g1"],))
        refused(conn, w, who, "SELECT _sp_import_books_core(%s, %s)", (pid, w[who]))
        refused(conn, w, who, "SELECT _sp_import_gstr2b_core(%s, %s::jsonb, 'DRAFT_2B', %s)",
                (pid, payload(w["g1"], "092026", []), w[who]))


def test_c2_staff_full_workflow_via_procedures(conn):
    w = world(conn)
    with conn.transaction():
        as_role(conn, "staff", w["staff_a"])
        pid = scalar(conn, "SELECT sp_open_period(%s, '092026')", (w["g1"],))
        imp = scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
                     (pid, payload(w["g1"], "092026", [
                         dict(ctin=w["sup"], inum="INV-1", dt="10-09-2026", txval=10000,
                              igst=1800),
                         dict(ctin=w["sup"], inum="INV-2", dt="11-09-2026", txval=10000,
                              igst=1000)]), w["staff_a"]))
        for no, d, tax in (("INV-1", "10-09-2026", "1800"), ("INV-2", "11-09-2026", "1800")):
            conn.execute(f"INSERT INTO books_staging(period_id, {BOOK_COLS}) VALUES "
                         "(%s, %s, 'Sup', 'Invoice', %s, %s, '07', 'N', '8471', '18', "
                         "'10000', %s, '0', '0', '0', 'Yes')", (pid, w["sup"], no, d, tax))
        assert scalar(conn, "SELECT sp_import_books(%s, %s)", (pid, w["staff_a"])) == 2
        assert scalar(conn, "SELECT sp_reconcile(%s, %s)", (imp, w["staff_a"])) == 2
        rows = conn.execute("SELECT result_id, status::text FROM v_result_key "
                            "WHERE import_id = %s ORDER BY invoice_no", (imp,)).fetchall()
        assert [r[1] for r in rows] == ["EXACT", "MISMATCH"]
        rev = scalar(conn, "SELECT sp_propose(%s, 'ACCEPT', 'ACCEPT', NULL, NULL)",
                     (rows[1][0],))
        assert rev is not None
        conn.execute("SELECT sp_submit_period(%s)", (pid,))
        assert scalar(conn, "SELECT state::text FROM return_period WHERE period_id = %s",
                      (pid,)) == "AWAITING_APPROVAL"
        conn.execute("RESET ROLE")


def test_c2_other_staff_procedures_not_your_client(conn):
    w = world(conn)
    pid, imp, _ = reconciled(conn, w)
    calls = [
        ("SELECT sp_reconcile(%s, %s)", (imp, w["staff_b"])),
        ("SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
         (pid, payload(w["g1"], "092026", []), w["staff_b"])),
        ("SELECT sp_import_books(%s, %s)", (pid, w["staff_b"])),
        ("SELECT sp_open_period(%s, '102026')", (w["g1"],)),
    ]
    for sql, params in calls:
        refused(conn, w, "staff_b", sql, params, psycopg.errors.RaiseException,
                "not your client")
    assert scalar(conn, "SELECT count(*) FROM gstr2b_import WHERE period_id = %s", (pid,)) == 1
    assert scalar(conn, "SELECT count(*) FROM return_period WHERE gstin = %s", (w["g1"],)) == 1


# --- C3: a CLOSED month is immutable for everyone ---------------------------------------

def test_c3_procedures_refuse_closed_month(conn):
    w = world(conn)
    pid, imp, _ = closed(conn, w)
    for who in ("staff_a", "partner"):
        for sql, params in [
            ("SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
             (pid, payload(w["g1"], "092026", []), w[who])),
            ("SELECT sp_import_books(%s, %s)", (pid, w[who])),
            ("SELECT sp_reconcile(%s, %s)", (imp, w[who])),
            ("SELECT sp_open_period(%s, '092026')", (w["g1"],)),
        ]:
            refused(conn, w, who, sql, params, psycopg.errors.RaiseException,
                    "period is closed")
    assert scalar(conn, "SELECT state::text FROM return_period WHERE period_id = %s",
                  (pid,)) == "CLOSED"


def test_c3_superuser_direct_writes_refused(conn):
    w = world(conn)
    pid, imp, _ = closed(conn, w)
    inv_id = scalar(conn, "SELECT invoice_id FROM invoice WHERE gstin = %s", (w["g1"],))
    for sql, params in [
        ("UPDATE gstr2b_entry SET igst = igst + 1 WHERE import_id = %s", (imp,)),
        ("DELETE FROM gstr2b_entry WHERE import_id = %s", (imp,)),
        ("INSERT INTO gstr2b_entry(import_id, fin_year, party_gstin, gstin_valid, section, "
         "doc_type, invoice_no, invoice_date, taxable_value) VALUES "
         "(%s, '2026-27', %s, true, 'b2b', 'INV', 'X-1', '2026-09-01', 1)", (imp, w["sup"])),
        ("UPDATE invoice_line SET igst = igst + 1 WHERE invoice_id = %s", (inv_id,)),
        ("DELETE FROM invoice_line WHERE invoice_id = %s", (inv_id,)),
        ("UPDATE invoice SET invoice_no = 'INV-1B' WHERE invoice_id = %s", (inv_id,)),
        ("UPDATE gstr2b_import SET is_current = false WHERE import_id = %s", (imp,)),
    ]:
        with pytest.raises(psycopg.errors.RaiseException, match="period is closed"):
            with conn.transaction():
                conn.execute(sql, params)
    with pytest.raises(psycopg.errors.RaiseException, match="period is closed"):
        with conn.transaction():
            make_invoice(conn, w["g1"], w["sup"], "LATE-1", "2026-09-15",
                         [dict(taxable_value=100, igst=18)], pos="07")
    # a month without a period, or after a reopen, is writable again
    make_invoice(conn, w["g1"], w["sup"], "OCT-1", "2026-10-05",
                 [dict(taxable_value=100, igst=18)], pos="07")
    run(conn, w, "partner", "SELECT sp_reopen_period(%s, 'late bill')", (pid,))
    conn.execute("UPDATE gstr2b_entry SET igst = igst + 1 WHERE import_id = %s", (imp,))
    make_invoice(conn, w["g1"], w["sup"], "LATE-1", "2026-09-15",
                 [dict(taxable_value=100, igst=18)], pos="07")


# --- I1: a review's decision counts only in its last period -----------------------------

def test_i1_decision_counts_only_in_last_period(conn):
    w = world(conn)
    g = w["g1"]
    p8 = make_period(conn, g, "082026")
    p9 = make_period(conn, g, "092026")
    make_invoice(conn, g, w["sup"], "AUG-1", "2026-08-20",
                 [dict(taxable_value=100000, igst=18000)], pos="07")
    imp8 = scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
                  (p8, payload(g, "082026", []), w["staff_a"]))
    scalar(conn, "SELECT sp_reconcile(%s, %s)", (imp8, w["staff_a"]))
    r8 = run(conn, w, "staff_a", "SELECT result_id, status::text FROM recon_result "
                                 "WHERE import_id = %s", (imp8,))[0]
    assert r8[1] == "ONLY_IN_BOOKS"
    run(conn, w, "staff_a", "SELECT sp_propose(%s, 'DEFER', 'NONE', NULL, 'wait')", (r8[0],))
    summary = lambda pid: conn.execute(
        "SELECT claimable_now, deferred, hold_payment_parties FROM v_client_summary "
        "WHERE period_id = %s", (pid,)).fetchone()
    assert summary(p8) == (D("0"), D("18000.00"), 0)

    imp9 = scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
                  (p9, payload(g, "092026", [dict(ctin=w["sup"], inum="AUG-1",
                                                  dt="20-08-2026", txval=100000,
                                                  igst=18000)]), w["staff_a"]))
    scalar(conn, "SELECT sp_reconcile(%s, %s)", (imp9, w["staff_a"]))
    r9 = run(conn, w, "staff_a", "SELECT result_id, status::text FROM recon_result "
                                 "WHERE import_id = %s", (imp9,))[0]
    assert r9[1] == "PERIOD_MISMATCH"
    run(conn, w, "staff_a", "SELECT sp_propose(%s, 'ACCEPT', 'ACCEPT', NULL, 'ok')", (r9[0],))

    assert summary(p9)[0] == D("18000.00")
    # August: the key is now unreviewed there (ONLY_IN_BOOKS, not claimable, hold payment)
    assert summary(p8) == (D("0"), D("0"), 1)
    led = conn.execute("SELECT period, claimed FROM v_itc_ledger WHERE gstin = %s "
                       "ORDER BY period", (g,)).fetchall()
    assert led == [("082026", D("0")), ("092026", D("18000.00"))]


# --- I2: IMS plan released only after client approval / override ----------------------

def test_i2_ims_plan_gated_by_client_approval(conn):
    w = world(conn)
    pid, _, rid = reconciled(conn, w)
    rev = run(conn, w, "staff_a", "SELECT sp_propose(%s, 'ACCEPT', 'ACCEPT', NULL, NULL)",
              (rid,))[0][0]
    run(conn, w, "partner", "SELECT sp_approve(%s)", (rev,))
    plan = lambda who: run(conn, w, who, "SELECT invoice_no, ims_action::text FROM v_ims_plan "
                                         "WHERE period_id = %s", (pid,))
    run(conn, w, "staff_a", "SELECT sp_submit_period(%s)", (pid,))
    assert plan("client1") == [] and plan("staff_a") == []
    run(conn, w, "client1", "SELECT sp_client_approve(%s)", (pid,))
    assert plan("client1") == [("INV-1", "ACCEPT")]

    # partner override releases it too
    p2, _, rid2 = reconciled(conn, w, g=w["g2"], no="INV-2")
    run(conn, w, "staff_b", "SELECT sp_propose(%s, 'CHASE', 'PENDING', NULL, NULL)", (rid2,))
    run(conn, w, "staff_b", "SELECT sp_submit_period(%s)", (p2,))
    assert run(conn, w, "partner", "SELECT count(*) FROM v_ims_plan WHERE period_id = %s",
               (p2,)) == [(0,)]
    run(conn, w, "partner", "SELECT sp_partner_override(%s, 'client travelling')", (p2,))
    assert run(conn, w, "partner", "SELECT count(*) FROM v_ims_plan WHERE period_id = %s",
               (p2,)) == [(1,)]


# --- I3: IMS status imported from the 2B / IMS payload ---------------------------------

def _with_ims(g, docs, extra):
    p = build_2b_payload(g, "092026", docs)
    dd = p["data"]["docdata"]
    for sec, key, num in (("b2b", "inv", "inum"), ("cdnr", "nt", "ntnum")):
        for grp in dd[sec]:
            for d in grp[key]:
                d.update(extra.get(d[num], {}))
    return json.dumps(p)


def test_i3_ims_status_imported(conn):
    w = world(conn)
    g = w["g1"]
    pid = make_period(conn, g)
    make_invoice(conn, g, w["sup"], "IMS-A", "2026-09-10",
                 [dict(taxable_value=10000, igst=1800)], pos="07")
    make_invoice(conn, g, w["sup"], "IMS-R", "2026-09-10",
                 [dict(taxable_value=10000, igst=1800)], pos="07")
    docs = [dict(ctin=w["sup"], inum=n, dt="10-09-2026", txval=10000, igst=1800)
            for n in ("IMS-A", "IMS-R", "IMS-N")]
    docs.append(dict(ctin=w["sup"], inum="CN-P", dt="12-09-2026", txval=100, igst=18,
                     section="cdnr", ntty="C"))
    imp = scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'IMS', %s)",
                 (pid, _with_ims(g, docs, {"IMS-A": {"ims_status": "ACCEPTED"},
                                           "IMS-R": {"imsactn": "R"},
                                           "CN-P": {"imsactn": "P"}}), w["staff_a"]))
    got = dict(conn.execute("SELECT invoice_no, ims_status FROM gstr2b_entry "
                            "WHERE import_id = %s", (imp,)).fetchall())
    assert got == {"IMS-A": "ACCEPTED", "IMS-R": "REJECTED", "IMS-N": "NO_ACTION",
                   "CN-P": "PENDING"}
    scalar(conn, "SELECT sp_reconcile(%s, %s)", (imp, w["staff_a"]))
    for no in ("IMS-A", "IMS-R"):
        rid = scalar(conn, "SELECT result_id FROM v_result_key WHERE import_id = %s "
                           "AND invoice_no = %s", (imp, no))
        run(conn, w, "staff_a", "SELECT sp_propose(%s, 'ACCEPT', 'ACCEPT', NULL, NULL)", (rid,))
    obs = conn.execute("SELECT rv.invoice_no, o.planned::text, o.observed, o.mismatch "
                       "FROM v_ims_plan_vs_observed o JOIN review rv USING (review_id) "
                       "WHERE o.period_id = %s ORDER BY 1", (pid,)).fetchall()
    assert obs == [("IMS-A", "ACCEPT", "ACCEPTED", False),
                   ("IMS-R", "ACCEPT", "REJECTED", True)]


@pytest.mark.parametrize("extra", [{"ims_status": "BOGUS"}, {"imsactn": "X"}])
def test_i3_invalid_ims_status_rejected(conn, extra):
    w = world(conn)
    pid = make_period(conn, w["g1"])
    docs = [dict(ctin=w["sup"], inum="IMS-1", dt="10-09-2026", txval=10000, igst=1800)]
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("SELECT sp_import_gstr2b(%s, %s::jsonb, 'IMS', %s)",
                         (pid, _with_ims(w["g1"], docs, {"IMS-1": extra}), w["staff_a"]))
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            imp = scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'IMS', %s)",
                         (pid, payload(w["g1"], "092026", docs), w["staff_a"]))
            conn.execute("UPDATE gstr2b_entry SET ims_status = 'maybe' WHERE import_id = %s",
                         (imp,))


# --- I4: supplier follow-ups -----------------------------------------------------------

FOLLOWUP_INSERT = ("INSERT INTO followup(gstin, party_gstin, period_id, channel, sent_on, "
                   "response, next_chase_on, status) VALUES "
                   "(%s, %s, %s, 'WHATSAPP', '2026-10-15', NULL, '2026-10-22', 'SENT') "
                   "RETURNING followup_id, sent_by")


def test_i4_followup_by_assigned_staff(conn):
    w = world(conn)
    pid = make_period(conn, w["g1"])
    fid, sent_by = run(conn, w, "staff_a", FOLLOWUP_INSERT, (w["g1"], w["sup"], pid))[0]
    assert sent_by == w["staff_a"]
    run(conn, w, "staff_a", "UPDATE followup SET response = 'will upload', "
                            "status = 'RESPONDED' WHERE followup_id = %s", (fid,))
    assert run(conn, w, "client1", "SELECT status FROM followup") == [("RESPONDED",)]
    assert run(conn, w, "partner", "SELECT count(*) FROM followup") == [(1,)]
    # another client's staff sees nothing and cannot log one for this client
    assert run(conn, w, "staff_b", "SELECT count(*) FROM followup") == [(0,)]
    refused(conn, w, "staff_b", FOLLOWUP_INSERT, (w["g1"], w["sup"], pid))
    refused(conn, w, "client1", FOLLOWUP_INSERT, (w["g1"], w["sup"], pid))
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("UPDATE followup SET channel = 'PIGEON' WHERE followup_id = %s",
                         (fid,))


# --- I5 / I6: header and PAN immutability ----------------------------------------------

def test_i5_invoice_header_fixed_once_lines_exist(conn):
    w = world(conn)
    inv, _ = make_invoice(conn, w["g1"], w["sup"], "H-1", "2026-09-10",
                          [dict(taxable_value=100, igst=18)], pos="07")
    for sql in ("UPDATE invoice SET place_of_supply = '27' WHERE invoice_id = %s",
                "UPDATE invoice SET doc_type = 'CRN' WHERE invoice_id = %s",
                "UPDATE invoice SET is_import = true WHERE invoice_id = %s",
                "UPDATE invoice SET party_gstin = gstin WHERE invoice_id = %s"):
        with pytest.raises(psycopg.errors.RaiseException,
                           match="invoice header is fixed once lines exist"):
            with conn.transaction():
                conn.execute(sql, (inv,))
    conn.execute("UPDATE invoice SET invoice_no = 'H-1A' WHERE invoice_id = %s", (inv,))
    bare, _ = make_invoice(conn, w["g1"], w["sup"], "H-2", "2026-09-10", [], pos="07")
    conn.execute("UPDATE invoice SET place_of_supply = '27' WHERE invoice_id = %s", (bare,))


def test_i6_pan_fixed_while_gstins_registered(conn):
    w = world(conn)
    with pytest.raises(psycopg.errors.RaiseException,
                       match="PAN cannot change while GSTINs are registered"):
        with conn.transaction():
            conn.execute("UPDATE client SET pan = 'ZZZZZ9999Z' WHERE client_id = %s", (w["c1"],))
    refused(conn, w, "partner", "UPDATE client SET pan = 'ZZZZZ9999Z' WHERE client_id = %s",
            (w["c1"],), psycopg.errors.RaiseException,
            "PAN cannot change while GSTINs are registered")
    c3 = make_client(conn, pan="AAACC3333C", name="Three")
    conn.execute("UPDATE client SET pan = 'AAACD4444D' WHERE client_id = %s", (c3,))
    conn.execute("UPDATE client SET name = 'Uno' WHERE client_id = %s", (w["c1"],))


# --- F8 ----------------------------------------------------------------------------------

def test_f8_client_of_helpers_hide_other_tenants(conn):
    w = world(conn)
    p1, i1, _ = reconciled(conn, w)
    p2, i2, _ = reconciled(conn, w, g=w["g2"], no="INV-2")
    for who in ("staff_a", "client1"):
        assert run(conn, w, who, "SELECT client_of_gstin(%s), client_of_period(%s), "
                                 "client_of_import(%s)", (w["g2"], p2, i2)) == [(None,) * 3]
        assert run(conn, w, who, "SELECT client_of_gstin(%s), client_of_period(%s), "
                                 "client_of_import(%s)", (w["g1"], p1, i1)) == [(w["c1"],) * 3]


def test_f8_app_role_is_exact_match(conn):
    with conn.transaction():
        conn.execute("SET LOCAL ROLE rc_api")
        for r in ("rc_partner", "rc_staff", "rc_client"):
            assert scalar(conn, "SELECT app_role_is(%s)", (r,)) is False
        conn.execute("RESET ROLE")
    with conn.transaction():
        conn.execute("SET LOCAL ROLE rc_staff")
        assert scalar(conn, "SELECT app_role_is('rc_staff')") is True
        assert scalar(conn, "SELECT app_role_is('rc_partner')") is False
        conn.execute("RESET ROLE")


def test_f8_password_hash_hidden(conn):
    w = world(conn)
    for who in ("staff_a", "partner", "client1"):
        refused(conn, w, who, "SELECT password_hash FROM app_user")
        refused(conn, w, who, "SELECT * FROM app_user")
        assert run(conn, w, who, "SELECT email FROM app_user WHERE user_id = %s",
                   (w[who],))[0][0].endswith("@test.local")


def test_f8_staff_cannot_execute_sp_approve(conn):
    w = world(conn)
    _, _, rid = reconciled(conn, w)
    rev = run(conn, w, "staff_a", "SELECT sp_propose(%s, 'DEFER', 'NONE', NULL, NULL)",
              (rid,))[0][0]
    refused(conn, w, "staff_a2", "SELECT sp_approve(%s)", (rev,))


def test_f8_no_project_function_executable_by_public(conn):
    rows = conn.execute(
        "SELECT p.oid::regprocedure::text FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' "
        "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass "
        "                AND d.objid = p.oid AND d.deptype = 'e') "
        "AND has_function_privilege('public', p.oid, 'EXECUTE') ORDER BY 1").fetchall()
    assert rows == []


def test_f8_rule37_excludes_rcm(conn):
    conn.execute("SELECT set_config('app.today', '2026-10-25', true)")
    w = world(conn)
    g = w["g1"]
    make_period(conn, g, "042026")
    normal, nfy = make_invoice(conn, g, w["sup"], "R-1", "2026-04-01",
                               [dict(taxable_value=10000, igst=1800)], pos="07")
    conn.execute("INSERT INTO payment(invoice_id, fin_year, paid_on, amount) "
                 "VALUES (%s, %s, '2026-05-01', 100)", (normal, nfy))
    make_invoice(conn, g, w["sup"], "RCM-1", "2026-04-02",
                 [dict(taxable_value=10000, igst=1800)], pos="07", is_rcm=True)
    make_invoice(conn, g, w["sup"], "BLK-1", "2026-04-03",
                 [dict(taxable_value=10000, igst=1800, itc_category="BLOCKED_17_5")],
                 pos="07")
    rows = conn.execute("SELECT invoice_no FROM v_rule37 WHERE client_id = %s ORDER BY 1",
                        (w["c1"],)).fetchall()
    assert rows == [("R-1",)]


def test_f8_s16_4_alert_only_for_defer_chase(conn):
    conn.execute("SELECT set_config('app.today', '2026-11-05', true)")
    w = world(conn)
    g = w["g1"]
    mar = make_period(conn, g, "032026")
    for no in ("OLD-A", "OLD-D", "OLD-C"):
        make_invoice(conn, g, w["sup"], no, "2026-03-10",
                     [dict(taxable_value=1000, igst=180)], pos="07")
    imp = scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
                 (mar, payload(g, "032026", []), w["staff_a"]))
    scalar(conn, "SELECT sp_reconcile(%s, %s)", (imp, w["staff_a"]))
    for no, dec in (("OLD-A", "ACCEPT"), ("OLD-D", "DEFER"), ("OLD-C", "CHASE")):
        rid = scalar(conn, "SELECT result_id FROM v_result_key WHERE import_id = %s "
                           "AND invoice_no = %s", (imp, no))
        run(conn, w, "staff_a", "SELECT sp_propose(%s, %s::review_decision, 'NONE', NULL, NULL)",
            (rid, dec))
    rows = conn.execute("SELECT ref, due_by FROM v_alerts WHERE gstin = %s "
                        "AND kind = 'S16_4_NEAR' ORDER BY ref", (g,)).fetchall()
    assert rows == [("OLD-C", dt.date(2026, 11, 30)), ("OLD-D", dt.date(2026, 11, 30))]


def test_f8_cross_period_bounded_by_s16_4(conn):
    """FY 2025-26 invoice (s.16(4) = 30 Nov 2026): matchable in Nov 2026, not in Dec 2026."""
    w = world(conn)
    out = {}
    for g, period in ((w["g1"], "112026"), (w["g2"], "122026")):
        pid = make_period(conn, g, period)
        make_invoice(conn, g, w["sup"], "OLD-9", "2026-03-10",
                     [dict(taxable_value=10000, igst=1800)], pos="07")
        imp = scalar(conn, "SELECT sp_import_gstr2b(%s, %s::jsonb, 'DRAFT_2B', %s)",
                     (pid, payload(g, period, [dict(ctin=w["sup"], inum="OLD-9",
                                                    dt="10-03-2026", txval=10000,
                                                    igst=1800)]), w["partner"]))
        scalar(conn, "SELECT sp_reconcile(%s, %s)", (imp, w["partner"]))
        out[period] = [r[0] for r in conn.execute(
            "SELECT status::text FROM recon_result WHERE import_id = %s ORDER BY 1",
            (imp,)).fetchall()]
    assert out == {"112026": ["PERIOD_MISMATCH"], "122026": ["ONLY_IN_2B"]}
