"""Triage: check PDFs (#58).

Looks at every PDF in <filestack>/sorted/documents and decides whether it is a document or
a photo scan:

- document: any page has more than `pdf_text_words` words (default 7) - in its text layer,
  or by OCR when the text layer has fewer. Checking stops at the first such page, and the
  PDF never goes to ScanSplitter. It stays where it is.
- otherwise the PDF goes to ScanSplitter: photos found on every page -> a photo PDF, split;
  any page without photos (or where detection failed) -> uncertain, moved to
  <filestack>/sorted/quarantine/pdf-uncertain/.

A word is a run of 2 or more letters or digits, so OCR specks on a photo don't count.

Photo PDFs go into a ScanSplitter project (one per run). Pages ScanSplitter raised no CHECK
flag for are approved by ImagePipe and their photos exported to <filestack>/sorted/images/.
Pages with a CHECK flag wait for you to review them in ScanSplitter's Projects page; every
later run exports the pages you approved. When every page is done, the PDF moves to
<filestack>/sorted/excluded/pdf-source/. ScanSplitter's own Deliver is never used.

PDFs that can't be read, and PDFs ScanSplitter can't take (over 200 MB or 200 pages, or a
page too big for it to decode), also go to <filestack>/sorted/quarantine/pdf-uncertain/.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from imagepipe.fspath import to_db
from imagepipe.progress import Progress
from imagepipe.scansplitter import (
    MAX_PDF_PAGES,
    MAX_UPLOAD_BYTES,
    ScanSplitter,
    ScanSplitterDown,
    ScanSplitterError,
    coverage,
)
from imagepipe.triage import db as tdb
from imagepipe.triage.apply import MoveError, free_name, sha256_of, transfer

log = logging.getLogger("imagepipe")
OCR_DPI = 150
_WORD = re.compile(r"[^\W_]{2,}", re.UNICODE)
logging.getLogger("pypdf").setLevel(logging.ERROR)  # "Ignoring wrong pointing object ...": harmless, noisy


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    log.info(msg)


def count_words(text: str | None) -> int:
    return len(_WORD.findall(text or ""))


def tools_missing() -> list[str]:
    missing = [t for t in ("pdftoppm", "tesseract") if shutil.which(t) is None]
    try:
        import pypdf  # noqa: F401
    except ImportError:
        missing.append("pypdf (Python package)")
    return missing


# -- reading a PDF --------------------------------------------------------------------
def page_texts(path: Path):
    """(number of pages, iterator of (page, words in its text layer))."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"encrypted: {exc}") from exc

    def pages():
        for i, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001 - a broken text layer counts as none
                text = ""
            yield i, count_words(text)

    return len(reader.pages), pages()


def ocr_words(path: Path, page: int) -> int:
    """Words Tesseract reads on one page (rendered at OCR_DPI)."""
    render = subprocess.run(["pdftoppm", "-r", str(OCR_DPI), "-f", str(page), "-l", str(page), "-gray", "-png",
                             str(path)], capture_output=True, timeout=600)
    if render.returncode != 0 or not render.stdout:
        return 0
    ocr = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "3"], input=render.stdout,
                         capture_output=True, timeout=600)
    return count_words(ocr.stdout.decode("utf-8", "replace"))


def page_verdict(boxes: int, failed: bool = False) -> str:
    """A page of a PDF with no wordy page: photos found -> photo, else uncertain."""
    return "photo" if boxes > 0 and not failed else "uncertain"


def pdf_verdict(pages: list[dict]) -> str:
    kinds = {p["verdict"] for p in pages}
    if kinds == {"photo"}:
        return "photo-pages"
    return "uncertain"


