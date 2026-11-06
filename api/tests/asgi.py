"""A tiny synchronous ASGI test client.

Starlette's TestClient needs httpx, which this build environment cannot install, so the
API tests drive the app through the ASGI interface directly: build the HTTP scope, feed
the body, collect the response. Same app code path as uvicorn, minus the socket.
"""
import asyncio
import json as _json
import uuid
from urllib.parse import urlencode, urlsplit


class Response:
    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = {k.decode().lower(): v.decode() for k, v in headers}
        self.content = body

    @property
    def text(self):
        return self.content.decode("utf-8")

    def json(self):
        return _json.loads(self.content)

    def __repr__(self):
        return f"<Response {self.status_code} {self.content[:200]!r}>"


def _multipart(files, data):
    boundary = uuid.uuid4().hex
    out = b""
    for name, value in (data or {}).items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode()
    for name, (filename, content, ctype) in (files or {}).items():
        if isinstance(content, str):
            content = content.encode("utf-8")
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode()
        out += content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


class Client:
    def __init__(self, app):
        self.app = app

    def request(self, method, url, json=None, headers=None, files=None, data=None,
                params=None):
        parts = urlsplit(url)
        query = parts.query
        if params:
            query = (query + "&" if query else "") + urlencode(params)
        hdrs = {"host": "testserver"}
        body = b""
        if json is not None:
            body = _json.dumps(json).encode()
            hdrs["content-type"] = "application/json"
        elif files is not None or data is not None:
            body, hdrs["content-type"] = _multipart(files, data)
        hdrs["content-length"] = str(len(body))
        hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method.upper(), "scheme": "http", "path": parts.path,
            "raw_path": parts.path.encode(), "query_string": query.encode(),
            "root_path": "", "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
            "client": ("testclient", 50000), "server": ("testserver", 80),
        }
        sent = {"body": False}
        result = {"status": None, "headers": [], "body": b""}

        async def receive():
            if not sent["body"]:
                sent["body"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                result["status"] = message["status"]
                result["headers"] = message.get("headers", [])
            elif message["type"] == "http.response.body":
                result["body"] += message.get("body", b"")

        async def main():
            await self.app(scope, receive, send)

        asyncio.run(main())
        return Response(result["status"], result["headers"], result["body"])

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)
