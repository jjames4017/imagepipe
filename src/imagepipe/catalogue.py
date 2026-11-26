"""Catalogue mode: a read-only inventory of a source folder.

Three phases, each with live progress:

1. Scan   - walk the tree and record every regular file's path, size and mtime
            into a temporary table (no ETA possible here: the total is unknown,
            so it shows a running count, rate and elapsed time).
2. Compare - work out which files are new or changed since the last catalogue,
            mark unchanged files as seen, and mark files that have disappeared
            as 'missing' (never deleted from the manifest).
3. Read   - for each new or changed file: detect its real type from its
            contents, take a SHA-256, and read dimensions/EXIF/camera/date.
            Shows count, %, files/s, MB/s and an ETA based on bytes.

Nothing in the source is ever opened for writing, moved or changed.
Interrupting (Ctrl+C) keeps everything already read; the next run carries on.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import signal
import sys
import multiprocessing

from imagepipe import exiftool as exiftool_mod
from imagepipe.config import Config, Source
from imagepipe.detect import HEAD_BYTES, detect, extension
from imagepipe.fspath import to_db, to_fs
from imagepipe.manifest import Manifest
from imagepipe.metadata import HEIF, exiftool_info, image_info, zip_members
from imagepipe.progress import Progress, fmt_bytes

log = logging.getLogger("imagepipe")

CHUNK = 1 << 20
STAGE = "catalogue"


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    log.info(msg)


class CatalogueAbort(Exception):
    pass


# ---------------------------------------------------------------- scanning
def _excluded(rel: str, name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(name, p) for p in patterns)


def scan(source: Source, progress: Progress, errors: list[str]):
    """Yield (rel_path, size, mtime) for every regular file under the source."""
    root = str(source.path)
    stack = [root]
    dirs = 0
    while stack:
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError as exc:
            errors.append(f"{current}: {exc.strerror or exc}")
            continue
        dirs += 1
        with it:
            for entry in it:
                rel = os.path.relpath(entry.path, root)
                if source.exclude and _excluded(rel, entry.name, source.exclude):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue  # symlinks, sockets, devices
                    st = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    errors.append(f"{entry.path}: {exc.strerror or exc}")
                    continue
                progress.update(1, st.st_size, extra=f"{dirs:,} folders")
                yield to_db(rel), st.st_size, st.st_mtime


def _insert_scan(db, batch: list, errors: list[str]) -> None:
    """Add scanned files to the temp table; a row that can't be stored is reported, not fatal."""
    try:
        db.executemany("INSERT OR REPLACE INTO temp.scan VALUES (?, ?, ?)", batch)
    except Exception:
        for row in batch:
            try:
                db.execute("INSERT OR REPLACE INTO temp.scan VALUES (?, ?, ?)", row)
            except Exception as exc:
                errors.append(f"{row[0]!r}: {exc}")


# ---------------------------------------------------------------- workers
_W: dict = {}


def _init_worker(opts: dict) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _W.clear()
    _W.update(opts)
    _W["et"] = None


def _get_exiftool():
    if _W.get("et") is None:
        _W["et"] = exiftool_mod.ExifTool()
    return _W["et"]


def read_file(item: tuple[str, str, int, float, int | None]) -> dict:
    """Read one file. Runs in a worker process. Never raises."""
    abs_path, rel, size, mtime, fid = item
    name = os.path.basename(rel)
    top = rel.split(os.sep, 1)[0] if os.sep in rel else "(root)"
    result = {
        "rel_path": rel, "top_folder": top, "size": size, "mtime": mtime,
        "ext": extension(name) or None, "kind": None, "mime": None, "ext_mismatch": 0,
        "sha256": None, "width": None, "height": None, "has_exif": None,
        "camera_make": None, "camera_model": None, "date_taken": None,
        "catalogue_error": None, "members": None, "fid": fid,
    }
    try:
        digest = hashlib.sha256() if _W.get("hash") == "sha256" else None
        with open(abs_path, "rb") as fh:
            first = fh.read(CHUNK)
            head = first[:HEAD_BYTES]
            if digest is not None:
                digest.update(first)
                while True:
                    block = fh.read(CHUNK)
                    if not block:
                        break
                    digest.update(block)
        if digest is not None:
            result["sha256"] = digest.hexdigest()
        kind, mime, mismatch = detect(head, name, size)
        result.update(kind=kind, mime=mime, ext_mismatch=int(mismatch))
    except OSError as exc:
        result["catalogue_error"] = f"read: {exc.strerror or exc}"
        result["kind"] = "unknown"
        return result

    if not _W.get("read_image_metadata"):
        return result
    try:
        if kind == "image" and (mime != "image/heic" or HEIF):
            result.update(image_info(abs_path))
        elif kind in ("raw", "video", "image") and _W.get("use_exiftool"):
            info = exiftool_info(_get_exiftool(), abs_path)
            result.update({k: v for k, v in info.items() if v is not None})
        if kind == "archive" and mime == "application/zip" and _W.get("list_archive_contents"):
            result["members"] = zip_members(abs_path)
    except Exception as exc:  # unreadable/corrupt files are recorded, not fatal
        result["catalogue_error"] = f"metadata: {type(exc).__name__}: {exc}"[:500]
    return result


