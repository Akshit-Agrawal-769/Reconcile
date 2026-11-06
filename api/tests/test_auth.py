import dataclasses

import psycopg
import pytest

from api import auth
from api.config import CurrentUser
from api.db import ApiError, map_db_error, tx
from api.tests.conftest import login


def test_login_ok_and_me(client, world):
    r = client.post("/api/login", json={"email": world["staff_a_email"], "password": "demo123"})
    assert r.status_code == 200, r
    body = r.json()
    assert body["user"] == {"user_id": world["staff_a"], "role": "staff", "client_id": None}
    me = client.get("/api/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json() == {"user_id": world["staff_a"], "role": "staff", "client_id": None}


def test_login_client_user_carries_client_id(client, world):
    h = login(client, world["client1_email"])
    assert client.get("/api/me", headers=h).json()["client_id"] == world["c1"]


def test_login_wrong_password_401(client, world):
    r = client.post("/api/login", json={"email": world["staff_a_email"], "password": "nope"})
    assert r.status_code == 401
    assert r.json() == {"error": "wrong email or password"}


def test_login_bad_body_400(client, world):
    r = client.post("/api/login", json={"email": world["staff_a_email"]})
    assert r.status_code == 400
    assert r.json()["error"].startswith("password:")


def test_me_requires_token_401(client):
    r = client.get("/api/me")
    assert r.status_code == 401
    assert r.json() == {"error": "please log in"}


def test_expired_and_tampered_tokens_401(client, world, settings):
    """Review Focus 4: a bad token is a 401 with a JSON body, never a 500."""
    user = CurrentUser(user_id=world["staff_a"], role="staff", client_id=None, firm_id=1)
    token = auth.sign(user, settings)
    with pytest.raises(ApiError) as e:
        auth.verify(token, dataclasses.replace(settings, token_max_age=-1))
    assert (e.value.status, e.value.message) == (401, "session expired, please log in again")

    tampered = token[:-3] + ("AAA" if token[-3:] != "AAA" else "BBB")
    r = client.get("/api/me", headers={"Authorization": f"Bearer {tampered}"})
    assert r.status_code == 401
    assert r.json() == {"error": "invalid session"}
    r = client.get("/api/me", headers={"Authorization": "Bearer not-a-token"})
    assert (r.status_code, r.json()) == (401, {"error": "invalid session"})
    r = client.get("/api/me", headers={"Authorization": "Basic abc"})
    assert (r.status_code, r.json()) == (401, {"error": "please log in"})


def test_token_signed_with_other_secret_rejected(world, settings):
    user = CurrentUser(user_id=world["staff_a"], role="staff", client_id=None, firm_id=1)
    other = auth.sign(user, dataclasses.replace(settings, secret="another"))
    with pytest.raises(ApiError) as e:
        auth.verify(other, settings)
    assert e.value.status == 401


def test_unknown_role_in_token_rejected(settings):
    """Even a correctly signed token cannot name a role outside the three groups,
    because the role is interpolated into SET ROLE."""
    forged = auth._serializer(settings).dumps({"u": 1, "r": "postgres", "c": None, "f": 1})
    with pytest.raises(ApiError) as e:
        auth.verify(forged, settings)
    assert (e.value.status, e.value.message) == (401, "invalid session")


@pytest.mark.parametrize("exc,status", [
    (psycopg.errors.InsufficientPrivilege("permission denied for table x"), 403),
    (psycopg.errors.RaiseException("period is closed"), 409),
    (psycopg.errors.CheckViolation("violates check"), 400),
    (psycopg.errors.UniqueViolation("duplicate key"), 400),
    (psycopg.errors.InvalidTextRepresentation("invalid input value"), 400),
    (psycopg.errors.NotNullViolation("null value"), 400),
])
def test_db_error_mapping(exc, status):
    err = map_db_error(exc)
    assert err.status == status
    assert err.message == str(exc)


def test_unexpected_db_error_is_500_without_detail():
    err = map_db_error(psycopg.errors.UndefinedTable('relation "secret" does not exist'))
    assert (err.status, err.message) == (500, "database error")


def test_request_leaves_connection_as_it_found_it(client, world, conn):
    """tx() must reset SET LOCAL ROLE / app.user_id after the request's savepoint, or the
    next request on a pooled connection would inherit the previous user."""
    before = conn.execute("SELECT current_user").fetchone()[0]
    h = login(client, world["staff_a_email"])
    assert client.get("/api/me", headers=h).status_code == 200
    with tx(conn, CurrentUser(world["staff_a"], "staff", None, 1)):
        assert conn.execute("SELECT current_user").fetchone()[0] == "rc_staff"
    assert conn.execute("SELECT current_user").fetchone()[0] == before
    assert conn.execute("SELECT current_setting('app.user_id', true)").fetchone()[0] in ("", None)


def test_db_error_inside_tx_maps_and_rolls_back(conn, world):
    with pytest.raises(ApiError) as e:
        with tx(conn, CurrentUser(world["staff_b"], "staff", None, 1)) as c:
            c.execute("SELECT sp_open_period(%s, '092026')", (world["g1"],))
    assert (e.value.status, e.value.message) == (409, "not your client")
    assert conn.execute("SELECT current_user").fetchone()[0] != "rc_staff"


def test_default_connection_per_request(committed):
    """Without conn_factory each request opens, commits and closes its own connection."""
    from api.app import create_app
    from api.tests.asgi import Client
    from api.tests.conftest import set_login, settings_for
    from db.tests.factories import make_user
    setup = psycopg.connect(committed, autocommit=False)
    try:
        uid = make_user(setup, "partner")
        set_login(setup, uid, "p@commit.test")
        setup.commit()
    finally:
        setup.close()
    c = Client(create_app(settings_for(committed)))
    h = login(c, "p@commit.test")
    assert c.get("/api/me", headers=h).json()["user_id"] == uid
