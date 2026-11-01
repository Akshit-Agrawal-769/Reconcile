from decimal import Decimal as D

from db.tests.factories import (build_2b_payload, import_2b, make_client, make_gstin,
                                make_invoice, make_party, make_period, make_reg, make_user,
                                recon_rows, reconcile)

SUP = "27"  # supplier in another state -> igst


def setup(conn, period="092026"):
    cid = make_client(conn)
    g = make_reg(conn, cid)
    uid = make_user(conn, "staff")
    return g, make_period(conn, g, period), uid


def books(conn, g, sup, no, date="2026-09-10", igst=1800, **kw):
    return make_invoice(conn, g, sup, no, date, [dict(taxable_value=10000, igst=igst)],
                        pos="07", **kw)


def doc(sup, no, date="10-09-2026", igst=1800, **kw):
    return dict(ctin=sup, inum=no, dt=date, txval=10000, igst=igst, **kw)


def portal(conn, g, pid, uid, docs, rtnprd="092026"):
    return import_2b(conn, pid, build_2b_payload(g, rtnprd, docs), uid)


def test_fuzzy_typo(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV1234", date="2026-09-10")
    imp = portal(conn, g, pid, uid, [doc(s, "INV1243", date="13-09-2026")])
    assert reconcile(conn, imp, uid) == 1
    assert recon_rows(conn, imp) == [("PROBABLE", "FUZZY_NO", "INV1234", "INV1243", D("0"))]


def test_fuzzy_date_window(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV1234", date="2026-09-05")
    imp = portal(conn, g, pid, uid, [doc(s, "INV1243", date="20-09-2026")])
    reconcile(conn, imp, uid)
    assert sorted(r[0] for r in recon_rows(conn, imp)) == ["ONLY_IN_2B", "ONLY_IN_BOOKS"]


def test_fuzzy_mutual_best(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "A100")
    books(conn, g, s, "A101")
    imp = portal(conn, g, pid, uid, [doc(s, "A101"), doc(s, "A100X")])
    assert reconcile(conn, imp, uid) == 2
    assert recon_rows(conn, imp) == [
        ("EXACT", "EXACT", "A101", "A101", D("0")),
        ("PROBABLE", "FUZZY_NO", "A100", "A100X", D("0")),
    ]


def test_fuzzy_ambiguous_not_mutual_best(conn):
    # Two books invoices equally close to one entry: the entry's best pick is the lower
    # invoice_id by tie-break, and that invoice's best is the entry -> exactly one pair.
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "B10")
    books(conn, g, s, "B12")
    imp = portal(conn, g, pid, uid, [doc(s, "B11")])
    reconcile(conn, imp, uid)
    rows = recon_rows(conn, imp)
    assert [(r[0], r[2], r[3]) for r in rows] == [
        ("ONLY_IN_BOOKS", "B12", None), ("PROBABLE", "B10", "B11")]


def test_value_mismatch(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV-9", date="2026-09-01")
    imp = portal(conn, g, pid, uid, [doc(s, "INV-9", date="28-09-2026", igst=2000)])
    assert reconcile(conn, imp, uid) == 1
    assert recon_rows(conn, imp) == [("MISMATCH", "VALUE", "INV-9", "INV-9", D("-200.00"))]


def test_pan_pass(conn):
    g, pid, uid = setup(conn)
    b_party = make_party(conn, "ABCDE1234F", "07")
    p_party = make_party(conn, "ABCDE1234F", "27")
    assert b_party == make_gstin("ABCDE1234F", "07")
    make_invoice(conn, g, b_party, "P-77", "2026-09-10",
                 [dict(taxable_value=10000, cgst=900, sgst=900)])
    imp = portal(conn, g, pid, uid, [doc(p_party, "P-77", igst=1800)])
    assert reconcile(conn, imp, uid) == 1
    assert recon_rows(conn, imp) == [("PROBABLE", "PAN", "P-77", "P-77", D("0"))]


def test_pan_pass_total_beyond_tolerance(conn):
    g, pid, uid = setup(conn)
    b_party = make_party(conn, "ABCDE1234F", "07")
    p_party = make_party(conn, "ABCDE1234F", "27")
    make_invoice(conn, g, b_party, "P-77", "2026-09-10",
                 [dict(taxable_value=10000, cgst=900, sgst=900)])
    imp = portal(conn, g, pid, uid, [doc(p_party, "P-77", igst=1850)])
    reconcile(conn, imp, uid)
    assert sorted(r[0] for r in recon_rows(conn, imp)) == ["ONLY_IN_2B", "ONLY_IN_BOOKS"]


def test_cross_period(conn):
    g, pid, uid = setup(conn, "092026")
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "AUG-5", date="2026-08-20")
    imp = portal(conn, g, pid, uid, [doc(s, "AUG-5", date="20-08-2026")])
    assert reconcile(conn, imp, uid) == 1
    assert recon_rows(conn, imp) == [
        ("PERIOD_MISMATCH", "CROSS_PERIOD", "AUG-5", "AUG-5", D("0"))]


def test_cross_period_after_august_residue(conn):
    g, sep, uid = setup(conn, "092026")
    aug = make_period(conn, g, "082026")
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "AUG-5", date="2026-08-20")
    imp_aug = portal(conn, g, aug, uid, [], rtnprd="082026")
    reconcile(conn, imp_aug, uid)
    assert [r[0] for r in recon_rows(conn, imp_aug)] == ["ONLY_IN_BOOKS"]
    imp = portal(conn, g, sep, uid, [doc(s, "AUG-5", date="20-08-2026")])
    reconcile(conn, imp, uid)
    assert [(r[0], r[1]) for r in recon_rows(conn, imp)] == [("PERIOD_MISMATCH", "CROSS_PERIOD")]


