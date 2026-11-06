"""GET endpoints. Each runs as the caller; row-level security decides what comes back.

A row the caller cannot see is indistinguishable from one that does not exist: both are
a 404, so the API never confirms another tenant's ids.
"""
import csv
import io

from starlette.responses import Response

from api import sql
from api.db import ApiError, tx

MATCH_STATUSES = {"EXACT", "ROUNDING", "PROBABLE", "MISMATCH", "PERIOD_MISMATCH",
                  "ONLY_IN_BOOKS", "ONLY_IN_2B", "DUPLICATE"}


def rows(c, query, params=()):
    cur = c.execute(query, params)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def one(c, query, params=(), missing="not found"):
    found = rows(c, query, params)
    if not found:
        raise ApiError(404, missing)
    return found[0]


def require_client(c, client_id):
    one(c, sql.CLIENT_VISIBLE, (client_id,), "client not found")


def require_period(c, period_id):
    return one(c, sql.PERIOD, (period_id,), "period not found")


def clients(ctx):
    with tx(ctx.conn, ctx.user) as c:
        return rows(c, sql.CLIENT_BOARD)


def client_periods(ctx):
    client_id = ctx.path("client_id")
    with tx(ctx.conn, ctx.user) as c:
        require_client(c, client_id)
        return rows(c, sql.CLIENT_PERIODS, (client_id,))


def period(ctx):
    period_id = ctx.path("period_id")
    with tx(ctx.conn, ctx.user) as c:
        p = require_period(c, period_id)
        totals = rows(c, sql.PERIOD_TOTALS, (period_id,))
        summary = rows(c, sql.PERIOD_SUMMARY, (period_id,))
    return {"period": p, "totals": totals[0] if totals else None,
            "summary": summary[0] if summary else None}


def queue(ctx):
    period_id = ctx.path("period_id")
    status = ctx.query("status") or None
    if status is not None and status not in MATCH_STATUSES:
        raise ApiError(400, "status: unknown match status")
    q = (ctx.query("q") or "").strip() or None
    with tx(ctx.conn, ctx.user) as c:
        return rows(c, sql.QUEUE, {"pid": period_id, "status": status, "q": q})


def result(ctx):
    result_id = ctx.path("result_id")
    with tx(ctx.conn, ctx.user) as c:
        detail = one(c, sql.RESULT_DETAIL, (result_id,), "result not found")
        review = rows(c, sql.RESULT_REVIEW, (result_id,))
    detail["review"] = review[0] if review else None
    return detail


def job(ctx):
    job_id = ctx.path("job_id")
    with tx(ctx.conn, ctx.user) as c:
        return one(c, sql.JOB, (job_id,), "job not found")


def period_jobs(ctx):
    period_id = ctx.path("period_id")
    with tx(ctx.conn, ctx.user) as c:
        require_period(c, period_id)
        return rows(c, sql.PERIOD_JOBS, (period_id,))


def ims_plan_csv(ctx):
    period_id = ctx.path("period_id")
    with tx(ctx.conn, ctx.user) as c:
        p = require_period(c, period_id)
        plan = c.execute(sql.IMS_PLAN, (period_id,)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["party_gstin", "invoice_no", "ims_action", "remark"])
    w.writerows(plan)
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="ims-plan-{p["period"]}.csv"'})


def _client_report(query):
    def handler(ctx):
        client_id = ctx.path("client_id")
        with tx(ctx.conn, ctx.user) as c:
            require_client(c, client_id)
            return rows(c, query, (client_id,))
    return handler


rule37 = _client_report(sql.RULE37)
party_scores = _client_report(sql.PARTY_SCORES)
filings = _client_report(sql.FILINGS)
alerts = _client_report(sql.ALERTS)