# ---------------------------------------------------------------- main
UPSERT = """
INSERT INTO files (source, rel_path, top_folder, size, mtime, ext, kind, mime, ext_mismatch,
                   sha256, width, height, has_exif, camera_make, camera_model, date_taken,
                   catalogue_error, status, first_seen_run, last_seen_run, catalogued_at, updated_at)
VALUES (:source, :rel_path, :top_folder, :size, :mtime, :ext, :kind, :mime, :ext_mismatch,
        :sha256, :width, :height, :has_exif, :camera_make, :camera_model, :date_taken,
        :catalogue_error, 'catalogued', :run, :run, :now, :now)
ON CONFLICT (source, rel_path) DO UPDATE SET
    top_folder=excluded.top_folder, size=excluded.size, mtime=excluded.mtime, ext=excluded.ext,
    kind=excluded.kind, mime=excluded.mime, ext_mismatch=excluded.ext_mismatch,
    sha256=excluded.sha256, width=excluded.width, height=excluded.height,
    has_exif=excluded.has_exif, camera_make=excluded.camera_make,
    camera_model=excluded.camera_model, date_taken=excluded.date_taken,
    catalogue_error=excluded.catalogue_error,
    status=CASE WHEN files.status = 'missing' THEN 'catalogued' ELSE files.status END,
    last_seen_run=excluded.last_seen_run, catalogued_at=excluded.catalogued_at,
    updated_at=excluded.updated_at
"""


