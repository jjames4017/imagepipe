"""Triage: check PDFs (#58), against a small fake ScanSplitter."""

from __future__ import annotations

import io
import json
import re
import shutil
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from PIL import Image

from imagepipe.config import load_config
from imagepipe.triage import pdfcheck
from imagepipe.triage.pdfcheck import page_verdict, pdf_verdict

needs_tools = pytest.mark.skipif(bool(pdfcheck.tools_missing()), reason="pdftoppm/tesseract/pypdf not installed")


def text_pdf(text="Dear Sir, this letter is about the account and has plenty of words on it. " * 3) -> bytes:
    stream = f"BT /F1 10 Tf 40 700 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = io.BytesIO(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def image_pdf(pages=1) -> bytes:
    ims = [Image.new("RGB", (300, 400), (120 + 20 * i, 90, 60)) for i in range(pages)]
    buf = io.BytesIO()
    ims[0].save(buf, "PDF", save_all=True, append_images=ims[1:], resolution=50)
    return buf.getvalue()


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (10, 200, 10)).save(buf, "JPEG")
    return buf.getvalue()


class FakeSS(BaseHTTPRequestHandler):
    projects: dict = {}
    jobs: dict = {}
    plan: dict = {}  # filename -> [(boxes, flags)] per page

    def log_message(self, *a):
        pass

    def _send(self, status, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/api/health":
            return self._send(200, {"status": "ok"})
        m = re.match(r"^/api/projects/([^/]+)$", p)
        if m:
            proj = self.projects.get(m.group(1))
            return self._send(200, proj) if proj else self._send(404, {"detail": "no"})
        m = re.match(r"^/api/jobs/([^/]+)$", p)
        if m:
            return self._send(200, {"job_id": m.group(1), "status": "succeeded", "progress": 100})
        m = re.match(r"^/api/jobs/([^/]+)/download$", p)
        if m:
            n = self.jobs[m.group(1)]
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                for i in range(n):
                    zf.writestr(f"scan_{i + 1}.jpg", jpeg())
            return self._send(200, buf.getvalue(), "application/zip")
        self._send(404, {"detail": "no"})

    def do_POST(self):
        p = self.path.split("?")[0]
        body = self._body()
        if p == "/api/projects":
            pid = uuid.uuid4().hex[:8]
            self.projects[pid] = {"id": pid, "name": json.loads(body)["name"], "scans": []}
            return self._send(200, self.projects[pid])
        m = re.match(r"^/api/projects/([^/]+)/scans$", p)
        if m:
            name = re.search(rb'filename="([^"]+)"', body).group(1).decode()
            pages = self.plan[name]
            scans = []
            for i, (boxes, flags) in enumerate(pages, 1):
                scans.append({"id": uuid.uuid4().hex[:8], "original_name": name, "page": i, "width": 1000,
                              "height": 1000, "status": "needs_review" if boxes else "pending",
                              "detected_count": len(boxes),
                              "boxes": [{"id": f"b{j}", "x": 0, "y": 0, "width": w, "height": h}
                                        for j, (w, h) in enumerate(boxes)],
                              "flags": [{"code": c} for c in flags]})
            self.projects[m.group(1)]["scans"] += scans
            return self._send(200, {"scans": scans, "jobs": []})
        m = re.match(r"^/api/projects/([^/]+)/scans/([^/]+)/export$", p)
        if m:
            scan = next(s for s in self.projects[m.group(1)]["scans"] if s["id"] == m.group(2))
            job = uuid.uuid4().hex[:8]
            self.jobs[job] = len(scan["boxes"])
            return self._send(202, {"job_id": job})
        if p.endswith("/detect-pending"):
            return self._send(202, {"jobs": []})
        self._send(404, {"detail": "no"})

    def do_PATCH(self):
        body = json.loads(self._body())
        m = re.match(r"^/api/projects/([^/]+)$", self.path)
        if m:
            self.projects[m.group(1)]["settings"] = body["settings"]
            return self._send(200, self.projects[m.group(1)])
        m = re.match(r"^/api/projects/([^/]+)/scans/([^/]+)$", self.path)
        scan = next(s for s in self.projects[m.group(1)]["scans"] if s["id"] == m.group(2))
        scan["status"] = body["status"]
        self._send(200, scan)

    def do_DELETE(self):
        m = re.match(r"^/api/projects/([^/]+)/scans/([^/]+)$", self.path)
        if m:
            proj = self.projects[m.group(1)]
            proj["scans"] = [s for s in proj["scans"] if s["id"] != m.group(2)]
            return self._send(200, {"status": "deleted"})
        m = re.match(r"^/api/projects/([^/]+)$", self.path)
        self.projects.pop(m.group(1), None)
        self._send(200, {"status": "deleted"})


@pytest.fixture
def fake_ss():
    FakeSS.projects, FakeSS.jobs, FakeSS.plan = {}, {}, {}
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeSS)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def setup(tmp_path, fake_ss, monkeypatch):
    stack = tmp_path / "filestack"
    monkeypatch.setenv("IMAGEPIPE_FILESTACK", str(stack))
    src = tmp_path / "src"
    src.mkdir()
    data = tmp_path / "data"
    cfg = {"manifest": str(data / "manifest.db"), "log_dir": str(data / "logs"), "reports_dir": str(data / "reports"),
           "tools": {"scansplitter": {"url": fake_ss}},
           "triage": {"database": str(data / "triage.db"), "log_dir": str(data / "tlogs"),
                      "reports_dir": str(data / "treports"), "sources": [{"name": "s", "path": str(src)}]}}
    path = tmp_path / "imagepipe.yml"
    path.write_text(yaml.safe_dump(cfg))
    docs = stack / "sorted" / "documents" / "box1"
    docs.mkdir(parents=True)
    return load_config(path), docs, stack


