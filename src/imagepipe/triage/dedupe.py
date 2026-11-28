"""Triage: remove duplicates.

Finds byte-identical files (same SHA-256, recorded by the triage scan) among everything
triage has sorted, keeps one copy of each, and moves the others to
<filestack>/sorted/excluded/triage-duplicate/. Nothing is deleted; every copy's original
place is recorded against the kept one (the `duplicates` table, actions.csv, the report).

Which copy is kept:
1. a copy already processed: the keeper from an earlier run, then a PDF already split,
   then a PDF waiting for review in ScanSplitter
2. the copy in the most meaningful folder: each folder of its original path scores
   0 if it matches `dedupe_ignore_words` (wildcards) or is generic (DCIM, Documents,
   New folder ...), -2 if it holds a backup/temporary word (`dedupe_backup_words`),
   +1 otherwise ("Whakapapa", "Bali 2014"); a file taken out of an archive scores -2 more.
   `dedupe_case_insensitive` decides whether case matters in both word lists.

Categories in `dedupe_skip_categories` (default web-pages, code, software) are left alone.
3. the oldest: the date triage found for the file, then the file's modified time

Companion files (sidecars, Live Photo clips) belong to their main file and aren't compared
themselves. A duplicate's companions move beside the kept file when it has none of that
kind. For images: when both copies have sidecars, both copies stay, so ImagePipe stage 4
can merge their metadata.

A duplicate PDF waiting for review in ScanSplitter has its pages taken out of the project.
A duplicate PDF already split (in excluded/pdf-source) is left there and only recorded.

Only identical files are found: the same document saved or scanned twice is not.
"""

from __future__ import annotations

import csv
import fnmatch
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath

from imagepipe.fspath import to_db
from imagepipe.progress import Progress
from imagepipe.triage import db as tdb
from imagepipe.triage.apply import MoveError, free_name, transfer
from imagepipe.triage.naming import meaningful_folder

log = logging.getLogger("imagepipe")
SIDECAR_EXTS = {"xmp", "aae", "thm", "json", "xmp_original"}
_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    log.info(msg)


def tokens(name: str) -> list[str]:
    """Words of a folder name: 'drive_backup' -> drive, backup; 'PhotoTemp' -> photo, temp."""
    return [t.lower() for t in _TOKEN.findall(name)]


class Scorer:
    def __init__(self, backup_words, ignore_words, case_insensitive: bool = True):
        self.ci = case_insensitive
        self.backup = [self._tokens(w) or [self._norm(w)] for w in backup_words]
        self.ignore = [self._norm(w.strip()) for w in ignore_words if w.strip()]

    def _norm(self, s: str) -> str:
        return s.lower() if self.ci else s

    def _tokens(self, name: str) -> list[str]:
        return [self._norm(t) for t in _TOKEN.findall(name)]

    def is_ignored(self, folder: str) -> bool:
        name = self._norm(folder.strip())
        return any(fnmatch.fnmatchcase(name, pat) for pat in self.ignore)

    def is_backup(self, folder: str) -> bool:
        toks = self._tokens(folder)
        joined = "".join(toks)
        for w in self.backup:
            n = len(w)
            if any(toks[i:i + n] == w for i in range(len(toks) - n + 1)):
                return True
            if n == 1 and joined == w[0]:
                return True
        return False

    def folder(self, folder: str) -> int:
        if self.is_ignored(folder):
            return 0
        if self.is_backup(folder):
            return -2
        if meaningful_folder(folder) is None:
            return 0
        return 1

    def score(self, row) -> int:
        parts = PurePosixPath(row["logical_path"] or "").parent.parts
        total = sum(self.folder(p) for p in parts)
        if row["parent_id"]:
            total -= 2
        return total


def is_image(row) -> bool:
    return (row["kind"] or "") in ("image", "raw")


