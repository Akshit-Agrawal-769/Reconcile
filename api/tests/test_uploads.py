import json

import pytest

from api.db import ApiError
from api.tests.conftest import login
from api.uploads import BOOKS_HEADER, parse_books_csv, validate_2b
from db.tests.factories import build_2b_payload, make_period

HDR = ",".join(BOOKS_HEADER)
ROW = "07ABCDE1234F1Z2,Alpha,Invoice,INV-1,10-09-2026,07,N,8471,18,\"1,00,000.00\",0,9000,9000,0,Yes"


@pytest.fixture
def pw(world, conn):
    world["pid"] = make_period(conn, world["g1"], "092026")
    return world


def two_b(w, rtnprd="092026"):
    return json.dumps(build_2b_payload(w["g1"], rtnprd, [
        dict(ctin=w["sup"], inum="INV-1", dt="10-09-2026", txval=100000, cgst=9000, sgst=9000)]))


def post_2b(client, w, content, who="staff_a_email", source=None, pid=None):
    data = {"source": source} if source else None
    return client.post(f"/api/periods/{pid or w['pid']}/uploads/2b", headers=login(client, w[who]),
                       files={"file": ("2b.json", content, "application/json")}, data=data)


def post_books(client, w, content, who="staff_a_email"):
    return client.post(f"/api/periods/{w['pid']}/uploads/books", headers=login(client, w[who]),
                       files={"file": ("books.csv", content, "text/csv")})


def job_count(conn):
    return conn.execute("SELECT count(*) FROM job_queue").fetchone()[0]


def test_upload_2b_ok_creates_job(client, pw, conn):
    r = post_2b(client, pw, two_b(pw), source="IMS")
    assert r.status_code == 201, r
    body = r.json()
    row = conn.execute("SELECT job_type, status, upload_id, params, created_by FROM job_queue "
                       "WHERE job_id = %s", (body["job_id"],)).fetchone()
    assert row == ("IMPORT_2B", "QUEUED", body["upload_id"], {"source": "IMS"}, pw["staff_a"])
    up = conn.execute("SELECT kind, filename, uploaded_by, length(sha256) FROM upload "
                      "WHERE upload_id = %s", (body["upload_id"],)).fetchone()
    assert up == ("GSTR2B", "2b.json", pw["staff_a"], 64)


def test_upload_2b_rejects_bad_json(client, pw, conn):
    """Review Focus 3: a malformed file is a 400 with a clear message and no job."""
    before = job_count(conn)
    r = post_2b(client, pw, "{not json")
    assert (r.status_code, r.json()) == (400, {"error": "file is not valid JSON"})
    r = post_2b(client, pw, b"\xff\xfe\x00bad")
    assert r.status_code == 400
    assert job_count(conn) == before


def test_upload_2b_missing_docdata_400(client, pw):
    r = post_2b(client, pw, json.dumps({"data": {"rtnprd": "092026"}}))
    assert (r.status_code, r.json()) == (400, {"error": "not a GSTR-2B file: data.docdata missing"})
    r = post_2b(client, pw, json.dumps([1, 2]))
    assert r.status_code == 400


def test_upload_2b_bad_source_400(client, pw):
    r = post_2b(client, pw, two_b(pw), source="PORTAL")
    assert r.status_code == 400
    assert "source" in r.json()["error"]


def test_upload_without_file_400(client, pw):
    r = client.post(f"/api/periods/{pw['pid']}/uploads/2b", headers=login(client, pw["staff_a_email"]),
                    data={"source": "IMS"})
    assert (r.status_code, r.json()) == (400, {"error": "choose a file to upload"})


def test_upload_duplicate_file_400(client, pw, conn):
    assert post_2b(client, pw, two_b(pw)).status_code == 201
    before = job_count(conn)
    r = post_2b(client, pw, two_b(pw))
    assert (r.status_code, r.json()) == (400, {"error": "this file was already uploaded for this period"})
    assert job_count(conn) == before


