"""Catalogue reports: summaries by type, folder and extension, plus full CSV export."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from imagepipe.config import Config, Source
from imagepipe.manifest import Manifest
from imagepipe.progress import Progress, fmt_bytes


def _write_csv(path: Path, header: list[str], rows) -> int:
    n = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for row in rows:
            w.writerow(list(row))
            n += 1
    return n


def write_reports(cfg: Config, source: Source) -> Path:
    """Write summary.md and CSV breakdowns for one source. Returns the report folder."""
    m = Manifest(cfg.manifest, cfg.tz)
    db = m.db
    stamp = datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
    out = cfg.reports_dir / source.name / f"catalogue-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    src = {"src": source.name}
    present = "source = :src AND status != 'missing'"

    by_kind = db.execute(
        f"SELECT kind, COUNT(*), SUM(size) FROM files WHERE {present} GROUP BY kind ORDER BY SUM(size) DESC", src
    ).fetchall()
    _write_csv(out / "by_kind.csv", ["kind", "files", "bytes"], by_kind)

    _write_csv(out / "by_folder.csv", ["top_folder", "kind", "files", "bytes"], db.execute(
        f"SELECT top_folder, kind, COUNT(*), SUM(size) FROM files WHERE {present} "
        "GROUP BY top_folder, kind ORDER BY top_folder, SUM(size) DESC", src))

    _write_csv(out / "by_extension.csv", ["ext", "kind", "files", "bytes"], db.execute(
        f"SELECT COALESCE(ext, ''), kind, COUNT(*), SUM(size) FROM files WHERE {present} "
        "GROUP BY ext, kind ORDER BY COUNT(*) DESC", src))

    n_mismatch = _write_csv(out / "extension_mismatches.csv", ["path", "ext", "detected_kind", "mime", "bytes"], (
        (source.display_path(r[0]), r[1], r[2], r[3], r[4]) for r in db.execute(
            f"SELECT rel_path, ext, kind, mime, size FROM files WHERE {present} AND ext_mismatch = 1 ORDER BY rel_path",
            src)))

    n_errors = _write_csv(out / "errors.csv", ["path", "error"], (
        (source.display_path(r[0]), r[1]) for r in db.execute(
            f"SELECT rel_path, catalogue_error FROM files WHERE {present} AND catalogue_error IS NOT NULL "
            "ORDER BY rel_path", src)))

    dupes = db.execute(
        f"""SELECT COUNT(*), COALESCE(SUM(n), 0), COALESCE(SUM((n - 1) * size), 0) FROM (
                SELECT COUNT(*) AS n, MAX(size) AS size FROM files
                WHERE {present} AND sha256 IS NOT NULL AND size > 0
                GROUP BY sha256 HAVING COUNT(*) > 1)""", src).fetchone()
    total = db.execute(f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files WHERE {present}", src).fetchone()
    missing = db.execute("SELECT COUNT(*) FROM files WHERE source = :src AND status = 'missing'", src).fetchone()[0]
    exif = db.execute(
        f"""SELECT SUM(has_exif = 1), SUM(camera_make IS NOT NULL), SUM(date_taken IS NOT NULL), COUNT(*)
            FROM files WHERE {present} AND kind IN ('image', 'raw')""", src).fetchone()
    folders = db.execute(
        f"SELECT top_folder, COUNT(*), SUM(size) FROM files WHERE {present} "
        "GROUP BY top_folder ORDER BY SUM(size) DESC, top_folder", src).fetchall()

    lines = [
        f"# Catalogue report: {source.name}",
        "",
        f"- Source: `{source.display_path('')}`",
        f"- Generated: {datetime.now(cfg.tz).isoformat(timespec='seconds')}",
        f"- Files present: {total[0]:,} ({fmt_bytes(total[1])})",
        f"- No longer present (marked missing): {missing:,}",
        f"- Exact duplicate groups (same SHA-256): {dupes[0]:,} groups, {dupes[1]:,} files, "
        f"{fmt_bytes(dupes[2])} in extra copies",
        f"- Extension doesn't match content: {n_mismatch:,} (see `extension_mismatches.csv`)",
        f"- Read errors: {n_errors:,} (see `errors.csv`)",
        "",
        "## By type",
        "",
        "| Kind | Files | Size |",
        "|---|---:|---:|",
        *[f"| {k or 'unread'} | {n:,} | {fmt_bytes(b or 0)} |" for k, n, b in by_kind],
        "",
    ]
    if exif and exif[3]:
        lines += [
            "## Images and RAW files",
            "",
            f"- With EXIF: {exif[0] or 0:,} of {exif[3]:,}",
            f"- With a camera make: {exif[1] or 0:,}",
            f"- With a date taken: {exif[2] or 0:,}",
            "",
        ]
    lines += [
        "## By top-level folder",
        "",
        "| Folder | Files | Size |",
        "|---|---:|---:|",
        *[f"| {f} | {n:,} | {fmt_bytes(b or 0)} |" for f, n, b in folders],
        "",
        "Breakdown of each folder by type: `by_folder.csv`. By extension: `by_extension.csv`.",
        "",
    ]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    m.close()
    return out


EXPORT_COLUMNS = [
    "path", "rel_path", "top_folder", "kind", "mime", "ext", "ext_mismatch", "size", "mtime",
    "sha256", "width", "height", "has_exif", "camera_make", "camera_model", "date_taken",
    "status", "catalogue_error",
]


def export_csv(cfg: Config, source: Source, output: Path | None = None) -> Path:
    """Write every file in the manifest for this source to one CSV, with progress."""
    m = Manifest(cfg.manifest, cfg.tz)
    db = m.db
    stamp = datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
    output = output or cfg.reports_dir / source.name / f"files-{stamp}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    total = db.execute("SELECT COUNT(*) FROM files WHERE source=?", (source.name,)).fetchone()[0]
    cols = ", ".join(EXPORT_COLUMNS[1:])
    with Progress("Export CSV", total=total, unit="rows") as p, output.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(EXPORT_COLUMNS)
        cur = db.execute(f"SELECT {cols} FROM files WHERE source=? ORDER BY rel_path", (source.name,))
        while True:
            rows = cur.fetchmany(5000)
            if not rows:
                break
            for r in rows:
                rec = list(r)
                rec[7] = datetime.fromtimestamp(rec[7], cfg.tz).isoformat(timespec="seconds")
                w.writerow([source.display_path(r[0])] + rec)
            p.update(len(rows))
    m.close()
    return output
