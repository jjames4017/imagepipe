"""Files tab: search the files triage has placed, and move them by hand to another triage
destination (a Triage Category, a quarantine folder or an excluded folder).

A move keeps the file's original sub-folders under the destination
(<destination>/<source folder>/<original folders>/<name>), moves its companions (sidecars,
Live Photo clips) with it, updates the triage database and logs a `manual-move` action. A
PDF moved by hand is recorded in pdf_checks as decided by you, so Check PDFs leaves it alone.
Moves are the same safe moves triage uses: checked, never overwriting, nothing deleted.
"""

from __future__ import annotations

import os
from pathlib import Path

from imagepipe.fspath import to_db
from imagepipe.triage import db as tdb
from imagepipe.triage.apply import MoveError, free_name, transfer

MAX_MOVE = 5000


def destinations(tc) -> list[str]:
    """Every triage destination: the categories, then quarantine/<x> and excluded/<x> folders that exist
    (plus the ones triage and ImagePipe use)."""
    out = list(tc.categories)
    for top, known in (("quarantine", ("pdf-uncertain", "unknown-type", "corrupt", "encrypted", "archive-failed",
                                       "zero-byte", "system-junk")),
                       ("excluded", ("triage-duplicate", "pdf-source", "pdf-reprocessed"))):
        names = set(known)
        d = tc.sorted_dir / top
        if d.is_dir():
            names |= {p.name for p in d.iterdir() if p.is_dir()}
        out += [f"{top}/{n}" for n in sorted(names)]
    return out


def dest_root(tc, dest: str) -> Path:
    return tc.sorted_dir.joinpath(*dest.split("/"))


def where_now(tc, row) -> str:
    """The destination a file is in now (category, quarantine/<x> or excluded/<x>)."""
    try:
        rel = Path(row["dest_path"]).relative_to(tc.sorted_dir).parts
    except (ValueError, TypeError):
        return ""
    if not rel:
        return ""
    return "/".join(rel[:2]) if rel[0] in ("quarantine", "excluded") else rel[0]


def search(cfg, q: str = "", dest: str = "", limit: int = 100, offset: int = 0) -> dict:
    tc = cfg.triage
    t = tdb.TriageDB(tc.database, cfg.tz, None)
    try:
        where, params = ["status=?", "companion_of IS NULL", "dest_path IS NOT NULL"], [tdb.DONE]
        if dest:
            where.append("dest_path LIKE ?")
            params.append(to_db(str(dest_root(tc, dest))) + "/%")
        for word in q.split():
            where.append("(dest_path LIKE ? OR origin LIKE ?)")
            params += [f"%{word}%", f"%{word}%"]
        w = " AND ".join(where)
        total = t.db.execute(f"SELECT COUNT(*) FROM files WHERE {w}", params).fetchone()[0]
        rows = t.db.execute(f"SELECT id, triage_id, dest_path, origin, size, ext FROM files WHERE {w} "
                            "ORDER BY dest_path LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
        return {"total": total, "rows": [{"id": r["id"], "triage_id": r["triage_id"], "name": os.path.basename(r["dest_path"]),
                                          "path": r["dest_path"], "origin": r["origin"], "size": r["size"],
                                          "where": where_now(tc, r)} for r in rows]}
    finally:
        t.close()


def move_files(cfg, ids: list[int], dest: str) -> dict:
    tc = cfg.triage
    if dest not in destinations(tc):
        raise ValueError(f"'{dest}' isn't a triage destination")
    if not ids:
        raise ValueError("No files chosen")
    if len(ids) > MAX_MOVE:
        raise ValueError(f"At most {MAX_MOVE:,} files at a time")
    folder_of = {s.name: s.path.name for s in tc.sources}
    t = tdb.TriageDB(tc.database, cfg.tz, tc.reports_dir / "actions.csv")
    run_id = t.start_run("manual-move")
    moved, skipped, failed = 0, [], []
    try:
        for fid in ids:
            row = t.get(int(fid))
            if row is None or row["status"] != tdb.DONE or not row["dest_path"]:
                skipped.append({"id": fid, "why": "not a placed triage file"})
                continue
            if where_now(tc, row) == dest:
                skipped.append({"id": fid, "why": f"already in {dest}"})
                continue
            if not os.path.lexists(row["dest_path"]):
                skipped.append({"id": fid, "why": f"not found at {row['dest_path']}"})
                continue
            parts = [p for p in (row["dir"] or "").split("/") if p]
            target_dir = dest_root(tc, dest).joinpath(folder_of.get(row["source"], row["source"]), *parts)
            try:
                group = [row] + [c for c in t.db.execute("SELECT * FROM files WHERE companion_of=? AND status=?",
                                                         (row["id"], tdb.DONE))
                                 if c["dest_path"] and os.path.lexists(c["dest_path"])]
                for r in group:
                    src = r["dest_path"]
                    target_dir.mkdir(parents=True, exist_ok=True)
                    new = free_name(target_dir / os.path.basename(src))
                    method = transfer(src, new, r["sha256"], tc.verify)
                    t.action(run_id, r, "manual-move", src, str(new), rule="manual", sha_before=r["sha256"],
                             sha_after=r["sha256"], detail=f"{method}; moved by you to {dest}")
                    t.update(r["id"], dest_path=str(new), current_path=str(new), bucket=dest)
                    if (r["ext"] or "").lower() == "pdf":
                        _pdf_decided(t, src, new, dest)
                t.db.commit()
                moved += 1
            except (MoveError, OSError) as exc:
                failed.append({"id": fid, "path": row["dest_path"], "why": f"{type(exc).__name__}: {exc}"})
        t.finish_run(run_id, "finished", {"moved": moved, "skipped": len(skipped), "failed": len(failed)})
    finally:
        t.close()
    return {"moved": moved, "skipped": skipped, "failed": failed}


def _pdf_decided(t, old: str, new: Path, dest: str) -> None:
    """Record a hand-moved PDF so Check PDFs leaves it alone (its verdict is yours)."""
    st = new.stat()
    note = f"moved by you to {dest}"
    for r in t.db.execute("SELECT id FROM pdf_checks WHERE path=? OR final_path=?", (to_db(old), to_db(old))):
        # a PDF still waiting in ScanSplitter stops waiting: you've decided where it goes
        t.db.execute("UPDATE pdf_checks SET final_path=?, state=CASE WHEN state IN ('waiting-review', 'checking') "
                     "THEN 'done' ELSE state END, note=?, updated_at=? WHERE id=?",
                     (to_db(str(new)), note, t.now(), r[0]))
    t.db.execute(
        "INSERT INTO pdf_checks (path, size, mtime, verdict, state, final_path, note, checked_at, updated_at) "
        "VALUES (?, ?, ?, 'manual', 'done', ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
        "mtime=excluded.mtime, verdict='manual', state='done', final_path=excluded.final_path, note=excluded.note, "
        "updated_at=excluded.updated_at",
        (to_db(str(new)), st.st_size, st.st_mtime, to_db(str(new)), note, t.now(), t.now()))
