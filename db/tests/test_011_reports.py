import datetime as dt
from decimal import Decimal as D

from db.tests.conftest import as_role
from db.tests.factories import (assign, build_2b_payload, import_2b, make_client,
                                make_invoice, make_party, make_period, make_reg, make_user,
                                recon_rows, reconcile)

SUP = "27"  # supplier in another state -> igst


def set_today(conn, day):
    conn.execute("SELECT set_config('app.today', %s, true)", (day,))


def set_user(conn, uid):
    conn.execute("SELECT set_config('app.user_id', %s, true)", (str(uid),))


def books(conn, g, sup, no, igst, date="2026-09-10", taxable=10000):
    return make_invoice(conn, g, sup, no, date, [dict(taxable_value=taxable, igst=igst)],
                        pos="07")


def doc(sup, no, igst, date="10-09-2026"):
    return dict(ctin=sup, inum=no, dt=date, txval=10000, igst=igst)


def setup(conn, pan="AAACA1111A", name="One", period="092026"):
    cid = make_client(conn, pan=pan, name=name)
    g = make_reg(conn, cid)
    staff = make_user(conn, "staff")
    assign(conn, staff, cid)
    return cid, g, make_period(conn, g, period), staff


def result_id(conn, imp, invoice_no):
    return conn.execute(
        "SELECT result_id FROM v_result_key WHERE import_id = %s AND invoice_no = %s",
        (imp, invoice_no)).fetchone()[0]


def propose(conn, rid, decision, ims="NONE", pct=None, remark=None):
    return conn.execute(
        "SELECT sp_propose(%s, %s::review_decision, %s::ims_action, %s, %s)",
        (rid, decision, ims, pct, remark)).fetchone()[0]


def three_way(conn, **kw):
    """EXACT 1800, ONLY_IN_BOOKS 500, MISMATCH books 1000 vs portal 800."""
    cid, g, pid, staff = setup(conn, **kw)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV-1", 1800)
    books(conn, g, s, "INV-2", 500)
    books(conn, g, s, "INV-3", 1000)
    imp = import_2b(conn, pid, build_2b_payload(g, "092026", [
        doc(s, "INV-1", 1800), doc(s, "INV-3", 800)]), staff)
    reconcile(conn, imp, staff)
    assert [r[0] for r in recon_rows(conn, imp)] == ["EXACT", "MISMATCH", "ONLY_IN_BOOKS"]
    return cid, g, pid, imp, staff, s


# --- brief tests -----------------------------------------------------------------------

def test_period_totals(conn):
    _, _, pid, _, _, _ = three_way(conn)
    row = conn.execute(
        "SELECT itc_books, itc_2b, itc_at_risk, match_rate_pct FROM v_period_totals "
        "WHERE period_id = %s", (pid,)).fetchone()
    assert row == (D("3300.00"), D("2600.00"), D("700.00"), D("33.33"))


def test_period_summary(conn):
    _, _, pid, _, _, _ = three_way(conn)
    rows = conn.execute(
        "SELECT status::text, doc_count, books_tax, portal_tax FROM v_period_summary "
        "WHERE period_id = %s ORDER BY 1", (pid,)).fetchall()
    assert rows == [("EXACT", 1, D("1800.00"), D("1800.00")),
                    ("MISMATCH", 1, D("1000.00"), D("800.00")),
                    ("ONLY_IN_BOOKS", 1, D("500.00"), None)]


