"""Triage scan: a read-only inventory of each triage source.

1. Scan    - walk the tree, recording every file's path, size and modified time
             (no total is known yet, so it shows a running count, rate and time).
2. Compare - work out which files are new or changed since the last scan; files
             that disappeared before being moved are marked missing.
3. Read    - for each new or changed file: real type from its contents, SHA-256,
             and the dates, GPS and titles used for naming. Shows %, rate and ETA.

Nothing in the source is opened for writing, moved or changed.
"""

from __future__ import annotations

import hashlib
import logging
import multiprocessing
import os
import signal
import sys

from imagepipe import exiftool as exiftool_mod
from imagepipe.catalogue import scan as walk
from imagepipe.config import Config, Source
from imagepipe.detect import HEAD_BYTES, OFFICE_ZIP_EXT, detect, extension
from imagepipe.metadata import HEIF
from imagepipe.progress import Progress, fmt_bytes
from imagepipe.triage import db as tdb
from imagepipe.triage.meta import exiftool_meta, image_meta, office_meta, takeout_meta, xmp_sidecar_meta

log = logging.getLogger("imagepipe")
CHUNK = 1 << 20

READ_FIELDS = ("size", "mtime", "ext", "kind", "mime", "ext_mismatch", "sha256", "width", "height",
               "camera_make", "camera_model", "meta_date", "meta_date_src", "gps_lat", "gps_lon",
               "meta_title", "takeout_title", "encrypted", "catalogue_error")


class TriageAbort(Exception):
    pass


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    log.info(msg)


# ---------------------------------------------------------------- worker side
_W: dict = {}


def init_worker(opts: dict) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _W.clear()
    _W.update(opts)
    _W["et"] = None


def _et():
    if _W.get("et") is None:
        _W["et"] = exiftool_mod.ExifTool()
    return _W["et"]


def read_entry(item: tuple) -> dict:
    """Read one file. Runs in a worker process. Never raises.

    item: (key, abs_path, name, size, mtime) - key is passed straight back.
    """
    key, abs_path, name, size, mtime = item
    out = {f: None for f in READ_FIELDS}
    out.update(key=key, size=size, mtime=mtime, ext=extension(name) or None, ext_mismatch=0)
    try:
        digest = hashlib.sha256()
        with open(abs_path, "rb") as fh:
            first = fh.read(CHUNK)
            digest.update(first)
            while True:
                block = fh.read(CHUNK)
                if not block:
                    break
                digest.update(block)
        out["sha256"] = digest.hexdigest()
        kind, mime, mismatch = detect(first[:HEAD_BYTES], name, size)
        out.update(kind=kind, mime=mime, ext_mismatch=int(mismatch))
    except OSError as exc:
        out.update(kind="unknown", catalogue_error=f"read: {exc.strerror or exc}")
        return out

    ext = out["ext"] or ""
    try:
        meta: dict = {}
        if kind == "image" and (mime != "image/heic" or HEIF):
            meta = image_meta(abs_path)
        elif kind in ("raw", "video", "audio", "pdf", "image") and _W.get("use_exiftool"):
            meta = exiftool_meta(_et(), abs_path, kind)
        elif kind == "document" and ext in OFFICE_ZIP_EXT and ext != "epub":
            meta = office_meta(abs_path)
        elif kind == "document" and ext == "json":
            meta = takeout_meta(abs_path, size)
        elif kind == "sidecar" and ext in ("xmp", "xmp_original"):
            meta = xmp_sidecar_meta(abs_path, size)
        out.update({k: v for k, v in meta.items() if k in READ_FIELDS and v is not None})
    except Exception as exc:  # unreadable/corrupt files are recorded, not fatal
        out["catalogue_error"] = f"metadata: {type(exc).__name__}: {exc}"[:500]
    return out


def make_pool(workers: int, use_exiftool: bool):
    # spawn, not fork: the progress thread is running and fork() with threads can deadlock
    return multiprocessing.get_context("spawn").Pool(
        workers, initializer=init_worker, initargs=({"use_exiftool": use_exiftool},))


