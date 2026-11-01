"""Task 12: synthetic data generator (tools/synth.py) and measured accuracy (v_accuracy)."""
import collections
import csv
import importlib.util
import json
import random
from pathlib import Path

from db.tests.factories import import_2b, make_period, make_user, default_firm

ROOT = Path(__file__).resolve().parents[2]
SEED, DOCS, PERIOD = 42, 500, "092026"

BOOK_COLS = ["gstin_of_supplier", "supplier_name", "document_type", "invoice_number",
             "invoice_date", "place_of_supply", "reverse_charge", "hsn_sac", "rate",
             "taxable_value", "integrated_tax_paid", "central_tax_paid",
             "state_ut_tax_paid", "cess_paid", "eligible_for_itc"]


def load_synth():
    spec = importlib.util.spec_from_file_location("synth", ROOT / "tools" / "synth.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def generate(tmp_path):
    synth = load_synth()
    out = tmp_path / "synth"
    synth.generate(clients=3, docs=DOCS, period=PERIOD, seed=SEED, out=out)
    return synth, out


def read_truth(out):
    with open(out / "truth.csv", newline="") as f:
        return list(csv.DictReader(f))


def test_generated_gstins_valid(conn):
    synth = load_synth()
    rng = random.Random(7)
    states = ["01", "07", "09", "19", "24", "27", "29", "33", "36", "37"]
    gstins = [synth.generate_gstin(rng.choice(states), synth.random_pan(rng))
              for _ in range(100)]
    assert len(gstins) == 100
    for g in gstins:
        assert conn.execute("SELECT gstin_checksum_ok(%s)", (g,)).fetchone()[0], g
        # the DB domain accepts it too
        conn.execute("SELECT %s::gstin", (g,))


def test_mix_within_tolerance(tmp_path):
    synth, out = generate(tmp_path)
    manifest = json.loads((out / "clients.json").read_text())
    assert sorted(c["taxpayer_type"] for c in manifest) == ["COMPOSITION", "REGULAR", "SERVICE"]
    truth = read_truth(out)
    reconciled = [c for c in manifest if c["taxpayer_type"] != "COMPOSITION"]
    comp = [c for c in manifest if c["taxpayer_type"] == "COMPOSITION"][0]
    # the composition client gets books only and no labels
    assert (out / f"books_{comp['gstin']}.csv").exists()
    assert not (out / f"gstr2b_{comp['gstin']}.json").exists()
    assert not any(t["gstin"] == comp["gstin"] for t in truth)
    for c in reconciled:
        assert (out / f"books_{c['gstin']}.csv").exists()
        assert (out / f"gstr2b_{c['gstin']}.json").exists()
        # one row per document; DUPLICATE docs add a second (portal-only) DUPLICATE row
        docs = [t for t in truth if t["gstin"] == c["gstin"]
                and t["expected_status"] != "DUPLICATE"]
        counts = collections.Counter(t["perturbation"] for t in docs)
        assert sum(counts.values()) == DOCS
        for name, share in synth.MIX.items():
            assert abs(counts[name] - share * DOCS) <= 3, (name, counts[name])
        dups = [t for t in truth if t["gstin"] == c["gstin"]
                and t["expected_status"] == "DUPLICATE"]
        assert len(dups) == counts["DUPLICATE"]
        crn = [t for t in docs
               if (t["books_invoice_no"] or t["portal_invoice_no"]).upper().startswith("CN")]
        assert abs(len(crn) - 0.10 * DOCS) <= 3


def test_same_seed_same_output(tmp_path):
    synth = load_synth()
    synth.generate(clients=3, docs=50, period=PERIOD, seed=SEED, out=tmp_path / "a")
    synth.generate(clients=3, docs=50, period=PERIOD, seed=SEED, out=tmp_path / "b")
    for f in sorted((tmp_path / "a").iterdir()):
        assert f.read_bytes() == (tmp_path / "b" / f.name).read_bytes(), f.name


def load_books(conn, period_id, path, user_id):
    with open(path, newline="") as f:
        rd = csv.reader(f)
        next(rd)
        rows = list(rd)
    cols = ", ".join(BOOK_COLS)
    ph = "(%s, " + ", ".join(["%s"] * len(BOOK_COLS)) + ")"
    for i in range(0, len(rows), 200):
        chunk = rows[i:i + 200]
        params = [v for r in chunk for v in [period_id, *r]]
        conn.execute(f"INSERT INTO books_staging(period_id, {cols}) VALUES "
                     + ", ".join([ph] * len(chunk)), params)
    return conn.execute("SELECT sp_import_books(%s, %s)", (period_id, user_id)).fetchone()[0]


def load_all(conn, out):
    """Create clients from clients.json, import books and 2B, reconcile, load truth."""
    manifest = json.loads((out / "clients.json").read_text())
    firm = default_firm(conn)
    uid = make_user(conn, "partner")
    conn.execute("SELECT set_config('app.user_id', %s, true)", (str(uid),))
    imports = {}
    for c in manifest:
        cid = conn.execute(
            "INSERT INTO client(firm_id, name, pan) VALUES (%s, %s, %s) RETURNING client_id",
            (firm, c["legal_name"], c["pan"])).fetchone()[0]
        conn.execute(
            "INSERT INTO gstin_reg(gstin, client_id, taxpayer_type, filing_freq, state_code, "
            "reg_date) VALUES (%s, %s, %s, %s, %s, '2020-01-01')",
            (c["gstin"], cid, c["taxpayer_type"], c.get("filing_freq", "MONTHLY"), c["state"]))
        pid = make_period(conn, c["gstin"], PERIOD)
        n = load_books(conn, pid, out / c["books_file"], uid)
        assert n > 0
        if c["taxpayer_type"] != "COMPOSITION":
            payload = json.loads((out / c["gstr2b_file"]).read_text())
            imports[c["gstin"]] = import_2b(conn, pid, payload, uid)
    for g, imp in imports.items():
        conn.execute("SELECT sp_reconcile(%s, %s)", (imp, uid))
    truth = read_truth(out)
    for i in range(0, len(truth), 200):
        chunk = truth[i:i + 200]
        params = [v for t in chunk for v in (t["gstin"], t["books_invoice_no"] or None,
                                             t["portal_invoice_no"] or None,
                                             t["perturbation"], t["expected_status"])]
        conn.execute("INSERT INTO synth_truth(gstin, books_invoice_no, portal_invoice_no, "
                     "perturbation, expected_status) VALUES "
                     + ", ".join(["(%s, %s, %s, %s, %s)"] * len(chunk)), params)
    return manifest, imports, uid


def accuracy(conn):
    return conn.execute(
        "SELECT expected_status::text, predicted_status::text, n, precision_pct, recall_pct "
        "FROM v_accuracy ORDER BY expected_status, predicted_status").fetchall()


def print_table(rows):
    print("\nexpected | predicted | n | precision_pct | recall_pct")
    for r in rows:
        print(" | ".join("" if v is None else str(v) for v in r))


def test_accuracy_targets(conn, tmp_path):
    _, out = generate(tmp_path)
    manifest, imports, _ = load_all(conn, out)
    assert len(imports) == 2
    rows = accuracy(conn)
    print_table(rows)
    diag = {e: (p, r) for e, pr, n, p, r in rows if e == pr}
    for status in ("EXACT", "ROUNDING", "PROBABLE"):
        assert status in diag, status
        assert diag[status][0] >= 95, (status, diag[status])
        assert diag[status][1] >= 95, (status, diag[status])   # recall
    # every truth row is accounted for exactly once
    total = conn.execute("SELECT count(*) FROM synth_truth").fetchone()[0]
    assert sum(r[2] for r in rows) == total
    # ... and the matcher produced no unpaired / spurious results for the truth GSTINs
    results = conn.execute(
        "SELECT count(*) FROM recon_result r "
        "JOIN gstr2b_import gi ON gi.import_id = r.import_id AND gi.is_current "
        "JOIN return_period p ON p.period_id = gi.period_id "
        "WHERE p.gstin IN (SELECT gstin FROM synth_truth)").fetchone()[0]
    assert results == total

    # the composition client is loaded but not reconciled
    comp = [c for c in manifest if c["taxpayer_type"] == "COMPOSITION"][0]
    assert comp["gstin"] not in imports
    # all its books lines were loaded as INELIGIBLE
    cats = conn.execute(
        "SELECT DISTINCT l.itc_category::text FROM invoice i JOIN invoice_line l "
        "ON l.invoice_id = i.invoice_id AND l.fin_year = i.fin_year WHERE i.gstin = %s",
        (comp["gstin"],)).fetchall()
    assert cats == [("INELIGIBLE",)]


def test_accuracy_view_pairs_duplicates_one_to_one(conn):
    """Hand-built case: a doc listed twice on the portal pairs EXACT + DUPLICATE."""
    from db.tests.factories import (build_2b_payload, make_client, make_party, make_reg)
    cid = make_client(conn, "AAPFU0939F", "Acc")
    g = make_reg(conn, cid)
    s = make_party(conn, "ABCDE1234F", "07")
    uid = make_user(conn, "partner")
    pid = make_period(conn, g)
    conn.execute(
        "INSERT INTO books_staging(period_id, " + ", ".join(BOOK_COLS) + ") VALUES "
        "(%s, %s, 'A', 'Invoice', 'INV-100010', '05-09-2026', '07', 'N', '8471', '18', "
        "'1000.00', '0', '90.00', '90.00', '0', 'Yes')", (pid, s))
    load = conn.execute("SELECT sp_import_books(%s, %s)", (pid, uid)).fetchone()[0]
    assert load == 1
    doc = {"ctin": s, "inum": "INV-100010", "dt": "05-09-2026", "txval": 1000,
           "cgst": 90, "sgst": 90}
    imp = import_2b(conn, pid, build_2b_payload(g, "092026", [doc, dict(doc)]), uid)
    conn.execute("SELECT sp_reconcile(%s, %s)", (imp, uid))
    conn.execute(
        "INSERT INTO synth_truth(gstin, books_invoice_no, portal_invoice_no, perturbation, "
        "expected_status) VALUES (%s, 'INV-100010', 'INV-100010', 'DUPLICATE', 'EXACT'), "
        "(%s, NULL, 'INV-100010', 'DUPLICATE', 'DUPLICATE')", (g, g))
    assert sorted(accuracy(conn)) == [("DUPLICATE", "DUPLICATE", 1, 100, 100),
                                      ("EXACT", "EXACT", 1, 100, 100)]
