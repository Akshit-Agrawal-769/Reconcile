import os
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

from db.migrate import run_migrations

TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://reconcile:reconcile@localhost:5432/reconcile_test",
)


def _admin_url(url):
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/postgres"))


def _dbname(url):
    return urlsplit(url).path.lstrip("/")


@pytest.fixture(scope="session")
def test_db_url():
    name = _dbname(TEST_URL)
    admin = psycopg.connect(_admin_url(TEST_URL), autocommit=True)
    try:
        admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        admin.close()
    run_migrations(TEST_URL)
    return TEST_URL


@pytest.fixture(scope="session")
def _session_conn(test_db_url):
    c = psycopg.connect(test_db_url, autocommit=False)
    yield c
    c.rollback()
    c.close()


@pytest.fixture
def conn(_session_conn):
    _session_conn.rollback()
    yield _session_conn
    _session_conn.rollback()


def as_role(conn, role, user_id):
    """Switch to rc_<role> and set app.user_id; both are transaction-scoped."""
    assert role.replace("_", "").isalnum()
    conn.execute(f"SET LOCAL ROLE rc_{role}")
    conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
