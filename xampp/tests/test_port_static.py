"""Checks on the MariaDB port that need no MariaDB server.

The real acceptance test is `python xampp/check.py` against XAMPP. These catch the
mistakes that would stop the files loading at all: PostgreSQL-only syntax, features
newer than MariaDB 10.4 (what XAMPP 8 ships), and drift from the PostgreSQL rules.
"""
import re
from pathlib import Path

import pytest

from xampp import rules

HERE = Path(__file__).resolve().parents[1]
SQL_FILES = ["schema.sql", "logic.sql", "seed.sql"]


def sql_text(name):
    text = (HERE / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)      # block comments
    return "\n".join(line.split("-- ")[0] for line in text.splitlines())  # line comments


BANNED = {
    "JSON_TABLE": r"\bJSON_TABLE\b",             # MariaDB 10.6
    "RETURNING": r"\bRETURNING\b",               # INSERT ... RETURNING is 10.5
    "dollar quoting": r"AS \$\$|\$\$\s*LANGUAGE|\$function\$",
    ":: casts": r"::",
    "ILIKE": r"\bILIKE\b",
    "SERIAL": r"\bSERIAL\b",
    "TIMESTAMPTZ": r"\bTIMESTAMPTZ\b",
    "jsonb": r"\bjsonb\b",
    "plpgsql": r"\bplpgsql\b",
    "RAISE EXCEPTION": r"\bRAISE\b",
    "GENERATED ALWAYS AS IDENTITY": r"AS IDENTITY",
    "ON CONFLICT": r"\bON CONFLICT\b",
    "DISTINCT ON": r"\bDISTINCT ON\b",
    "CREATE POLICY": r"\bCREATE POLICY\b",
    "FILTER (WHERE": r"\bFILTER\s*\(\s*WHERE",
    "boolean literal casts": r"'t'::|'f'::",
}


@pytest.mark.parametrize("name", SQL_FILES)
@pytest.mark.parametrize("label", sorted(BANNED))
def test_sql_avoids_newer_features(name, label):
    hits = [m.group(0) for m in re.finditer(BANNED[label], sql_text(name), flags=re.I)]
    assert not hits, f"{name} uses {label}: {hits[:3]}"


def test_routines_use_delimiter_blocks():
    """phpMyAdmin and the mysql client need DELIMITER around every routine body."""
    text = sql_text("logic.sql")
    routines = re.findall(r"CREATE\s+(?:DEFINER\S*\s+)?(PROCEDURE|FUNCTION|TRIGGER)\s+(\w+)", text, re.I)
    assert len(routines) >= 15
    assert text.count("DELIMITER $$") == text.count("DELIMITER ;") >= 1
    for kind, name in routines:
        body = text[text.index(name):]
        assert "END$$" in body.split("CREATE", 1)[0] + body, name


def table_names():
    return re.findall(r"CREATE TABLE (\w+)", sql_text("schema.sql"))


def test_every_table_has_a_primary_key_and_innodb():
    text = sql_text("schema.sql")
    for t in table_names():
        block = text[text.index(f"CREATE TABLE {t}"):].split(";", 1)[0]
        assert "PRIMARY KEY" in block, t
        assert "ENGINE=InnoDB" in block, t


def test_expected_tables():
    assert set(table_names()) == {
        "firm", "client", "gstin_reg", "party", "app_user", "staff_assignment",
        "period_transition", "return_period", "return_calendar", "filing",
        "invoice", "invoice_line", "books_staging", "gstr2b_import", "gstr2b_entry",
        "recon_result", "review", "followup", "audit_log"}


EXPECTED_ROUTINES = {
    "gstin_checksum_ok", "norm_doc_no", "fin_year_of", "sp_open_period", "sp_import_books",
    "sp_begin_2b_import", "sp_add_2b_entry", "sp_reconcile", "sp_propose", "sp_approve",
    "sp_submit_period", "sp_client_approve", "sp_close_period", "sp_reopen_period",
}


def test_expected_routines_exist():
    text = sql_text("logic.sql")
    found = set(re.findall(r"CREATE\s+(?:PROCEDURE|FUNCTION)\s+(\w+)", text, re.I))
    assert EXPECTED_ROUTINES <= found, EXPECTED_ROUTINES - found


@pytest.mark.parametrize("message", [
    "not your client", "period is closed", "client approval missing",
    "reviews awaiting approval", "maker cannot approve own review",
    "only a partner can approve", "invalid GSTIN", "illegal state transition",
    "reopen reason required", "invoice of a closed period cannot change"])
