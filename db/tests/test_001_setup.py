from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def test_migrations_recorded(conn):
    recorded = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
    assert recorded == {p.name for p in MIGRATIONS.glob("*.sql")}


def test_extensions(conn):
    names = {r[0] for r in conn.execute("SELECT extname FROM pg_extension").fetchall()}
    assert {"fuzzystrmatch", "pg_trgm", "pgcrypto"} <= names


def test_roles(conn):
    rows = conn.execute(
        "SELECT rolname, rolinherit FROM pg_roles WHERE rolname LIKE 'rc\\_%'"
    ).fetchall()
    roles = {r[0]: r[1] for r in rows}
    for r in ("rc_partner", "rc_staff", "rc_client", "rc_api"):
        assert r in roles
    assert roles["rc_api"] is False
    members = {
        r[0]
        for r in conn.execute(
            "SELECT g.rolname FROM pg_auth_members m "
            "JOIN pg_roles g ON g.oid = m.roleid "
            "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = 'rc_api'"
        ).fetchall()
    }
    assert {"rc_partner", "rc_staff", "rc_client"} <= members


def test_as_role(conn):
    from conftest import as_role

    as_role(conn, "staff", 1)
    assert conn.execute("SELECT current_user").fetchone()[0] == "rc_staff"
    assert conn.execute("SELECT current_setting('app.user_id')").fetchone()[0] == "1"
    conn.rollback()
    assert conn.execute("SELECT current_user").fetchone()[0] == "reconcile"
    assert conn.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] in ("", None)


def test_failed_migration_rolls_back(tmp_path, test_db_url):
    import psycopg
    import pytest
    from urllib.parse import urlsplit, urlunsplit

    from db.migrate import run_migrations

    admin_url = urlunsplit(urlsplit(test_db_url)._replace(path="/postgres"))
    scratch = "reconcile_migfail"
    scratch_url = urlunsplit(urlsplit(test_db_url)._replace(path="/" + scratch))
    (tmp_path / "900_ok.sql").write_text("CREATE TABLE t900 (x int);")
    # Runtime error after a CREATE: proves the whole file is rolled back.
    (tmp_path / "901_bad.sql").write_text("CREATE TABLE t901 (x int); SELECT 1/0;")
    (tmp_path / "902_later.sql").write_text("CREATE TABLE t902 (x int);")

    admin = psycopg.connect(admin_url, autocommit=True)
    try:
        admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        admin.execute(f'CREATE DATABASE "{scratch}"')
        with pytest.raises(Exception):
            run_migrations(scratch_url, tmp_path)
        c = psycopg.connect(scratch_url)
        try:
            rec = {r[0] for r in c.execute("SELECT filename FROM schema_migrations").fetchall()}
            assert rec == {"900_ok.sql"}
            tabs = {
                r[0]
                for r in c.execute(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                ).fetchall()
            }
            assert "t900" in tabs and "t901" not in tabs and "t902" not in tabs
        finally:
            c.close()
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{scratch}"')
        admin.close()