# -- the job --------------------------------------------------------------------------
class PdfChecker:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tc = cfg.triage
        self.db = tdb.TriageDB(self.tc.database, cfg.tz, self.tc.reports_dir / "actions.csv")
        self.run_id = None
        self.ss: ScanSplitter | None = None
        self.project: str | None = None
        self.projects: list[str] = []      # made by this run
        self.project_pdfs = 0              # photo PDFs kept in the current project
        self.settings_sent: set[str] = set()
        self.ss_cfg = cfg.tools.scansplitter if cfg.tools else None
        self.stats: dict[str, int] = {}

    # helpers
    def bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def triage_row(self, path: Path):
        return self.db.db.execute("SELECT * FROM files WHERE dest_path=?", (to_db(str(path)),)).fetchone()

    def move(self, src: Path, dest_dir: Path, action: str, detail: str = "", flat: bool = False) -> Path:
        documents = self.tc.bucket_dir("documents")
        rel = Path(src.name) if flat else src.relative_to(documents)
        dest_dir = dest_dir / rel.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = free_name(dest_dir / src.name)
        sha = sha256_of(src)
        method = transfer(str(src), dest, sha, self.tc.verify)
        row = self.triage_row(src)
        self.db.action(self.run_id, row, action, str(src), str(dest), rule="pdf-check", sha_before=sha,
                       sha_after=sha, detail=f"{method}; {detail}".strip("; "))
        if row is not None:
            self.db.update(row["id"], dest_path=str(dest))
        return dest

    def save(self, path: Path, **cols) -> None:
        cols["updated_at"] = self.db.now()
        for k in ("page_detail", "ss_scans"):
            if k in cols and not isinstance(cols[k], str) and cols[k] is not None:
                cols[k] = json.dumps(cols[k])
        key = to_db(str(path))
        if self.db.db.execute("SELECT 1 FROM pdf_checks WHERE path=?", (key,)).fetchone():
            sets = ", ".join(f"{k}=?" for k in cols)
            self.db.db.execute(f"UPDATE pdf_checks SET {sets} WHERE path=?", (*cols.values(), key))
        else:
            cols["path"] = key
            names = ", ".join(cols)
            self.db.db.execute(f"INSERT INTO pdf_checks ({names}) VALUES ({', '.join('?' * len(cols))})",
                               tuple(cols.values()))
        self.db.db.commit()

    def scansplitter(self) -> ScanSplitter:
        if self.ss is None:
            url = self.cfg.tools.scansplitter.url if self.cfg.tools else ""
            self.ss = ScanSplitter(url)
            if not self.ss.health():
                raise ScanSplitterDown(f"ScanSplitter at {url} isn't answering /api/health - the PDF check "
                                       "stopped; nothing was changed for the PDF it was on")
        return self.ss

    def run_project(self) -> str:
        """This run's current project; a new one once it holds max_pdfs_per_project photo PDFs."""
        limit = self.ss_cfg.max_pdfs_per_project if self.ss_cfg else 200
        if self.project is None or self.project_pdfs >= limit:
            name = f"ImagePipe PDF check {datetime.now(self.cfg.tz):%Y-%m-%d %H:%M}"
            if self.projects:
                name += f" ({len(self.projects) + 1})"
            self.project = self.scansplitter().create_project(name)
            self.projects.append(self.project)
            self.project_pdfs = 0
            self.send_settings(self.project)
            say(f"ScanSplitter project: '{name}' ({self.project}), up to {limit:,} photo PDFs")
        return self.project

    def send_settings(self, pid: str) -> None:
        """Give a project the ScanSplitter settings from Settings > External tools (once per run)."""
        if pid in self.settings_sent or self.ss_cfg is None:
            return
        self.scansplitter().update_settings(pid, self.ss_cfg.project_settings())
        self.settings_sent.add(pid)

    # phase 1: PDFs waiting for review in ScanSplitter
    def follow_up(self) -> None:
        rows = self.db.db.execute("SELECT * FROM pdf_checks WHERE state IN ('waiting-review', 'checking')").fetchall()
        if not rows:
            return
        say(f"Checking {len(rows):,} PDF(s) already in ScanSplitter projects...")
        with Progress("PDFs in ScanSplitter", total=len(rows), unit="PDFs") as prog:
            for row in rows:
                prog.set_extra(os.path.basename(row["path"]))
                try:
                    if row["state"] == "checking":  # stopped during the check last time: start it again
                        self.forget_scans(row)
                        self.db.db.execute("DELETE FROM pdf_checks WHERE id=?", (row["id"],))
                        self.db.db.commit()
                    else:
                        self.export_ready(row, prog)
                except ScanSplitterDown:
                    raise
                except ScanSplitterError as exc:
                    say(f"  {row['path']}: {exc}")
                    self.bump("errors")
                prog.update()

    def forget_scans(self, row) -> None:
        scans = json.loads(row["ss_scans"] or "[]")
        if not (row["ss_project"] and scans):
            return
        ss = self.scansplitter()
        for s in scans:
            try:
                ss.delete_scan(row["ss_project"], s["sid"])
            except ScanSplitterError:
                pass

    def export_ready(self, row, prog) -> None:
        """Export every approved page not exported yet; finish the PDF when all pages are done."""
        pdf = Path(row["final_path"] or row["path"])
        pid = row["ss_project"]
        scans = json.loads(row["ss_scans"] or "[]")
        ss = self.scansplitter()
        if any(not s.get("exported") for s in scans):
            try:
                self.send_settings(pid)
            except ScanSplitterError as exc:
                if "HTTP 404" not in str(exc):
                    raise
        try:
            live = {s["id"]: s for s in ss.project(pid).get("scans", [])}
        except ScanSplitterError as exc:
            if "HTTP 404" in str(exc):
                self.save(Path(row["path"]), state="error", note=f"ScanSplitter project {pid} no longer exists")
                say(f"  {pdf}: its ScanSplitter project was deleted - left where it is")
                self.bump("errors")
                return
            raise
        images = self.tc.bucket_dir("images")
        documents = self.tc.bucket_dir("documents")
        try:
            rel_parent = pdf.relative_to(documents).parent
        except ValueError:
            rel_parent = Path()
        for s in scans:
            if s.get("exported"):
                continue
            scan = live.get(s["sid"])
            if scan is None:  # you deleted the page in ScanSplitter: nothing to export
                s["exported"], s["files"] = True, []
                continue
            if scan.get("status") not in ("approved", "auto_approved"):
                continue
            if not scan.get("boxes"):
                s["exported"], s["files"] = True, []
                continue
            photos = ss.export_scan(pid, s["sid"], tick=prog.set_extra)
            out_dir = images / rel_parent
            out_dir.mkdir(parents=True, exist_ok=True)
            written = []
            for n, (name, data) in enumerate(photos, 1):
                ext = Path(name).suffix.lower() or ".jpg"
                dest = free_name(out_dir / f"{pdf.stem}_p{s['page']:03d}_{n}{ext}")
                with open(dest, "xb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                self.db.action(self.run_id, None, "pdf-photo-exported", str(pdf), str(dest), rule="pdf-check",
                               detail=f"page {s['page']}, photo {n}, ScanSplitter project {pid}")
                written.append(str(dest))
            s["exported"], s["files"] = True, written
            self.bump("photos-exported", len(written))
            self.save(Path(row["path"]), ss_scans=scans)
        waiting = sum(1 for s in scans if not s.get("exported"))
        if waiting:
            self.save(Path(row["path"]), ss_scans=scans, state="waiting-review",
                      note=f"{waiting} page(s) waiting for review in ScanSplitter")
            self.bump("pages-waiting-review", waiting)
            return
        dest = self.move(pdf, self.tc.sorted_dir / "excluded" / "pdf-source", "pdf-source", "all pages split",
                         flat=True)
        self.save(Path(row["path"]), ss_scans=scans, state="done", final_path=str(dest), note="all pages split")
        self.bump("pdfs-split")

    # phase 2: new PDFs
    def todo(self) -> list[Path]:
        documents = self.tc.bucket_dir("documents")
        if not documents.is_dir():
            return []
        known = {r["path"]: (r["size"], r["mtime"]) for r in
                 self.db.db.execute("SELECT path, size, mtime FROM pdf_checks")}
        out = []
        for root, dirs, files in os.walk(documents):
            dirs.sort()
            for name in sorted(files):
                if not name.lower().endswith(".pdf"):
                    continue
                p = Path(root) / name
                st = p.stat()
                if known.get(to_db(str(p))) == (st.st_size, st.st_mtime):
                    continue
                out.append(p)
        return out

    def check_one(self, pdf: Path, prog) -> None:
        st = pdf.stat()
        base = {"size": st.st_size, "mtime": st.st_mtime, "checked_at": self.db.now()}
        limit = self.tc.pdf_text_words
        try:
            count, texts = page_texts(pdf)
            pages = []
            for n, words in texts:
                p = {"page": n, "text_words": words, "ocr_words": None}
                if words <= limit:
                    prog.set_extra(f"{pdf.name}: OCR page {n}/{count}")
                    p["ocr_words"] = ocr_words(pdf, n)
                pages.append(p)
                if max(words, p["ocr_words"] or 0) > limit:  # a page of text: a document, stop here
                    p["verdict"] = "document"
                    break
        except (subprocess.SubprocessError, OSError):
            raise
        except Exception as exc:  # noqa: BLE001
            self.set_aside(pdf, base, 0, [], "unreadable", f"pypdf can't read it: {type(exc).__name__}: {exc}")
            return
        if count == 0:
            self.set_aside(pdf, base, 0, [], "unreadable", "no pages")
            return
        if pages and pages[-1].get("verdict") == "document":
            self.bump("verdict:document")
            self.save(pdf, **base, pages=count, verdict="document", state="done", page_detail=pages,
                      ss_project=None, ss_scans=None, final_path=str(pdf),
                      note=f"page {pages[-1]['page']} has more than {limit} words")
            return
        # no page with words: only now ScanSplitter looks for photos
        if st.st_size > MAX_UPLOAD_BYTES or count > MAX_PDF_PAGES:
            self.too_large(pdf, base, count, pages, "over ScanSplitter's limit (200 MB, 200 pages)")
            return
        project = self.run_project()
        ss = self.scansplitter()
        try:
            uploaded = ss.add_pdf(project, pdf)
        except ScanSplitterError as exc:
            if "HTTP 413" in str(exc) or "pixel limit" in str(exc):
                self.too_large(pdf, base, count, pages, f"ScanSplitter refused it: {str(exc)[-160:]}")
                return
            raise
        sids = [s["id"] for s in uploaded]
        self.save(pdf, **base, pages=count, state="checking", ss_project=project,
                  ss_scans=[{"sid": s["id"], "page": s.get("page") or 1} for s in uploaded])
        done = ss.wait_detection(project, sids, tick=lambda t: prog.set_extra(f"{pdf.name}: {t}"))
        scans = {int(s.get("page") or 1): s for s in done.values()}
        for p in pages:
            scan = scans.get(p["page"])
            p["boxes"] = len(scan.get("boxes") or []) if scan else 0
            p["coverage"] = round(coverage(scan), 3) if scan else None
            p["flags"] = [f.get("code") for f in scan.get("flags") or []] if scan else []
            failed = bool(scan and scan.get("status") == "failed")
            if failed:
                p["why"] = "ScanSplitter detection failed"
            p["verdict"] = page_verdict(p["boxes"], failed)
        verdict = pdf_verdict(pages)
        self.bump(f"verdict:{verdict}")
        if verdict == "photo-pages":
            self.project_pdfs += 1
        if verdict != "photo-pages":
            for s in scans.values():  # not a photo PDF: take its pages out of the review list again
                try:
                    ss.delete_scan(project, s["id"])
                except ScanSplitterError:
                    pass
            final = self.move(pdf, self.tc.bucket_dir("quarantine/pdf-uncertain"), "pdf-uncertain", summary(pages))
            self.save(pdf, **base, pages=count, verdict=verdict, state="done", page_detail=pages,
                      ss_project=None, ss_scans=None, final_path=str(final), note=summary(pages))
            return
        # photo pages: approve the pages ScanSplitter is sure about, then export what's approved
        entries = []
        for page_no, scan in sorted(scans.items()):
            if scan.get("boxes") and not scan.get("flags"):
                ss.approve(project, scan["id"])
            entries.append({"sid": scan["id"], "page": page_no, "exported": False, "files": []})
        checks = sum(1 for s in scans.values() if s.get("flags"))
        self.save(pdf, **base, pages=count, verdict=verdict, state="waiting-review", page_detail=pages,
                  ss_project=project, ss_scans=entries, final_path=str(pdf),
                  note=f"{count - checks} page(s) approved automatically, {checks} with a CHECK flag")
        row = self.db.db.execute("SELECT * FROM pdf_checks WHERE path=?", (to_db(str(pdf)),)).fetchone()
        self.export_ready(row, prog)

    def too_large(self, pdf: Path, base: dict, count: int, pages: list, why: str) -> None:
        self.set_aside(pdf, base, count, pages, "too-large", why)

    def set_aside(self, pdf: Path, base: dict, count: int, pages: list, verdict: str, why: str) -> None:
        """Unreadable and too-large PDFs go to quarantine/pdf-uncertain."""
        why = why[:400]
        final = self.move(pdf, self.tc.bucket_dir("quarantine/pdf-uncertain"), f"pdf-{verdict}", why)
        self.save(pdf, **base, pages=count, verdict=verdict, state="done", final_path=str(final),
                  page_detail=pages, ss_project=None, ss_scans=None, note=why)
        self.bump(verdict)

    def move_left_behind(self) -> None:
        """Unreadable/too-large PDFs that 0.5.0-0.5.2 left in sorted/documents: to quarantine now."""
        documents = self.tc.bucket_dir("documents")
        rows = [r for r in self.db.db.execute("SELECT * FROM pdf_checks WHERE verdict IN ('unreadable', 'too-large') "
                                              "AND state='done'")
                if r["final_path"] and Path(r["final_path"]).is_file()
                and Path(r["final_path"]).is_relative_to(documents)]
        if not rows:
            return
        say(f"Moving {len(rows):,} unreadable / too-large PDF(s) left in sorted/documents to quarantine/pdf-uncertain...")
        with Progress("Unreadable / too large", total=len(rows), unit="PDFs") as prog:
            for r in rows:
                pdf = Path(r["final_path"])
                prog.set_extra(pdf.name)
                try:
                    final = self.move(pdf, self.tc.bucket_dir("quarantine/pdf-uncertain"), f"pdf-{r['verdict']}",
                                      r["note"] or "")
                    self.save(Path(r["path"]), final_path=str(final))
                    self.bump(f"moved-to-quarantine:{r['verdict']}")
                except (MoveError, OSError) as exc:
                    say(f"  {pdf}: {type(exc).__name__}: {exc}")
                    self.bump("errors")
                prog.update()

    def run(self, limit: int | None = None) -> dict:
        missing = tools_missing()
        if missing:
            raise RuntimeError("The PDF check needs: " + ", ".join(missing))
        self.run_id = self.db.start_run("pdf-check")
        status = "finished"
        try:
            self.follow_up()
            self.move_left_behind()
            todo = self.todo()
            say(f"PDFs to check in {self.tc.bucket_dir('documents')}: {len(todo):,}")
            if limit is not None and len(todo) > limit:
                todo = todo[:limit]
                say(f"Limit: checking {limit:,} of them this run; the next run carries on with the rest.")
            total_bytes = sum(p.stat().st_size for p in todo)
            with Progress("Check PDFs", total=len(todo), total_bytes=total_bytes, unit="PDFs") as prog:
                for pdf in todo:
                    prog.set_extra(pdf.name)
                    size = pdf.stat().st_size
                    try:
                        self.check_one(pdf, prog)
                    except ScanSplitterDown:
                        raise
                    except (ScanSplitterError, MoveError, OSError, subprocess.SubprocessError) as exc:
                        say(f"  {pdf}: {type(exc).__name__}: {exc}")
                        row = self.db.db.execute("SELECT * FROM pdf_checks WHERE path=?",
                                                 (to_db(str(pdf)),)).fetchone()
                        if row is not None and row["state"] == "checking":
                            try:
                                self.forget_scans(row)
                            except ScanSplitterError:
                                pass
                        # size/mtime cleared, so the next run checks it again
                        self.save(pdf, state="error", size=None, mtime=None, ss_scans=None,
                                  note=f"{type(exc).__name__}: {exc}"[:500])
                        self.bump("errors")
                    prog.update(1, size)
        except ScanSplitterDown:
            status = "failed"
            raise
        except KeyboardInterrupt:
            status = "interrupted"
            say("Stopped. Everything finished so far is kept; the next run carries on.")
        finally:
            self.tidy_project()
            self.db.finish_run(self.run_id, status, self.stats)
            self.db.close()
        return {"status": status, "stats": dict(self.stats)}

    def tidy_project(self) -> None:
        """Delete this run's ScanSplitter projects that have nothing waiting for review."""
        for pid in self.projects:
            try:
                if self.ss is not None and not self.ss.project(pid).get("scans"):
                    self.ss.delete_project(pid)
                    say(f"ScanSplitter project {pid} was left empty, so it was deleted.")
            except ScanSplitterError:
                pass


def summary(pages: list[dict]) -> str:
    counts: dict[str, int] = {}
    for p in pages:
        counts[p["verdict"]] = counts.get(p["verdict"], 0) + 1
    return ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))


def run_pdf_check(cfg, limit: int | None = None) -> dict:
    return PdfChecker(cfg).run(limit)


def status_rows(db) -> list:
    return db.execute("SELECT verdict, state, COUNT(*) AS n FROM pdf_checks GROUP BY verdict, state "
                      "ORDER BY verdict, state").fetchall()
