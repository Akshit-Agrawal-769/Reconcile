import psycopg
import pytest

from db.tests.factories import make_client, make_gstin, make_reg, make_user


@pytest.mark.parametrize("raw,expected", [
    ("DP-0089", "DP89"),
    ("dp/89", "DP89"),
    ("00089", "89"),
    ("INV/2026-27/0045", "INV45"),
    ("INV/26-27/45", "INV45"),
    ("---", "0"),
    ("INV/12-34", "INV1234"),
    ("INV/12-35", "INV1235"),
    ("123-45", "12345"),
    ("INV12-34567", "INV1234567"),
    ("INV/2026-2027/7", "INV7"),
    ("A/25-26", "A"),
    ("26-27/45", "45"),
    ("99-00/5", "5"),
])
def test_norm_doc_no(conn, raw, expected):
    assert conn.execute("SELECT norm_doc_no(%s)", (raw,)).fetchone()[0] == expected


def test_fin_year_of(conn):
    q = "SELECT fin_year_of(%s::date)"
    assert conn.execute(q, ("2026-03-31",)).fetchone()[0] == "2025-26"
    assert conn.execute(q, ("2026-04-01",)).fetchone()[0] == "2026-27"


def test_period_start(conn):
    assert str(conn.execute("SELECT period_start('092026')").fetchone()[0]) == "2026-09-01"


def test_client_defaults(conn):
    cid = make_client(conn)
    row = conn.execute(
        "SELECT rounding_tol, review_tol, approval_threshold, date_window_days "
        "FROM client WHERE client_id = %s", (cid,)).fetchone()
    assert [float(row[0]), float(row[1]), float(row[2]), row[3]] == [1.00, 10.00, 5000.00, 10]


def test_gstin_pan_mismatch(conn):
    cid = make_client(conn, pan="AAPFU0939F")
    other = make_gstin("ABCDE1234F")
    with pytest.raises(psycopg.errors.RaiseException, match="PAN mismatch"):
        with conn.transaction():
            conn.execute(
                "INSERT INTO gstin_reg(gstin, client_id, taxpayer_type, filing_freq, state_code, reg_date) "
                "VALUES (%s, %s, 'REGULAR', 'MONTHLY', '07', '2020-01-01')", (other, cid))
    assert make_reg(conn, cid)


def test_client_user_needs_client(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            make_user(conn, "client")


def test_norm_doc_no_distinguishes_non_fy(conn):
    q = "SELECT norm_doc_no(%s)"
    assert conn.execute(q, ("INV/12-34",)).fetchone()[0] != conn.execute(q, ("INV/12-35",)).fetchone()[0]
