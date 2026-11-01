import psycopg
import pytest

from db.tests.conftest import as_role
from db.tests.factories import (assign, build_2b_payload, import_2b, make_client,
                                make_invoice, make_party, make_period, make_reg, make_user,
                                reconcile)

SUP = "27"  # supplier in another state -> igst


def setup(conn, period="092026"):
    cid = make_client(conn)
    g = make_reg(conn, cid)
    ctx = dict(cid=cid, g=g, pid=make_period(conn, g, period),
               staff=make_user(conn, "staff"), partner=make_user(conn, "partner"),
               client=make_user(conn, "client", cid))
    assign(conn, ctx["staff"], cid)  # 010: staff act only on assigned clients
    return ctx


def portal(conn, ctx, docs, uid=None):
    return import_2b(conn, ctx["pid"], build_2b_payload(ctx["g"], "092026", docs),
                     uid or ctx["staff"])


def doc(sup, no, igst):
    return dict(ctin=sup, inum=no, dt="10-09-2026", txval=10000, igst=igst)


def with_invoice(conn, books_igst=1800, portal_igst=1800, no="INV-1"):
    """Period in IN_REVIEW with one reconciled invoice; returns (ctx, import_id, result_id)."""
    ctx = setup(conn)
    ctx["sup"] = make_party(conn, "AAAPA1111A", SUP)
    make_invoice(conn, ctx["g"], ctx["sup"], no, "2026-09-10",
                 [dict(taxable_value=10000, igst=books_igst)], pos="07")
    imp = portal(conn, ctx, [doc(ctx["sup"], no, portal_igst)])
    reconcile(conn, imp, ctx["staff"])
    rid = conn.execute("SELECT result_id FROM recon_result WHERE import_id = %s",
                       (imp,)).fetchone()[0]
    return ctx, imp, rid


def call(conn, role, uid, sql, params=()):
    with conn.transaction():
        as_role(conn, role, uid)
        cur = conn.execute(sql, params)
        row = cur.fetchone() if cur.description else None
        conn.execute("RESET ROLE")
    return row


def submit(conn, ctx, role="staff"):
    call(conn, role, ctx[role], "SELECT sp_submit_period(%s)", (ctx["pid"],))


def client_approve(conn, ctx, uid=None):
    call(conn, "client", uid or ctx["client"], "SELECT sp_client_approve(%s)", (ctx["pid"],))


def close(conn, ctx, role="partner"):
    call(conn, role, ctx[role], "SELECT sp_close_period(%s)", (ctx["pid"],))


def propose(conn, ctx, rid, role="staff"):
    return call(conn, role, ctx[role],
                "SELECT sp_propose(%s, 'ACCEPT', 'ACCEPT', NULL, NULL)", (rid,))[0]


def state(conn, pid):
    return conn.execute("SELECT state::text FROM return_period WHERE period_id = %s",
                        (pid,)).fetchone()[0]


def closed_period(conn, **kw):
    ctx, imp, rid = with_invoice(conn, **kw)
    submit(conn, ctx)
    client_approve(conn, ctx)
    close(conn, ctx)
    assert state(conn, ctx["pid"]) == "CLOSED"
    return ctx, imp, rid


def raises(msg, exc=psycopg.errors.RaiseException):
    return pytest.raises(exc, match=msg)


# --- state machine -------------------------------------------------------------------

def test_illegal_transition(conn):
    ctx = setup(conn)
    with raises("illegal period transition OPEN -> CLOSED"):
        with conn.transaction():
            conn.execute("UPDATE return_period SET state = 'CLOSED' WHERE period_id = %s",
                         (ctx["pid"],))
    assert state(conn, ctx["pid"]) == "OPEN"


def test_transitions_are_data(conn):
    rows = conn.execute(
        "SELECT from_state::text, to_state::text, allowed_role FROM period_transition "
        "ORDER BY 1, 2, 3").fetchall()
    assert rows == [
        ("AWAITING_APPROVAL", "CLOSED", "rc_partner"),
        ("AWAITING_APPROVAL", "IN_REVIEW", None),
        ("CLOSED", "IN_REVIEW", "rc_partner"),
        ("IN_REVIEW", "AWAITING_APPROVAL", "rc_partner"),
        ("IN_REVIEW", "AWAITING_APPROVAL", "rc_staff"),
        ("OPEN", "IN_REVIEW", None),
    ]


