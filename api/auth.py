"""Login, session tokens, and the current user of a request.

Tokens are itsdangerous URL-safe timed signatures over {user, role, client, firm}, valid
for 8 hours. They prove who the user is; what they may do is decided by the database.
"""
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel

from api import sql
from api.config import ROLES, CurrentUser
from api.db import ApiError, tx

SALT = "reconcile-session"


def _serializer(settings):
    return URLSafeTimedSerializer(settings.secret, salt=SALT)


def sign(user: CurrentUser, settings) -> str:
    return _serializer(settings).dumps(
        {"u": user.user_id, "r": user.role, "c": user.client_id, "f": user.firm_id})


def verify(token: str, settings) -> CurrentUser:
    try:
        data = _serializer(settings).loads(token, max_age=settings.token_max_age)
    except SignatureExpired:
        raise ApiError(401, "session expired, please log in again")
    except BadSignature:
        raise ApiError(401, "invalid session")
    try:
        user = CurrentUser(user_id=int(data["u"]), role=str(data["r"]),
                           client_id=None if data["c"] is None else int(data["c"]),
                           firm_id=int(data["f"]))
    except (KeyError, TypeError, ValueError):
        raise ApiError(401, "invalid session")
    if user.role not in ROLES:
        raise ApiError(401, "invalid session")
    return user


def require_user(request) -> CurrentUser:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "please log in")
    return verify(token.strip(), request.app.state.settings)


class LoginBody(BaseModel):
    email: str
    password: str


def user_json(user: CurrentUser) -> dict:
    return {"user_id": user.user_id, "role": user.role, "client_id": user.client_id}


def login(conn, body: LoginBody, settings) -> dict:
    with tx(conn, None) as c:
        row = c.execute(sql.LOGIN, (body.email.strip(), body.password)).fetchone()
    if row is None:
        raise ApiError(401, "wrong email or password")
    user = CurrentUser(user_id=row[0], role=row[1], client_id=row[2], firm_id=row[3])
    return {"token": sign(user, settings), "user": user_json(user)}
