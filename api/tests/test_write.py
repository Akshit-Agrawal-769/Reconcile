import pytest

from api.tests.conftest import login
from api.tests.test_read import build_reconciled


@pytest.fixture
def rw(world, conn):
    world["pid"], world["imp"] = build_reconciled(conn, world)
    return world


def call(client, w, who, method, url, json=None):
    return client.request(method, url, headers=login(client, w[who + "_email"]), json=json)


def result_id(conn, w, invoice_no):
    return conn.execute("SELECT result_id FROM v_queue WHERE invoice_no = %s AND period_id = %s",
                        (invoice_no, w["pid"])).fetchone()[0]


def state(conn, pid):
    return conn.execute("SELECT state::text FROM return_period WHERE period_id = %s",
                        (pid,)).fetchone()[0]


def propose(client, w, rid, decision="ACCEPT", ims="ACCEPT", who="staff_a", **extra):
    return call(client, w, who, "POST", f"/api/results/{rid}/propose",
                {"decision": decision, "ims_action": ims, **extra})


def test_open_period(client, world, conn):
    r = call(client, world, "staff_a", "POST", "/api/periods", {"gstin": world["g1"], "period": "102026"})
    assert r.status_code == 201, r
    assert state(conn, r.json()["period_id"]) == "OPEN"
    again = call(client, world, "staff_a", "POST", "/api/periods", {"gstin": world["g1"], "period": "102026"})
    assert (again.status_code, again.json()) == (201, r.json())  # idempotent, per sp_open_period
    r = call(client, world, "staff_b", "POST", "/api/periods", {"gstin": world["g1"], "period": "112026"})
    assert (r.status_code, r.json()) == (409, {"error": "not your client"})
    r = call(client, world, "staff_a", "POST", "/api/periods", {"gstin": world["g1"], "period": "132026"})
    assert r.status_code == 400 and r.json()["error"].startswith("period:")


def test_propose_then_partner_approve(client, rw, conn):
    rid = result_id(conn, rw, "INV-3")  # 8000 at risk: above the default approval threshold
    r = propose(client, rw, rid, remark="supplier will amend")
    assert r.status_code == 200, r
    rev = r.json()["review_id"]
    assert conn.execute("SELECT state::text, remark FROM review WHERE review_id = %s",
                        (rev,)).fetchone() == ("PROPOSED", "supplier will amend")
    assert call(client, rw, "partner", "POST", f"/api/reviews/{rev}/approve").status_code == 204
    assert conn.execute("SELECT state::text, approved_by FROM review WHERE review_id = %s",
                        (rev,)).fetchone() == ("APPROVED", rw["partner"])
    r = call(client, rw, "staff_a", "POST", f"/api/reviews/{rev}/done", {"executed_on": "2026-10-05"})
    assert r.status_code == 204
    assert conn.execute("SELECT state::text FROM review WHERE review_id = %s", (rev,)).fetchone()[0] == "DONE"


def test_staff_approve_refused(client, rw, conn):
    rev = propose(client, rw, result_id(conn, rw, "INV-3")).json()["review_id"]
    r = call(client, rw, "staff_a", "POST", f"/api/reviews/{rev}/approve")
    assert r.status_code == 403
    assert "permission denied" in r.json()["error"]


def test_mark_done_before_approval_409(client, rw, conn):
    rev = propose(client, rw, result_id(conn, rw, "INV-3")).json()["review_id"]
    r = call(client, rw, "staff_a", "POST", f"/api/reviews/{rev}/done", {"executed_on": "2026-10-05"})
    assert (r.status_code, r.json()) == (409, {"error": "review must be approved first"})


def test_bad_decision_value_400(client, rw, conn):
    r = propose(client, rw, result_id(conn, rw, "INV-3"), decision="MAYBE")
    assert r.status_code == 400
    assert r.json()["error"].startswith("decision:")
    r = propose(client, rw, result_id(conn, rw, "INV-3"), decision="PARTIAL", claim_pct=150)
    assert r.status_code == 400  # the DB check (0..100) or the model: either way, 400


def test_propose_invisible_result(client, rw, conn):
    r = propose(client, rw, result_id(conn, rw, "INV-3"), who="staff_b")
    assert r.status_code in (403, 404, 409)
    assert conn.execute("SELECT count(*) FROM review").fetchone()[0] == 0


def test_close_refused_409_message(client, rw, conn):
    """The DB's reason comes back verbatim, and the period is unchanged."""
    assert call(client, rw, "staff_a", "POST", f"/api/periods/{rw['pid']}/submit").status_code == 204
    r = call(client, rw, "partner", "POST", f"/api/periods/{rw['pid']}/close")
    assert (r.status_code, r.json()) == (409, {"error": "client approval missing"})
    assert state(conn, rw["pid"]) == "AWAITING_APPROVAL"


