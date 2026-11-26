"""The ImagePipe web UI: `imagepipe serve`.

A small built-in web server (Python standard library only). It edits the
config file and runs the same commands as the command line - each job runs as
a separate `imagepipe` process, so a job behaves exactly as it does in a
terminal. One job at a time. LAN use only: there is no login.
"""

from __future__ import annotations

import collections
import json
import mimetypes
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

from imagepipe import __version__
from imagepipe.config import ConfigError, load_config
from imagepipe.web.schema import SCHEMA

# Commands the UI may run: id -> (label, argv builder from options)
JOBS = {
    "catalogue": "Catalogue a source",
    "report": "Rebuild catalogue reports",
    "export-csv": "Export catalogue CSV",
    "notify-test": "Send a test notification",
    "triage-scan": "Triage: scan",
    "triage-plan": "Triage: plan (dry run)",
    "triage-apply": "Triage: apply (moves files)",
    "triage-export-csv": "Triage: export CSV",
}


def build_argv(job: str, opts: dict) -> list[str]:
    src = str(opts.get("source") or "").strip()
    if job == "catalogue":
        argv = ["catalogue"] + (["--source", src] if src else [])
        return argv + (["--retry-errors"] if opts.get("retry_errors") else [])
    if job in ("report", "export-csv"):
        return [job] + (["--source", src] if src else [])
    if job == "notify-test":
        return ["notify-test"]
    if job == "triage-scan":
        reread = str(opts.get("reread") or "").strip()
        flag = {"problems": ["--reread-problems"], "all": ["--reread-all"]}.get(reread, [])
        return ["triage", "scan"] + (["--source", src] if src else []) + flag
    if job == "triage-plan":
        return ["triage", "plan"]
    if job == "triage-apply":
        limit = str(opts.get("limit") or "").strip()
        if limit and not limit.isdigit():
            raise ValueError("Limit must be a whole number")
        return ["triage", "apply"] + (["--limit", limit] if limit else [])
    if job == "triage-export-csv":
        return ["triage", "export-csv"]
    raise ValueError(f"Unknown job '{job}'")


