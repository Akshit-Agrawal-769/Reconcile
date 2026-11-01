import psycopg
import pytest

from db.tests.factories import make_client, make_invoice, make_party, make_reg


def setup_pair(conn, ttype="REGULAR"):
    c = make_client(conn)
    g = make_reg(conn, c, ttype=ttype)
    p = make_party(conn)
    return g, p


def test_fin_year_and_partition(conn):
    g, p = setup_pair(conn)
    inv_id, fy = make_invoice(conn, g, p, "INV/26-27/045", "2026-09-10",
                              [{"taxable_value": 100, "cgst": 9, "sgst": 9}])
    assert fy == "2026-27"
    row = conn.execute(
        "SELECT invoice_no_norm, fin_year, tableoid::regclass::text FROM invoice "
        "WHERE invoice_id = %s", (inv_id,)).fetchone()
    assert row == ("INV45", "2026-27", "invoice_2026_27")


def test_fin_year_mismatch_rejected(conn):
    g, p = setup_pair(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute(
                "INSERT INTO invoice(gstin, party_gstin, direction, doc_type, invoice_no, "
                "invoice_date, fin_year, place_of_supply) "
                "VALUES (%s, %s, 'INWARD', 'INV', 'X1', '2026-09-10', '2025-26', '07')",
                (g, p))


def test_duplicate_invoice_rejected(conn):
    g, p = setup_pair(conn)
    make_invoice(conn, g, p, "INV/26-27/045", "2026-09-10",
                 [{"taxable_value": 100, "cgst": 9, "sgst": 9}])
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.transaction():
            make_invoice(conn, g, p, "inv-45", "2026-09-11",
                         [{"taxable_value": 100, "cgst": 9, "sgst": 9}])


def test_crn_sign(conn):
    g, p = setup_pair(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="credit note amounts must be negative"):
        with conn.transaction():
            make_invoice(conn, g, p, "CN1", "2026-09-10",
                         [{"taxable_value": 100}], doc_type="CRN")
    make_invoice(conn, g, p, "CN2", "2026-09-10",
                 [{"taxable_value": -100, "cgst": -9, "sgst": -9}], doc_type="CRN")


def test_intrastate_needs_cgst_sgst(conn):
    g, p = setup_pair(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="tax heads do not match place of supply"):
        with conn.transaction():
            make_invoice(conn, g, p, "A1", "2026-09-10",
                         [{"taxable_value": 100, "igst": 18}])


def test_interstate_needs_igst(conn):
    g, p = setup_pair(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="tax heads do not match place of supply"):
        with conn.transaction():
            make_invoice(conn, g, p, "A2", "2026-09-10",
                         [{"taxable_value": 100, "cgst": 9}], pos="27")
    make_invoice(conn, g, p, "A3", "2026-09-10", [{"taxable_value": 100, "igst": 18}], pos="27")


def test_import_skips_pos_check(conn):
    g, p = setup_pair(conn)
    make_invoice(conn, g, p, "IMP1", "2026-09-10",
                 [{"taxable_value": 100, "igst": 18}], is_import=True)


def test_outward_uses_own_state(conn):
    c = make_client(conn)
    g = make_reg(conn, c, state="07")
    client = make_party(conn, pan="ABCDE1234F", state="07")
    make_invoice(conn, g, client, "S1", "2026-09-10",
                 [{"taxable_value": 100, "cgst": 9, "sgst": 9}], direction="OUTWARD")
    with pytest.raises(psycopg.errors.RaiseException, match="tax heads do not match place of supply"):
        with conn.transaction():
            make_invoice(conn, g, client, "S2", "2026-09-10",
                         [{"taxable_value": 100, "igst": 18}], direction="OUTWARD")


def test_composition_blocks_itc(conn):
    g, p = setup_pair(conn, ttype="COMPOSITION")
    with pytest.raises(psycopg.errors.RaiseException, match="composition dealers cannot claim ITC"):
        with conn.transaction():
            make_invoice(conn, g, p, "C1", "2026-09-10",
                         [{"taxable_value": 100, "cgst": 9, "sgst": 9}])
    make_invoice(conn, g, p, "C2", "2026-09-10",
                 [{"taxable_value": 100, "cgst": 9, "sgst": 9, "itc_category": "INELIGIBLE"}])


def test_service_outward_needs_sac(conn):
    g, p = setup_pair(conn, ttype="SERVICE")
    with pytest.raises(psycopg.errors.RaiseException, match="service providers must use SAC codes"):
        with conn.transaction():
            make_invoice(conn, g, p, "V1", "2026-09-10",
                         [{"taxable_value": 100, "cgst": 9, "sgst": 9, "hsn_sac": "8471"}],
                         direction="OUTWARD")
    make_invoice(conn, g, p, "V2", "2026-09-10",
                 [{"taxable_value": 100, "cgst": 9, "sgst": 9, "hsn_sac": "998314"}],
                 direction="OUTWARD")


def test_rate_warning(conn):
    g, p = setup_pair(conn)
    inv_id, _ = make_invoice(conn, g, p, "R1", "2026-09-10",
                             [{"taxable_value": 100, "cgst": 6, "sgst": 6, "rate": 12},
                              {"taxable_value": 100, "cgst": 9, "sgst": 9, "rate": 18}])
    rows = conn.execute(
        "SELECT hsn_sac, rate, expected_rate FROM v_rate_warnings WHERE invoice_id = %s",
        (inv_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "8471" and float(rows[0][1]) == 12 and float(rows[0][2]) == 18


def test_invoice_total_view(conn):
    g, p = setup_pair(conn)
    inv_id, fy = make_invoice(conn, g, p, "T1", "2026-09-10",
                              [{"taxable_value": 100, "igst": 18.00},
                               {"taxable_value": 10, "igst": 1.80}], pos="27")
    row = conn.execute(
        "SELECT taxable_value, igst, total_tax FROM v_invoice_total "
        "WHERE invoice_id = %s AND fin_year = %s", (inv_id, fy)).fetchone()
    assert float(row[0]) == 110 and float(row[1]) == 19.80 and float(row[2]) == 19.80


def test_payment_references_invoice(conn):
    g, p = setup_pair(conn)
    inv_id, fy = make_invoice(conn, g, p, "P1", "2026-09-10",
                              [{"taxable_value": 100, "cgst": 9, "sgst": 9}])
    conn.execute("INSERT INTO payment(invoice_id, fin_year, paid_on, amount) "
                 "VALUES (%s, %s, '2026-10-01', 50)", (inv_id, fy))
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("INSERT INTO payment(invoice_id, fin_year, paid_on, amount) "
                         "VALUES (%s, %s, '2026-10-01', 0)", (inv_id, fy))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            conn.execute("INSERT INTO payment(invoice_id, fin_year, paid_on, amount) "
                         "VALUES (%s, '2025-26', '2026-10-01', 5)", (inv_id,))