def test_guard_messages_match_postgres(message):
    assert f"MESSAGE_TEXT = '{message}" in sql_text("logic.sql")


def test_checksum_alphabet_matches_postgres():
    assert "'0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'" in sql_text("logic.sql")


# --- the Python mirrors of the SQL rules (check.py compares them on the server) ----

VALID = ["07ABCDE1234F1Z2", "07EMUFB1186R1ZS", "27AAPFU0939F1ZV", "29AAGCB7383J1Z4"]
INVALID = ["07ABCDE1234F1Z9", "07ABCDE1234F1Z", "07abcde1234f1z2!", ""]


@pytest.mark.parametrize("g", VALID)
def test_gstin_ok_accepts_valid(g):
    assert rules.gstin_ok(g)


@pytest.mark.parametrize("g", INVALID)
def test_gstin_ok_rejects_invalid(g):
    assert not rules.gstin_ok(g)


@pytest.mark.parametrize("raw,norm", [
    ("INV-104", "INV104"), ("inv/0104", "INV104"), ("INV-104/26-27", "INV104"),
    ("2026-27/INV/7", "INV7"), ("INV-26-28", "INV2628"), ("0007", "7"), ("---", "0"),
    ("A-007-B", "A7B"), ("INV 2026-2027 009", "INV9")])
def test_norm_doc_no_matches_postgres(raw, norm):
    assert rules.norm_doc_no(raw) == norm


@pytest.mark.parametrize("d,fy", [("2026-04-01", "2026-27"), ("2026-03-31", "2025-26"),
                                  ("2027-01-15", "2026-27")])
def test_fin_year_of(d, fy):
    assert rules.fin_year_of(d) == fy


# --- Task 2: the CLI's pure helpers --------------------------------------------------

def test_split_sql_keeps_routines_whole():
    from xampp.reconcile_cli import split_sql
    stmts = split_sql((HERE / "logic.sql").read_text(encoding="utf-8"))
    routines = [s for s in stmts if re.match(r"CREATE\s+(PROCEDURE|FUNCTION|TRIGGER)", s, re.I)]
    expected = len(re.findall(r"^CREATE\s+(?:PROCEDURE|FUNCTION|TRIGGER)", sql_text("logic.sql"), re.I | re.M))
    assert len(routines) == expected
    assert all(s.rstrip().endswith("END") for s in routines)
    assert not any(s.upper().startswith("DELIMITER") for s in stmts)
    assert any(s.startswith("CREATE OR REPLACE VIEW v_queue") for s in stmts)


def test_split_sql_drops_database_lines_for_another_db():
    from xampp.reconcile_cli import split_sql
    stmts = split_sql((HERE / "schema.sql").read_text(encoding="utf-8"), skip_database=True)
    assert not any(s.upper().startswith(("CREATE DATABASE", "USE ")) for s in stmts)
    assert sum(s.startswith("CREATE TABLE") for s in stmts) == 19


def test_flatten_2b_counts_and_signs(tmp_path):
    import json
    from tools.synth import generate
    from xampp.reconcile_cli import flatten_2b
    m = generate(clients=1, docs=60, period="092026", seed=7, out=str(tmp_path))[0]
    rows = flatten_2b(json.loads((tmp_path / m["gstr2b_file"]).read_text()))
    assert len(rows) == m["portal_docs"]
    assert {r["doc_type"] for r in rows} <= {"INV", "CRN", "DBN"}
    crn = [r for r in rows if r["doc_type"] == "CRN"]
    assert crn and all(r["txval"] < 0 for r in crn)
    assert set(rows[0]) == {"ctin", "trdnm", "inum", "dt", "doc_type", "txval", "igst", "cgst", "sgst", "cess"}


def test_parse_books_csv_matches_staging_columns(tmp_path):
    from tools.synth import generate
    from xampp.reconcile_cli import STAGING_COLUMNS, parse_books_csv
    m = generate(clients=1, docs=20, period="092026", seed=7, out=str(tmp_path))[0]
    rows = parse_books_csv((tmp_path / m["books_file"]).read_text(encoding="utf-8-sig"))
    assert rows and set(rows[0]) == set(STAGING_COLUMNS)


