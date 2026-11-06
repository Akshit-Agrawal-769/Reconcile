"""Shared pytest fixtures for db/tests and api/tests (one test database per session)."""
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


def _fresh_db(url):
    name = _dbname(url)
    admin = psycopg.connect(_admin_url(url), autocommit=True)
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
    run_migrations(url)


@pytest.fixture(scope="session")
def commit_db_url(test_db_url):
    """A second migrated database for tests whose code under test commits (worker,
    concurrent claims). Kept apart so committed rows never leak into `conn` tests."""
    parts = urlsplit(TEST_URL)
    url = urlunsplit(parts._replace(path="/" + _dbname(TEST_URL) + "_commit"))
    _fresh_db(url)
    return url


@pytest.fixture
def committed(commit_db_url):
    """Empty the committed database, then hand out its URL. Callers open their own
    connections and must close them."""
    c = psycopg.connect(commit_db_url, autocommit=True)
    try:
        c.execute("TRUNCATE firm, party, job_queue, upload RESTART IDENTITY CASCADE")
    finally:
        c.close()
    return commit_db_url


from db.tests.conftest import as_role  # noqa: E402,F401  (re-export)
