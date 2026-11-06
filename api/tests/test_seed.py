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
