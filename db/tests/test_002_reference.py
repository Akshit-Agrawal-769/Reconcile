import psycopg
import pytest


def test_checksum_vectors(conn):
    f = lambda g: conn.execute("SELECT gstin_checksum_ok(%s)", (g,)).fetchone()[0]
    assert f("27AAPFU0939F1ZV") is True
    assert f("27AAPFU0939F1ZO") is False
    assert f("09AAACT2727Q1ZU") is True
    assert f("SHORT") is False
    assert f("27aapfu0939f1zv") is False


def test_domain_rejects(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("SELECT 'ABC'::gstin")
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute("SELECT '27AAPFU0939F1ZO'::gstin")
    assert conn.execute("SELECT '27AAPFU0939F1ZV'::gstin").fetchone()[0] == "27AAPFU0939F1ZV"


def test_composition_no_itc(conn):
    rows = dict(conn.execute("SELECT code, itc_allowed FROM taxpayer_type").fetchall())
    assert rows["COMPOSITION"] is False
    assert rows["REGULAR"] is True
    assert rows["SERVICE"] is True


def test_rate_on_by_date(conn):
    conn.execute("INSERT INTO hsn_sac(code, description, is_service) VALUES ('X1','t',false)")
    conn.execute(
        "INSERT INTO tax_rate(code, effective_from, effective_to, rate) VALUES "
        "('X1','2020-01-01','2025-09-21',12), ('X1','2025-09-22',NULL,5)"
    )
    r = lambda d: conn.execute("SELECT rate_on('X1', %s::date)", (d,)).fetchone()[0]
    assert r("2025-09-21") == 12
    assert r("2025-09-22") == 5
    assert r("2019-12-31") is None


def test_tax_rate_period_check(conn):
    conn.execute("INSERT INTO hsn_sac(code, description, is_service) VALUES ('X2','t',false)")
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute(
                "INSERT INTO tax_rate(code, effective_from, effective_to, rate) "
                "VALUES ('X2','2020-01-01','2020-01-01',5)"
            )


def test_filing_rules_composition(conn):
    rows = conn.execute(
        "SELECT return_type FROM filing_rule WHERE taxpayer_type='COMPOSITION'"
    ).fetchall()
    assert {r[0] for r in rows} == {"CMP08", "GSTR4"}
    n = lambda t: conn.execute(
        "SELECT count(*) FROM filing_rule WHERE taxpayer_type=%s", (t,)
    ).fetchone()[0]
    assert n("REGULAR") == 4 and n("SERVICE") == 4


def test_seed_8471(conn):
    assert conn.execute("SELECT rate_on('8471','2024-01-01')").fetchone()[0] == 18
    assert conn.execute("SELECT rate_on('8471','2025-10-01')").fetchone()[0] == 18
    assert conn.execute("SELECT count(*) FROM hsn_sac WHERE code IN ('8471','7308','998314','996511')").fetchone()[0] == 4
