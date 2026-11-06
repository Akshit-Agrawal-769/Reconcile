"""Every normalization claim the report makes, checked against the toolkit and the schema."""
import re
from pathlib import Path

import pytest

from report import relations as rel
from report.fd import candidate_keys, chase_lossless, closure, is_bcnf, minimal_cover, normal_form, preserves

SCHEMA = (Path(__file__).resolve().parents[2] / "xampp" / "schema.sql").read_text()


def schema_tables():
    """{table: (columns, [key column sets])} parsed from xampp/schema.sql."""
    out = {}
    for name, body in re.findall(r"CREATE TABLE (\w+) \((.*?)\n\) ENGINE", SCHEMA, re.S):
        cols, keys = [], []
        for line in body.splitlines():
            line = line.split("--")[0].strip()
            m = re.match(r"(PRIMARY KEY|UNIQUE KEY \w+) \(([^)]*)\)", line)
            if m:
                keys.append(frozenset(c.strip() for c in m.group(2).split(",")))
                continue
            m = re.match(r"([a-z_0-9]+)\s+(INT|BIGINT|SMALLINT|TINYINT|CHAR|VARCHAR|DATE|DATETIME|DECIMAL|ENUM|BOOLEAN)", line)
            if m:
                cols.append(m.group(1))
        out[name] = (cols, keys)
    return out


TABLES = schema_tables()


def test_every_schema_table_is_covered():
    assert set(rel.FINAL) == set(TABLES) - {"books_staging"}


@pytest.mark.parametrize("name", sorted(rel.FINAL))
def test_relation_matches_schema(name):
    r = rel.FINAL[name]
    cols, keys = TABLES[name]
    assert r.attrs == frozenset(cols) - r.derived, name
    logical_keys = {k for k in keys if k <= r.attrs and k not in r.unique_not_keys}
    assert set(candidate_keys(r.attrs, r.fds)) == logical_keys, name


@pytest.mark.parametrize("name", sorted(rel.FINAL))
def test_final_relation_is_bcnf(name):
    r = rel.FINAL[name]
    assert is_bcnf(r.attrs, r.fds), name


def test_worked_a_stages():
    w = rel.WORKED_A
    assert normal_form(w.attrs, w.fds) == "1NF"
    for stage, expected in (("2NF", {"2NF", "3NF", "BCNF"}), ("3NF", {"3NF", "BCNF"}), ("BCNF", {"BCNF"})):
        for name, attrs in w.stages[stage].items():
            nf = normal_form(attrs, rel.projected(w, attrs))
            assert nf in expected, (stage, name, nf)
    parts = list(w.stages["BCNF"].values())
    assert chase_lossless(w.attrs, parts, w.fds)[0]
    assert preserves(w.attrs, parts, w.fds) == (True, [])


def test_worked_a_2nf_stage_still_has_transitive_dependencies():
    """The text says 2NF leaves two transitive dependencies for 3NF to remove."""
    w = rel.WORKED_A
    not_3nf = [n for n, a in w.stages["2NF"].items() if normal_form(a, rel.projected(w, a)) == "2NF"]
    assert sorted(not_3nf) == ["INVOICE_HDR", "REGISTRATION"]


def test_worked_b_trade_off():
    w = rel.WORKED_B
    assert normal_form(w.stages["3NF"]["ASSIGNMENT"], rel.projected(w, w.stages["3NF"]["ASSIGNMENT"])) == "3NF"
    parts = list(w.stages["BCNF"].values())
    assert all(is_bcnf(p, rel.projected(w, p)) for p in parts)
    assert chase_lossless(w.attrs, parts, w.fds)[0]
    ok, lost = preserves(w.attrs, parts, w.fds)
    assert not ok and [(sorted(l), sorted(r)) for l, r in lost] == [
        (["client_gstin", "return_type"], ["staff_email"])]


def test_minimal_cover_rows_are_minimal():
    for name, r in rel.FINAL.items():
        cover = minimal_cover(r.fds)
        assert all(len(rhs) == 1 for _, rhs in cover), name
        for lhs, rhs in cover:
            assert rhs <= closure(lhs, r.fds)


def test_recon_result_keys_are_honest():
    """I2: the UNIQUE (import_id, invoice_id/entry_id) constraints contain NULLs, so they
    are not candidate keys; result_id is the only one."""
    r = rel.FINAL["recon_result"]
    assert candidate_keys(r.attrs, r.fds) == [frozenset({"result_id"})]


def test_chase_table_printed_in_letter_order():
    """I1: the 'after the chase' rows must be in the same A-N column order as 'before'."""
    from report.build import tables
    t = tables()
    start, end = t["a_chase_start"], t["a_chase_end"]
    letters = start[0][1:]
    sup_s = dict(zip(letters, start[3][1:]))
    sup_e = dict(zip(letters, end[3][1:]))
    assert start[3][0] == end[3][0] == "SUPPLIER"
    assert sup_s["D"] == sup_s["E"] == sup_e["D"] == sup_e["E"] == "a"
    for row_s, row_e in zip(start[1:], end[1:]):          # the chase only turns b into a
        assert all(a == "a" for s_, a in zip(row_s[1:], row_e[1:]) if s_ == "a")