def test_rule37_buckets(conn):
    set_today(conn, "2026-10-25")
    today = dt.date(2026, 10, 25)
    cid, g, _, _ = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    ids = {}
    for no, age in (("R-100", 100), ("R-170", 170), ("R-200", 200)):
        ids[no] = books(conn, g, s, no, 1800, date=today - dt.timedelta(days=age))
    inv, fy = ids["R-100"]
    conn.execute("INSERT INTO payment(invoice_id, fin_year, paid_on, amount) "
                 "VALUES (%s, %s, '2026-09-01', 5000)", (inv, fy))
    # another client with an unpaid invoice but no payments at all: no rows
    cid2, g2, _, _ = setup(conn, pan="AAACB2222B", name="Two")
    books(conn, g2, s, "X-1", 1800, date=today - dt.timedelta(days=200))

    rows = conn.execute(
        "SELECT invoice_no, unpaid, days_outstanding, bucket, interest_estimate, "
        "proposed_reversal FROM v_rule37 WHERE client_id = %s ORDER BY invoice_no",
        (cid,)).fetchall()
    assert rows == [
        ("R-100", D("6800.00"), 100, "0-150", D("0"), D("0")),
        ("R-170", D("11800.00"), 170, "150-180", D("0"), D("0")),
        # tax portion 1800; 1800 * 18% * 20/365 = 17.75
        ("R-200", D("11800.00"), 200, ">180", D("17.75"), D("1800.00")),
    ]
    assert conn.execute("SELECT count(*) FROM v_rule37 WHERE client_id = %s",
                        (cid2,)).fetchone()[0] == 0


def test_party_score(conn):
    cid, g, pid, staff = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    for no in ("P-1", "P-2", "P-3", "P-4"):
        books(conn, g, s, no, 1800)
    imp = import_2b(conn, pid, build_2b_payload(g, "092026", [
        doc(s, "P-1", 1800), doc(s, "P-2", 1800)]), staff)
    reconcile(conn, imp, staff)
    conn.execute("SELECT sp_refresh_party_score()")
    row = conn.execute(
        "SELECT docs, pct_only_in_books, pct_mismatch, avg_filing_delay_days, score "
        "FROM v_party_score WHERE party_gstin = %s AND client_id = %s", (s, cid)).fetchone()
    assert row[0] == 4
    assert row[1] == D("0.5") and row[2] == 0 and row[3] == 0
    assert row[4] == D("75.00")
    comment = conn.execute(
        "SELECT obj_description('mv_party_score'::regclass, 'pg_class')").fetchone()[0]
    assert "50*pct_only_in_books" in comment


def gen(conn, g, frm="2026-04-01", to="2026-06-30"):
    return conn.execute("SELECT sp_generate_filings(%s, %s::date, %s::date)",
                        (g, frm, to)).fetchone()[0]


def filings(conn, g):
    return conn.execute(
        "SELECT return_type, period::text, due_date, status FROM filing WHERE gstin = %s "
        "ORDER BY due_date, return_type", (g,)).fetchall()


def test_generate_filings_by_type(conn):
    c1 = make_client(conn, pan="AAACA1111A", name="Reg")
    g1 = make_reg(conn, c1, "REGULAR", "07", "MONTHLY")
    assert gen(conn, g1) == 6
    rows = filings(conn, g1)
    assert len(rows) == 6
    assert sorted({(r[0]) for r in rows}) == ["GSTR1", "GSTR3B"]
    assert ("GSTR1", "042026", dt.date(2026, 5, 11), "DUE") in rows
    assert ("GSTR3B", "062026", dt.date(2026, 7, 20), "DUE") in rows
    assert gen(conn, g1) == 0  # idempotent

    c2 = make_client(conn, pan="AAACB2222B", name="Comp")
    g2 = make_reg(conn, c2, "COMPOSITION", "07", "QRMP")
    assert gen(conn, g2) == 1
    assert filings(conn, g2) == [("CMP08", "062026", dt.date(2026, 7, 18), "DUE")]


def test_alerts_overdue(conn):
    set_today(conn, "2026-10-25")
    cid = make_client(conn, pan="AAACA1111A")
    g = make_reg(conn, cid)
    conn.execute("INSERT INTO filing(gstin, return_type, period, due_date) "
                 "VALUES (%s, 'GSTR3B', '092026', '2026-10-20')", (g,))
    rows = conn.execute("SELECT client_id, gstin, kind, ref, due_by FROM v_alerts "
                        "WHERE gstin = %s", (g,)).fetchall()
    assert rows == [(cid, g, "FILING_OVERDUE", "GSTR3B 092026", dt.date(2026, 10, 20))]


