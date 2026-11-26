"""Triage apply: carry out the plan.

Moves are done so a file is never lost, even if the run is stopped halfway:
- same file system: hard link to the new name, check it's the same file, then
  remove the old name (never overwrites anything)
- different file systems: copy to a .triage-partial file, flush to disk, check
  its SHA-256 matches the one taken at scan time, rename into place, then remove
  the original
Every action is written to the triage database and appended to actions.csv as
it happens. A run that is stopped is finished off by the next one.

Archives are extracted into work_dir, every file is checked (see archives.py)
and read back, the files are planned and moved like any other, and only then is
the archive itself deleted. One that can't be extracted goes to quarantine.

Finally the pipeline ID and original location are written into the files that
can carry them (XMP-dc:Identifier and XMP-dc:Source), keeping their modified time.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import multiprocessing
import os
import shutil
import signal
import stat
import sys
from pathlib import Path

from imagepipe import exiftool as exiftool_mod
from imagepipe.config import Config
from imagepipe.progress import Progress
from imagepipe.triage import db as tdb
from imagepipe.triage.archives import ArchiveError, NoSpace, extract
from imagepipe.triage.kinds import archive_stem
from imagepipe.triage.naming import build_name
from imagepipe.triage.plan import _dest_dir, plan_rows
from imagepipe.triage.scan import TriageAbort, exiftool_ok, make_pool, read_items

log = logging.getLogger("imagepipe")
CHUNK = 1 << 20


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    log.info(msg)


class MoveError(Exception):
    pass


def sha256_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def free_name(dest: Path) -> Path:
    """dest, or dest with -1, -2 ... added before the extension if the name is taken."""
    if not os.path.lexists(dest):
        return dest
    stem, dot, suffix = dest.name.partition(".")
    n = 1
    while True:
        cand = dest.with_name(f"{stem}-{n}{dot}{suffix}")
        if not os.path.lexists(cand):
            return cand
        n += 1


def transfer(src: str, dest: Path, expect_sha: str | None, verify: str) -> str:
    """Move src to dest safely. Returns the method used. Raises MoveError."""
    st = os.lstat(src)
    try:
        os.link(src, dest)
        method = "link"
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            return _copy(src, dest, expect_sha)
        if exc.errno in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK):
            if os.path.lexists(dest):
                raise MoveError(f"destination appeared: {dest}")
            try:
                os.rename(src, dest)
            except OSError as exc2:
                if exc2.errno == errno.EXDEV:
                    return _copy(src, dest, expect_sha)
                raise MoveError(f"rename failed: {exc2.strerror or exc2}")
            method = "rename"
            dst = os.stat(dest)
            if dst.st_ino != st.st_ino or dst.st_size != st.st_size:
                raise MoveError("renamed file does not match the original")
            if verify == "hash" and expect_sha and sha256_of(dest) != expect_sha:
                raise MoveError("checksum after rename does not match")
            return method
        raise MoveError(f"link failed: {exc.strerror or exc}")
    dst = os.stat(dest)
    if dst.st_ino != st.st_ino or dst.st_size != st.st_size:
        os.unlink(dest)
        raise MoveError("new name does not point at the original file")
    if verify == "hash" and expect_sha and sha256_of(dest) != expect_sha:
        os.unlink(dest)
        raise MoveError("checksum at the destination does not match")
    os.unlink(src)
    return method


def _copy(src: str, dest: Path, expect_sha: str | None) -> str:
    partial = dest.with_name(dest.name + ".triage-partial")
    digest = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(partial, "xb") as fo:
            for block in iter(lambda: fi.read(CHUNK), b""):
                fo.write(block)
                digest.update(block)
            fo.flush()
            os.fsync(fo.fileno())
        if expect_sha and digest.hexdigest() != expect_sha:
            raise MoveError("the original changed since it was scanned")
        if sha256_of(partial) != digest.hexdigest():
            raise MoveError("the copy does not match the original")
        shutil.copystat(src, partial)
        os.link(partial, dest)  # never overwrites
        os.unlink(partial)
    except Exception:
        if os.path.lexists(partial):
            os.unlink(partial)
        raise
    os.unlink(src)
    return "copy-verified"


class Applier:
    def __init__(self, cfg: Config, t: tdb.TriageDB, run_id: int):
        self.cfg = cfg
        self.tc = cfg.triage
        self.t = t
        self.run_id = run_id
        self.stats: dict[str, int] = {}
        self.folder_of = {s.name: s.path.name for s in self.tc.sources}

    def bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    # -- recovery ------------------------------------------------------------
    def recover(self) -> None:
        rows = self.t.db.execute(f"SELECT * FROM files WHERE status='{tdb.MOVING}'").fetchall()
        for row in rows:
            src, dest = row["current_path"], row["dest_path"]
            partial = dest + ".triage-partial" if dest else None
            if partial and os.path.lexists(partial):
                os.unlink(partial)
            s_ok, d_ok = os.path.lexists(src), bool(dest) and os.path.lexists(dest)
            if s_ok and d_ok:
                same = os.stat(src).st_ino == os.stat(dest).st_ino
                if same or (row["sha256"] and sha256_of(dest) == row["sha256"]):
                    os.unlink(src)
                    self._done(row, dest, "recovered")
                else:
                    self.t.update(row["id"], status=tdb.ERROR)
                    self.t.action(self.run_id, row, "error", src, dest, row["rule"],
                                  detail="stopped mid-move; both copies differ - left for checking")
            elif s_ok:
                self.t.update(row["id"], status=tdb.PLANNED)
                self.t.action(self.run_id, row, "move-reset", src, dest, row["rule"], detail="stopped before move")
            elif d_ok:
                self._done(row, dest, "recovered")
            else:
                self.t.update(row["id"], status=tdb.ERROR)
                self.t.action(self.run_id, row, "error", src, dest, row["rule"], detail="file not found at either end")
        self.t.db.commit()
        if rows:
            say(f"Finished off {len(rows):,} moves from a run that was stopped.")

    def _done(self, row, dest: str, method: str) -> None:
        embed = "pending" if row["kind"] in self.tc.embed_metadata and row["companion_of"] is None else "n/a"
        self.t.update(row["id"], status=tdb.DONE, current_path=dest, dest_path=dest, done_at=self.t.now(),
                      embed_status=embed)
        self.t.action(self.run_id, row, "moved", row["current_path"], dest, row["rule"],
                      sha_before=row["sha256"], detail=f"{row['bucket']} ({method})")
        self.bump(f"moved:{row['bucket']}")

    # -- moving ----------------------------------------------------------------
    def move_one(self, row) -> bool:
        src = row["current_path"]
        try:
            st = os.lstat(src)
        except FileNotFoundError:
            self.t.update(row["id"], status=tdb.MISSING)
            self.t.action(self.run_id, row, "missing", src, None, row["rule"], detail="gone before it could be moved")
            self.bump("missing")
            return False
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            self.t.update(row["id"], status=tdb.ERROR)
            self.t.action(self.run_id, row, "error", src, None, row["rule"], detail="no longer a regular file")
            self.bump("errors")
            return False
        if st.st_size != row["size"] or abs(st.st_mtime - row["mtime"]) > 1e-6:
            self.t.update(row["id"], status=tdb.CHANGED)
            self.t.action(self.run_id, row, "skipped", src, None, row["rule"],
                          detail="changed since it was scanned - scan again, then plan")
            self.bump("changed")
            return False
        dest = free_name(Path(row["dest_path"]))
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.t.update(row["id"], status=tdb.MOVING, dest_path=str(dest))
        self.t.db.commit()
        try:
            method = transfer(src, dest, row["sha256"], self.tc.verify)
        except (MoveError, OSError) as exc:
            self.t.update(row["id"], status=tdb.ERROR)
            self.t.action(self.run_id, row, "error", src, str(dest), row["rule"], detail=str(exc)[:500])
            self.bump("errors")
            log.warning("move failed: %s: %s", src, exc)
            return False
        self._done(row, str(dest), method)
        return True

    def move_planned(self, limit: int | None) -> int:
        where = f"status='{tdb.PLANNED}' AND bucket != 'extract'"
        total = self.t.db.execute(f"SELECT COUNT(*) FROM files WHERE {where}").fetchone()[0]
        if limit is not None:
            total = min(total, limit)
        if total == 0:
            return 0
        done = 0
        with Progress("Move", total=total) as p:
            while done < total:
                page = self.t.db.execute(f"SELECT * FROM files WHERE {where} ORDER BY id LIMIT ?",
                                         (min(1000, total - done),)).fetchall()
                if not page:
                    break
                for row in page:
                    self.move_one(row)
                    done += 1
                    p.update(1, extra=f"{self.stats.get('errors', 0):,} errors")
                self.t.db.commit()
        return done

    # -- archives --------------------------------------------------------------
    def reroute(self, row, bucket: str, rule: str, detail: str) -> None:
        dest = _dest_dir(self.tc, self.folder_of[row["source"]], bucket, row["dir"]) / build_name(
            row["triage_id"], row["name_date"], row["smartname"], (row["ext"] or "").lower())
        self.t.update(row["id"], bucket=bucket, rule=rule, dest_path=str(dest))
        self.t.action(self.run_id, row, "rerouted", row["current_path"], str(dest), rule, detail=detail[:500])
        self.t.db.commit()
        self.move_one(self.t.get(row["id"]))

    def handle_archive(self, row, pool) -> None:
        src = row["current_path"]
        name = os.path.basename(row["logical_path"])
        if row["source"] not in self.folder_of:
            return
        if tdb.file_depth(self.t, row) >= self.tc.max_archive_depth:
            self.reroute(row, "quarantine/archive-failed", "archive-too-deep",
                         f"nested more than {self.tc.max_archive_depth} archives deep")
            return
        try:
            st = os.lstat(src)
        except FileNotFoundError:
            self.t.update(row["id"], status=tdb.MISSING)
            self.t.action(self.run_id, row, "missing", src, detail="archive gone before extraction")
            return
        if st.st_size != row["size"] or abs(st.st_mtime - row["mtime"]) > 1e-6:
            self.t.update(row["id"], status=tdb.CHANGED)
            self.t.action(self.run_id, row, "skipped", src, detail="archive changed since it was scanned")
            return
        work = self.tc.work_dir / row["triage_id"]
        if work.exists():
            shutil.rmtree(work)  # left over from a stopped run; it only ever holds our own extracted copies
        say(f"Extracting {name} ({row['size']:,} bytes) -> {work}")
        try:
            with Progress(f"Extract {name[:40]}", unit="files") as p:
                files = extract(src, row["mime"] or "", work, self.tc.max_extract_bytes, p)
        except ArchiveError as exc:
            if work.exists():
                shutil.rmtree(work)
            self.reroute(row, f"quarantine/{exc.reason}", f"archive-{exc.reason}", exc.detail)
            self.bump(f"archive:{exc.reason}")
            return
        except NoSpace as exc:
            if work.exists():
                shutil.rmtree(work)
            raise TriageAbort(str(exc))
        if not files:
            shutil.rmtree(work)
            self.reroute(row, "quarantine/archive-failed", "archive-empty", "no files inside")
            return

        base_logical = "/".join(x for x in (row["dir"], archive_stem(name)) if x)
        items, meta_of = [], {}
        for f in files:
            logical = f"{base_logical}/{f.name}"
            key = f"{row['id']}:{f.name}"
            meta_of[key] = {
                "fid": None, "source": row["source"], "parent_id": row["id"], "rel_path": f.name,
                "logical_path": logical, "dir": os.path.dirname(logical), "origin": f"{row['origin']}::{f.name}",
                "current_path": str(f.path),
            }
            items.append((key, str(f.path), os.path.basename(f.name), f.size, f.mtime))
        with Progress(f"Read {len(files):,} extracted", total=len(files), total_bytes=sum(f.size for f in files)) as p:
            read_items(self.t, pool, items, meta_of, self.run_id, p)
        # the files were read back from disk: their checksums must match those taken while extracting
        sha_of = {f.name: f.sha256 for f in files}
        kids = self.t.db.execute("SELECT * FROM files WHERE parent_id=?", (row["id"],)).fetchall()
        bad = [k["rel_path"] for k in kids if k["sha256"] != sha_of.get(k["rel_path"])]
        if bad or len(kids) != len(files):
            self.t.db.execute("DELETE FROM files WHERE parent_id=?", (row["id"],))
            self.t.db.commit()
            shutil.rmtree(work)
            self.reroute(row, "quarantine/archive-failed", "archive-verify-failed",
                         f"read-back check failed for {len(bad) or 'some'} files")
            return
        self.t.db.executemany("INSERT INTO archive_members (archive_id, name, size, file_id) VALUES (?, ?, ?, ?)",
                              [(row["id"], k["rel_path"], k["size"], k["id"]) for k in kids])
        for k in kids:
            self.t.action(self.run_id, k, "extracted", f"{src}::{k['rel_path']}", k["current_path"],
                          sha_after=k["sha256"], detail=f"from archive {row['triage_id']}")
        self.t.db.commit()
        os.unlink(src)
        self.t.update(row["id"], status=tdb.EXTRACTED, done_at=self.t.now())
        self.t.action(self.run_id, row, "archive-deleted", src, None, row["rule"], sha_before=row["sha256"],
                      detail=f"all {len(kids):,} files extracted and verified")
        self.t.db.commit()
        self.bump("archives-extracted")
        self.bump("files-extracted", len(kids))
        plan_rows(self.cfg, self.t, "parent_id=?", (row["id"],), quiet=True)

    # -- tags ------------------------------------------------------------------
    def embed(self) -> None:
        kinds = self.tc.embed_metadata
        if not kinds:
            return
        marks = ",".join("?" * len(kinds))
        where = f"status='{tdb.DONE}' AND embed_status='pending' AND kind IN ({marks})"
        total = self.t.db.execute(f"SELECT COUNT(*) FROM files WHERE {where}", kinds).fetchone()[0]
        if total == 0:
            return
        if not exiftool_mod.available():
            say(f"Note: exiftool not found - {total:,} files are waiting for their ID to be written in. "
                "Run apply again where exiftool is installed.")
            return
        ctx = multiprocessing.get_context("spawn")
        with Progress("Write tags", total=total) as p:
            pool = ctx.Pool(self.tc.workers, initializer=_init_embed)
            try:
                last = 0
                while True:
                    page = self.t.db.execute(f"SELECT * FROM files WHERE {where} AND id > ? ORDER BY id LIMIT 2000",
                                             (*kinds, last)).fetchall()
                    if not page:
                        break
                    last = page[-1]["id"]
                    by_id = {r["id"]: r for r in page}
                    items = [(r["id"], r["current_path"], r["triage_id"], r["origin"]) for r in page]
                    for fid, ok, msg, sha in pool.imap_unordered(_embed_one, items, chunksize=8):
                        row = by_id[fid]
                        if ok:
                            self.t.update(fid, embed_status="done", sha256_final=sha)
                            self.t.action(self.run_id, row, "tags-written", row["current_path"], None, None,
                                          sha_before=row["sha256"], sha_after=sha,
                                          detail="XMP-dc:Identifier, XMP-dc:Source")
                            self.bump("tags-written")
                        else:
                            self.t.update(fid, embed_status="failed", sha256_final=sha)
                            self.t.action(self.run_id, row, "tags-failed", row["current_path"], None, None,
                                          detail=msg)
                            self.bump("tags-failed")
                        p.update(1, extra=f"{self.stats.get('tags-failed', 0):,} not writable")
                    self.t.db.commit()
                pool.close()
            except KeyboardInterrupt:
                pool.terminate()
                raise
            finally:
                pool.join()


_E: dict = {}


def _init_embed() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _E["et"] = exiftool_mod.ExifTool(read=False)


def _embed_one(item: tuple) -> tuple:
    fid, path, pid, origin = item
    try:
        ok, msg = _E["et"].write(path, {"XMP-dc:Identifier": pid, "XMP-dc:Source": origin})
        return fid, ok, msg, sha256_of(path)
    except Exception as exc:
        try:
            sha = sha256_of(path)
        except OSError:
            sha = None
        return fid, False, f"{type(exc).__name__}: {exc}"[:300], sha


def _clean_work_dir(work: Path) -> None:
    """Remove empty folders left in work_dir (folders only, and only if empty)."""
    if not work.is_dir():
        return
    for root, dirs, files in os.walk(work, topdown=False):
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass


def run_apply(cfg: Config, limit: int | None = None) -> dict:
    tc = cfg.triage
    t = tdb.TriageDB(tc.database, cfg.tz, tc.reports_dir / "actions.csv")
    run_id = t.start_run("triage-apply")
    a = Applier(cfg, t, run_id)
    status = "failed"
    summary: dict = {"run_id": run_id}
    try:
        planned = t.db.execute(f"SELECT COUNT(*) FROM files WHERE status='{tdb.PLANNED}'").fetchone()[0]
        moving = t.db.execute(f"SELECT COUNT(*) FROM files WHERE status='{tdb.MOVING}'").fetchone()[0]
        if planned == 0 and moving == 0:
            say("Nothing is planned. Run `triage scan` and `triage plan` first.")
        for root in list(tc.destinations.values()) + [tc.work_dir]:
            root.mkdir(parents=True, exist_ok=True)
            if not os.access(root, os.W_OK):
                raise TriageAbort(f"No write access to {root}")
        a.recover()
        remaining = limit
        pool = None
        try:
            while True:
                moved = a.move_planned(remaining)
                if remaining is not None:
                    remaining -= moved
                    if remaining <= 0:
                        break
                archives = t.db.execute(
                    f"SELECT * FROM files WHERE status='{tdb.PLANNED}' AND bucket='extract' ORDER BY id").fetchall()
                if not archives:
                    break
                if pool is None:
                    pool = make_pool(tc.workers, exiftool_ok())
                for row in archives:
                    a.handle_archive(row, pool)
                    if remaining is not None:
                        remaining -= 1
                        if remaining <= 0:
                            break
                if remaining is not None and remaining <= 0:
                    break
            if pool is not None:
                pool.close()
        finally:
            if pool is not None:
                pool.terminate()
                pool.join()
        _clean_work_dir(tc.work_dir)
        a.embed()
        status = "finished"
        left = t.db.execute(f"SELECT COUNT(*) FROM files WHERE status='{tdb.PLANNED}'").fetchone()[0]
        summary.update(stats=a.stats, still_planned=left)
        return summary | {"status": status}
    except KeyboardInterrupt:
        status = "interrupted"
        say("Interrupted. Every finished move is recorded; run apply again to carry on.")
        summary.update(stats=a.stats)
        return summary | {"status": status}
    finally:
        t.db.commit()
        t.finish_run(run_id, status, summary)
        t.close()