def test_compare_with_truth():
    from xampp.check import compare_with_truth
    truth = [{"books_invoice_no": "INV-1", "portal_invoice_no": "INV-1", "perturbation": "EXACT", "expected_status": "EXACT"},
             {"books_invoice_no": "INV-2", "portal_invoice_no": "", "perturbation": "BOOKS_ONLY", "expected_status": "ONLY_IN_BOOKS"},
             {"books_invoice_no": "", "portal_invoice_no": "INV-3", "perturbation": "DUPLICATE", "expected_status": "DUPLICATE"},
             {"books_invoice_no": "INV-4", "portal_invoice_no": "INV-04X", "perturbation": "TYPO", "expected_status": "PROBABLE"}]
    books = {"INV-1": "EXACT", "INV-2": "ONLY_IN_BOOKS", "INV-4": "ONLY_IN_BOOKS"}
    portal = {"INV-1": ["EXACT"], "INV-3": ["EXACT", "DUPLICATE"], "INV-04X": ["ONLY_IN_2B"]}
    ok, wrong, skipped = compare_with_truth(truth, books, portal)
    assert (ok, wrong, skipped) == (3, [], 1)
    ok, wrong, skipped = compare_with_truth(truth, dict(books, **{"INV-2": "EXACT"}), portal)
    assert ok == 2 and wrong[0][0] == "INV-2"


def test_cli_help_lists_commands(capsys):
    from xampp.reconcile_cli import main
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for cmd in ("init", "load-month", "reconcile", "queue", "totals", "decide", "approve",
                "submit", "client-approve", "close", "reopen"):
        assert cmd in out, cmd


# --- final review fixes ------------------------------------------------------------------

def test_date_window_matches_postgres():
    """C1: synth shifts dates by up to 5 days and expects ROUNDING; PG uses a 10-day window."""
    pg = (HERE.parent / "db" / "migrations" / "003_tenancy.sql").read_text()
    pg_default = re.search(r"date_window_days\s+int[^,]*DEFAULT (\d+)", pg, re.I).group(1)
    port = re.search(r"date_window_days\s+TINYINT UNSIGNED NOT NULL DEFAULT (\d+)", sql_text("schema.sql")).group(1)
    assert port == pg_default == "10"


def test_connect_forces_a_mariadb_collation(monkeypatch):
    """C2: newer mysql-connector defaults to utf8mb4_0900_ai_ci, which MariaDB 10.4 lacks."""
    import sys
    import types
    from xampp import reconcile_cli as cli
    seen = {}
    fake = types.ModuleType("mysql.connector")
    fake.Error = Exception
    fake.connect = lambda **kw: seen.update(kw) or object()
    pkg = types.ModuleType("mysql")
    pkg.connector = fake
    monkeypatch.setitem(sys.modules, "mysql", pkg)
    monkeypatch.setitem(sys.modules, "mysql.connector", fake)
    cli.connect(types.SimpleNamespace(host="h", port=1, user="u", password="", db="d"))
    assert seen["charset"] == "utf8mb4" and seen["collation"] == "utf8mb4_unicode_ci"
    assert seen["use_pure"] is True


def test_invoice_line_has_no_cgst_equals_sgst_check():
    """I3: CHECK (cgst = sgst) makes cgst -> sgst an FD (not BCNF), and the generator's
    odd-paise splits (₹0.01 apart) would be refused."""
    assert "cgst = sgst" not in sql_text("schema.sql")


def test_rate_check_allows_current_gst_rates():
    """I8: 40% (from 22 Sept 2025), 1.5%, 0.1% and 6% are valid rates."""
    m = re.search(r"CHECK \(rate IN \(([^)]*)\)\)", sql_text("schema.sql"))
    rates = {float(x) for x in m.group(1).split(",")}
    assert {0, 0.1, 0.25, 1.5, 3, 5, 6, 12, 18, 28, 40} <= rates


def test_books_import_does_not_use_insert_ignore_for_invoices():
    """I6: INSERT IGNORE also hides CHECK/FK/date errors; duplicates are filtered explicitly."""
    assert "INSERT IGNORE INTO invoice " not in sql_text("logic.sql")


def test_views_are_not_granted_to_app_roles():
    """I5: the views have no tenancy filter, so app roles read them only through procedures."""
    text = sql_text("logic.sql")
    assert not re.search(r"GRANT SELECT ON v_\w+ TO", text)
    for proc in ("sp_queue", "sp_totals"):
        assert re.search(rf"CREATE PROCEDURE {proc}\(", text), proc
        assert f"GRANT EXECUTE ON PROCEDURE {proc} TO" in text


def test_demo_commands_use_seeded_data():
    """Minor #5 (re-graded): README commands must work right after `init`."""
    readme = (HERE / "README.md").read_text()
    assert "07EMUFB1186R1ZS" not in readme