def exiftool_ok() -> bool:
    ok = exiftool_mod.available()
    if not ok:
        say("Note: exiftool not found - dates, GPS and titles from RAW, video, audio and PDF files will be skipped.")
    return ok


# ---------------------------------------------------------------- main side
def _write_rows(t: tdb.TriageDB, results: list[dict], meta_of: dict, run_id: int) -> None:
    now = t.now()
    for r in results:
        info = meta_of.pop(r["key"])
        cols = {f: r[f] for f in READ_FIELDS}
        if info.get("fid"):
            # changed or reappeared: read again and plan again (keeps its ID)
            t.update(info["fid"], **cols, status=tdb.CATALOGUED, bucket=None, rule=None, dest_path=None,
                     companion_of=None, last_seen_run=run_id)
            t.action(run_id, t.get(info["fid"]), "re-read", from_path=info["current_path"],
                     detail="changed on disk since last read")
        else:
            names = ["source", "parent_id", "rel_path", "logical_path", "dir", "origin", "current_path",
                     *READ_FIELDS, "status", "first_seen_run", "last_seen_run", "updated_at"]
            vals = [info["source"], info["parent_id"], info["rel_path"], info["logical_path"], info["dir"],
                    info["origin"], info["current_path"], *cols.values(), tdb.CATALOGUED, run_id, run_id, now]
            t.db.execute(f"INSERT INTO files ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", vals)
        if r["catalogue_error"]:
            log.info("read problem: %s: %s", info["current_path"], r["catalogue_error"])
    t.db.commit()


def read_items(t: tdb.TriageDB, pool, items: list[tuple], meta_of: dict, run_id: int, progress: Progress,
               batch_size: int = 500) -> int:
    """Read files with the pool and write their rows. Returns the error count."""
    errors = 0
    pending: list[dict] = []
    for r in pool.imap_unordered(read_entry, items, chunksize=8):
        if r["catalogue_error"]:
            errors += 1
        pending.append(r)
        progress.update(1, r["size"], extra=f"{errors:,} problems")
        if len(pending) >= batch_size:
            _write_rows(t, pending, meta_of, run_id)
            pending.clear()
    if pending:
        _write_rows(t, pending, meta_of, run_id)
    return errors


