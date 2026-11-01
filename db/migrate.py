"""Minimal migration runner: applies db/migrations/*.sql in sorted order,
one transaction per file, recorded in schema_migrations."""
import argparse
import os
from pathlib import Path

import psycopg

DEFAULT_DIR = Path(__file__).resolve().parent / "migrations"


def run_migrations(url, migrations_dir=DEFAULT_DIR):
    applied = []
    conn = psycopg.connect(url, autocommit=False)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.commit()
        done = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
        conn.commit()
        for path in sorted(Path(migrations_dir).glob("*.sql")):
            if path.name in done:
                continue
            try:
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            applied.append(path.name)
            print(f"applied {path.name}")
    finally:
        conn.close()
    return applied


def main():
    ap = argparse.ArgumentParser(description="Apply SQL migrations.")
    ap.add_argument("--url", default=os.environ.get("DATABASE_URL"), required=False)
    args = ap.parse_args()
    if not args.url:
        ap.error("--url or DATABASE_URL required")
    run_migrations(args.url)


if __name__ == "__main__":
    main()
