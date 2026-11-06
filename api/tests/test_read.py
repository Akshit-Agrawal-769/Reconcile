import pytest

from api.tests.conftest import login
from db.tests.conftest import as_role
from db.tests.factories import build_2b_payload, import_2b, make_invoice, make_period, reconcile


def doc(w, no, txval=100000, tax=9000):
    return dict(ctin=w["sup"], inum=no, dt="10-09-2026", txval=txval, cgst=tax, sgst=tax)


def build_reconciled(conn, w, gstin=None, period="092026"):
    """INV-1 exact, INV-2 only in books, INV-3 value mismatch (books 9000+9000 vs 2B
    5000+5000), INV-4 only in 2B. Returns (period_id, import_id)."""
    g = gstin or w["g1"]
    pid = make_period(conn, g, period)
    for no, tax in (("INV-1", 9000), ("INV-2", 9000), ("INV-3", 9000)):
        make_invoice(conn, g, w["sup"], no, "2026-09-10",
                     [dict(taxable_value=100000, cgst=tax, sgst=tax)], pos="07")
    payload = build_2b_payload(g, period, [doc(w, "INV-1"), doc(w, "INV-3", tax=5000),
                                           doc(w, "INV-4", txval=20000, tax=1800)])
    imp = import_2b(conn, pid, payload, w["partner"])
    reconcile(conn, imp, w["partner"])
    conn.execute("SELECT sp_refresh_reviews(%s)", (imp,))
    conn.execute("SELECT sp_refresh_party_score()")
    return pid, imp


@pytest.fixture
def rw(world, conn):
    world["pid"], world["imp"] = build_reconciled(conn, world)
    return world


def get(client, w, who, url, **kw):
    return client.get(url, headers=login(client, w[who + "_email"]), **kw)


def test_board_staff_sees_assigned(client, rw):
    names = lambda who: sorted(r["client_name"] for r in get(client, rw, who, "/api/clients").json())
    assert names("staff_a") == ["One Traders", "Two Exports"]
    assert names("staff_b") == ["Three Mills"]
    assert names("partner") == ["One Traders", "Three Mills", "Two Exports"]
    assert names("client1") == ["One Traders"]
    row = next(r for r in get(client, rw, "staff_a", "/api/clients").json()
               if r["client_name"] == "One Traders")
    assert set(row) == {"client_id", "client_name", "gstin", "period", "state", "itc_at_risk",
                        "last_import_at", "assignee_names"}
    assert row["period"] == "092026" and row["state"] == "IN_REVIEW"


def test_requires_login(client, rw):
    assert client.get("/api/clients").status_code == 401


def test_client_periods_newest_first(client, rw, conn):
    p2 = make_period(conn, rw["g1"], "102026")
    p0 = make_period(conn, rw["g1"], "032026")
    rows = get(client, rw, "staff_a", f"/api/clients/{rw['c1']}/periods").json()
    assert [r["period_id"] for r in rows] == [p2, rw["pid"], p0]
    assert set(rows[0]) == {"period_id", "gstin", "period", "state", "fin_year"}
    r = get(client, rw, "staff_b", f"/api/clients/{rw['c1']}/periods")
    assert (r.status_code, r.json()) == (404, {"error": "client not found"})


def test_period_detail_and_404_for_other_staff(client, rw):
    body = get(client, rw, "staff_a", f"/api/periods/{rw['pid']}").json()
    p = body["period"]
    assert (p["period_id"], p["gstin"], p["period"], p["state"], p["client_approved"],
            p["client_name"], p["client_id"]) == (
        rw["pid"], rw["g1"], "092026", "IN_REVIEW", False, "One Traders", rw["c1"])
    assert body["totals"]["itc_books"] == 54000
    assert body["totals"]["itc_2b"] == 31600
    assert set(body["summary"]) >= {"claimable_now", "deferred", "lost", "hold_payment_parties"}
    r = get(client, rw, "staff_b", f"/api/periods/{rw['pid']}")
    assert (r.status_code, r.json()) == (404, {"error": "period not found"})
    assert get(client, rw, "staff_a", "/api/periods/abc").status_code == 404


def test_queue_order_and_filters(client, rw):
    rows = get(client, rw, "staff_a", f"/api/periods/{rw['pid']}/queue").json()
    assert {r["status"] for r in rows} == {"EXACT", "ONLY_IN_BOOKS", "MISMATCH", "ONLY_IN_2B"}
    assert rows[-1]["status"] == "EXACT"  # matched rows last
    risks = [r["tax_at_risk"] for r in rows[:-1]]
    assert risks == sorted(risks, reverse=True)
    only = get(client, rw, "staff_a", f"/api/periods/{rw['pid']}/queue",
               params={"status": "ONLY_IN_BOOKS"}).json()
    assert [r["invoice_no"] for r in only] == ["INV-2"]
    found = get(client, rw, "staff_a", f"/api/periods/{rw['pid']}/queue",
                params={"q": "inv-3"}).json()
    assert [r["invoice_no"] for r in found] == ["INV-3"]
    assert get(client, rw, "staff_a", f"/api/periods/{rw['pid']}/queue",
               params={"q": "100%"}).json() == []
    r = get(client, rw, "staff_a", f"/api/periods/{rw['pid']}/queue", params={"status": "NOPE"})
    assert r.status_code == 400
    assert get(client, rw, "staff_b", f"/api/periods/{rw['pid']}/queue").json() == []