def run_scan(cfg: Config, source: Source) -> dict:
    tc = cfg.triage
    if not source.path.is_dir():
        raise TriageAbort(f"Source folder not found or not a folder: {source.path}")
    t = tdb.TriageDB(tc.database, cfg.tz, tc.reports_dir / "actions.csv")
    run_id = t.start_run("triage-scan", source.name)
    db = t.db
    summary: dict = {"source": source.name, "run_id": run_id}
    status = "failed"
    try:
        say(f"Triage scan: source '{source.name}' ({source.path})")
        say(f"Triage database: {tc.database}")

        db.execute("DROP TABLE IF EXISTS temp.scan")
        db.execute("CREATE TEMP TABLE scan (rel_path TEXT PRIMARY KEY, size INTEGER, mtime REAL)")
        errs: list[str] = []
        batch = []
        with Progress("1/3 Scan", unit="files") as p:
            for row in walk(source, p, errs):
                batch.append(row)
                if len(batch) >= 5000:
                    db.executemany("INSERT OR REPLACE INTO temp.scan VALUES (?, ?, ?)", batch)
                    batch.clear()
            if batch:
                db.executemany("INSERT OR REPLACE INTO temp.scan VALUES (?, ?, ?)", batch)
            scanned, scanned_bytes = p.count, p.bytes
        for e in errs:
            log.warning("scan problem: %s", e)
        summary.update(scanned=scanned, scanned_bytes=scanned_bytes, scan_errors=len(errs))

        waiting = db.execute(
            f"SELECT COUNT(*) FROM files WHERE source=? AND parent_id=0 AND status IN "
            f"('{tdb.CATALOGUED}', '{tdb.PLANNED}')", (source.name,)).fetchone()[0]
        if scanned == 0 and waiting > 0:
            raise TriageAbort(
                f"The source has no files but {waiting:,} from it are waiting to be moved. "
                "The source is probably not mounted. Nothing was changed."
            )

        active = ",".join(f"'{s}'" for s in tdb.ACTIVE)
        with Progress("2/3 Compare", unit="steps", total=2) as p:
            p.set_extra("finding new and changed files")
            db.execute("DROP TABLE IF EXISTS temp.todo")
            db.execute(f"""
                CREATE TEMP TABLE todo AS
                SELECT s.rel_path, s.size, s.mtime, f.id AS fid, f.current_path AS current_path FROM temp.scan s
                LEFT JOIN files f ON f.source = :src AND f.parent_id = 0 AND f.rel_path = s.rel_path
                     AND f.status IN ({active})
                WHERE f.id IS NULL OR f.size != s.size OR f.mtime != s.mtime
                   OR f.status IN ('{tdb.MISSING}', '{tdb.CHANGED}')
            """, {"src": source.name})
            p.update(1, extra="marking files that disappeared")
            gone = db.execute(f"""
                SELECT * FROM files WHERE source = :src AND parent_id = 0
                AND status IN ('{tdb.CATALOGUED}', '{tdb.PLANNED}', '{tdb.ERROR}')
                AND NOT EXISTS (SELECT 1 FROM temp.scan s WHERE s.rel_path = files.rel_path)
            """, {"src": source.name}).fetchall()
            for row in gone:
                t.update(row["id"], status=tdb.MISSING)
                t.action(run_id, row, "missing", from_path=row["current_path"],
                         detail="no longer in the source; not moved by triage")
            db.execute("UPDATE files SET last_seen_run=? WHERE source=? AND parent_id=0 AND status IN ("
                       f"{active}) AND rel_path IN (SELECT rel_path FROM temp.scan)", (run_id, source.name))
            db.commit()
            p.update(1, extra="done")
        todo, todo_bytes = db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM temp.todo").fetchone()
        summary.update(to_read=todo, to_read_bytes=todo_bytes, missing=len(gone))
        say(f"{todo:,} new or changed files to read ({fmt_bytes(todo_bytes)}); "
            f"{scanned - todo:,} unchanged; {len(gone):,} gone before being moved (marked missing).")

        use_et = exiftool_ok()
        root = str(source.path)
        errors = 0
        interrupted = False
        with Progress("3/3 Read", total=todo, total_bytes=todo_bytes) as p:
            pool = make_pool(tc.workers, use_et)
            try:
                last = 0
                while True:
                    page = db.execute("SELECT rowid, * FROM temp.todo WHERE rowid > ? ORDER BY rowid LIMIT 20000",
                                      (last,)).fetchall()
                    if not page:
                        break
                    last = page[-1]["rowid"]
                    items, meta_of = [], {}
                    for r in page:
                        rel = r["rel_path"]
                        abs_path = os.path.join(root, rel)
                        meta_of[rel] = {
                            "fid": r["fid"], "source": source.name, "parent_id": 0, "rel_path": rel,
                            "logical_path": rel, "dir": os.path.dirname(rel), "origin": abs_path,
                            "current_path": abs_path,
                        }
                        items.append((rel, abs_path, os.path.basename(rel), r["size"], r["mtime"]))
                    errors += read_items(t, pool, items, meta_of, run_id, p)
                pool.close()
            except KeyboardInterrupt:
                interrupted = True
                pool.terminate()
            finally:
                pool.join()
            summary.update(read=p.count, read_problems=errors)
        status = "interrupted" if interrupted else "finished"
        if interrupted:
            say("Interrupted. Everything read so far is saved; run the scan again to carry on.")
        return summary | {"status": status}
    except KeyboardInterrupt:
        status = "interrupted"
        say("Interrupted before reading started. Nothing was lost; run the scan again.")
        return summary | {"status": status}
    finally:
        summary["status"] = status
        t.finish_run(run_id, status, summary)
        t.close()
