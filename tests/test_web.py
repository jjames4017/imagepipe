"""Web UI: config editing, running jobs, plan/actions/report endpoints."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

from imagepipe.web.server import App, make_handler
from tests.test_triage import env  # noqa: F401  (fixture)


@pytest.fixture
def web(env):  # noqa: F811
    cfg, vault = env
    app = App(cfg.path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, cfg, vault, app
    httpd.shutdown()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as r:
        return json.loads(r.read())


def post(base, path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def wait_job(base, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        s = get(base, "/api/job")
        if s["job"] and s["job"]["status"] not in ("running", "stopping"):
            return s
        time.sleep(0.3)
    raise AssertionError("job did not finish")


def test_page_and_meta(web):
    base, cfg, vault, app = web
    with urllib.request.urlopen(base + "/", timeout=10) as r:
        assert b"ImagePipe" in r.read()
    meta = get(base, "/api/meta")
    assert meta["triage_sources"] == ["photos", "file-cabinet"] and meta["config_writable"]
    assert any(s["section"] == "triage" for s in get(base, "/api/schema"))


def test_save_config_validates_and_backs_up(web):
    base, cfg, vault, app = web
    data = get(base, "/api/config")["data"]
    data["triage"]["workers"] = 3
    r = post(base, "/api/config", {"data": data})
    assert r["ok"] and r["backup"]
    assert yaml.safe_load(cfg.path.read_text())["triage"]["workers"] == 3

    data["triage"]["verify"] = "sometimes"
    r = post(base, "/api/config", {"data": data})
    assert not r["ok"] and "verify" in r["error"]
    assert "verify" not in yaml.safe_load(cfg.path.read_text())["triage"]  # file unchanged

    r = post(base, "/api/config/raw", {"text": "manifest: [unclosed"})
    assert not r["ok"]


def test_run_scan_plan_and_review(web):
    base, cfg, vault, app = web
    assert post(base, "/api/job/start", {"kind": "triage-scan", "options": {}})["ok"]
    assert "already running" in post(base, "/api/job/start", {"kind": "triage-plan", "options": {}})["error"]
    s = wait_job(base)
    assert s["job"]["status"] == "finished", s
    assert any("3/3 Read" in line for line in s["job"]["lines"])

    assert post(base, "/api/job/start", {"kind": "triage-plan", "options": {}})["ok"]
    assert wait_job(base)["job"]["status"] == "finished"
    plan = get(base, "/api/plan?bucket=docupipe")
    assert plan["total"] >= 3 and all(r["bucket"] == "docupipe" for r in plan["rows"])
    assert get(base, "/api/meta")["planned"]["files"] > 0

    assert post(base, "/api/job/start", {"kind": "triage-apply", "options": {"limit": "2"}})["ok"]
    assert wait_job(base)["job"]["status"] == "finished"
    acts = get(base, "/api/actions?action=moved")
    assert acts["total"] == 2

    reports = get(base, "/api/reports")
    assert any(r["name"].endswith("summary.md") for r in reports)
    with urllib.request.urlopen(base + "/api/file?path=" + urllib.request.quote(reports[0]["path"]), timeout=10) as r:
        assert r.status == 200
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(base + "/api/file?path=/etc/passwd", timeout=10)


def test_bad_limit_rejected(web):
    base, *_ = web
    assert "whole number" in post(base, "/api/job/start", {"kind": "triage-apply", "options": {"limit": "1; rm"}})["error"]


def test_delete_reports(web):
    base, cfg, vault, app = web
    rep = cfg.triage.reports_dir
    (rep / "plan-1").mkdir(parents=True, exist_ok=True)
    (rep / "plan-1" / "summary.md").write_text("x")
    (rep / "plan-1" / "plan.csv").write_text("a,b\n")
    (rep / "actions.csv").write_text("ts,action\n")
    cfg.triage.log_dir.mkdir(parents=True, exist_ok=True)
    log = cfg.triage.log_dir / "triage-2026-11-26.log"
    log.write_text("hello\n")
    listed = {f["path"]: f for f in get(base, "/api/reports")}
    assert listed[str(rep / "actions.csv")]["protected"] and not listed[str(log)]["protected"]

    r = post(base, "/api/reports/delete", {"paths": [str(log), "/etc/passwd", str(rep / "actions.csv")]})
    assert r["ok"] and r["deleted"] == 1 and not log.exists()
    assert {x["why"] for x in r["skipped"]} == {"not a log or report", "audit trail - kept"}
    assert (rep / "actions.csv").exists() and Path("/etc/passwd").exists()

    r = post(base, "/api/reports/delete", {"all": True})
    assert r["ok"] and r["deleted"] >= 2
    assert not (rep / "plan-1").exists()  # emptied folder removed
    assert (rep / "actions.csv").exists()
    assert rep.is_dir()  # roots themselves stay

    app.runner.job = {"status": "running"}
    assert "job is running" in post(base, "/api/reports/delete", {"all": True})["error"]
    app.runner.job = None


def _state(pid: int) -> str:
    with open(f"/proc/{pid}/stat") as fh:
        return fh.read().rsplit(")", 1)[1].split()[0]


def test_pause_resume_and_stop_a_job(web, monkeypatch):
    import subprocess
    import sys as _sys

    import imagepipe.web.server as server

    base, cfg, vault, app = web
    script = ("import subprocess, sys, time\n"
              "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
              "print('child', child.pid, flush=True)\n"
              "try:\n"
              "    while True:\n"
              "        print('[Work] | tick', flush=True); time.sleep(0.2)\n"
              "except KeyboardInterrupt:\n"
              "    child.kill(); sys.exit(130)\n")
    real = subprocess.Popen
    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda cmd, **kw: real([_sys.executable, "-c", script], **kw))
    assert post(base, "/api/job/start", {"kind": "triage-scan", "options": {}})["ok"]
    for _ in range(50):
        lines = get(base, "/api/job")["job"]["lines"]
        if any(x.startswith("child ") for x in lines):
            break
        time.sleep(0.1)
    child = int(next(x for x in lines if x.startswith("child ")).split()[1])
    main = app.runner.proc.pid

    assert "No paused job" in post(base, "/api/job/resume", {})["error"]
    assert post(base, "/api/job/pause", {})["ok"]
    time.sleep(0.3)
    assert get(base, "/api/job")["job"]["status"] == "paused"
    assert _state(main) == "T" and _state(child) == "T"  # the whole job, workers included
    assert "already running" in post(base, "/api/job/start", {"kind": "triage-plan", "options": {}})["error"]

    assert post(base, "/api/job/resume", {})["ok"]
    time.sleep(0.3)
    assert get(base, "/api/job")["job"]["status"] == "running"
    assert _state(main) != "T" and _state(child) != "T"

    assert post(base, "/api/job/pause", {})["ok"]
    assert post(base, "/api/job/stop", {})["ok"]  # stopping a paused job resumes it so it can stop cleanly
    s = wait_job(base)
    assert s["job"]["status"] == "interrupted", s["job"]
    lines = s["job"]["lines"]
    assert any("paused at" in x for x in lines) and any("resumed at" in x for x in lines)
