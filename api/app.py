"""The Starlette application: routes, JSON errors, and (later) the static web app."""
import datetime as _dt
import decimal
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from api import auth, routes_read as rd, routes_write as wr, uploads
from api.config import CurrentUser, Settings, load_settings
from api.db import ApiError, borrowed_connection, owned_connection

log = logging.getLogger("reconcile.api")
MAX_UPLOAD = uploads.MAX_BYTES


def _default(o):
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (_dt.date, _dt.datetime)):
        return o.isoformat()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


class Json(JSONResponse):
    def render(self, content) -> bytes:
        return json.dumps(content, default=_default, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")


def error_response(status, message):
    return Json({"error": message}, status_code=status)


def parse_body(model, data):
    """Validate a request body; the first problem becomes `400 {"error": "<field>: <msg>"}`."""
    try:
        return model.model_validate(data)
    except ValidationError as e:
        err = e.errors()[0]
        field = ".".join(str(p) for p in err.get("loc", ())) or "body"
        raise ApiError(400, f"{field}: {err.get('msg', 'invalid value')}")


@dataclass
class Ctx:
    """What a handler receives. Handlers are plain functions run in a worker thread."""
    conn: Any
    user: Optional[CurrentUser]
    request: Request
    body: Any = None
    form: Any = None

    @property
    def settings(self) -> Settings:
        return self.request.app.state.settings

    def path(self, name, cast=int):
        try:
            return cast(self.request.path_params[name])
        except (KeyError, ValueError):
            raise ApiError(404, "not found")

    def query(self, name, default=None):
        return self.request.query_params.get(name, default)


async def _read_form(request):
    """Multipart fields as {name: str} and files as {name: (filename, bytes)}, read up
    front so handlers (which run in a thread) never touch the async request."""
    try:
        # One byte over the limit is enough to report "too large" precisely.
        form = await request.form(max_part_size=MAX_UPLOAD + 1024 * 1024)
    except Exception:
        raise ApiError(400, "upload could not be read (max 10 MB)")
    out = {}
    try:
        for name, value in form.multi_items():
            if isinstance(value, str):
                out[name] = value
            else:
                out[name] = (value.filename, await value.read())
    finally:
        await form.close()
    return out


def endpoint(fn: Callable, *, login_required=True, body: type[BaseModel] | None = None,
             form=False):
    async def handler(request: Request):
        user = auth.require_user(request) if login_required else None
        payload = None
        if body is not None:
            try:
                data = await request.json()
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, "request body is not valid JSON")
            payload = parse_body(body, data)
        form_data = None
        if form:
            form_data = await _read_form(request)

        def work():
            with request.app.state.connection() as conn:
                return fn(Ctx(conn, user, request, payload, form_data))

        result = await run_in_threadpool(work)
        if isinstance(result, Response):
            return result
        if result is None:
            return Response(status_code=204)
        if isinstance(result, tuple):
            status, content = result
            return Json(content, status_code=status)
        return Json(result)

    return handler


# --- auth ------------------------------------------------------------------------------

def _login(ctx: Ctx):
    return auth.login(ctx.conn, ctx.body, ctx.settings)


def _me(ctx: Ctx):
    return auth.user_json(ctx.user)


def routes():
    return [
        Route("/api/login", endpoint(_login, login_required=False, body=auth.LoginBody),
              methods=["POST"]),
        Route("/api/me", endpoint(_me), methods=["GET"]),
        Route("/api/periods/{period_id}/uploads/2b", endpoint(uploads.upload_2b, form=True),
              methods=["POST"]),
        Route("/api/periods/{period_id}/uploads/books",
              endpoint(uploads.upload_books, form=True), methods=["POST"]),
        *[Route(path, endpoint(fn), methods=["GET"]) for path, fn in (
            ("/api/clients", rd.clients),
            ("/api/clients/{client_id}/periods", rd.client_periods),
            ("/api/clients/{client_id}/rule37", rd.rule37),
            ("/api/clients/{client_id}/party-scores", rd.party_scores),
            ("/api/clients/{client_id}/filings", rd.filings),
            ("/api/clients/{client_id}/alerts", rd.alerts),
            ("/api/periods/{period_id}", rd.period),
            ("/api/periods/{period_id}/queue", rd.queue),
            ("/api/periods/{period_id}/jobs", rd.period_jobs),
            ("/api/periods/{period_id}/ims-plan.csv", rd.ims_plan_csv),
            ("/api/results/{result_id}", rd.result),
            ("/api/jobs/{job_id}", rd.job),
            ("/api/periods/{period_id}/followups", wr.followups),
        )],
        *[Route(path, endpoint(fn, body=model), methods=["POST"]) for path, fn, model in (
            ("/api/periods", wr.open_period, wr.OpenPeriodBody),
            ("/api/results/{result_id}/propose", wr.propose, wr.ProposeBody),
            ("/api/reviews/{review_id}/approve", wr.approve, None),
            ("/api/reviews/{review_id}/done", wr.mark_done, wr.DoneBody),
            ("/api/periods/{period_id}/submit", wr.submit, None),
            ("/api/periods/{period_id}/client-approve", wr.client_approve, None),
            ("/api/periods/{period_id}/override", wr.override, wr.ReasonBody),
            ("/api/periods/{period_id}/close", wr.close, None),
            ("/api/periods/{period_id}/reopen", wr.reopen, wr.ReasonBody),
            ("/api/clients/{client_id}/filings/generate", wr.generate_filings, wr.FilingsBody),
            ("/api/periods/{period_id}/followups", wr.add_followup, wr.FollowupBody),
        )],
    ]


async def _api_not_found(request):
    return error_response(404, "not found")


async def _api_error(request, exc: ApiError):
    return error_response(exc.status, exc.message)


async def _unexpected(request, exc: Exception):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return error_response(500, "internal error")


def create_app(settings: Settings, conn_factory: Optional[Callable] = None) -> Starlette:
    """`conn_factory` hands every request the same caller-owned connection (tests); by
    default each request opens, commits and closes its own."""
    all_routes = routes() + [
        # Unknown /api paths answer in JSON, never with the web app's index page.
        Route("/api/{rest:path}", _api_not_found, methods=["GET", "POST", "PUT", "DELETE"]),
        Mount("/", app=StaticFiles(directory=settings.static_dir, html=True, check_dir=False)),
    ]
    app = Starlette(routes=all_routes,
                    exception_handlers={ApiError: _api_error, Exception: _unexpected})
    app.state.settings = settings
    if conn_factory is None:
        app.state.connection = lambda: owned_connection(settings)
    else:
        app.state.connection = lambda: borrowed_connection(conn_factory())
    return app


def app_from_env() -> Starlette:
    """Entry point for uvicorn: `uvicorn --factory api.app:app_from_env`."""
    logging.basicConfig(level=logging.INFO)
    return create_app(load_settings())
