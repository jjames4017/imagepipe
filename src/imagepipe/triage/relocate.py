"""Move files triage already sorted from the old layout (before 0.4) into the filestack.

Old layout (config triage.destinations + work_dir):
  <imagepipe>/...          photos, RAW and their companion files
  <docupipe>/...           documents and PDFs
  <sorted>/<type>/...      video, audio, ebooks ... specials
  <quarantine>/<reason>/...

New layout (<filestack> from IMAGEPIPE_FILESTACK):
  <filestack>/sorted/images/...      photos (and companions of photos)
  <filestack>/sorted/raw/...         RAW files (and their companions)
  <filestack>/sorted/documents/...   was docupipe
  <filestack>/sorted/<type>/...      unchanged below
  <filestack>/sorted/quarantine/<reason>/...

Folders below each root are kept exactly. Moves are the same safe move as triage apply
(hard link + same-file check, never overwrites; copy + SHA-256 check across file systems).
The triage database follows every file (current path, bucket, an action row), and every
move is written to relocate-<time>/moves.csv. Files under the old roots that triage has no
record of are moved by the same rules. With dry_run nothing moves.
"""

from __future__ import annotations

import csv
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from imagepipe.config import Config, ConfigError
from imagepipe.detect import EXT_KIND
from imagepipe.fspath import to_db, to_fs
from imagepipe.progress import Progress
from imagepipe.triage import db as tdb

COMMIT_EVERY = 500


def _under(path: str, root: str) -> str | None:
    """path relative to root, or None if it isn't under it."""
    root = root.rstrip(os.sep)
    if path.startswith(root + os.sep):
        return path[len(root) + 1:]
    return None