class Deduper:
    def __init__(self, cfg, dry_run: bool = False):
        self.cfg, self.tc, self.dry = cfg, cfg.triage, dry_run
        self.t = tdb.TriageDB(self.tc.database, cfg.tz, None if dry_run else self.tc.reports_dir / "actions.csv")
        self.scorer = Scorer(self.tc.dedupe_backup_words, self.tc.dedupe_ignore_words, self.tc.dedupe_case_insensitive)
        self.skip = set(self.tc.dedupe_skip_categories)
        self.run_id = None
        self.stats: dict[str, int] = defaultdict(int)
        self.ss = None
        self.dup_dir = self.tc.sorted_dir / "excluded" / "triage-duplicate"

    # -- finding ----------------------------------------------------------------------------
    def groups(self) -> list[list]:
        say("Looking for identical files in the triage database...")
        rows = self.t.db.execute(
            "SELECT * FROM files WHERE status=? AND sha256 IS NOT NULL AND size > 0 AND companion_of IS NULL "
            "AND sha256 IN (SELECT sha256 FROM files WHERE status=? AND sha256 IS NOT NULL AND size > 0 "
            "AND companion_of IS NULL GROUP BY sha256 HAVING COUNT(*) > 1) ORDER BY sha256, id",
            (tdb.DONE, tdb.DONE)).fetchall()
        done = {r["file_id"] for r in self.t.db.execute("SELECT file_id FROM duplicates")}
        by_sha = defaultdict(list)
        with Progress("Check copies are there", total=len(rows), unit="files") as prog:
            for r in rows:
                if r["bucket"] in self.skip:
                    continue
                if r["id"] not in done and r["dest_path"] and os.path.lexists(r["dest_path"]):
                    by_sha[r["sha256"]].append(r)
                prog.update()
        return [g for g in by_sha.values() if len(g) > 1]

    def processed(self, row) -> int:
        """How far a copy has been processed: 3 a keeper before, 2 split, 1 waiting in ScanSplitter, else 0."""
        if self.t.db.execute("SELECT 1 FROM duplicates WHERE keeper_id=? LIMIT 1", (row["id"],)).fetchone():
            return 3
        pc = self.pdf_row(row)
        if pc is not None and pc["verdict"] == "photo-pages":
            return 2 if pc["state"] == "done" else 1
        return 0

    def pdf_row(self, row):
        if (row["ext"] or "").lower() != "pdf":
            return None
        dest = to_db(row["dest_path"] or "")
        return self.t.db.execute("SELECT * FROM pdf_checks WHERE path=? OR final_path=? ORDER BY id DESC LIMIT 1",
                                 (dest, dest)).fetchone()

    def rank(self, row) -> tuple:
        return (-self.processed(row), -self.scorer.score(row), row["name_date"] or "9999", row["mtime"] or 0,
                row["id"])

    def companions(self, row) -> list:
        return [c for c in self.t.db.execute("SELECT * FROM files WHERE companion_of=? AND status=?",
                                             (row["id"], tdb.DONE))
                if c["dest_path"] and os.path.lexists(c["dest_path"])]

    # -- acting -----------------------------------------------------------------------------
    def decide(self, keeper, dup) -> str:
        """What happens to one duplicate: excluded | passed-to-stage-4 | already-split."""
        pc = self.pdf_row(dup)
        if pc is not None and pc["verdict"] == "photo-pages" and pc["state"] == "done":
            return "already-split"
        if is_image(dup):
            k_side = {c["ext"] for c in self.companions(keeper) if (c["ext"] or "") in SIDECAR_EXTS}
            d_side = {c["ext"] for c in self.companions(dup) if (c["ext"] or "") in SIDECAR_EXTS}
            if k_side and d_side:
                return "passed-to-stage-4"
        return "excluded"

    def record(self, keeper, dup, outcome: str, moved_to: str | None) -> None:
        self.t.db.execute("INSERT OR REPLACE INTO duplicates (file_id, keeper_id, sha256, origin, outcome, moved_to, "
                          "run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (dup["id"], keeper["id"], dup["sha256"], dup["origin"], outcome, to_db(moved_to or ""),
                           self.run_id, self.t.now()))
        self.stats[outcome] += 1

    def move_file(self, row, dest_dir: Path, action: str, detail: str, name: str | None = None) -> Path:
        src = row["dest_path"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = free_name(dest_dir / (name or os.path.basename(src)))
        method = transfer(src, dest, row["sha256"], self.tc.verify)
        self.t.action(self.run_id, row, action, src, str(dest), rule="duplicate", sha_before=row["sha256"],
                      sha_after=row["sha256"], detail=f"{method}; {detail}")
        self.t.update(row["id"], current_path=str(dest), dest_path=str(dest))
        return dest

    def handle(self, keeper, dup) -> None:
        outcome = self.decide(keeper, dup)
        if outcome != "excluded":
            self.record(keeper, dup, outcome, None)
            return
        detail = f"identical to {keeper['triage_id']} ({keeper['dest_path']})"
        pc = self.pdf_row(dup)
        if pc is not None and pc["state"] in ("waiting-review", "checking") and pc["ss_project"]:
            self.forget_scans(pc)
        # companions: beside the keeper when it has none of that kind, else with the duplicate
        k_exts = {c["ext"] for c in self.companions(keeper)}
        moved = self.move_file(dup, self.dup_dir, "duplicate-excluded", detail)
        for c in self.companions(dup):
            if c["ext"] not in k_exts:
                kstem = Path(keeper["dest_path"]).stem
                dstem = Path(dup["dest_path"]).stem
                cname = os.path.basename(c["dest_path"])
                new_name = kstem + cname[len(dstem):] if cname.startswith(dstem) else f"{kstem}.{c['ext']}"
                self.move_file(c, Path(keeper["dest_path"]).parent, "companion-to-keeper", detail, new_name)
                self.t.update(c["id"], companion_of=keeper["id"])
                self.stats["companions-to-keeper"] += 1
            else:
                self.move_file(c, self.dup_dir, "duplicate-excluded", f"companion; {detail}")
        if pc is not None:
            self.t.db.execute("UPDATE pdf_checks SET verdict='duplicate', state='done', final_path=?, ss_scans=NULL, "
                              "note=?, updated_at=? WHERE id=?",
                              (to_db(str(moved)), f"identical to {keeper['dest_path']}"[:500], self.t.now(), pc["id"]))
        self.record(keeper, dup, outcome, str(moved))
        self.stats["bytes-excluded"] += dup["size"] or 0

    def forget_scans(self, pc) -> None:
        from imagepipe.scansplitter import ScanSplitter, ScanSplitterError

        try:
            if self.ss is None:
                self.ss = ScanSplitter(self.cfg.tools.scansplitter.url if self.cfg.tools else "")
            for s in json.loads(pc["ss_scans"] or "[]"):
                if not s.get("exported"):
                    self.ss.delete_scan(pc["ss_project"], s["sid"])
                    self.stats["scansplitter-pages-removed"] += 1
        except ScanSplitterError as exc:
            say(f"  couldn't take {pc['path']}'s pages out of ScanSplitter: {exc}")
            self.stats["scansplitter-errors"] += 1

    # -- the run ----------------------------------------------------------------------------
    def run(self) -> dict:
        self.run_id = None if self.dry else self.t.start_run("dedupe")
        status = "finished"
        out_dir = self.tc.reports_dir / f"dedupe-{datetime.now(self.cfg.tz):%Y%m%d-%H%M%S}"
        out_dir.mkdir(parents=True, exist_ok=True)
        report = out_dir / ("duplicates-dry-run.csv" if self.dry else "duplicates.csv")
        try:
            groups = self.groups()
            n_dups = sum(len(g) - 1 for g in groups)
            say(f"Groups of identical files: {len(groups):,} ({n_dups:,} duplicate copies)")
            with open(report, "w", newline="", encoding="utf-8") as fh, \
                    Progress("Duplicates" + (" (dry run)" if self.dry else ""), total=n_dups,
                             total_bytes=sum(sum(r["size"] for r in g[1:]) for g in groups), unit="files") as prog:
                w = csv.writer(fh)
                w.writerow(["group", "role", "outcome", "score", "date", "size", "category", "original", "now"])
                for gi, group in enumerate(groups, 1):
                    ordered = sorted(group, key=self.rank)
                    keeper = ordered[0]
                    w.writerow([gi, "keep", "", self.scorer.score(keeper), keeper["name_date"], keeper["size"],
                                keeper["bucket"], keeper["origin"], keeper["dest_path"]])
                    for dup in ordered[1:]:
                        prog.set_extra(os.path.basename(dup["dest_path"]))
                        outcome = self.decide(keeper, dup)
                        if not self.dry:
                            try:
                                self.handle(keeper, dup)
                                self.t.db.commit()
                            except (MoveError, OSError) as exc:
                                say(f"  {dup['dest_path']}: {type(exc).__name__}: {exc}")
                                self.stats["errors"] += 1
                                outcome = f"error: {exc}"
                        else:
                            self.stats[outcome] += 1
                        w.writerow([gi, "duplicate", outcome, self.scorer.score(dup), dup["name_date"], dup["size"],
                                    dup["bucket"], dup["origin"], dup["dest_path"]])
                        prog.update(1, dup["size"] or 0)
        except KeyboardInterrupt:
            status = "interrupted"
            say("Stopped. Everything finished so far is kept; the next run carries on.")
        finally:
            self.write_summary(out_dir, status)
            if self.run_id is not None:
                self.t.finish_run(self.run_id, status, dict(self.stats))
            self.t.close()
        say(f"Report: {report}")
        return {"status": status, "stats": dict(self.stats), "report": str(report)}

    def write_summary(self, out_dir: Path, status: str) -> None:
        lines = [f"# Remove duplicates{' - dry run, nothing moved' if self.dry else ''}", "", f"Status: {status}", "",
                 "| What | Count |", "|---|---:|"]
        lines += [f"| {k} | {v:,} |" for k, v in sorted(self.stats.items())]
        (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_dedupe(cfg, dry_run: bool = False) -> dict:
    return Deduper(cfg, dry_run).run()