def test_verdict_rules():
    assert page_verdict(1) == "photo"
    assert page_verdict(0) == "uncertain"
    assert page_verdict(2, failed=True) == "uncertain"
    assert pdf_verdict([{"verdict": "photo"}] * 2) == "photo-pages"
    assert pdf_verdict([{"verdict": "photo"}, {"verdict": "uncertain"}]) == "uncertain"
    assert pdfcheck.count_words("a 1 ab cd3 -- x_y hello") == 3


@needs_tools
def test_text_pdf_never_goes_to_scansplitter(setup):
    cfg, docs, stack = setup
    (docs / "letter.pdf").write_bytes(text_pdf())
    (docs / "short.pdf").write_bytes(text_pdf("Page one only"))  # 3 words: not a document by words
    FakeSS.plan = {"short.pdf": [([], [])]}
    res = pdfcheck.run_pdf_check(cfg)
    assert res["stats"]["verdict:document"] == 1 and res["stats"]["verdict:uncertain"] == 1
    assert (docs / "letter.pdf").exists()
    assert (stack / "sorted/quarantine/pdf-uncertain/box1/short.pdf").exists()


@needs_tools
def test_check_pdfs(setup):
    cfg, docs, stack = setup
    (docs / "letter.pdf").write_bytes(text_pdf())
    (docs / "album.pdf").write_bytes(image_pdf(2))
    (docs / "mixed.pdf").write_bytes(image_pdf(2))
    FakeSS.plan = {"album.pdf": [([(900, 900)], []), ([(800, 900)], ["edge"])],   # page 2 has a CHECK flag
                   "mixed.pdf": [([(900, 900)], []), ([], [])]}
    res = pdfcheck.run_pdf_check(cfg)
    assert res["status"] == "finished"
    assert (docs / "letter.pdf").exists()                                       # document: stays
    assert (stack / "sorted/quarantine/pdf-uncertain/box1/mixed.pdf").exists()  # uncertain
    images = stack / "sorted/images/box1"
    assert [p.name for p in images.iterdir()] == ["album_p001_1.jpg"]          # page 1 auto-approved
    assert (docs / "album.pdf").exists()                                        # page 2 waits for review
    assert res["stats"]["pages-waiting-review"] == 1

    # you approve page 2 in ScanSplitter; the next run exports it and retires the PDF
    proj = next(iter(FakeSS.projects.values()))
    assert len(proj["scans"]) == 2  # the mixed PDF's pages were taken out again
    for s in proj["scans"]:
        s["status"] = "approved"
    res = pdfcheck.run_pdf_check(cfg)
    assert sorted(p.name for p in images.iterdir()) == ["album_p001_1.jpg", "album_p002_1.jpg"]
    assert (stack / "sorted/excluded/pdf-source/album.pdf").exists()
    assert not (docs / "album.pdf").exists()
    assert res["stats"]["pdfs-split"] == 1

    # nothing new: nothing checked again
    res = pdfcheck.run_pdf_check(cfg)
    assert "verdict:document" not in res["stats"]


