import copy
import csv
import json
from pathlib import Path

import psycopg
import pytest

from db.tests.factories import (build_2b_payload, import_2b, make_client, make_gstin,
                                make_period, make_reg, make_user)

FIX = Path(__file__).parent / "fixtures"
BOOK_COLS = ["gstin_of_supplier", "supplier_name", "document_type", "invoice_number",
             "invoice_date", "place_of_supply", "reverse_charge", "hsn_sac", "rate",
             "taxable_value", "integrated_tax_paid", "central_tax_paid",
             "state_ut_tax_paid", "cess_paid", "eligible_for_itc"]


def setup(conn, period="092026"):
    cid = make_client(conn)
    g = make_reg(conn, cid)
    uid = make_user(conn, "staff")
    return g, make_period(conn, g, period), uid


def fixture_payload(g, rtnprd="092026"):
    p = json.loads((FIX / "gstr2b_small.json").read_text())
    p["data"]["gstin"] = g
    p["data"]["rtnprd"] = rtnprd
    return p


def test_open_period_idempotent(conn):
    g, pid, _ = setup(conn)
    assert make_period(conn, g) == pid
    assert conn.execute("SELECT fin_year, state FROM return_period WHERE period_id=%s",
                        (pid,)).fetchone() == ("2026-27", "OPEN")


def test_import_counts(conn):
    g, pid, uid = setup(conn)
    iid = import_2b(conn, pid, fixture_payload(g), uid)
    assert conn.execute("SELECT count(*) FROM gstr2b_entry WHERE import_id=%s", (iid,)).fetchone()[0] == 5
    rows = conn.execute("SELECT doc_type, taxable_value, fin_year FROM gstr2b_entry "
                        "WHERE import_id=%s AND section='cdnr'", (iid,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == "CRN" and rows[0][1] < 0 and rows[0][2] == "2026-27"
    # items[] summed
    assert conn.execute("SELECT taxable_value, cgst FROM gstr2b_entry WHERE import_id=%s "
                        "AND invoice_no='INV-001'", (iid,)).fetchone() == (10000, 900)
    assert conn.execute("SELECT orig_invoice_no FROM gstr2b_entry WHERE import_id=%s "
                        "AND section='b2ba'", (iid,)).fetchone()[0] == "INV-9"
    assert conn.execute("SELECT count(*) FROM party").fetchone()[0] >= 3


def test_import_versions(conn):
    g, pid, uid = setup(conn)
    a = import_2b(conn, pid, fixture_payload(g), uid)
    b = import_2b(conn, pid, fixture_payload(g), uid)
    rows = conn.execute("SELECT import_id, version, is_current FROM gstr2b_import "
                        "WHERE period_id=%s ORDER BY version", (pid,)).fetchall()
    assert rows == [(a, 1, False), (b, 2, True)]


def test_import_rtnprd_mismatch(conn):
    g, pid, uid = setup(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="rtnprd mismatch"):
        with conn.transaction():
            import_2b(conn, pid, fixture_payload(g, "082026"), uid)


def test_import_bad_source(conn):
    g, pid, uid = setup(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="invalid source"):
        with conn.transaction():
            import_2b(conn, pid, fixture_payload(g), uid, source="BOGUS")


def test_import_keeps_invalid_checksum_supplier(conn):
    g, pid, uid = setup(conn)
    payload = build_2b_payload(g, "092026", [
        {"ctin": "01AABCE2207R1Z5", "inum": "X1", "dt": "03-09-2026", "txval": 100, "igst": 18}])
    iid = import_2b(conn, pid, payload, uid)
    assert conn.execute("SELECT gstin_valid FROM gstr2b_entry WHERE import_id=%s", (iid,)).fetchone()[0] is False
    assert conn.execute("SELECT count(*) FROM party WHERE gstin='01AABCE2207R1Z5'").fetchone()[0] == 0


def test_import_moves_period_to_review(conn):
    g, pid, uid = setup(conn)
    import_2b(conn, pid, fixture_payload(g), uid)
    assert conn.execute("SELECT state FROM return_period WHERE period_id=%s", (pid,)).fetchone()[0] == "IN_REVIEW"


def load_csv(conn, pid, path_or_rows):
    for r in path_or_rows:
        conn.execute("INSERT INTO books_staging(period_id, " + ",".join(BOOK_COLS) + ") VALUES (%s,"
                     + ",".join(["%s"] * 15) + ")", (pid, *r))


def csv_rows(path):
    with open(path, newline="") as f:
        rd = csv.reader(f)
        next(rd)
        return list(rd)


def test_books_rate_rows_grouped(conn):
    g, pid, uid = setup(conn)
    s = make_gstin("ABCDE1234F", "07")
    load_csv(conn, pid, [
        [s, "Alpha", "Invoice", "B-1", "05-09-2026", "07", "N", "8471", "5", "1000", "0", "25", "25", "0", "Yes"],
        [s, "Alpha", "Invoice", "B-1", "05-09-2026", "07", "N", "8471", "18", "1000", "0", "90", "90", "0", "Yes"]])
    assert conn.execute("SELECT sp_import_books(%s, %s)", (pid, uid)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM invoice WHERE gstin=%s", (g,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM invoice_line").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM books_staging WHERE period_id=%s", (pid,)).fetchone()[0] == 0


def test_books_conventions(conn):
    g, pid, uid = setup(conn)
    s = make_gstin("ABCDE1234F", "07")
    load_csv(conn, pid, [
        [s, "Alpha", "Credit Note", "C-1", "06-09-2026", "07", "Y", "8471", "18", "500", "0", "45", "45", "0", "Blocked"]])
    assert conn.execute("SELECT sp_import_books(%s, %s)", (pid, uid)).fetchone()[0] == 1
    assert conn.execute("SELECT doc_type, is_rcm, direction, fin_year FROM invoice WHERE gstin=%s",
                        (g,)).fetchone() == ("CRN", True, "INWARD", "2026-27")
    assert conn.execute("SELECT taxable_value, cgst, itc_category FROM invoice_line").fetchone() == (-500, -45, "BLOCKED_17_5")
    assert conn.execute("SELECT count(*) FROM party WHERE gstin=%s", (s,)).fetchone()[0] == 1


def test_books_invalid_gstin(conn):
    g, pid, uid = setup(conn)
    load_csv(conn, pid, [
        ["01AABCE2207R1Z5", "Bad", "INV", "B-1", "05-09-2026", "07", "N", "8471", "18", "1000", "0", "90", "90", "0", "Yes"]])
    with pytest.raises(psycopg.errors.RaiseException, match="invalid supplier GSTIN in books"):
        with conn.transaction():
            conn.execute("SELECT sp_import_books(%s, %s)", (pid, uid))


def test_books_sample_csv_loads(conn):
    g, pid, uid = setup(conn)
    load_csv(conn, pid, csv_rows(FIX / "books_small.csv"))
    assert conn.execute("SELECT sp_import_books(%s, %s)", (pid, uid)).fetchone()[0] == 1