def test_submit_from_open_is_illegal(conn):
    ctx = setup(conn)
    with raises("illegal period transition OPEN -> AWAITING_APPROVAL"):
        submit(conn, ctx)


def test_submit_by_staff_and_partner(conn):
    ctx, _, _ = with_invoice(conn)
    assert state(conn, ctx["pid"]) == "IN_REVIEW"
    submit(conn, ctx, "partner")
    assert state(conn, ctx["pid"]) == "AWAITING_APPROVAL"


def test_client_cannot_submit(conn):
    ctx, _, _ = with_invoice(conn)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        submit(conn, ctx, "client")


# --- close guards --------------------------------------------------------------------

def test_close_needs_client_approval(conn):
    ctx, _, _ = with_invoice(conn)
    submit(conn, ctx)
    with raises("client approval missing"):
        close(conn, ctx)
    client_approve(conn, ctx)
    close(conn, ctx)
    assert conn.execute(
        "SELECT state::text, client_approved_by, closed_by, closed_at IS NOT NULL "
        "FROM return_period WHERE period_id = %s", (ctx["pid"],)).fetchone() == (
        "CLOSED", ctx["client"], ctx["partner"], True)


def test_only_partner_closes(conn):
    ctx, _, _ = with_invoice(conn)
    submit(conn, ctx)
    client_approve(conn, ctx)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        close(conn, ctx, "staff")
    # even with EXECUTE, the procedure itself refuses non-partners
    conn.execute("GRANT EXECUTE ON FUNCTION sp_close_period(bigint) TO rc_staff")
    with raises("only a partner can close"):
        close(conn, ctx, "staff")


def test_close_from_in_review_is_illegal(conn):
    ctx, _, _ = with_invoice(conn)
    # approval present but never submitted: guards pass, transition refuses
    conn.execute("UPDATE return_period SET override_reason = 'x' WHERE period_id = %s",
                 (ctx["pid"],))
    with raises("illegal period transition IN_REVIEW -> CLOSED"):
        close(conn, ctx)


def test_override_needs_reason(conn):
    ctx, _, _ = with_invoice(conn)
    submit(conn, ctx)
    for bad in ("", "   "):
        with raises("override reason required"):
            call(conn, "partner", ctx["partner"],
                 "SELECT sp_partner_override(%s, %s)", (ctx["pid"], bad))
    with raises("only a partner can override"):
        conn.execute("GRANT EXECUTE ON FUNCTION sp_partner_override(bigint, text) TO rc_staff")
        call(conn, "staff", ctx["staff"],
             "SELECT sp_partner_override(%s, %s)", (ctx["pid"], "x"))
    call(conn, "partner", ctx["partner"],
         "SELECT sp_partner_override(%s, %s)", (ctx["pid"], "  client unreachable  "))
    close(conn, ctx)
    assert conn.execute(
        "SELECT state::text, client_approved_by, override_reason FROM return_period "
        "WHERE period_id = %s", (ctx["pid"],)).fetchone() == (
        "CLOSED", None, "client unreachable")


def test_close_blocked_by_proposed(conn):
    ctx, _, rid = with_invoice(conn, books_igst=30000, portal_igst=10000)
    rev = propose(conn, ctx, rid)
    assert conn.execute("SELECT state::text FROM review WHERE review_id = %s",
                        (rev,)).fetchone()[0] == "PROPOSED"
    submit(conn, ctx)
    client_approve(conn, ctx)
    with raises("reviews awaiting approval"):
        close(conn, ctx)
    call(conn, "partner", ctx["partner"], "SELECT sp_approve(%s)", (rev,))
    close(conn, ctx)
    assert state(conn, ctx["pid"]) == "CLOSED"