@needs_tools
def test_scansplitter_down(setup, monkeypatch):
    cfg, docs, stack = setup
    cfg.tools.scansplitter.url = "http://127.0.0.1:9"
    (docs / "album.pdf").write_bytes(image_pdf(1))
    with pytest.raises(Exception, match="ScanSplitter"):
        pdfcheck.run_pdf_check(cfg)
    assert (docs / "album.pdf").exists()


@needs_tools
def test_limit(setup):
    cfg, docs, stack = setup
    for n in range(3):
        (docs / f"letter{n}.pdf").write_bytes(text_pdf())
    res = pdfcheck.run_pdf_check(cfg, limit=2)
    assert res["stats"]["verdict:document"] == 2
    res = pdfcheck.run_pdf_check(cfg, limit=2)
    assert res["stats"]["verdict:document"] == 1


@needs_tools
def test_page_too_big_for_scansplitter(setup, monkeypatch):
    cfg, docs, stack = setup
    (docs / "chart.pdf").write_bytes(image_pdf(1))
    from imagepipe.scansplitter import ScanSplitter, ScanSplitterError

    def refuse(self, pid, path):
        raise ScanSplitterError("POST ...: HTTP 400 {\"detail\":\"PDF page exceeds the decoded-pixel limit\"}")

    monkeypatch.setattr(ScanSplitter, "add_pdf", refuse)
    res = pdfcheck.run_pdf_check(cfg)
    assert res["stats"] == {"too-large": 1}
    assert not (docs / "chart.pdf").exists()
    assert (stack / "sorted/quarantine/pdf-uncertain/box1/chart.pdf").exists()
    res = pdfcheck.run_pdf_check(cfg)          # not tried again
    assert res["stats"] == {}


@needs_tools
def test_unreadable_to_quarantine_and_left_behind_moved(setup):
    cfg, docs, stack = setup
    (docs / "broken.pdf").write_bytes(b"not a pdf at all")
    res = pdfcheck.run_pdf_check(cfg)
    assert res["stats"] == {"unreadable": 1}
    q = stack / "sorted/quarantine/pdf-uncertain/box1"
    assert (q / "broken.pdf").exists()
    # a PDF an earlier version left in sorted/documents as too large
    (docs / "old.pdf").write_bytes(b"x")
    from imagepipe.triage import db as tdb
    from imagepipe.fspath import to_db
    t = tdb.TriageDB(cfg.triage.database, cfg.tz, cfg.triage.reports_dir / "actions.csv")
    st = (docs / "old.pdf").stat()
    t.db.execute("INSERT INTO pdf_checks (path, size, mtime, verdict, state, final_path, note) "
                 "VALUES (?, ?, ?, 'too-large', 'done', ?, 'old')",
                 (to_db(str(docs / "old.pdf")), st.st_size, st.st_mtime, to_db(str(docs / "old.pdf"))))
    t.db.commit()
    t.close()
    res = pdfcheck.run_pdf_check(cfg)
    assert res["stats"] == {"moved-to-quarantine:too-large": 1}
    assert (q / "old.pdf").exists() and not (docs / "old.pdf").exists()


@needs_tools
def test_project_settings_and_max_pdfs(setup):
    cfg, docs, stack = setup
    cfg.tools.scansplitter.max_pdfs_per_project = 2
    for n in range(3):
        (docs / f"album{n}.pdf").write_bytes(image_pdf(1))
        FakeSS.plan[f"album{n}.pdf"] = [([(900, 900)], ["edge"])]   # all wait for review
    res = pdfcheck.run_pdf_check(cfg)
    assert res["stats"]["verdict:photo-pages"] == 3
    projects = list(FakeSS.projects.values())
    assert [len(p["scans"]) for p in projects] == [2, 1]
    assert projects[1]["name"].endswith("(2)")
    want = cfg.tools.scansplitter.project_settings()
    assert all(p["settings"] == want for p in projects)
    assert want["min_area_ratio"] == 2.0 and want["max_area_ratio"] == 100.0 and want["quality"] == 100
    assert want["auto_deskew"] and want["restore_color"] and want["upscale_2x"] and not want["include_gps"]