class Relocator:
    def __init__(self, cfg: Config, dry_run: bool):
        tc = cfg.triage
        if not tc.legacy_destinations:
            raise ConfigError("Nothing to relocate: the config has no old triage.destinations "
                              "(the layout before 0.4).")
        self.cfg, self.tc, self.dry_run = cfg, tc, dry_run
        self.roots = {k: os.path.realpath(v) for k, v in tc.legacy_destinations.items()}
        filestack = os.path.realpath(tc.filestack)
        for key, root in self.roots.items():
            if _under(filestack, root) is not None or filestack == root:
                raise ConfigError(f"The filestack ({filestack}) is inside the old '{key}' folder ({root})")
        self.counts: Counter = Counter()
        self._seen: set[str] = set()

    def new_place(self, path: str, kind: str | None) -> tuple[str, Path] | None:
        """(bucket, new path) for a file at `path` under one of the old roots, else None."""
        for key, root in self.roots.items():
            rel = _under(path, root)
            if rel is None:
                continue
            if key == "imagepipe":
                bucket = "raw" if kind == "raw" else "images"
                return bucket, self.tc.bucket_dir(bucket) / rel
            if key == "docupipe":
                return "documents", self.tc.bucket_dir("documents") / rel
            if key == "sorted":
                return rel.split(os.sep, 1)[0], self.tc.sorted_dir / rel
            if key == "quarantine":
                return "quarantine/" + rel.split(os.sep, 1)[0], self.tc.sorted_dir / "quarantine" / rel
        return None

    def run(self, t: tdb.TriageDB | None, writer) -> None:
        from imagepipe.triage.apply import MoveError, free_name, transfer

        # 1. files the triage database knows about
        rows = []
        kind_of: dict[int, str] = {}
        if t is not None:
            for r in t.db.execute(f"SELECT id, kind, companion_of FROM files WHERE status='{tdb.DONE}'"):
                kind_of[r["id"]] = r["kind"]
            for r in t.db.execute(f"SELECT * FROM files WHERE status='{tdb.DONE}' AND current_path IS NOT NULL"):
                path = to_fs(r["current_path"])
                if any(_under(path, root) is not None for root in self.roots.values()):
                    rows.append(r)
        total_bytes = sum(r["size"] or 0 for r in rows)
        done = 0
        label = "Relocate (dry run)" if self.dry_run else "Relocate"
        with Progress(f"{label}: files in the triage database", total=len(rows), total_bytes=total_bytes) as p:
            for r in rows:
                src = to_fs(r["current_path"])
                kind = r["kind"]
                if r["companion_of"]:  # a sidecar / Live Photo clip goes where its photo goes
                    kind = kind_of.get(r["companion_of"], kind)
                bucket, dest = self.new_place(src, kind)
                self._move(src, dest, r["sha256"], writer, MoveError, free_name, transfer, t, r, bucket)
                done += 1
                if t is not None and not self.dry_run and done % COMMIT_EVERY == 0:
                    t.db.commit()
                p.update(1, r["size"] or 0, extra=self._extra())
        if t is not None and not self.dry_run:
            t.db.commit()

        # 2. anything left under the old roots that the database doesn't know about
        with Progress(f"{label}: other files under the old folders") as p:
            for root in self.roots.values():
                if not os.path.isdir(root):
                    continue
                for dirpath, _dirs, files in os.walk(root):
                    for name in files:
                        src = os.path.join(dirpath, name)
                        if self.dry_run and src in self._seen:
                            continue
                        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                        bucket, dest = self.new_place(src, EXT_KIND.get(ext))
                        size = os.lstat(src).st_size
                        self.counts["untracked"] += 1
                        self._move(src, dest, None, writer, MoveError, free_name, transfer, None, None, bucket)
                        p.update(1, size, extra=self._extra())

        # 3. old folders left empty
        if not self.dry_run:
            for root in self.roots.values():
                for dirpath, _dirs, _files in os.walk(root, topdown=False):
                    if dirpath != root:
                        try:
                            os.rmdir(dirpath)
                        except OSError:
                            pass
            if self.tc.legacy_work_dir and os.path.isdir(self.tc.legacy_work_dir):
                for dirpath, _dirs, _files in os.walk(self.tc.legacy_work_dir, topdown=False):
                    if dirpath != str(self.tc.legacy_work_dir):
                        try:
                            os.rmdir(dirpath)
                        except OSError:
                            pass

    def _extra(self) -> str:
        return ", ".join(f"{v:,} {k}" for k, v in self.counts.items())

    def _move(self, src, dest, sha, writer, MoveError, free_name, transfer, t, row, bucket) -> None:
        if not os.path.lexists(src):
            self.counts["missing"] += 1
            writer.writerow(["missing", src, "", "not there any more"])
            return
        if self.dry_run:
            self._seen.add(src)
            self.counts["would-move"] += 1
            writer.writerow(["would move", src, str(dest), bucket])
            return
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest = free_name(dest)
            method = transfer(src, dest, sha, "auto")
        except (MoveError, OSError) as exc:
            self.counts["error"] += 1
            writer.writerow(["error", src, str(dest), str(exc)])
            return
        self.counts["moved"] += 1
        if method != "link" and method != "rename":
            self.counts["copied"] += 1  # a copy means the two folders are on different mounts
        writer.writerow(["moved", src, str(dest), f"{bucket} ({method})"])
        if t is not None and row is not None:
            new = to_db(str(dest))
            t.update(row["id"], current_path=new, dest_path=new, bucket=bucket)
            t.action(None, row, "relocated", src, str(dest), row["rule"], detail=f"{bucket} ({method})")


def run_relocate(cfg: Config, dry_run: bool = False) -> dict:
    tc = cfg.triage
    r = Relocator(cfg, dry_run)
    stamp = datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
    folder = tc.reports_dir / f"relocate-{'dry-run-' if dry_run else ''}{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    t = None
    if Path(tc.database).is_file():
        t = tdb.TriageDB(tc.database, cfg.tz, None if dry_run else tc.reports_dir / "actions.csv")
    try:
        with open(folder / "moves.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["result", "from", "to", "detail"])
            r.run(t, w)
    finally:
        if t is not None:
            t.close()
    return {"folder": str(folder), **r.counts}
