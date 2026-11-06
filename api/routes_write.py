"""POST endpoints: reviews, the month workflow, filings and supplier follow-ups.

Each one is a thin call to a Plan A procedure, run as the user. The procedure decides
whether the action is allowed; its message comes back verbatim as a 403/409.
"""
import datetime as _dt
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from api import sql
from api.db import tx
from api.routes_read import require_client, require_period, rows

Decision = Literal["ACCEPT", "CHASE", "DEFER", "PARTIAL", "INELIGIBLE"]
ImsAction = Literal["ACCEPT", "REJECT", "PENDING", "NONE"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OpenPeriodBody(Strict):
    gstin: str = Field(min_length=15, max_length=15)
    period: str = Field(pattern=r"^(0[1-9]|1[0-2])[0-9]{4}$")


class ProposeBody(Strict):
    decision: Decision
    ims_action: ImsAction
    claim_pct: Optional[Decimal] = Field(default=None, ge=0, le=100)
    remark: Optional[str] = Field(default=None, max_length=500)


class DoneBody(Strict):
    executed_on: _dt.date


class ReasonBody(Strict):
    reason: str = Field(default="", max_length=500)


class FilingsBody(Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    date_from: _dt.date = Field(alias="from")
    date_to: _dt.date = Field(alias="to")


class FollowupBody(Strict):
    party_gstin: str = Field(min_length=15, max_length=15)
    channel: Literal["EMAIL", "WHATSAPP", "CALL", "LETTER"]
    sent_on: Optional[_dt.date] = None
    response: Optional[str] = Field(default=None, max_length=1000)
    next_chase_on: Optional[_dt.date] = None
    status: Literal["OPEN", "SENT", "RESPONDED", "RESOLVED"] = "OPEN"


def open_period(ctx):
    b = ctx.body
    with tx(ctx.conn, ctx.user) as c:  # sp_open_period is idempotent: same id if open
        pid = c.execute(sql.OPEN_PERIOD, (b.gstin, b.period)).fetchone()[0]
    return 201, {"period_id": pid}


def propose(ctx):
    b = ctx.body
    result_id = ctx.path("result_id")
    with tx(ctx.conn, ctx.user) as c:
        rev = c.execute(sql.PROPOSE, (result_id, b.decision, b.ims_action, b.claim_pct,
                                      b.remark or None)).fetchone()[0]
    return {"review_id": rev}


def _call(query, *, body_args=lambda b: (), path="period_id"):
    def handler(ctx):
        key = ctx.path(path)
        with tx(ctx.conn, ctx.user) as c:
            c.execute(query, (key, *body_args(ctx.body)))
        return None  # 204
    return handler


approve = _call(sql.APPROVE, path="review_id")
mark_done = _call(sql.MARK_DONE, path="review_id", body_args=lambda b: (b.executed_on,))
submit = _call(sql.SUBMIT)
client_approve = _call(sql.CLIENT_APPROVE)
override = _call(sql.OVERRIDE, body_args=lambda b: (b.reason,))
close = _call(sql.CLOSE)
reopen = _call(sql.REOPEN, body_args=lambda b: (b.reason,))


def generate_filings(ctx):
    client_id = ctx.path("client_id")
    b = ctx.body
    with tx(ctx.conn, ctx.user) as c:
        require_client(c, client_id)
        inserted = 0
        for (gstin,) in c.execute(sql.CLIENT_GSTINS, (client_id,)).fetchall():
            inserted += c.execute(sql.GENERATE_FILINGS, (gstin, b.date_from, b.date_to)).fetchone()[0]
    return {"inserted": inserted}


def followups(ctx):
    period_id = ctx.path("period_id")
    with tx(ctx.conn, ctx.user) as c:
        require_period(c, period_id)
        return rows(c, sql.FOLLOWUPS, (period_id,))


def add_followup(ctx):
    period_id = ctx.path("period_id")
    b = ctx.body
    with tx(ctx.conn, ctx.user) as c:
        p = require_period(c, period_id)
        fid = c.execute(sql.INSERT_FOLLOWUP, (p["gstin"], b.party_gstin, period_id, b.channel,
                                              b.sent_on, b.response, b.next_chase_on,
                                              b.status)).fetchone()[0]
    return 201, {"followup_id": fid}
