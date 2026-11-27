"""Outside tools ImagePipe talks to: where they are, and a quick check that each answers.

Config section `tools:` (all optional; nothing is hard-coded, so ImagePipe works on any
system):

    tools:
      scansplitter: {url: http://host:8010}
      photoprism:   {url: http://host:2342, username: admin, password: ...}
      immich:       {url: http://host:2283, api_key: ...}
      a_eye:        {url: http://host:8000}
      digikam:      {url: http://host:3011}

check(tool, values) is what the web UI's Test buttons call. It never changes anything in
the tool: it reads a health/status page and, where a login is given, checks the login.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field, fields

from imagepipe.config import ConfigError

TIMEOUT = 8


@dataclass
class ScanSplitterTool:
    url: str = ""


@dataclass
class PhotoPrismTool:
    url: str = ""
    username: str = ""
    password: str = ""


@dataclass
class ImmichTool:
    url: str = ""
    api_key: str = ""


@dataclass
class AEyeTool:
    url: str = ""


@dataclass
class DigikamTool:
    url: str = ""


TOOL_CLASSES = {
    "scansplitter": ScanSplitterTool,
    "photoprism": PhotoPrismTool,
    "immich": ImmichTool,
    "a_eye": AEyeTool,
    "digikam": DigikamTool,
}


@dataclass
class ToolsConfig:
    scansplitter: ScanSplitterTool = field(default_factory=ScanSplitterTool)
    photoprism: PhotoPrismTool = field(default_factory=PhotoPrismTool)
    immich: ImmichTool = field(default_factory=ImmichTool)
    a_eye: AEyeTool = field(default_factory=AEyeTool)
    digikam: DigikamTool = field(default_factory=DigikamTool)


def parse_tools(data) -> ToolsConfig:
    if data is None:
        return ToolsConfig()
    if not isinstance(data, dict):
        raise ConfigError("'tools' must be a mapping")
    unknown = {str(k) for k in data} - set(TOOL_CLASSES)
    if unknown:
        raise ConfigError(f"Unknown tool(s) in 'tools': {', '.join(sorted(unknown))} "
                          f"(known: {', '.join(TOOL_CLASSES)})")
    out = ToolsConfig()
    for name, cls in TOOL_CLASSES.items():
        raw = data.get(name) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"'tools.{name}' must be a mapping")
        allowed = {f.name for f in fields(cls)}
        bad = {str(k) for k in raw} - allowed
        if bad:
            raise ConfigError(f"Unknown key(s) in 'tools.{name}': {', '.join(sorted(bad))} "
                              f"(known: {', '.join(sorted(allowed))})")
        values = {k: "" if raw.get(k) is None else str(raw.get(k)).strip() for k in allowed}
        url = values.get("url", "")
        if url and not url.startswith(("http://", "https://")):
            raise ConfigError(f"tools.{name}.url must start with http:// or https:// (got '{url}')")
        setattr(out, name, cls(**values))
    return out


# -- checks ---------------------------------------------------------------------------
def _request(url: str, method: str = "GET", body: dict | None = None, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json", **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False  # LAN tools often use self-signed certificates
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        return resp.status, resp.read(65536)


def _json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


def _why(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError) or "timed out" in str(reason):
            return f"no answer within {TIMEOUT} s (wrong address, firewall, or not reachable from this host)"
        return f"can't connect: {reason}"
    if isinstance(exc, TimeoutError):
        return f"no answer within {TIMEOUT} s"
    return f"{type(exc).__name__}: {exc}"


def check(tool: str, values: dict) -> dict:
    """{ok: bool, message: str} for one tool, using the given (possibly unsaved) values."""
    if tool not in TOOL_CLASSES:
        return {"ok": False, "message": f"Unknown tool '{tool}'"}
    url = str(values.get("url") or "").strip().rstrip("/")
    if not url:
        return {"ok": False, "message": "No address set"}
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "message": "The address must start with http:// or https://"}
    try:
        if tool == "scansplitter":
            status, raw = _request(f"{url}/api/health")
            ok = status == 200 and (_json(raw) or {}).get("status") == "ok"
            return {"ok": ok, "message": "ScanSplitter answered: healthy" if ok else f"Unexpected answer: {raw[:200]!r}"}
        if tool == "photoprism":
            status, raw = _request(f"{url}/api/v1/status")
            if (_json(raw) or {}).get("status") != "operational":
                return {"ok": False, "message": f"Unexpected answer from /api/v1/status: {raw[:200]!r}"}
            user, pw = str(values.get("username") or ""), str(values.get("password") or "")
            if not (user and pw):
                return {"ok": True, "message": "PhotoPrism is operational (no login set, so the login wasn't checked)"}
            try:
                _status, raw = _request(f"{url}/api/v1/session", "POST", {"username": user, "password": pw})
            except urllib.error.HTTPError as exc:
                if exc.code in (400, 401, 403):
                    return {"ok": False, "message": "PhotoPrism is operational, but the username or password was refused"}
                raise
            token = (_json(raw) or {}).get("access_token") or (_json(raw) or {}).get("id")
            if token:  # sign the test session out again
                try:
                    _request(f"{url}/api/v1/session", "DELETE", headers={"X-Auth-Token": token})
                except Exception:  # noqa: BLE001 - the check itself succeeded
                    pass
            return {"ok": True, "message": f"PhotoPrism is operational and the login for '{user}' works"}
        if tool == "immich":
            status, raw = _request(f"{url}/api/server/ping")
            if (_json(raw) or {}).get("res") != "pong":
                return {"ok": False, "message": f"Unexpected answer from /api/server/ping: {raw[:200]!r}"}
            key = str(values.get("api_key") or "")
            if not key:
                return {"ok": True, "message": "Immich answered (no API key set, so it wasn't checked)"}
            try:
                _request(f"{url}/api/users/me", headers={"x-api-key": key})
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    return {"ok": False, "message": "Immich answered, but the API key was refused"}
                raise
            return {"ok": True, "message": "Immich answered and the API key works"}
        # A-Eye and digiKam: the web page answering is the check
        status, _raw = _request(url)
        return {"ok": 200 <= status < 400, "message": f"Answered with HTTP {status}"}
    except Exception as exc:  # noqa: BLE001 - report any failure to the page
        return {"ok": False, "message": _why(exc)}
