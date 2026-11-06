"""API test fixtures. Database fixtures (`conn`, `committed`) come from the root conftest."""
import pytest

from api.app import create_app
from api.config import Settings
from api.tests.asgi import Client
from db.tests.factories import assign, make_client, make_party, make_reg, make_user

SECRET = "test-secret-not-for-production"
PASSWORD = "demo123"


def settings_for(url, **kw):
    return Settings(database_url=url, secret=SECRET, static_dir="web/dist", **kw)


def set_login(conn, user_id, email):
    conn.execute(
        "UPDATE app_user SET email = %s, password_hash = crypt(%s, gen_salt('bf', 4)) "
        "WHERE user_id = %s", (email, PASSWORD, user_id))


@pytest.fixture
def world(conn):
    """Firm with a partner, staff A (clients 1 and 2), staff B (client 3), and a client
    user for client 1. Every login uses the password demo123."""
    c1 = make_client(conn, pan="AAACA1111A", name="One Traders")
    c2 = make_client(conn, pan="AAACB2222B", name="Two Exports")
    c3 = make_client(conn, pan="AAACC3333C", name="Three Mills")
    w = dict(c1=c1, c2=c2, c3=c3, g1=make_reg(conn, c1), g2=make_reg(conn, c2),
             g3=make_reg(conn, c3))
    for key, role, client in (("partner", "partner", None), ("staff_a", "staff", None),
                              ("staff_b", "staff", None), ("client1", "client", c1)):
        w[key] = make_user(conn, role, client)
        w[key + "_email"] = f"{key.replace('_', '.')}@world.test"
        set_login(conn, w[key], w[key + "_email"])
    assign(conn, w["staff_a"], c1)
    assign(conn, w["staff_a"], c2)
    assign(conn, w["staff_b"], c3)
    w["sup"] = make_party(conn, "AAAPA1111A")
    return w


@pytest.fixture
def settings(test_db_url):
    return settings_for(test_db_url)


@pytest.fixture
def client(conn, settings):
    """The app wired to the per-test connection: requests see the test's uncommitted
    world, and everything is rolled back afterwards."""
    return Client(create_app(settings, conn_factory=lambda: conn))


def login(client, email, password=PASSWORD):
    r = client.post("/api/login", json={"email": email, "password": password})
    assert r.status_code == 200, r
    return {"Authorization": f"Bearer {r.json()['token']}"}