def test_close_blocked_by_changed_items(conn):
    ctx, _, rid = with_invoice(conn, books_igst=1800, portal_igst=1600)
    propose(conn, ctx, rid)  # small diff -> auto-approved
    imp2 = portal(conn, ctx, [doc(ctx["sup"], "INV-1", 1500)])
    reconcile(conn, imp2, ctx["staff"])
    submit(conn, ctx)
    client_approve(conn, ctx)
    with raises("changed items not reviewed"):
        close(conn, ctx)


# --- client approval -----------------------------------------------------------------

def test_client_cannot_approve_other_client(conn):
    ctx, _, _ = with_invoice(conn)
    submit(conn, ctx)
    other = make_client(conn, pan="AAACB2222B", name="Other")
    other_user = make_user(conn, "client", other)
    with raises("only the client can approve this period"):
        client_approve(conn, ctx, other_user)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        call(conn, "staff", ctx["staff"], "SELECT sp_client_approve(%s)", (ctx["pid"],))
    assert conn.execute("SELECT client_approved_by FROM return_period WHERE period_id = %s",
                        (ctx["pid"],)).fetchone()[0] is None


def test_client_approves_only_submitted_period(conn):
    ctx, _, _ = with_invoice(conn)
    with raises("period is not awaiting approval"):
        client_approve(conn, ctx)


# --- closed month is read-only -------------------------------------------------------

def test_staff_cannot_edit_closed(conn):
    ctx, _, rid = closed_period(conn)
    with raises("period is closed"):
        propose(conn, ctx, rid)
    with raises("only a partner can reopen"):
        conn.execute("GRANT EXECUTE ON FUNCTION sp_reopen_period(bigint, text) TO rc_staff")
        call(conn, "staff", ctx["staff"], "SELECT sp_reopen_period(%s, 'x')", (ctx["pid"],))
    with raises("reopen reason required"):
        call(conn, "partner", ctx["partner"],
             "SELECT sp_reopen_period(%s, ' ')", (ctx["pid"],))
    call(conn, "partner", ctx["partner"],
         "SELECT sp_reopen_period(%s, 'client sent a missing invoice')", (ctx["pid"],))
    assert conn.execute(
        "SELECT state::text, reopen_reason, client_approved_by, closed_by FROM return_period "
        "WHERE period_id = %s", (ctx["pid"],)).fetchone() == (
        "IN_REVIEW", "client sent a missing invoice", None, None)
    rev = propose(conn, ctx, rid)
    assert rev is not None


def test_reopen_only_from_closed(conn):
    ctx, _, _ = with_invoice(conn)
    with raises("illegal period transition IN_REVIEW -> IN_REVIEW"):
        call(conn, "partner", ctx["partner"],
             "SELECT sp_reopen_period(%s, 'why')", (ctx["pid"],))


def _grant_import(conn, role):
    conn.execute(f"GRANT SELECT, INSERT, UPDATE ON return_period, gstr2b_import, gstr2b_entry, "
                 f"party TO {role}")


def test_staff_import_into_closed_raises(conn):
    ctx, _, _ = closed_period(conn)
    _grant_import(conn, "rc_staff")
    payload = build_2b_payload(ctx["g"], "092026", [doc(ctx["sup"], "INV-9", 900)])
    with raises("period is closed"):
        with conn.transaction():
            as_role(conn, "staff", ctx["staff"])
            import_2b(conn, ctx["pid"], payload, ctx["staff"])
    assert state(conn, ctx["pid"]) == "CLOSED"


def test_partner_import_reopens_closed(conn):
    ctx, _, _ = closed_period(conn)
    _grant_import(conn, "rc_partner")
    payload = build_2b_payload(ctx["g"], "092026", [doc(ctx["sup"], "INV-9", 900)])
    with conn.transaction():
        as_role(conn, "partner", ctx["partner"])
        import_2b(conn, ctx["pid"], payload, ctx["partner"])
        conn.execute("RESET ROLE")
    assert conn.execute(
        "SELECT state::text, client_approved_by, closed_by FROM return_period "
        "WHERE period_id = %s", (ctx["pid"],)).fetchone() == ("IN_REVIEW", None, None)


