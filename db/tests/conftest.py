"""Helpers shared by the database tests. Fixtures live in the root conftest.py."""


def as_role(conn, role, user_id):
    """Switch to rc_<role> and set app.user_id; both are transaction-scoped."""
    assert role.replace("_", "").isalnum()
    conn.execute(f"SET LOCAL ROLE rc_{role}")
    conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
