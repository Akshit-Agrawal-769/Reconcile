"""scripts/seed_e2e.py builds the demo world; dev.sh runs it on every start."""
import importlib.util
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

spec = importlib.util.spec_from_file_location(
    "seed_e2e", Path(__file__).resolve().parents[2] / "scripts" / "seed_e2e.py")
seed_e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_e2e)


def test_seed_twice_is_harmless(test_db_url, tmp_path):
    parts = urlsplit(test_db_url)
    url = urlunsplit(parts._replace(path=parts.path + "_seed"))
    seed_e2e.recreate(url)
    try:
        first = seed_e2e.seed(url, tmp_path / "a", docs=12)
        again = seed_e2e.seed(url, tmp_path / "b", docs=12)
        assert (again["client_id"], again["period_id"], again["gstin"]) == (
            first["client_id"], first["period_id"], first["gstin"])
        assert Path(again["books"]).exists() and Path(again["gstr2b"]).exists()
    finally:
        seed_e2e.drop(url)


def test_months_get_distinct_invoice_numbers(tmp_path):
    """Two months of the same client must not reuse invoice numbers (same FY)."""
    import csv
    from tools.synth import generate
    numbers = {}
    for period in ("082026", "092026"):
        m = generate(clients=1, docs=30, period=period, seed=7, out=str(tmp_path / period))
        with open(tmp_path / period / m[0]["books_file"]) as f:
            numbers[period] = {r["Invoice Number"] for r in csv.DictReader(f)}
    assert numbers["082026"] and not numbers["082026"] & numbers["092026"]


def test_seed_preloads_a_reconciled_month(test_db_url, tmp_path):
    """Opening the app after seeding shows a month with matches, decisions and a filing
    calendar; September is left for a live upload."""
    import psycopg
    parts = urlsplit(test_db_url)
    url = urlunsplit(parts._replace(path=parts.path + "_seedh"))
    seed_e2e.recreate(url)
    try:
        info = seed_e2e.seed(url, tmp_path, docs=40, history=True)
        c = psycopg.connect(url)
        try:
            q = lambda s, *p: c.execute(s, p).fetchall()
            assert q("SELECT period, state::text FROM return_period WHERE gstin = %s ORDER BY period",
                     info["gstin"]) == [("082026", "IN_REVIEW")]
            assert q("SELECT count(*) FROM recon_result")[0][0] > 20
            assert q("SELECT count(*) FROM review")[0][0] >= 3
            assert q("SELECT count(*) FROM filing WHERE gstin = %s", info["gstin"])[0][0] > 0
            assert q("SELECT count(*) FROM mv_party_score")[0][0] > 0
        finally:
            c.close()
        assert info["period"] == "092026" and info["period_id"] is None
        assert Path(info["books"]).exists() and Path(info["gstr2b"]).exists()
    finally:
        seed_e2e.drop(url)