def test_import_into_awaiting_moves_to_in_review(conn):
    ctx, _, _ = with_invoice(conn)
    submit(conn, ctx)
    client_approve(conn, ctx)
    imp2 = portal(conn, ctx, [doc(ctx["sup"], "INV-1", 1700)])
    # Pass 2 sends the month back to review but keeps the client's approval
    assert conn.execute(
        "SELECT state::text, client_approved_by FROM return_period WHERE period_id = %s",
        (ctx["pid"],)).fetchone() == ("IN_REVIEW", ctx["client"])
    reconcile(conn, imp2, ctx["staff"])
    submit(conn, ctx)
    close(conn, ctx)  # approval guard passes without a second client approval
    assert state(conn, ctx["pid"]) == "CLOSED"


def test_staff_books_staging_into_closed_raises(conn):
    ctx, _, _ = closed_period(conn)
    conn.execute("GRANT INSERT ON books_staging TO rc_staff")
    with raises("period is closed"):
        with conn.transaction():
            as_role(conn, "staff", ctx["staff"])
            conn.execute("INSERT INTO books_staging(period_id, invoice_number) VALUES (%s, 'X')",
                         (ctx["pid"],))


# --- audit log -----------------------------------------------------------------------

def test_audit_rows(conn):
    ctx, _, rid = with_invoice(conn, books_igst=30000, portal_igst=10000)
    rev = propose(conn, ctx, rid)
    call(conn, "partner", ctx["partner"], "SELECT sp_approve(%s)", (rev,))
    rows = conn.execute(
        "SELECT action, user_id, old_value->>'state', new_value->>'state', "
        "fin_year = fin_year_of(current_date) "
        "FROM audit_log WHERE table_name = 'review' AND row_id = %s ORDER BY log_id",
        (rev,)).fetchall()
    assert rows == [
        ("INSERT", ctx["staff"], None, "PROPOSED", True),
        ("UPDATE", ctx["partner"], "PROPOSED", "APPROVED", True),
    ]


def test_audit_period_and_import(conn):
    ctx, imp, _ = with_invoice(conn)
    rows = conn.execute(
        "SELECT action, old_value->>'state', new_value->>'state' FROM audit_log "
        "WHERE table_name = 'return_period' AND row_id = %s ORDER BY log_id",
        (ctx["pid"],)).fetchall()
    assert rows == [("INSERT", None, "OPEN"), ("UPDATE", "OPEN", "IN_REVIEW")]
    new = conn.execute(
        "SELECT new_value FROM audit_log WHERE table_name = 'gstr2b_import' AND row_id = %s "
        "AND action = 'INSERT'", (imp,)).fetchone()[0]
    assert "payload" not in new and new["import_id"] == imp
    conn.execute("DELETE FROM recon_result WHERE import_id = %s", (imp,))
    conn.execute("DELETE FROM gstr2b_entry WHERE import_id = %s", (imp,))
    conn.execute("DELETE FROM gstr2b_import WHERE import_id = %s", (imp,))
    old = conn.execute(
        "SELECT old_value, new_value FROM audit_log WHERE table_name = 'gstr2b_import' "
        "AND row_id = %s AND action = 'DELETE'", (imp,)).fetchone()
    assert old[0]["import_id"] == imp and "payload" not in old[0] and old[1] is None


def test_audit_log_partitioned(conn):
    assert conn.execute("SELECT relkind::text FROM pg_class WHERE relname = 'audit_log'"
                        ).fetchone()[0] == "p"
    parts = conn.execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = 'audit_log'::regclass ORDER BY 1").fetchall()
    assert [p[0] for p in parts] == ["audit_log_2025_26", "audit_log_2026_27",
                                     "audit_log_2027_28", "audit_log_default"]


def test_audit_without_app_user(conn):
    ctx = setup(conn)
    conn.execute("SELECT set_config('app.user_id', '', true)")
    conn.execute("UPDATE return_period SET state = 'IN_REVIEW' WHERE period_id = %s",
                 (ctx["pid"],))
    assert conn.execute(
        "SELECT user_id FROM audit_log WHERE table_name = 'return_period' AND row_id = %s "
        "AND action = 'UPDATE'", (ctx["pid"],)).fetchone() == (None,)