def run_catalogue(cfg: Config, source_name: str, retry_errors: bool = False) -> dict:
    source = cfg.source(source_name)
    if not source.path.is_dir():
        raise CatalogueAbort(f"Source folder not found or not a folder: {source.path}")

    m = Manifest(cfg.manifest, cfg.tz)
    run_id = m.start_run("catalogue", source.name)
    db = m.db
    summary: dict = {"source": source.name, "run_id": run_id}
    status = "failed"
    try:
        say(f"ImagePipe catalogue: source '{source.name}' ({source.display_path('')})")
        say(f"Manifest: {cfg.manifest}")

        # Phase 1 - scan
        db.execute("DROP TABLE IF EXISTS temp.scan")
        db.execute("CREATE TEMP TABLE scan (rel_path TEXT PRIMARY KEY, size INTEGER, mtime REAL)")
        scan_errors: list[str] = []
        batch = []
        with Progress("1/3 Scan", unit="files") as p:
            for row in scan(source, p, scan_errors):
                batch.append(row)
                if len(batch) >= 5000:
                    _insert_scan(db, batch, scan_errors)
                    batch.clear()
            if batch:
                _insert_scan(db, batch, scan_errors)
            scanned = p.count
            scanned_bytes = p.bytes
        for err in scan_errors:
            m.event(run_id, None, STAGE, "scan-error", err)
        summary.update(scanned=scanned, scanned_bytes=scanned_bytes, scan_errors=len(scan_errors))

        known = db.execute("SELECT COUNT(*) FROM files WHERE source=? AND status!='missing'", (source.name,)).fetchone()[0]
        if scanned == 0 and known > 0:
            raise CatalogueAbort(
                f"The source has no files but the manifest knows {known:,} from it. "
                "The source is probably not mounted. Nothing was changed."
            )

        # Phase 2 - compare
        with Progress("2/3 Compare", unit="steps", total=3) as p:
            p.set_extra("finding new and changed files")
            todo_sql = """
                CREATE TEMP TABLE todo AS
                SELECT s.rel_path, s.size, s.mtime, f.id AS fid FROM temp.scan s
                LEFT JOIN files f ON f.source = :src AND f.rel_path = s.rel_path
                WHERE f.id IS NULL OR f.size != s.size OR f.mtime != s.mtime
                   OR f.status = 'missing'
                   OR (:want_hash = 1 AND f.sha256 IS NULL AND s.size > 0)
            """
            if retry_errors:
                todo_sql += " OR f.catalogue_error IS NOT NULL"
            db.execute("DROP TABLE IF EXISTS temp.todo")
            db.execute(todo_sql, {"src": source.name, "want_hash": int(cfg.catalogue.hash == "sha256")})
            p.update(1, extra="marking unchanged files as seen")
            db.execute(
                """UPDATE files SET last_seen_run = :run WHERE source = :src AND EXISTS (
                       SELECT 1 FROM temp.scan s WHERE s.rel_path = files.rel_path
                       AND s.size = files.size AND s.mtime = files.mtime)""",
                {"run": run_id, "src": source.name},
            )
            p.update(1, extra="marking disappeared files as missing")
            gone = db.execute(
                """SELECT id, rel_path FROM files WHERE source = :src AND status = 'catalogued'
                   AND NOT EXISTS (SELECT 1 FROM temp.scan s WHERE s.rel_path = files.rel_path)""",
                {"src": source.name},
            ).fetchall()
            now = m.now()
            for row in gone:
                db.execute("UPDATE files SET status='missing', updated_at=? WHERE id=?", (now, row["id"]))
                m.event(run_id, row["id"], STAGE, "missing", row["rel_path"])
            db.commit()
            p.update(1, extra="done")
        todo_count, todo_bytes = db.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM temp.todo").fetchone()
        summary.update(to_read=todo_count, to_read_bytes=todo_bytes, missing=len(gone))
        say(f"{todo_count:,} new or changed files to read ({fmt_bytes(todo_bytes)}); "
            f"{scanned - todo_count:,} unchanged; {len(gone):,} no longer present (marked missing).")

        # Phase 3 - read
        et_ok = cfg.catalogue.use_exiftool and exiftool_mod.available()
        if cfg.catalogue.use_exiftool and not et_ok:
            say("Note: exiftool not found - RAW/video camera and date details will be skipped.")
        opts = {
            "hash": cfg.catalogue.hash,
            "read_image_metadata": cfg.catalogue.read_image_metadata,
            "use_exiftool": et_ok,
            "list_archive_contents": cfg.catalogue.list_archive_contents,
        }
        root = str(source.path)
        read_errors = 0
        kinds: dict[str, int] = {}
        pending: list[dict] = []

        def file_id(rel: str) -> int:
            return db.execute("SELECT id FROM files WHERE source=? AND rel_path=?", (source.name, rel)).fetchone()[0]

        failed_rows: list[str] = []

        def flush() -> None:
            now = m.now()
            for r in pending:
                r.update(source=source.name, run=run_id, now=now)
            try:
                db.executemany(UPSERT, pending)
            except Exception:
                # one bad row must not lose the batch or stop the run: write them one at a time
                db.rollback()
                good = []
                for r in pending:
                    try:
                        db.execute(UPSERT, r)
                        good.append(r)
                    except Exception as exc:
                        failed_rows.append(r["rel_path"])
                        log.error("could not record %s: %s", r["rel_path"], exc)
                pending[:] = good
            for r in pending:
                if r["fid"] is not None:
                    m.event(run_id, r["fid"], STAGE, "changed", r["rel_path"])
                if r["catalogue_error"]:
                    m.event(run_id, r["fid"] or file_id(r["rel_path"]), STAGE, "error", r["catalogue_error"])
                if r["members"] is not None:
                    fid = r["fid"] or file_id(r["rel_path"])
                    db.execute("DELETE FROM archive_members WHERE file_id=?", (fid,))
                    db.executemany(
                        "INSERT OR IGNORE INTO archive_members (file_id, name, size, kind_guess) VALUES (?, ?, ?, ?)",
                        [(fid, n, s, k) for n, s, k in r["members"]],
                    )
            db.commit()
            pending.clear()

        interrupted = False
        page_size = 20_000
        with Progress("3/3 Read", total=todo_count, total_bytes=todo_bytes) as p:
            # spawn, not fork: the progress thread is running and fork() with threads can deadlock
            pool = multiprocessing.get_context("spawn").Pool(cfg.catalogue.workers, initializer=_init_worker, initargs=(opts,))
            try:
                last_rowid = 0
                while True:
                    page = db.execute(
                        "SELECT rowid, rel_path, size, mtime, fid FROM temp.todo WHERE rowid > ? ORDER BY rowid LIMIT ?",
                        (last_rowid, page_size),
                    ).fetchall()
                    if not page:
                        break
                    last_rowid = page[-1]["rowid"]
                    items = [(to_fs(os.path.join(root, r["rel_path"])), r["rel_path"], r["size"], r["mtime"], r["fid"])
                             for r in page]
                    for r in pool.imap_unordered(read_file, items, chunksize=8):
                        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
                        if r["catalogue_error"]:
                            read_errors += 1
                        pending.append(r)
                        p.update(1, r["size"], extra=f"{read_errors:,} errors")
                        if len(pending) >= cfg.catalogue.batch_size:
                            flush()
                pool.close()
            except KeyboardInterrupt:
                interrupted = True
                pool.terminate()
            finally:
                pool.join()
                if pending:
                    flush()
            summary.update(read=p.count, read_errors=read_errors + len(failed_rows), kinds=kinds)

        status = "interrupted" if interrupted else "finished"
        if interrupted:
            say("Interrupted. Everything read so far is saved; run catalogue again to carry on.")
        return summary | {"status": status}
    except CatalogueAbort:
        raise
    except KeyboardInterrupt:
        status = "interrupted"
        say("Interrupted before reading started. Nothing was lost; run catalogue again.")
        return summary | {"status": status}
    finally:
        summary["status"] = status
        m.finish_run(run_id, status, summary)
        m.close()