def test_full_month_via_api(client, rw, conn):
    big = propose(client, rw, result_id(conn, rw, "INV-3")).json()["review_id"]
    chase = propose(client, rw, result_id(conn, rw, "INV-2"), decision="CHASE",
                    ims="PENDING").json()["review_id"]
    assert call(client, rw, "staff_a", "POST", f"/api/periods/{rw['pid']}/submit").status_code == 204
    assert call(client, rw, "client1", "POST", f"/api/periods/{rw['pid']}/client-approve").status_code == 204
    r = call(client, rw, "partner", "POST", f"/api/periods/{rw['pid']}/close")
    assert (r.status_code, r.json()) == (409, {"error": "reviews awaiting approval"})
    for rev in (big, chase):
        assert call(client, rw, "partner", "POST", f"/api/reviews/{rev}/approve").status_code == 204
    assert call(client, rw, "partner", "POST", f"/api/periods/{rw['pid']}/close").status_code == 204
    assert state(conn, rw["pid"]) == "CLOSED"
    r = propose(client, rw, result_id(conn, rw, "INV-4"))
    assert (r.status_code, r.json()) == (409, {"error": "period is closed"})


def test_client_cannot_approve_other_client(client, rw, conn):
    pid2, _ = build_reconciled(conn, rw, gstin=rw["g2"])
    call(client, rw, "staff_a", "POST", f"/api/periods/{pid2}/submit")
    r = call(client, rw, "client1", "POST", f"/api/periods/{pid2}/client-approve")
    assert r.status_code in (403, 404, 409)
    assert state(conn, pid2) == "AWAITING_APPROVAL"


def test_override_and_reopen_require_reason_409(client, rw, conn):
    call(client, rw, "staff_a", "POST", f"/api/periods/{rw['pid']}/submit")
    r = call(client, rw, "partner", "POST", f"/api/periods/{rw['pid']}/override", {"reason": " "})
    assert (r.status_code, r.json()) == (409, {"error": "override reason required"})
    r = call(client, rw, "partner", "POST", f"/api/periods/{rw['pid']}/override",
             {"reason": "client travelling"})
    assert r.status_code == 204
    for inv in ("INV-2", "INV-3", "INV-4"):
        rev = propose(client, rw, result_id(conn, rw, inv)).json()["review_id"]
        call(client, rw, "partner", "POST", f"/api/reviews/{rev}/approve")
    assert call(client, rw, "partner", "POST", f"/api/periods/{rw['pid']}/close").status_code == 204
    r = call(client, rw, "partner", "POST", f"/api/periods/{rw['pid']}/reopen", {"reason": ""})
    assert (r.status_code, r.json()) == (409, {"error": "reopen reason required"})
    r = call(client, rw, "partner", "POST", f"/api/periods/{rw['pid']}/reopen",
             {"reason": "supplier amended"})
    assert r.status_code == 204
    assert state(conn, rw["pid"]) == "IN_REVIEW"


def test_followup_roundtrip_and_tenant(client, rw):
    url = f"/api/periods/{rw['pid']}/followups"
    body = {"party_gstin": rw["sup"], "channel": "WHATSAPP", "sent_on": "2026-10-02",
            "response": "will upload by 10th", "next_chase_on": "2026-10-10", "status": "SENT"}
    r = call(client, rw, "staff_a", "POST", url, body)
    assert r.status_code == 201, r
    rows = call(client, rw, "staff_a", "GET", url).json()
    assert len(rows) == 1
    row = rows[0]
    assert (row["followup_id"], row["party_gstin"], row["channel"], row["status"],
            row["sent_on"], row["sent_by"], row["gstin"]) == (
        r.json()["followup_id"], rw["sup"], "WHATSAPP", "SENT", "2026-10-02", rw["staff_a"], rw["g1"])
    r = call(client, rw, "staff_b", "POST", url, body)
    assert r.status_code == 404
    assert call(client, rw, "staff_b", "GET", url).status_code == 404
    r = call(client, rw, "staff_a", "POST", url, dict(body, channel="PIGEON"))
    assert r.status_code == 400 and r.json()["error"].startswith("channel:")


def test_generate_filings_endpoint(client, rw, conn):
    url = f"/api/clients/{rw['c1']}/filings/generate"
    r = call(client, rw, "partner", "POST", url, {"from": "2026-04-01", "to": "2026-06-30"})
    assert r.status_code == 200, r
    assert r.json()["inserted"] > 0
    again = call(client, rw, "partner", "POST", url, {"from": "2026-04-01", "to": "2026-06-30"})
    assert again.json()["inserted"] == 0
    r = call(client, rw, "staff_b", "POST", url, {"from": "2026-04-01", "to": "2026-06-30"})
    assert r.status_code == 404
    r = call(client, rw, "partner", "POST", url, {"from": "2026-06-30", "to": "2026-04-01"})
    assert r.status_code == 409


def test_propose_sql_resolves_with_psycopg3_parameter_types(conn):
    """Finding #2: psycopg 3 binds a Python float as double precision and a str as
    unknown. The statement must resolve sp_propose with those types (the test shim
    inlines literals, so this checks the server-side resolution directly)."""
    from api import sql
    n = iter(range(1, 6))
    stmt = __import__("re").sub(r"%s", lambda m: f"${next(n)}", sql.PROPOSE)
    conn.execute(f"PREPARE propose_check(bigint, unknown, unknown, double precision, unknown) AS {stmt}")
    conn.execute("DEALLOCATE propose_check")


def test_partial_claim_roundtrip(client, rw, conn):
    rev = propose(client, rw, result_id(conn, rw, "INV-3"), decision="PARTIAL", ims="ACCEPT",
                  claim_pct=50).json()["review_id"]
    assert float(conn.execute("SELECT claim_pct FROM review WHERE review_id = %s",
                              (rev,)).fetchone()[0]) == 50.0
