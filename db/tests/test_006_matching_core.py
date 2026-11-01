from decimal import Decimal as D

import psycopg
import pytest

from db.tests.factories import (build_2b_payload, import_2b, make_client, make_invoice,
                                make_party, make_period, make_reg, make_user, recon_rows,
                                reconcile)

SUP = "27"  # supplier in another state -> igst


def setup(conn, ttype="REGULAR"):
    cid = make_client(conn)
    g = make_reg(conn, cid, ttype)
    uid = make_user(conn, "staff")
    return g, make_period(conn, g), uid


def books(conn, g, sup, no, date="2026-09-10", igst=1800, doc="INV", **kw):
    sign = -1 if doc == "CRN" else 1
    return make_invoice(conn, g, sup, no, date, [dict(taxable_value=sign * 10000, igst=sign * igst)],
                        doc_type=doc, pos="07", **kw)


def doc(sup, no, date="10-09-2026", igst=1800, **kw):
    return dict(ctin=sup, inum=no, dt=date, txval=10000, igst=igst, **kw)


def portal(conn, g, pid, uid, docs):
    return import_2b(conn, pid, build_2b_payload(g, "092026", docs), uid)


def test_exact(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV-1")
    imp = portal(conn, g, pid, uid, [doc(s, "INV-1")])
    assert reconcile(conn, imp, uid) == 1
    assert recon_rows(conn, imp) == [("EXACT", "EXACT", "INV-1", "INV-1", D("0"))]


def test_rounding(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV-1")
    imp = portal(conn, g, pid, uid, [doc(s, "INV-1", igst=1800.60)])
    reconcile(conn, imp, uid)
    assert recon_rows(conn, imp) == [("ROUNDING", "ROUNDING", "INV-1", "INV-1", D("-0.60"))]


def test_over_rounding_not_rounding(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV-1")
    imp = portal(conn, g, pid, uid, [doc(s, "INV-1", igst=1805)])
    # Beyond rounding_tol: pass 4 (VALUE, added in 007) pairs it as a MISMATCH.
    assert reconcile(conn, imp, uid) == 1
    assert recon_rows(conn, imp) == [("MISMATCH", "VALUE", "INV-1", "INV-1", D("-5"))]


def test_rounding_date_window(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV-1", date="2026-09-02")
    imp = portal(conn, g, pid, uid, [doc(s, "INV-1", date="08-09-2026")])
    reconcile(conn, imp, uid)
    assert [r[0] for r in recon_rows(conn, imp)] == ["ROUNDING"]


def test_residue_both_sides(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    # Numbers far apart so the FUZZY_NO pass (007) cannot pair them.
    books(conn, g, s, "BK-1001")
    imp = portal(conn, g, pid, uid, [doc(s, "PT-7777")])
    reconcile(conn, imp, uid)
    assert recon_rows(conn, imp) == [
        ("ONLY_IN_2B", "RESIDUE", None, "PT-7777", D("-1800")),
        ("ONLY_IN_BOOKS", "RESIDUE", "BK-1001", None, D("1800")),
    ]


def test_duplicate_entry(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV-1")
    imp = portal(conn, g, pid, uid, [doc(s, "INV-1"), doc(s, "INV-1")])
    assert reconcile(conn, imp, uid) == 2
    rows = recon_rows(conn, imp)
    assert [(r[0], r[1]) for r in rows] == [("DUPLICATE", "PREFLIGHT"), ("EXACT", "EXACT")]
    assert rows[0][2] is None
    # the lowest entry_id is the one paired
    assert conn.execute(
        "SELECT (SELECT min(entry_id) FROM gstr2b_entry WHERE import_id=%s) = "
        "(SELECT entry_id FROM recon_result WHERE import_id=%s AND status='EXACT')",
        (imp, imp)).fetchone()[0]


def test_credit_note_never_pairs_with_invoice(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "5", doc="CRN")
    imp = portal(conn, g, pid, uid, [doc(s, "5")])
    reconcile(conn, imp, uid)
    assert sorted(r[0] for r in recon_rows(conn, imp)) == ["ONLY_IN_2B", "ONLY_IN_BOOKS"]


def test_credit_note_pairs_with_credit_note(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "5", doc="CRN")
    imp = portal(conn, g, pid, uid,
                 [dict(ctin=s, inum="5", dt="10-09-2026", txval=10000, igst=1800,
                       section="cdnr", ntty="C")])
    reconcile(conn, imp, uid)
    assert recon_rows(conn, imp) == [("EXACT", "EXACT", "5", "5", D("0"))]


def test_same_number_other_supplier_not_paired(conn):
    g, pid, uid = setup(conn)
    a = make_party(conn, "AAAPA1111A", SUP)
    b = make_party(conn, "BBBPB2222B", SUP)
    books(conn, g, a, "101")
    imp = portal(conn, g, pid, uid, [doc(b, "101")])
    reconcile(conn, imp, uid)
    assert sorted(r[0] for r in recon_rows(conn, imp)) == ["ONLY_IN_2B", "ONLY_IN_BOOKS"]


def test_rcm_excluded(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    inv, _ = books(conn, g, s, "R-1", is_rcm=True)
    imp = portal(conn, g, pid, uid, [])
    assert reconcile(conn, imp, uid) == 0
    assert conn.execute("SELECT period_id, invoice_id, reason FROM v_excluded_itc").fetchall() \
        == [(pid, inv, "RCM")]


def test_import_and_blocked_excluded(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    i1, _ = books(conn, g, s, "I-1", is_import=True)
    i2, _ = make_invoice(conn, g, s, "B-1", "2026-09-10",
                         [dict(taxable_value=100, igst=18, itc_category="BLOCKED_17_5")], pos="07")
    i3, _ = make_invoice(conn, g, s, "M-1", "2026-09-10",
                         [dict(taxable_value=100, igst=18, itc_category="BLOCKED_17_5"),
                          dict(taxable_value=100, igst=18)], pos="07")
    imp = portal(conn, g, pid, uid, [])
    reconcile(conn, imp, uid)
    assert sorted(conn.execute("SELECT invoice_id, reason FROM v_excluded_itc").fetchall()) \
        == sorted([(i1, "IMPORT"), (i2, "BLOCKED_17_5")])
    assert [r[2] for r in recon_rows(conn, imp)] == ["M-1"]


def test_books_outside_period_not_candidate(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "O-1", date="2026-10-01")
    books(conn, g, s, "O-2", date="2026-08-31")
    imp = portal(conn, g, pid, uid, [])
    assert reconcile(conn, imp, uid) == 0


def test_composition_refused(conn):
    g, pid, uid = setup(conn, "COMPOSITION")
    imp = portal(conn, g, pid, uid, [])
    with pytest.raises(psycopg.errors.RaiseException, match="composition dealers cannot claim ITC"):
        with conn.transaction():
            reconcile(conn, imp, uid)


def test_empty_portal_side(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    for n in ("A", "B", "C"):
        books(conn, g, s, n)
    imp = portal(conn, g, pid, uid, [])
    assert reconcile(conn, imp, uid) == 3
    assert {r[0] for r in recon_rows(conn, imp)} == {"ONLY_IN_BOOKS"}


def test_rerun_rebuilds(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV-1")
    books(conn, g, s, "BK-2002")
    imp = portal(conn, g, pid, uid, [doc(s, "INV-1"), doc(s, "PT-3003")])
    n1 = reconcile(conn, imp, uid)
    first = recon_rows(conn, imp)
    n2 = reconcile(conn, imp, uid)
    assert n1 == n2 == 3 and recon_rows(conn, imp) == first


def test_one_to_one_constraints(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV-1")
    imp = portal(conn, g, pid, uid, [doc(s, "INV-1")])
    reconcile(conn, imp, uid)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            conn.execute("INSERT INTO recon_result(import_id, invoice_id, invoice_fy, status, matched_by) "
                         "SELECT import_id, invoice_id, invoice_fy, 'ONLY_IN_BOOKS', 'X' "
                         "FROM recon_result WHERE import_id=%s", (imp,))
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("INSERT INTO recon_result(import_id, status, matched_by) "
                         "VALUES (%s, 'ONLY_IN_BOOKS', 'X')", (imp,))


def test_fin_year_in_matching_key(conn):
    cid = make_client(conn)
    g = make_reg(conn, cid)
    uid = make_user(conn, "staff")
    pid = make_period(conn, g, "042026")
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "X/26-27/5", date="2026-04-10")
    imp = import_2b(conn, pid, build_2b_payload(g, "042026", [
        doc(s, "X/25-26/5", date="10-04-2025"), doc(s, "X/26-27/5", date="10-04-2026")]), uid)
    reconcile(conn, imp, uid)
    assert recon_rows(conn, imp) == [
        ("EXACT", "EXACT", "X/26-27/5", "X/26-27/5", D("0")),
        ("ONLY_IN_2B", "RESIDUE", None, "X/25-26/5", D("-1800")),
    ]