def test_upload_other_client_404(client, pw, conn):
    r = post_2b(client, pw, two_b(pw), who="staff_b_email")
    assert (r.status_code, r.json()) == (404, {"error": "period not found"})
    assert job_count(conn) == 0


def test_client_user_cannot_upload(client, pw):
    r = post_2b(client, pw, two_b(pw), who="client1_email")
    assert r.status_code == 403


def test_upload_closed_period_409(client, pw, conn):
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("UPDATE return_period SET state = 'CLOSED' WHERE period_id = %s", (pw["pid"],))
        conn.execute("SET LOCAL session_replication_role = origin")
    r = post_2b(client, pw, two_b(pw))
    assert (r.status_code, r.json()) == (409, {"error": "period is closed"})
    assert conn.execute("SELECT count(*) FROM upload").fetchone()[0] == 0


def test_upload_too_large_400(client, pw):
    big = '{"data": {"rtnprd": "092026", "docdata": {}, "pad": "' + "x" * (10 * 1024 * 1024) + '"}}'
    r = post_2b(client, pw, big)
    assert (r.status_code, r.json()) == (400, {"error": "file is larger than 10 MB"})


def test_upload_books_ok_stages_rows(client, pw, conn):
    r = post_books(client, pw, "﻿" + HDR + "\r\n" + ROW + "\r\n\r\n")
    assert r.status_code == 201, r
    body = r.json()
    assert body["rows"] == 1
    staged = conn.execute("SELECT invoice_number, invoice_date, taxable_value, central_tax_paid "
                          "FROM books_staging WHERE period_id = %s", (pw["pid"],)).fetchall()
    assert staged == [("INV-1", "10-09-2026", "100000.00", "9000")]
    assert conn.execute("SELECT job_type FROM job_queue WHERE job_id = %s",
                        (body["job_id"],)).fetchone()[0] == "IMPORT_BOOKS"


def test_upload_books_rejects_missing_header(client, pw, conn):
    """Review Focus 3: the message lists every missing column; no job is created."""
    hdr = HDR.replace("Invoice date,", "").replace(",Cess Paid", "")
    r = post_books(client, pw, hdr + "\n")
    assert (r.status_code, r.json()) == (400, {"error": "missing columns: Invoice date, Cess Paid"})
    assert job_count(conn) == 0


def test_upload_books_bad_date_row_number(client, pw):
    bad = ROW.replace("10-09-2026", "2026-09-10")
    r = post_books(client, pw, HDR + "\n" + ROW + "\n" + bad + "\n")
    assert r.status_code == 400
    assert r.json()["error"] == 'row 2: Invoice date "2026-09-10" is not DD-MM-YYYY'


def test_upload_books_bad_amount(client, pw):
    bad = ROW.replace(",9000,9000,", ",nine,9000,")
    r = post_books(client, pw, HDR + "\n" + bad + "\n")
    assert r.json()["error"] == 'row 1: Central Tax Paid "nine" is not a number'


def test_upload_books_empty_file(client, pw):
    r = post_books(client, pw, HDR + "\n")
    assert (r.status_code, r.json()) == (400, {"error": "the file has no invoice rows"})


def test_parse_books_csv_bom_and_whitespace():
    hdr = ",".join(f"  {h.lower()} " for h in BOOKS_HEADER)
    rows = parse_books_csv("﻿" + hdr + "\n" + ROW.replace("INV-1", " INV-1 ") + "\n,,,\n")
    assert len(rows) == 1
    assert rows[0]["invoice_number"] == "INV-1"
    assert rows[0]["taxable_value"] == "100000.00"
    assert rows[0]["cess_paid"] == "0"


def test_parse_books_blank_taxes_become_zero():
    rows = parse_books_csv(HDR + "\n" + ROW.replace(",0,9000,9000,0,", ",,9000,9000,,") + "\n")
    assert (rows[0]["integrated_tax_paid"], rows[0]["cess_paid"]) == ("0", "0")


