"""Connections, the per-request role transaction, and database error → HTTP mapping.

Every request runs inside `tx(conn, user)`: SET LOCAL ROLE rc_<role> and app.user_id are
set first, so row-level security and the Plan A procedures decide what the user may see
and do. The API never repeats those checks; it translates the database's answer.
"""
from contextlib import contextmanager

import psycopg

from api.config import ROLES, CurrentUser


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


_BAD_INPUT = (
    psycopg.errors.CheckViolation,
    psycopg.errors.UniqueViolation,
    psycopg.errors.NotNullViolation,
    psycopg.errors.ForeignKeyViolation,
    psycopg.errors.DataError,  # invalid text representation, bad dates, overflow…
)


def _message(exc) -> str:
    diag = getattr(exc, "diag", None)
    msg = getattr(diag, "message_primary", None) if diag is not None else None
    return msg or str(exc)


def map_db_error(exc: Exception) -> ApiError:
    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return ApiError(403, _message(exc))
    if isinstance(exc, psycopg.errors.RaiseException):
        return ApiError(409, _message(exc))
    if isinstance(exc, _BAD_INPUT):
        return ApiError(400, _message(exc))
    # Anything else is a bug or an outage; do not leak schema details to the client.
    return ApiError(500, "database error")


def connect(settings) -> "psycopg.Connection":
    return psycopg.connect(settings.database_url, autocommit=False)


@contextmanager
def tx(conn, user: CurrentUser | None):
    """Run a block as `user` inside a transaction (a savepoint if one is already open).

    SET LOCAL survives a released savepoint, so the role and user id are reset explicitly
    on the way out; on error the savepoint rollback undoes them."""
    if user is not None and user.role not in ROLES:
        raise ApiError(401, "invalid session")
    try:
        with conn.transaction():
            if user is not None:
                conn.execute(f"SET LOCAL ROLE rc_{user.role}")
                conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user.user_id),))
            yield conn
            if user is not None:
                conn.execute("RESET ROLE")
                conn.execute("SELECT set_config('app.user_id', '', true)")
    except psycopg.Error as exc:
        raise map_db_error(exc) from exc


@contextmanager
def owned_connection(settings):
    """One connection per request: commit on success, roll back on error, close."""
    conn = connect(settings)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def borrowed_connection(conn):
    """A connection owned by someone else (tests): never commit or close it."""
    yield conn