def test_client_summary_numbers(conn):
    """Hand-computed fixture.
    P1: INV-1 EXACT 1800 (no review)            -> claimable 1800
        INV-3 MISMATCH 1000/800, ACCEPT         -> claimable 1000
        INV-4 MISMATCH 2000/1500, PARTIAL 50%   -> claimable 1000
        INV-5 ONLY_IN_BOOKS 600, DEFER          -> deferred 600
        INV-8 MISMATCH 900/500, INELIGIBLE      -> lost 400
        INV-0 (August) ONLY_IN_BOOKS 250, DEFER, carried into September -> deferred 250
    P2: INV-6 ONLY_IN_BOOKS 400, no review      -> hold
    P3: INV-7 ONLY_IN_BOOKS 300, CHASE          -> deferred 300, hold
    => claimable 3800, deferred 1150, lost 400, hold_payment_parties 2."""
    cid, g, pid, staff = setup(conn)
    set_user(conn, staff)
    p1 = make_party(conn, "AAAPA1111A", SUP)
    p2 = make_party(conn, "AAAPB2222B", SUP)
    p3 = make_party(conn, "AAAPC3333C", SUP)

    aug = make_period(conn, g, "082026")
    books(conn, g, p1, "INV-0", 250, date="2026-08-10")
    imp_aug = import_2b(conn, aug, build_2b_payload(g, "082026", []), staff)
    reconcile(conn, imp_aug, staff)
    propose(conn, result_id(conn, imp_aug, "INV-0"), "DEFER")

    for no, tax in (("INV-1", 1800), ("INV-3", 1000), ("INV-4", 2000), ("INV-5", 600),
                    ("INV-8", 900)):
        books(conn, g, p1, no, tax)
    books(conn, g, p2, "INV-6", 400)
    books(conn, g, p3, "INV-7", 300)
    imp = import_2b(conn, pid, build_2b_payload(g, "092026", [
        doc(p1, "INV-1", 1800), doc(p1, "INV-3", 800), doc(p1, "INV-4", 1500),
        doc(p1, "INV-8", 500)]), staff)
    reconcile(conn, imp, staff)
    assert [(r[0], r[2]) for r in recon_rows(conn, imp)] == [
        ("EXACT", "INV-1"), ("MISMATCH", "INV-3"), ("MISMATCH", "INV-4"),
        ("MISMATCH", "INV-8"), ("ONLY_IN_BOOKS", "INV-5"), ("ONLY_IN_BOOKS", "INV-6"),
        ("ONLY_IN_BOOKS", "INV-7")]

    propose(conn, result_id(conn, imp, "INV-3"), "ACCEPT", "ACCEPT")
    propose(conn, result_id(conn, imp, "INV-4"), "PARTIAL", "PENDING", 50, "half now")
    propose(conn, result_id(conn, imp, "INV-5"), "DEFER")
    propose(conn, result_id(conn, imp, "INV-7"), "CHASE")
    propose(conn, result_id(conn, imp, "INV-8"), "INELIGIBLE", "REJECT", None, "blocked")

    row = conn.execute(
        "SELECT claimable_now, deferred, lost, hold_payment_parties FROM v_client_summary "
        "WHERE period_id = %s", (pid,)).fetchone()
    assert row == (D("3800.00"), D("1150.00"), D("400.00"), 2)

    plan = conn.execute(
        "SELECT party_gstin, invoice_no, ims_action::text, remark FROM v_ims_plan "
        "WHERE period_id = %s ORDER BY invoice_no", (pid,)).fetchall()
    assert plan == [(p1, "INV-3", "ACCEPT", None), (p1, "INV-4", "PENDING", "half now"),
                    (p1, "INV-8", "REJECT", "blocked")]

    led = conn.execute(
        "SELECT available, claimed, deferred, blocked FROM v_itc_ledger "
        "WHERE gstin = %s AND period = '092026'", (g,)).fetchone()
    assert led == (D("4600.00"), D("3800.00"), D("1150.00"), D("0"))


