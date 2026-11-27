"""A small client for ScanSplitter's Projects API (0.8+), used by the triage PDF check.

Only these calls are used: create a project, upload a PDF into it (one scan per page,
photo detection runs on each), read the project, approve a scan, export one scan (a
background job whose zip is downloaded), delete a scan or a project. ScanSplitter's own
Deliver is never used.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path

TIMEOUT = 120
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # ScanSplitter's limits per file
MAX_PDF_PAGES = 200


class ScanSplitterError(Exception):
    pass


class ScanSplitterDown(ScanSplitterError):
    """ScanSplitter can't be reached at all: the run stops rather than failing every file."""


class ScanSplitter:
    def __init__(self, url: str, timeout: int = TIMEOUT):
        if not url:
            raise ScanSplitterError("No ScanSplitter address set (Settings > External tools > ScanSplitter)")
        self.url = url.rstrip("/")
        self.timeout = timeout

    # -- plumbing ----------------------------------------------------------------
    def _call(self, method: str, path: str, body: dict | None = None, data: bytes | None = None,
              headers: dict | None = None, raw: bool = False):
        hdrs = {"Accept": "application/json", **(headers or {})}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            raise ScanSplitterError(f"{method} {path}: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise ScanSplitterDown(f"can't reach ScanSplitter at {self.url}: {exc.reason}") from exc
        if raw:
            return payload
        return json.loads(payload) if payload else {}

    # -- API ---------------------------------------------------------------------
    def health(self) -> bool:
        try:
            return self._call("GET", "/api/health").get("status") == "ok"
        except ScanSplitterError:
            return False

    def create_project(self, name: str) -> str:
        return self._call("POST", "/api/projects", {"name": name})["id"]

    def project(self, pid: str) -> dict:
        return self._call("GET", f"/api/projects/{pid}")

    def delete_project(self, pid: str) -> None:
        self._call("DELETE", f"/api/projects/{pid}")

    def add_pdf(self, pid: str, path: Path) -> list[dict]:
        """Upload one PDF; ScanSplitter makes one scan per page and starts detection."""
        boundary = uuid.uuid4().hex
        name = Path(path).name.replace('"', "_")
        body = io.BytesIO()
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{name}\"\r\n"
                   "Content-Type: application/pdf\r\n\r\n".encode("utf-8", "replace"))
        body.write(Path(path).read_bytes())
        body.write(f"\r\n--{boundary}--\r\n".encode())
        res = self._call("POST", f"/api/projects/{pid}/scans?detect=true", data=body.getvalue(),
                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        return res.get("scans", [])

    def detect_pending(self, pid: str) -> None:
        self._call("POST", f"/api/projects/{pid}/detect-pending")

    def approve(self, pid: str, sid: str) -> dict:
        return self._call("PATCH", f"/api/projects/{pid}/scans/{sid}", {"status": "approved"})

    def delete_scan(self, pid: str, sid: str) -> None:
        self._call("DELETE", f"/api/projects/{pid}/scans/{sid}")

    def export_scan(self, pid: str, sid: str, wait: float = 1800, tick=None) -> list[tuple[str, bytes]]:
        """Crop one scan's photos. Returns [(name, bytes)] for each photo in the export zip."""
        job = self._call("POST", f"/api/projects/{pid}/scans/{sid}/export", {})["job_id"]
        self.wait_job(job, wait, tick)
        data = self._call("GET", f"/api/jobs/{job}/download", raw=True, headers={"Accept": "*/*"})
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir() or info.filename.startswith("masters/") or "manifest" in info.filename:
                    continue
                out.append((Path(info.filename).name, zf.read(info)))
        return out

    def wait_job(self, job_id: str, wait: float = 1800, tick=None) -> dict:
        end = time.monotonic() + wait
        while True:
            job = self._call("GET", f"/api/jobs/{job_id}")
            if job.get("status") == "succeeded":
                return job
            if job.get("status") in ("failed", "cancelled"):
                raise ScanSplitterError(f"ScanSplitter job {job_id} {job['status']}: {job.get('error') or ''}")
            if time.monotonic() > end:
                raise ScanSplitterError(f"ScanSplitter job {job_id} didn't finish within {wait:.0f} s")
            if tick:
                tick(f"ScanSplitter: {job.get('stage') or job.get('status')} {job.get('progress') or 0}%")
            time.sleep(1)

    def wait_detection(self, pid: str, sids: list[str], wait: float = 3600, tick=None) -> dict[str, dict]:
        """Wait until every scan in sids has been through detection. Returns {sid: scan}."""
        end = time.monotonic() + wait
        nudged = 0.0
        while True:
            scans = {s["id"]: s for s in self.project(pid).get("scans", []) if s["id"] in sids}
            waiting = [s for s in scans.values()
                       if s.get("status") == "detecting" or (s.get("detected_count") is None and s.get("status") == "pending")]
            if not waiting:
                return scans
            if time.monotonic() > end:
                raise ScanSplitterError(f"photo detection didn't finish within {wait:.0f} s")
            # uploads only queue a few detections at once; ask for the rest
            if any(s.get("status") == "pending" for s in waiting) and time.monotonic() - nudged > 10:
                try:
                    self.detect_pending(pid)
                except ScanSplitterError:
                    pass
                nudged = time.monotonic()
            if tick:
                tick(f"ScanSplitter: detecting photos, {len(scans) - len(waiting)}/{len(scans)} pages done")
            time.sleep(1)


def box_area(box: dict) -> float:
    return max(0.0, float(box.get("width", 0))) * max(0.0, float(box.get("height", 0)))


def coverage(scan: dict) -> float:
    """Share of the page (0-1) covered by the scan's photo boxes (overlaps not subtracted, capped at 1)."""
    page = float(scan.get("width") or 0) * float(scan.get("height") or 0)
    if page <= 0:
        return 0.0
    return min(1.0, sum(box_area(b) for b in scan.get("boxes") or []) / page)