def test_cross_period_skips_already_matched(conn):
    g, sep, uid = setup(conn, "092026")
    aug = make_period(conn, g, "082026")
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "AUG-5", date="2026-08-20")
    imp_aug = portal(conn, g, aug, uid, [doc(s, "AUG-5", date="20-08-2026")], rtnprd="082026")
    reconcile(conn, imp_aug, uid)
    assert [r[0] for r in recon_rows(conn, imp_aug)] == ["EXACT"]
    imp = portal(conn, g, sep, uid, [doc(s, "AUG-5", date="20-08-2026")])
    reconcile(conn, imp, uid)
    assert [(r[0], r[2], r[3]) for r in recon_rows(conn, imp)] == [("ONLY_IN_2B", None, "AUG-5")]


def test_cross_period_limit_18_months(conn):
    g, pid, uid = setup(conn, "092026")
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "OLD-1", date="2025-02-28")  # before 2025-03-01 = Sep 2026 - 18 months
    imp = portal(conn, g, pid, uid, [doc(s, "OLD-1", date="28-02-2025")])
    reconcile(conn, imp, uid)
    assert [r[0] for r in recon_rows(conn, imp)] == ["ONLY_IN_2B"]


def test_cross_period_excludes_rcm(conn):
    g, pid, uid = setup(conn, "092026")
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "AUG-R", date="2026-08-20", is_rcm=True)
    imp = portal(conn, g, pid, uid, [doc(s, "AUG-R", date="20-08-2026")])
    reconcile(conn, imp, uid)
    assert [r[0] for r in recon_rows(conn, imp)] == ["ONLY_IN_2B"]


def test_cross_fy_period_mismatch(conn):
    g, pid, uid = setup(conn, "042026")
    s = make_party(conn, "AAAPA1111A", SUP)
    _, fy = books(conn, g, s, "MAR-28", date="2026-03-28")
    assert fy == "2025-26"
    imp = portal(conn, g, pid, uid, [doc(s, "MAR-28", date="28-03-2026")], rtnprd="042026")
    assert reconcile(conn, imp, uid) == 1
    assert recon_rows(conn, imp) == [
        ("PERIOD_MISMATCH", "CROSS_PERIOD", "MAR-28", "MAR-28", D("0"))]


def test_naive_vs_multipass(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "DP-0089")
    imp = portal(conn, g, pid, uid, [doc(s, "89")])
    naive = conn.execute(
        "SELECT books_no, portal_no, paired FROM v_naive_match WHERE import_id = %s "
        "ORDER BY books_no NULLS LAST", (imp,)).fetchall()
    assert naive == [("DP-0089", None, False), (None, "89", False)]
    reconcile(conn, imp, uid)
    assert recon_rows(conn, imp) == [("PROBABLE", "FUZZY_NO", "DP-0089", "89", D("0"))]


def test_naive_pairs_identical_numbers(conn):
    g, pid, uid = setup(conn)
    s = make_party(conn, "AAAPA1111A", SUP)
    books(conn, g, s, "INV-1")
    imp = portal(conn, g, pid, uid, [doc(s, "INV-1")])
    assert conn.execute("SELECT books_no, portal_no, paired FROM v_naive_match "
                        "WHERE import_id = %s", (imp,)).fetchall() == [("INV-1", "INV-1", True)]