def test_validate_2b():
    validate_2b({"data": {"rtnprd": "092026", "docdata": {}}})
    for bad, msg in (({}, "data missing"), ({"data": {"docdata": {}}}, "data.rtnprd missing"),
                     ({"data": {"rtnprd": "092026", "docdata": []}}, "data.docdata missing")):
        with pytest.raises(ApiError, match=msg):
            validate_2b(bad)


# --- final review: books batches can never poison a month (finding #1) --------------

def test_upload_books_bad_gstin_row_number(client, pw, conn):
    bad = ROW.replace("07ABCDE1234F1Z2", "07ABCDE1234F1Z9")
    r = post_books(client, pw, HDR + "\n" + ROW + "\n" + bad + "\n")
    assert (r.status_code, r.json()) == (
        400, {"error": 'row 2: GSTIN of Supplier "07ABCDE1234F1Z9" is not a valid GSTIN'})
    assert job_count(conn) == 0


def test_upload_books_bad_document_type(client, pw):
    r = post_books(client, pw, HDR + "\n" + ROW.replace(",Invoice,", ",Bill,") + "\n")
    assert r.json() == {"error": 'row 1: Document Type "Bill" must be Invoice, Credit Note or Debit Note'}


def test_upload_books_bad_itc_flag(client, pw):
    r = post_books(client, pw, HDR + "\n" + ROW[:-3] + "Maybe\n")
    assert r.json() == {"error": 'row 1: Eligible for ITC "Maybe" must be Yes, No, Ineligible or Blocked'}


def test_new_books_upload_replaces_unimported_rows(client, pw, conn):
    """Rows left in staging by an earlier upload (e.g. its job failed) are replaced, so
    they can never make the next import fail."""
    conn.execute("INSERT INTO books_staging(period_id, gstin_of_supplier, invoice_number, "
                 "document_type, invoice_date) VALUES (%s, 'BADGSTIN', 'OLD-1', 'Bill', 'x')",
                 (pw["pid"],))
    r = post_books(client, pw, HDR + "\n" + ROW + "\n")
    assert r.status_code == 201
    assert conn.execute("SELECT invoice_number FROM books_staging WHERE period_id = %s",
                        (pw["pid"],)).fetchall() == [("INV-1",)]


def test_reexported_books_skip_invoices_already_imported(client, pw, conn):
    from db.tests.factories import make_invoice, make_party
    make_party(conn, "ABCDE1234F")
    make_invoice(conn, pw["g1"], "07ABCDE1234F1Z2", "inv/1", "2026-09-10",
                 [dict(taxable_value=100000, cgst=9000, sgst=9000)], pos="07")
    row2 = ROW.replace("INV-1", "INV-2")
    r = post_books(client, pw, HDR + "\n" + ROW + "\n" + row2 + "\n")
    assert r.status_code == 201, r
    assert (r.json()["rows"], r.json()["skipped"]) == (1, 1)
    assert conn.execute("SELECT invoice_number FROM books_staging WHERE period_id = %s",
                        (pw["pid"],)).fetchall() == [("INV-2",)]


def test_malformed_csv_quote_is_400_not_500(client, pw):
    """Finding #4: an unclosed quote that runs past the csv field limit."""
    r = post_books(client, pw, HDR + "\n" + '07ABCDE1234F1Z2,"' + "x" * 140000 + "\n")
    assert r.status_code == 400
    assert r.json()["error"].startswith("row 1:")


def test_same_file_can_be_uploaded_again_after_its_job_failed(client, pw, conn):
    """Finding #8: after a failure the user retries with the same file."""
    first = post_2b(client, pw, two_b(pw)).json()
    conn.execute("UPDATE job_queue SET status = 'RUNNING', lease_until = now() WHERE job_id = %s",
                 (first["job_id"],))
    conn.execute("UPDATE job_queue SET status = 'FAILED', lease_until = NULL, "
                 "last_error = 'boom' WHERE job_id = %s", (first["job_id"],))
    again = post_2b(client, pw, two_b(pw))
    assert again.status_code == 201, again
    assert again.json()["upload_id"] == first["upload_id"]
    assert again.json()["job_id"] != first["job_id"]
