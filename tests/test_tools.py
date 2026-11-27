"""External tools: config, and the Test checks against small fake servers."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from imagepipe.config import ConfigError
from imagepipe.tools import check, parse_tools


class Fake(BaseHTTPRequestHandler):
    routes: dict = {}
    seen: list = []

    def log_message(self, *a):
        pass

    def _send(self, method):
        key = (method, self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        Fake.seen.append((method, self.path, dict(self.headers), body))
        status, payload = Fake.routes.get(key, (404, {"error": "nope"}))
        if callable(payload):
            status, payload = payload(self.headers, body)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send("GET")

    def do_POST(self):
        self._send("POST")

    def do_DELETE(self):
        self._send("DELETE")


@pytest.fixture
def server():
    Fake.routes, Fake.seen = {}, []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_parse_tools():
    t = parse_tools({"scansplitter": {"url": "http://h:8010"}, "photoprism": {"url": "http://h:2342",
                                                                                  "username": "admin", "password": "x"}})
    assert t.scansplitter.url == "http://h:8010" and t.photoprism.username == "admin" and t.immich.url == ""
    assert parse_tools(None).digikam.url == ""
    with pytest.raises(ConfigError, match="Unknown tool"):
        parse_tools({"paperless": {}})
    with pytest.raises(ConfigError, match="Unknown key"):
        parse_tools({"immich": {"token": "x"}})
    with pytest.raises(ConfigError, match="http://"):
        parse_tools({"a_eye": {"url": "10.10.1.63:8000"}})


def test_scansplitter(server):
    Fake.routes[("GET", "/api/health")] = (200, {"status": "ok"})
    assert check("scansplitter", {"url": server + "/"})["ok"]
    assert not check("scansplitter", {"url": ""})["ok"]


def test_photoprism_login(server):
    Fake.routes[("GET", "/api/v1/status")] = (200, {"status": "operational"})
    Fake.routes[("POST", "/api/v1/session")] = (0, lambda h, b: (200, {"access_token": "tok"}) if b["password"] == "right"
                                                else (401, {"error": "bad"}))
    Fake.routes[("DELETE", "/api/v1/session")] = (200, {})
    assert "wasn't checked" in check("photoprism", {"url": server})["message"]
    r = check("photoprism", {"url": server, "username": "admin", "password": "right"})
    assert r["ok"] and "login" in r["message"]
    assert any(m == "DELETE" and h.get("X-Auth-Token") == "tok" for m, _p, h, _b in Fake.seen)  # signed out again
    r = check("photoprism", {"url": server, "username": "admin", "password": "wrong"})
    assert not r["ok"] and "refused" in r["message"]


def test_immich_key(server):
    Fake.routes[("GET", "/api/server/ping")] = (200, {"res": "pong"})
    Fake.routes[("GET", "/api/users/me")] = (0, lambda h, b: (200, {"id": 1}) if h.get("x-api-key") == "k"
                                             else (401, {"error": "bad"}))
    assert check("immich", {"url": server, "api_key": "k"})["ok"]
    assert "refused" in check("immich", {"url": server, "api_key": "x"})["message"]


def test_unreachable():
    r = check("a_eye", {"url": "http://127.0.0.1:9"})
    assert not r["ok"] and "connect" in r["message"]
