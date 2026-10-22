from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from httpx import ASGITransport, AsyncClient
import pytest

from app.core.config import Settings
from app.main import create_app
from deploy.local_gateway import Gateway

TOKEN = "test-token-for-local-gateway"


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.fixture
def gateway():
    calls = []

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            calls.append({"path": self.path, "headers": dict(self.headers), "method": self.command})
            body = json.dumps({"status": "ok", "data": {"authenticated": True}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "untrusted=value")
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

    with running(ThreadingHTTPServer(("127.0.0.1", 0), Upstream)) as upstream:
        with running(Gateway(0, upstream.server_port, TOKEN)) as server:
            def request(path, method="GET", headers=None, body=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                try:
                    connection.request(method, path, headers=headers or {}, body=body)
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()
            yield server, request, calls


def test_gateway_keeps_token_out_of_browser_and_read_requests(gateway):
    server, request, calls = gateway
    code, headers, body = request("/api/v1/auth/status", headers={"X-NMS-Local-Auth": "1"})
    assert code == 200 and headers["X-NMS-Local-Gateway"] == "1"
    assert calls[-1]["headers"]["X-Admin-Token"] == TOKEN
    assert TOKEN.encode() not in body and TOKEN not in str(headers)
    assert "Set-Cookie" not in headers and headers["Cache-Control"] == "no-store"
    request("/api/v1/health", headers={"X-Admin-Token": "do-not-forward", "Cookie": "ignore=me"})
    assert "X-Admin-Token" not in calls[-1]["headers"] and "Cookie" not in calls[-1]["headers"]
    for path in ("/api/v1/events/42/acknowledge", "/api/v1/risks/5/acknowledge"):
        code, _, body = request(path, "POST", {"Origin": server.origin, "X-NMS-Local-Auth": "1",
                                             "Sec-Fetch-Site": "same-origin"})
        assert code == 200 and calls[-1]["headers"]["X-Admin-Token"] == TOKEN
        assert TOKEN.encode() not in body


@pytest.mark.parametrize("path,method,headers", [
    ("/api/v1/auth/status", "GET", {}),
    ("/api/v1/auth/status", "GET", {"Host": "attacker.example", "X-NMS-Local-Auth": "1"}),
    ("/api/v1/auth/status", "GET", {"Origin": "https://attacker.example", "X-NMS-Local-Auth": "1"}),
    ("/api/v1/auth/status", "GET", {"Sec-Fetch-Site": "cross-site", "X-NMS-Local-Auth": "1"}),
    ("/api/v1/auth/status", "GET", {"Sec-Fetch-Site": "same-site", "X-NMS-Local-Auth": "1"}),
    ("/api/v1/events/1/acknowledge", "POST", {"X-NMS-Local-Auth": "1"}),
    ("/api/v1/events/1/acknowledge", "POST", {"Origin": "null", "X-NMS-Local-Auth": "1"}),
    ("/api/v1/events/1/acknowledge", "OPTIONS", {}),
    ("/", "GET", {"Sec-Fetch-Dest": "iframe"}),
    ("http://attacker.example/", "GET", {}),
])
def test_gateway_rejects_cross_origin_or_unauthorized_requests(gateway, path, method, headers):
    _, request, calls = gateway
    assert request(path, method, headers)[0] in (400, 403)
    assert not calls


def test_gateway_rejects_unknown_writes_and_oversized_bodies(gateway):
    server, request, calls = gateway
    headers = {"Origin": server.origin, "X-NMS-Local-Auth": "1"}
    assert request("/api/v1/unknown", "POST", headers)[0] == 403
    assert request("/api/v1/events/1/acknowledge", "POST",
                   {**headers, "Content-Length": "65537"})[0] == 413
    assert not calls


def test_auth_status_checks_credentials_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("NMS_API_TOKEN", TOKEN)

    async def verify():
        app = create_app(replace(Settings(), db_path=tmp_path / "auth.sqlite3", enable_live=False, poll_interval_s=0))
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/api/v1/auth/status")).status_code == 401
                assert (await client.get("/api/v1/auth/status", headers={"X-Admin-Token": "wrong"})).status_code == 401
                response = await client.get("/api/v1/auth/status", headers={"X-Admin-Token": TOKEN})
                assert response.status_code == 200 and response.json()["data"] == {"authenticated": True}
                assert TOKEN not in response.text and "X-NMS-Local-Gateway" not in response.headers
                assert (await client.get("/api/v1/collector-runs")).json()["data"]["items"] == []
                assert (await client.get("/static/auth.js")).status_code == 200
    asyncio.run(verify())