# --- carry-forward after a close, results guard ----------------------------------------

def closed_september_with_chase(conn):
    """September closed with two approved CHASE reviews (SEP-A, SEP-B)."""
    ctx = setup(conn)
    ctx["sup"] = s = make_party(conn, "AAAPA1111A", SUP)
    for no in ("SEP-A", "SEP-B"):
        make_invoice(conn, ctx["g"], s, no, "2026-09-10",
                     [dict(taxable_value=10000, igst=1800)], pos="07")
    imp = portal(conn, ctx, [doc(s, "SEP-A", 1500), doc(s, "SEP-B", 1500)])
    reconcile(conn, imp, ctx["staff"])
    revs = {}
    for no in ("SEP-A", "SEP-B"):
        rid = conn.execute(
            "SELECT r.result_id FROM recon_result r JOIN invoice i "
            "ON i.invoice_id = r.invoice_id AND i.fin_year = r.invoice_fy "
            "WHERE r.import_id = %s AND i.invoice_no = %s", (imp, no)).fetchone()[0]
        revs[no] = call(conn, "staff", ctx["staff"],
                        "SELECT sp_propose(%s, 'CHASE', 'PENDING', NULL, NULL)", (rid,))[0]
    assert conn.execute("SELECT count(*) FROM review WHERE state = 'APPROVED'"
                        ).fetchone()[0] == 2
    submit(conn, ctx)
    client_approve(conn, ctx)
    close(conn, ctx)
    return ctx, imp, revs


def review_row(conn, rev):
    return conn.execute(
        "SELECT r.state::text, p.period FROM review r "
        "JOIN return_period p ON p.period_id = r.last_period_id WHERE r.review_id = %s",
        (rev,)).fetchone()


def test_carry_forward_after_close(conn):
    ctx, _, revs = closed_september_with_chase(conn)
    p10 = make_period(conn, ctx["g"], "102026")
    # SEP-B is filed late with a different amount; it shows up in October's 2B
    imp10 = import_2b(conn, p10, build_2b_payload(ctx["g"], "102026",
                                                  [doc(ctx["sup"], "SEP-B", 1600)]),
                      ctx["staff"])
    reconcile(conn, imp10, ctx["staff"])
    n = call(conn, "staff", ctx["staff"], "SELECT sp_refresh_reviews(%s)", (imp10,))[0]
    assert n == 1
    assert review_row(conn, revs["SEP-B"]) == ("PROPOSED", "102026")
    call(conn, "staff", ctx["staff"], "SELECT sp_mark_done(%s, '2026-10-20')",
         (revs["SEP-A"],))
    assert review_row(conn, revs["SEP-A"]) == ("DONE", "092026")


def test_closed_review_without_later_open_month(conn):
    ctx, _, revs = closed_september_with_chase(conn)
    with raises("period is closed"):
        call(conn, "staff", ctx["staff"], "SELECT sp_mark_done(%s, '2026-10-20')",
             (revs["SEP-A"],))
    # an earlier month does not count as "later"
    make_period(conn, ctx["g"], "082026")
    with raises("period is closed"):
        call(conn, "staff", ctx["staff"], "SELECT sp_mark_done(%s, '2026-10-20')",
             (revs["SEP-A"],))
    assert review_row(conn, revs["SEP-A"]) == ("APPROVED", "092026")


def test_results_of_closed_month_are_read_only(conn):
    ctx, imp, _ = closed_period(conn)
    # superuser counts as a partner: still refused
    with raises("period is closed"):
        with conn.transaction():
            reconcile(conn, imp, ctx["partner"])
    with raises("period is closed"):
        with conn.transaction():
            conn.execute("UPDATE recon_result SET diff_tax = 0 WHERE import_id = %s", (imp,))
    call(conn, "partner", ctx["partner"],
         "SELECT sp_reopen_period(%s, 'recheck')", (ctx["pid"],))
    reconcile(conn, imp, ctx["partner"])