# --- extra tests -----------------------------------------------------------------------

def test_itc_ledger_blocked(conn):
    cid, g, pid, staff = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    make_invoice(conn, g, s, "B-1", "2026-09-10",
                 [dict(taxable_value=10000, igst=1800, itc_category="BLOCKED_17_5")],
                 pos="07")
    imp = import_2b(conn, pid, build_2b_payload(g, "092026", []), staff)
    reconcile(conn, imp, staff)
    assert conn.execute("SELECT blocked FROM v_itc_ledger WHERE gstin = %s AND period = "
                        "'092026'", (g,)).fetchone()[0] == D("1800.00")


def test_generate_filings_regular_qrmp(conn):
    # Seeded REGULAR/QRMP rules: IFF monthly (day 13) and GSTR3B quarterly (day 22).
    cid = make_client(conn, pan="AAACA1111A")
    g = make_reg(conn, cid, "REGULAR", "07", "QRMP")
    assert gen(conn, g) == 4
    assert filings(conn, g) == [
        ("IFF", "042026", dt.date(2026, 5, 13), "DUE"),
        ("IFF", "052026", dt.date(2026, 6, 13), "DUE"),
        ("IFF", "062026", dt.date(2026, 7, 13), "DUE"),
        ("GSTR3B", "062026", dt.date(2026, 7, 22), "DUE"),
    ]


def test_generate_filings_annual_needs_march(conn):
    cid = make_client(conn, pan="AAACA1111A")
    g = make_reg(conn, cid, "COMPOSITION", "07", "QRMP")
    assert gen(conn, g, "2027-01-01", "2027-03-31") == 2
    assert filings(conn, g) == [("CMP08", "032027", dt.date(2027, 4, 18), "DUE"),
                                ("GSTR4", "032027", dt.date(2027, 4, 30), "DUE")]


def test_alerts_due_and_s16_4(conn):
    set_today(conn, "2026-11-05")
    cid, g, _, staff = setup(conn)
    set_user(conn, staff)
    conn.execute("INSERT INTO filing(gstin, return_type, period, due_date) VALUES "
                 "(%s, 'GSTR1', '102026', '2026-11-11'), "     # due in 6 days
                 "(%s, 'GSTR3B', '102026', '2026-11-20'), "    # too far ahead
                 "(%s, 'GSTR1', '092026', '2026-10-11')", (g, g, g))
    conn.execute("UPDATE filing SET filed_on = '2026-10-10', status = 'FILED' "
                 "WHERE gstin = %s AND period = '092026'", (g,))
    s = make_party(conn, "AAAPA1111A", SUP)
    mar = make_period(conn, g, "032026")
    books(conn, g, s, "OLD-1", 700, date="2026-03-10")
    imp = import_2b(conn, mar, build_2b_payload(g, "032026", []), staff)
    reconcile(conn, imp, staff)
    propose(conn, result_id(conn, imp, "OLD-1"), "DEFER")
    rows = conn.execute("SELECT kind, ref, due_by FROM v_alerts WHERE gstin = %s "
                        "ORDER BY kind", (g,)).fetchall()
    assert rows == [("FILING_DUE", "GSTR1 102026", dt.date(2026, 11, 11)),
                    ("S16_4_NEAR", "OLD-1", dt.date(2026, 11, 30))]


