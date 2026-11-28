"""Final ready check (before a photo goes to the library).

A photo is ready when it has:
- a pipeline ID (in the manifest, in its name and in its metadata or RAW sidecar)
- a date, or is marked undated in the manifest
- no conflicts waiting for review, no held/error status
- no stray sidecars beside it (Takeout .json, .aae, or an .xmp that isn't its own RAW sidecar)
- a format PhotoPrism can read

The library stage (phase 6) runs this on ready/<batch>; until then it can be run on any batch
in processing to see what is still missing.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from imagepipe import ids
from imagepipe.detect import extension
from imagepipe.manifest import Manifest
from imagepipe.phase2 import meta
from imagepipe.progress import Progress

STRAY = (".json", ".aae", ".AAE", ".xmp", ".XMP")


def problems(m, tools, r) -> list[str]:
    out = []
    path = Path(r["current_path"] or "")
    if r["status"] in ("held", "error", "pending-conflict"):
        out.append(f"status {r['status']}")
    if not r["pipeline_id"]:
        out.append("no ID")
    elif ids.id_from_name(path.name) != r["pipeline_id"]:
        out.append("ID not at the front of the name")
    if not path.is_file():
        out.append("file missing")
        return out
    if r["pipeline_id"]:
        src = meta.sidecar_for(path) if meta.is_raw(path) else path
        got = tools.r.read(str(src), ("-XMP-dc:Identifier",)).get("Identifier") if src.is_file() else None
        if got != r["pipeline_id"]:
            out.append("ID not in the metadata" + (" (RAW sidecar)" if meta.is_raw(path) else ""))
    if not r["date_taken"] and not r["undated"]:
        out.append("no date and not marked undated")
    n = m.db.execute("SELECT COUNT(*) FROM conflicts WHERE file_id=? AND status='pending'", (r["id"],)).fetchone()[0]
    if n:
        out.append(f"{n} conflict(s) waiting for review")
    own = meta.sidecar_for(path) if meta.is_raw(path) else None
    for cand in [path.with_name(path.name + s) for s in STRAY] + [path.with_suffix(s) for s in STRAY]:
        if cand.is_file() and cand != own and cand != path:
            out.append(f"stray sidecar {cand.name}")
    if r["takeout_json"] and Path(r["takeout_json"]).is_file() and Path(r["takeout_json"]).parent == path.parent:
        name = Path(r["takeout_json"]).name
        if not any(name in p for p in out):
            out.append(f"stray sidecar {name}")
    if extension(path.name) not in meta.PHOTOPRISM_EXT:
        out.append(f"format .{extension(path.name)} isn't one PhotoPrism reads")
    return out


def ready_check(cfg, batch: str | None = None) -> dict:
    m = Manifest(cfg.manifest, cfg.tz)
    tools = meta.Tools()
    try:
        where = "(source LIKE 'sorted/%' OR source LIKE 'intake/%') AND status IN ('processed', 'pending-conflict', 'held', 'error')"
        params: tuple = ()
        if batch:
            where += " AND batch=?"
            params = (batch,)
        rows = m.db.execute(f"SELECT * FROM files WHERE {where} ORDER BY batch, current_path", params).fetchall()
        out_dir = cfg.reports_dir / "phase2"
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(cfg.tz).strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"ready-check-{batch or 'all'}-{stamp}.csv"
        ready = 0
        with open(path, "w", newline="", encoding="utf-8") as fh, \
                Progress("Ready check", total=len(rows)) as prog:
            w = csv.writer(fh)
            w.writerow(["pipeline_id", "batch", "path", "ready", "problems"])
            for r in rows:
                p = problems(m, tools, r)
                ready += not p
                w.writerow([r["pipeline_id"] or "", r["batch"], r["current_path"], "yes" if not p else "no", "; ".join(p)])
                prog.update()
        return {"checked": len(rows), "ready": ready, "not_ready": len(rows) - ready, "report": str(path)}
    finally:
        tools.close()
        m.close()