def test_result_detail_side_by_side(client, rw, conn):
    rid = conn.execute("SELECT result_id FROM v_queue WHERE invoice_no = 'INV-3' AND period_id = %s",
                       (rw["pid"],)).fetchone()[0]
    body = get(client, rw, "staff_a", f"/api/results/{rid}").json()
    assert (body["status"], body["books_invoice_no"], body["portal_invoice_no"],
            body["books_tax"], body["portal_tax"]) == ("MISMATCH", "INV-3", "INV-3", 18000, 10000)
    assert body["review"] is None
    assert get(client, rw, "staff_b", f"/api/results/{rid}").status_code == 404


def test_jobs_endpoints(client, rw, conn):
    with conn.transaction():
        as_role(conn, "staff", rw["staff_a"])
        jid = conn.execute("SELECT sp_enqueue_job('RECONCILE', %s, NULL, %s)",
                           (rw["pid"], rw["imp"])).fetchone()[0]
        conn.execute("RESET ROLE")
    job = get(client, rw, "staff_a", f"/api/jobs/{jid}").json()
    assert set(job) == {"job_id", "job_type", "status", "attempts", "last_error", "result",
                        "created_at", "finished_at"}
    assert (job["job_type"], job["status"]) == ("RECONCILE", "QUEUED")
    assert [j["job_id"] for j in get(client, rw, "staff_a", f"/api/periods/{rw['pid']}/jobs").json()] == [jid]
    assert get(client, rw, "staff_b", f"/api/jobs/{jid}").status_code == 404


def test_ims_plan_csv_header_and_gate(client, rw, conn):
    url = f"/api/periods/{rw['pid']}/ims-plan.csv"
    r = get(client, rw, "staff_a", url)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "ims-plan-092026.csv" in r.headers["content-disposition"]
    assert r.text.splitlines() == ["party_gstin,invoice_no,ims_action,remark"]
    rid = conn.execute("SELECT result_id FROM v_queue WHERE invoice_no = 'INV-4' AND period_id = %s",
                       (rw["pid"],)).fetchone()[0]
    with conn.transaction():
        as_role(conn, "staff", rw["staff_a"])
        conn.execute("SELECT sp_propose(%s, 'INELIGIBLE', 'REJECT', NULL, 'not ours')", (rid,))
        conn.execute("RESET ROLE")
    with conn.transaction():
        as_role(conn, "staff", rw["staff_a"])
        conn.execute("SELECT sp_submit_period(%s)", (rw["pid"],))
        conn.execute("RESET ROLE")
    assert get(client, rw, "staff_a", url).text.splitlines()[1:] == []  # not approved yet
    with conn.transaction():
        as_role(conn, "partner", rw["partner"])
        conn.execute("SELECT sp_partner_override(%s, 'client on leave')", (rw["pid"],))
        conn.execute("RESET ROLE")
    lines = get(client, rw, "client1", url).text.splitlines()
    assert lines[1:] == [f"{rw['sup']},INV-4,REJECT,not ours"]


def test_reports_endpoints_shape(client, rw, conn):
    conn.execute("SELECT sp_generate_filings(%s, '2026-09-01', '2026-10-31')", (rw["g1"],))
    for path, keys in (
            ("rule37", {"client_id", "invoice_id", "party_gstin", "invoice_no", "invoice_date",
                        "unpaid", "days_outstanding", "bucket", "interest_estimate",
                        "proposed_reversal"}),
            ("party-scores", {"party_gstin", "client_id", "docs", "pct_only_in_books",
                              "pct_mismatch", "avg_filing_delay_days", "score"}),
            ("filings", {"filing_id", "gstin", "return_type", "period", "due_date", "filed_on",
                         "status", "late_fee"}),
            ("alerts", {"client_id", "gstin", "kind", "ref", "due_by"})):
        r = get(client, rw, "staff_a", f"/api/clients/{rw['c1']}/{path}")
        assert r.status_code == 200, (path, r)
        rows = r.json()
        assert isinstance(rows, list)
        if rows:
            assert set(rows[0]) == keys, path
        assert get(client, rw, "staff_b", f"/api/clients/{rw['c1']}/{path}").status_code == 404
    assert get(client, rw, "staff_a", f"/api/clients/{rw['c1']}/party-scores").json()
    assert get(client, rw, "staff_a", f"/api/clients/{rw['c1']}/filings").json()


def test_client_user_sees_only_own(client, rw, conn):
    pid2, _ = build_reconciled(conn, rw, gstin=rw["g2"])
    assert get(client, rw, "client1", f"/api/periods/{rw['pid']}").status_code == 200
    assert get(client, rw, "client1", f"/api/periods/{pid2}").status_code == 404
    assert get(client, rw, "client1", f"/api/clients/{rw['c2']}/periods").status_code == 404