class JobRunner:
    """Runs one imagepipe command at a time and keeps its output."""

    def __init__(self, config_path: Path, log_dir_fn):
        self.config_path = config_path
        self.log_dir_fn = log_dir_fn
        self.lock = threading.Lock()
        self.job: dict | None = None
        self.history: collections.deque = collections.deque(maxlen=25)
        self.proc: subprocess.Popen | None = None
        self.seq = 0

    def snapshot(self, since: int = 0) -> dict:
        with self.lock:
            job = self.job
            if job is None:
                return {"job": None, "history": list(self.history)}
            lines = job["lines"][since:] if since <= len(job["lines"]) else []
            return {
                "job": {k: job[k] for k in ("id", "kind", "label", "argv", "status", "started", "finished",
                                            "returncode")}
                | {"progress": list(job["progress"].values()), "lines": lines, "line_count": len(job["lines"])},
                "history": list(self.history),
            }

    def start(self, kind: str, opts: dict) -> dict:
        argv = build_argv(kind, opts)
        with self.lock:
            if self.job is not None and self.job["status"] == "running":
                raise RuntimeError("A job is already running. Wait for it or stop it first.")
            self.seq += 1
            job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{self.seq}"
            env = dict(os.environ, PYTHONUNBUFFERED="1", IMAGEPIPE_PROGRESS_INTERVAL="1")
            cmd = [sys.executable, "-m", "imagepipe", "-c", str(self.config_path), *argv]
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            self.job = {"id": job_id, "kind": kind, "label": JOBS.get(kind, kind), "argv": argv,
                        "status": "running", "started": datetime.now().isoformat(timespec="seconds"),
                        "finished": None, "returncode": None, "lines": [],
                        "progress": collections.OrderedDict()}
        threading.Thread(target=self._pump, args=(self.proc, self.job), daemon=True).start()
        return {"id": job_id}

    def stop(self) -> None:
        with self.lock:
            if self.proc is not None and self.job and self.job["status"] == "running":
                self.proc.send_signal(signal.SIGINT)  # the same as Ctrl+C: stops cleanly, keeps what is done
                self.job["status"] = "stopping"

    def _pump(self, proc: subprocess.Popen, job: dict) -> None:
        buf = b""
        while True:
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                cut = min((i for i in (buf.find(b"\n"), buf.find(b"\r")) if i != -1), default=-1)
                if cut == -1:
                    break
                line, buf = buf[:cut], buf[cut + 1:]
                self._line(job, line.decode("utf-8", "replace").replace("\x1b[2K", "").rstrip())
        if buf:
            self._line(job, buf.decode("utf-8", "replace").rstrip())
        rc = proc.wait()
        with self.lock:
            # the last value of each progress bar goes into the log
            job["lines"].extend(job["progress"].values())
            job["returncode"] = rc
            job["status"] = {0: "finished", 130: "interrupted"}.get(rc, "failed")
            job["finished"] = datetime.now().isoformat(timespec="seconds")
            self.history.appendleft({k: job[k] for k in ("id", "label", "argv", "status", "started", "finished",
                                                         "returncode")})
            lines = list(job["lines"])
        try:
            log_dir = self.log_dir_fn()
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / f"ui-job-{job['id']}.log").write_text(
                f"$ imagepipe {' '.join(job['argv'])}\n" + "\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

    def _line(self, job: dict, line: str) -> None:
        if not line:
            return
        with self.lock:
            if line.startswith("[") and "]" in line:
                label = line[1:line.index("]")]
                prog = job["progress"]
                if label not in prog and prog:
                    # a new progress bar: the earlier ones are finished - keep their last line in the log
                    job["lines"].extend(prog.values())
                    prog.clear()
                prog[label] = line
            else:
                job["lines"].append(line)
                if len(job["lines"]) > 5000:
                    del job["lines"][:1000]


class App:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.runner = JobRunner(self.config_path, self._log_dir)

    # -- config ----------------------------------------------------------------
    def cfg(self):
        return load_config(self.config_path)

    def _log_dir(self) -> Path:
        try:
            return self.cfg().log_dir
        except Exception:
            return Path(tempfile.gettempdir())

    def raw_config(self) -> str:
        return self.config_path.read_text(encoding="utf-8") if self.config_path.is_file() else ""

    def save_config_text(self, text: str) -> dict:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return {"ok": False, "error": f"Not valid YAML: {exc}"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "The config must be a mapping"}
        folder = self.config_path.parent
        if not os.access(folder, os.W_OK):
            return {"ok": False, "error": f"The config folder {folder} is read-only. Mount it read-write to save."}
        fd, tmp = tempfile.mkstemp(prefix=".imagepipe-", suffix=".yml", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            try:
                load_config(tmp)
            except ConfigError as exc:
                return {"ok": False, "error": str(exc)}
            backup = None
            if self.config_path.exists():
                backup = folder / f"{self.config_path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}"
                backup.write_bytes(self.config_path.read_bytes())
            os.replace(tmp, self.config_path)
            tmp = None
            return {"ok": True, "backup": str(backup) if backup else None}
        finally:
            if tmp and os.path.exists(tmp):
                os.unlink(tmp)

    def save_config_data(self, data: dict) -> dict:
        text = "# Saved by the ImagePipe web UI. The previous file is kept as a .bak copy.\n"
        text += yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
        return self.save_config_text(text)

    def config_data(self) -> dict:
        try:
            data = yaml.safe_load(self.raw_config()) or {}
            return data if isinstance(data, dict) else {}
        except yaml.YAMLError:
            return {}

    # -- info ------------------------------------------------------------------
    def meta(self) -> dict:
        out = {"version": __version__, "config_path": str(self.config_path),
               "config_writable": os.access(self.config_path.parent, os.W_OK), "jobs": JOBS,
               "sources": [], "triage_sources": [], "config_error": None, "planned": None}
        try:
            cfg = self.cfg()
            out["sources"] = [s.name for s in cfg.sources]
            if cfg.triage:
                out["triage_sources"] = [s.name for s in cfg.triage.sources]
                db = self._triage_db()
                if db is not None:
                    with db:
                        row = db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files WHERE status='planned'"
                                         ).fetchone()
                        out["planned"] = {"files": row[0], "bytes": row[1]}
        except ConfigError as exc:
            out["config_error"] = str(exc)
        return out

    def status_text(self) -> str:
        parts = []
        for argv in (["status"], ["triage", "status"]):
            try:
                res = subprocess.run([sys.executable, "-m", "imagepipe", "-c", str(self.config_path), *argv],
                                     capture_output=True, text=True, timeout=120)
                parts.append((res.stdout + res.stderr).strip())
            except subprocess.TimeoutExpired:
                parts.append(f"({' '.join(argv)} took too long)")
        return "\n\n".join(p for p in parts if p)

    def _triage_db(self) -> sqlite3.Connection | None:
        cfg = self.cfg()
        if not cfg.triage or not cfg.triage.database.is_file():
            return None
        db = sqlite3.connect(f"file:{cfg.triage.database}?mode=ro", uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def plan_rows(self, q: dict) -> dict:
        db = self._triage_db()
        if db is None:
            return {"rows": [], "total": 0, "facets": []}
        where, params = ["f.status='planned'"], []
        if q.get("bucket"):
            where.append("f.bucket=?")
            params.append(q["bucket"])
        if q.get("rule"):
            where.append("f.rule=?")
            params.append(q["rule"])
        if q.get("q"):
            where.append("(f.origin LIKE ? OR f.dest_path LIKE ?)")
            params += [f"%{q['q']}%"] * 2
        w = " AND ".join(where)
        limit, offset = min(int(q.get("limit") or 200), 1000), int(q.get("offset") or 0)
        with db:
            total = db.execute(f"SELECT COUNT(*) FROM files f WHERE {w}", params).fetchone()[0]
            rows = db.execute(f"""
                SELECT f.triage_id, f.origin, f.kind, f.size, f.bucket, f.rule, f.name_date, f.date_source,
                       f.smartname, f.smartname_source, f.dest_path, p.triage_id AS companion_of
                FROM files f LEFT JOIN files p ON p.id = f.companion_of
                WHERE {w} ORDER BY f.source, f.logical_path LIMIT ? OFFSET ?""", (*params, limit, offset)).fetchall()
            facets = db.execute("SELECT bucket, rule, COUNT(*) AS n FROM files WHERE status='planned' "
                                "GROUP BY bucket, rule ORDER BY bucket, n DESC").fetchall()
        return {"rows": [dict(r) for r in rows], "total": total, "facets": [dict(r) for r in facets]}

    def action_rows(self, q: dict) -> dict:
        db = self._triage_db()
        if db is None:
            return {"rows": [], "total": 0, "kinds": []}
        where, params = ["1=1"], []
        if q.get("action"):
            where.append("action=?")
            params.append(q["action"])
        if q.get("q"):
            where.append("(from_path LIKE ? OR to_path LIKE ? OR triage_id LIKE ? OR detail LIKE ?)")
            params += [f"%{q['q']}%"] * 4
        w = " AND ".join(where)
        limit, offset = min(int(q.get("limit") or 200), 1000), int(q.get("offset") or 0)
        with db:
            total = db.execute(f"SELECT COUNT(*) FROM actions WHERE {w}", params).fetchone()[0]
            rows = db.execute(f"SELECT ts, triage_id, action, from_path, to_path, rule, detail FROM actions "
                              f"WHERE {w} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
            kinds = [r[0] for r in db.execute("SELECT DISTINCT action FROM actions ORDER BY action")]
        return {"rows": [dict(r) for r in rows], "total": total, "kinds": kinds}

    def report_roots(self) -> list[Path]:
        cfg = self.cfg()
        roots = [cfg.reports_dir, cfg.log_dir]
        if cfg.triage:
            roots += [cfg.triage.reports_dir, cfg.triage.log_dir]
        out = []
        for r in roots:
            if r not in out:
                out.append(r)
        return out

    def reports(self, limit: int | None = 300) -> list[dict]:
        files = []
        for root in self.report_roots():
            if not root.is_dir():
                continue
            for p in root.rglob("*"):
                if p.is_file() and p.suffix in (".md", ".csv", ".log", ".txt"):
                    st = p.stat()
                    files.append({"path": str(p), "name": str(p.relative_to(root)), "root": str(root),
                                  "size": st.st_size, "mtime": st.st_mtime,
                                  "protected": p.name in self.PROTECTED_NAMES})
        files.sort(key=lambda f: f["mtime"], reverse=True)
        return files if limit is None else files[:limit]

    PROTECTED_NAMES = {"actions.csv"}  # the audit trail; never deleted from the Reports tab

    def delete_reports(self, paths: list[str] | None = None, everything: bool = False) -> dict:
        """Delete listed logs/reports - the given ones, or all of them. Returns counts."""
        job = self.runner.job
        if job is not None and job["status"] in ("running", "stopping"):
            raise RuntimeError("A job is running. Wait for it to finish before deleting logs.")
        if everything:
            targets = [f["path"] for f in self.reports(limit=None)]
        else:
            targets = list(paths or [])
        deleted, skipped, freed = 0, [], 0
        for path in targets:
            p = self.allowed_file(path)
            if p is None:
                skipped.append({"path": path, "why": "not a log or report"})
                continue
            if p.name in self.PROTECTED_NAMES:
                skipped.append({"path": str(p), "why": "audit trail - kept"})
                continue
            size = p.stat().st_size
            p.unlink()
            deleted += 1
            freed += size
        # remove folders left empty (e.g. plan-<time>/), never the roots themselves
        for root in self.report_roots():
            if not root.is_dir():
                continue
            for d in sorted((x for x in root.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
                try:
                    d.rmdir()
                except OSError:
                    pass
        return {"ok": True, "deleted": deleted, "freed": freed, "skipped": skipped}

    def allowed_file(self, path: str) -> Path | None:
        p = Path(os.path.realpath(path))
        for root in self.report_roots():
            r = os.path.realpath(root)
            if str(p).startswith(r + os.sep) and p.is_file():
                return p
        return None


# ---------------------------------------------------------------- HTTP
def make_handler(app: App):
    static = resources.files("imagepipe.web") / "static"

    class Handler(BaseHTTPRequestHandler):
        server_version = f"ImagePipe/{__version__}"

        def log_message(self, fmt, *args):  # keep the container log quiet
            pass

        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 10 << 20:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _error(self, exc: Exception, status=HTTPStatus.BAD_REQUEST):
            self._json({"ok": False, "error": str(exc)}, status)

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path in ("/", "/index.html"):
                    body = (static / "index.html").read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif url.path == "/api/meta":
                    self._json(app.meta())
                elif url.path == "/api/schema":
                    self._json(SCHEMA)
                elif url.path == "/api/config":
                    self._json({"data": app.config_data(), "raw": app.raw_config()})
                elif url.path == "/api/job":
                    self._json(app.runner.snapshot(int(q.get("since") or 0)))
                elif url.path == "/api/status":
                    self._json({"text": app.status_text()})
                elif url.path == "/api/plan":
                    self._json(app.plan_rows(q))
                elif url.path == "/api/actions":
                    self._json(app.action_rows(q))
                elif url.path == "/api/reports":
                    self._json(app.reports())
                elif url.path == "/api/file":
                    p = app.allowed_file(q.get("path", ""))
                    if p is None:
                        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    body = p.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    ctype = "text/plain; charset=utf-8"
                    if q.get("download"):
                        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                        self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except ConfigError as exc:
                self._error(exc)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self._error(exc, HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_POST(self):
            url = urlparse(self.path)
            try:
                body = self._body()
                if url.path == "/api/job/start":
                    self._json({"ok": True, **app.runner.start(body.get("kind", ""), body.get("options") or {})})
                elif url.path == "/api/job/stop":
                    app.runner.stop()
                    self._json({"ok": True})
                elif url.path == "/api/config":
                    self._json(app.save_config_data(body.get("data") or {}))
                elif url.path == "/api/config/raw":
                    self._json(app.save_config_text(body.get("text") or ""))
                elif url.path == "/api/reports/delete":
                    self._json(app.delete_reports(body.get("paths") or [], everything=bool(body.get("all"))))
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (RuntimeError, ValueError) as exc:
                self._error(exc)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                self._error(exc, HTTPStatus.INTERNAL_SERVER_ERROR)

    return Handler


def serve(config_path: Path, host: str = "0.0.0.0", port: int = 8020) -> int:
    app = App(config_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    print(f"ImagePipe web UI {__version__} on http://{host}:{port}/ (config: {config_path})", file=sys.stderr,
          flush=True)

    def _term(*_):
        app.runner.stop()
        time.sleep(0.5)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