def test_staff_b_cannot_see_client1_reports(conn):
    cid, g, pid, imp, staff_a, s = three_way(conn)
    cid2, _, _, staff_b = setup(conn, pan="AAACB2222B", name="Two")
    conn.execute("SELECT sp_refresh_party_score()")

    as_role(conn, "staff", staff_a)
    assert conn.execute("SELECT count(*) FROM v_period_totals WHERE period_id = %s",
                        (pid,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM v_party_score WHERE client_id = %s",
                        (cid,)).fetchone()[0] == 1
    conn.execute("RESET ROLE")

    as_role(conn, "staff", staff_b)
    assert conn.execute("SELECT count(*) FROM v_period_totals WHERE period_id = %s",
                        (pid,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM v_party_score WHERE client_id = %s",
                        (cid,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM v_client_board WHERE client_id = %s",
                        (cid,)).fetchone()[0] == 0
    conn.execute("RESET ROLE")


def test_client_board(conn):
    cid, g, pid, imp, staff, _ = three_way(conn)
    row = conn.execute(
        "SELECT client_name, gstin, period::text, state::text, itc_at_risk, "
        "last_import_at IS NOT NULL, assignee_names FROM v_client_board "
        "WHERE client_id = %s", (cid,)).fetchone()
    email = conn.execute("SELECT email FROM app_user WHERE user_id = %s",
                         (staff,)).fetchone()[0]
    assert row == ("One", g, "092026", "IN_REVIEW", D("700.00"), True, email)


def test_mv_hidden_and_filing_rls(conn):
    import psycopg
    import pytest

    c1, g1, _, staff_a = setup(conn)
    c2, g2, _, staff_b = setup(conn, pan="AAACB2222B", name="Two")
    gen(conn, g1)
    as_role(conn, "staff", staff_b)
    assert conn.execute("SELECT count(*) FROM filing WHERE gstin = %s",
                        (g1,)).fetchone()[0] == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with conn.transaction():
            conn.execute("SELECT count(*) FROM mv_party_score")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # RLS WITH CHECK
        with conn.transaction():
            conn.execute("INSERT INTO filing(gstin, return_type, period, due_date) "
                         "VALUES (%s, 'GSTR1', '072026', '2026-08-11')", (g1,))
    assert gen(conn, g2) == 6  # own client via the invoker procedure
    conn.execute("RESET ROLE")


def test_party_score_view_is_security_barrier(conn):
    opts = conn.execute("SELECT reloptions FROM pg_class WHERE oid = 'v_party_score'::regclass"
                        ).fetchone()[0]
    assert "security_barrier=true" in (opts or [])


def test_party_score_no_leak_through_cheap_predicate(conn):
    """A caller's cheap function must not see other clients' MV rows before the filter."""
    cid, g, pid, imp, staff_a, s = three_way(conn)
    cid2, _, _, staff_b = setup(conn, pan="AAACB2222B", name="Two")
    conn.execute("SELECT sp_refresh_party_score()")
    as_role(conn, "staff", staff_b)
    conn.execute(
        "CREATE FUNCTION pg_temp.peek(c bigint) RETURNS boolean LANGUAGE plpgsql "
        "COST 0.0000001 AS $$ BEGIN PERFORM set_config('test.seen', "
        "coalesce(current_setting('test.seen', true), '') || ',' || c, true); "
        "RETURN true; END $$")
    rows = conn.execute("SELECT client_id FROM v_party_score WHERE pg_temp.peek(client_id)"
                        ).fetchall()
    seen = conn.execute("SELECT coalesce(current_setting('test.seen', true), '')").fetchone()[0]
    conn.execute("RESET ROLE")
    assert rows == []
    assert str(cid) not in [x for x in seen.split(",") if x]


def test_rule37_excludes_credit_notes(conn):
    set_today(conn, "2026-10-25")
    cid, g, _, _ = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    inv, fy = books(conn, g, s, "R-1", 1800, date="2026-04-01")
    conn.execute("INSERT INTO payment(invoice_id, fin_year, paid_on, amount) "
                 "VALUES (%s, %s, '2026-05-01', 11800)", (inv, fy))  # fully paid
    make_invoice(conn, g, s, "CN-1", "2026-04-02",
                 [dict(taxable_value=-1000, igst=-180)], pos="07", doc_type="CRN")
    assert conn.execute("SELECT count(*) FROM v_rule37 WHERE client_id = %s AND "
                        "invoice_no = 'CN-1'", (cid,)).fetchone()[0] == 0
