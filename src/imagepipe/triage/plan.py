"""Triage plan: decide where every catalogued file goes and what it will be
called. Changes nothing on disk - the result is a dry-run report to review
before `triage apply`.

Per folder:
  1. classify each file into a bucket (see kinds.py)
  2. pair sidecars (XMP/AAE/THM, Google Takeout JSON) and Live Photo clips
     with the file they belong to - they travel with it and share its name
  3. give each file a pipeline ID, a date and a smartname, and work out its
     destination: <bucket root>/[<type or reason>/]<source folder>/<original folders>/<new name>
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

from imagepipe.config import Config
from imagepipe.ids import id_from_name, new_id
from imagepipe.progress import Progress
from imagepipe.triage import db as tdb
from imagepipe.triage.kinds import classify
from imagepipe.triage.naming import (Geocoder, build_name, date_from_mtime, date_from_path, smartname)

log = logging.getLogger("imagepipe")

IMAGE_SIDECAR_EXT = {"xmp", "xmp_original", "aae"}
EMBEDDED_SOURCES = ("exif", "video", "pdf", "office", "audio")
SIDECAR_SOURCES = ("takeout", "xmp")


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    log.info(msg)


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name


def takeout_candidates(json_name: str, title: str | None) -> list[str]:
    """Names of the photo a Google Takeout JSON may describe."""
    out = []
    if title:
        out.append(title)
    base = json_name[:-5] if json_name.lower().endswith(".json") else json_name
    num = ""
    m = re.match(r"^(.*)(\(\d+\))$", base)
    if m:
        base, num = m.group(1), m.group(2)
    head, _, last = base.rpartition(".")
    if head and last and last[0].lower() == "s" and "supplemental-metadata".startswith(last.lower()):
        base = head  # IMG.jpg.supplemental-metadata.json (often truncated)
    out.append(base)
    if num:
        st, dot, ext = base.rpartition(".")
        out.append(f"{st}{num}.{ext}" if dot else f"{base}{num}")
    return out


def _dest_dir(tc, source_folder: str, bucket: str, logical_dir: str) -> Path:
    if "/" in bucket:
        root_key, sub = bucket.split("/", 1)
        base = tc.destinations[root_key] / sub
    else:
        base = tc.destinations[bucket]
    parts = [p for p in logical_dir.split("/") if p] if logical_dir else []
    return base.joinpath(source_folder, *parts)


class Planner:
    def __init__(self, cfg: Config, t: tdb.TriageDB):
        self.cfg = cfg
        self.tc = cfg.triage
        self.t = t
        self._geo = None
        self.used_ids: set[str] = set()

    @property
    def geo(self) -> Geocoder:
        if self._geo is None:
            self._geo = Geocoder(self.tc.reverse_geocode)
            if self.tc.reverse_geocode and not self._geo.available:
                say("Note: reverse_geocoder is not installed - GPS will not be turned into place names.")
        return self._geo

    def _pick_id(self, row) -> str:
        if row["triage_id"]:
            return row["triage_id"]
        existing = id_from_name(row["rel_path"])
        if existing and existing not in self.used_ids and not self.t.db.execute(
                "SELECT 1 FROM files WHERE triage_id=?", (existing,)).fetchone():
            pid = existing  # an ID already in the name is kept, never replaced
        else:
            pid = new_id()
        self.used_ids.add(pid)
        return pid

    def plan_group(self, rows: list, source_folder: str, progress: Progress | None) -> dict[str, int]:
        tc = self.tc
        counts: dict[str, int] = {}
        by_id = {r["id"]: r for r in rows}
        decided: dict[int, tuple[str, str]] = {r["id"]: classify(r, tc.bucket_overrides) for r in rows}
        by_name = {os.path.basename(r["rel_path"]).lower(): r for r in rows}
        media_by_stem: dict[str, list] = {}
        for r in rows:
            if r["kind"] in ("image", "raw", "video"):
                media_by_stem.setdefault(_stem(os.path.basename(r["rel_path"])).lower(), []).append(r)

        companions: dict[int, tuple[int, str]] = {}  # companion id -> (parent id, naming form)
        side_meta: dict[int, list] = {}              # parent id -> sidecar rows

        def attach(comp, parent, form: str, rule: str) -> None:
            companions[comp["id"]] = (parent["id"], form)
            side_meta.setdefault(parent["id"], []).append(comp)
            decided[comp["id"]] = (None, rule)  # bucket filled from the parent below

        for r in rows:
            name = os.path.basename(r["rel_path"])
            low = name.lower()
            ext = (r["ext"] or "").lower()
            if r["kind"] == "sidecar" and ext in IMAGE_SIDECAR_EXT | {"thm"}:
                full = low[: -(len(ext) + 1)]
                target = by_name.get(full)
                if target is not None and target["kind"] in ("image", "raw", "video") and target["id"] != r["id"]:
                    attach(r, target, "full", f"sidecar-{ext}")
                    continue
                cands = media_by_stem.get(_stem(name).lower(), [])
                want = ("video",) if ext == "thm" else ("image", "raw")
                cands = [c for c in cands if c["kind"] in want] or [c for c in cands if c["id"] != r["id"]]
                if cands:
                    attach(r, cands[0], "stem", f"sidecar-{ext}")
            elif r["takeout_title"]:
                target = None
                for cand in takeout_candidates(name, r["takeout_title"]):
                    target = by_name.get(cand.lower())
                    if target is not None and target["kind"] in ("image", "raw", "video"):
                        break
                    target = None
                if target is None:
                    base = takeout_candidates(name, None)[0].lower()
                    if len(base) >= 30:
                        hits = [x for n, x in by_name.items() if n.startswith(base) and x["id"] != r["id"]
                                and x["kind"] in ("image", "raw", "video")]
                        target = hits[0] if len(hits) == 1 else None
                if target is not None:
                    attach(r, target, "full", "takeout-json")

        # Live Photo clips: a short video with the same name as a photo beside it
        for r in rows:
            if r["kind"] == "video" and r["id"] not in companions and r["size"] <= tc.live_photo_max_bytes:
                stills = [c for c in media_by_stem.get(_stem(os.path.basename(r["rel_path"])).lower(), [])
                          if c["kind"] in ("image", "raw")]
                if stills:
                    attach(r, stills[0], "stem", "live-photo-clip")

        primaries = [r for r in rows if r["id"] not in companions]

        # place names for everything with GPS (own or from a sidecar), in one batch
        gps_of: dict[int, tuple[float, float]] = {}
        for r in primaries:
            g = (r["gps_lat"], r["gps_lon"]) if r["gps_lat"] is not None else None
            if g is None:
                for s in side_meta.get(r["id"], []):
                    if s["gps_lat"] is not None:
                        g = (s["gps_lat"], s["gps_lon"])
                        break
            if g is not None:
                gps_of[r["id"]] = g
        place_of = {}
        if gps_of:
            ids = list(gps_of)
            for fid, place in zip(ids, self.geo.places([gps_of[i] for i in ids])):
                place_of[fid] = place

        now = self.t.now()
        planned: dict[int, dict] = {}
        taken: set[str] = set()
        for r in primaries:
            bucket, rule = decided[r["id"]]
            pid = self._pick_id(r)
            name = os.path.basename(r["rel_path"])
            folders = [p for p in r["dir"].split("/") if p]
            date = src = None
            if r["meta_date"] and r["meta_date_src"] in EMBEDDED_SOURCES:
                date, src = r["meta_date"][:10], r["meta_date_src"]
            if date is None:
                for s in side_meta.get(r["id"], []):
                    if s["meta_date"]:
                        date, src = s["meta_date"][:10], f"sidecar-{s['meta_date_src']}"
                        break
            if date is None and r["meta_date"]:
                date, src = r["meta_date"][:10], r["meta_date_src"]
            if date is None:
                found = date_from_path(name, folders, tc.day_first)
                if found:
                    date, src = found
            if date is None:
                date, src = date_from_mtime(r["mtime"], self.cfg.tz), "mtime"
            title = r["meta_title"] or next((s["meta_title"] for s in side_meta.get(r["id"], [])
                                             if s["meta_title"]), None)
            smart, smart_src = smartname(place_of.get(r["id"]), title, folders, _stem(name), tc.smartname_max_length)
            dest = None
            if bucket != "extract":
                new_name = build_name(pid, date, smart, (r["ext"] or "").lower())
                dest = _dest_dir(tc, source_folder, bucket, r["dir"]) / new_name
                dest = self._unique(dest, taken)
            planned[r["id"]] = dict(triage_id=pid, bucket=bucket, rule=rule, name_date=date, date_source=src,
                                    smartname=smart, smartname_source=smart_src,
                                    dest_path=str(dest) if dest else None, companion_of=None)

        pending = dict(companions)
        while pending:
            progressed = False
            for cid, (parent_id, form) in list(pending.items()):
                if parent_id not in planned:
                    continue  # its parent is itself a companion not placed yet (THM -> Live Photo clip)
                del pending[cid]
                progressed = True
                comp = by_id[cid]
                parent = planned[parent_id]
                pid = self._pick_id(comp)
                ext = (comp["ext"] or "").lower()
                dest = None
                if parent["dest_path"]:
                    pdest = Path(parent["dest_path"])
                    cname = f"{pdest.name}.{ext}" if form == "full" else f"{pdest.name.split('.', 1)[0]}.{ext}"
                    dest = self._unique(pdest.with_name(cname), taken)
                planned[cid] = dict(triage_id=pid, bucket=parent["bucket"], rule=decided[cid][1],
                                    name_date=parent["name_date"], date_source=parent["date_source"],
                                    smartname=parent["smartname"], smartname_source=parent["smartname_source"],
                                    dest_path=str(dest) if dest else None, companion_of=parent_id)
            if not progressed:  # a loop of companions - place them on their own
                for cid in list(pending):
                    del pending[cid]
                    comp = by_id[cid]
                    planned[cid] = dict(triage_id=self._pick_id(comp), bucket=None, rule=decided[cid][1],
                                        name_date=date_from_mtime(comp["mtime"], self.cfg.tz),
                                        date_source="mtime", smartname=_stem(os.path.basename(comp["rel_path"])),
                                        smartname_source="original-name", dest_path=None, companion_of=None)

        for fid, cols in planned.items():
            if cols["bucket"] == "extract" and cols["companion_of"] is None:
                pass
            elif cols["dest_path"] is None:
                # a companion whose parent is an archive: it goes on its own
                row = by_id[fid]
                bucket, rule = classify(row, tc.bucket_overrides)
                if bucket == "extract":
                    cols.update(bucket=bucket, rule=rule, companion_of=None)
                else:
                    new_name = build_name(cols["triage_id"], cols["name_date"], cols["smartname"],
                                          (row["ext"] or "").lower())
                    cols.update(bucket=bucket, rule=rule, companion_of=None, dest_path=str(
                        self._unique(_dest_dir(tc, source_folder, bucket, row["dir"]) / new_name, taken)))
            self.t.update(fid, **cols, status=tdb.PLANNED, planned_at=now)
            counts[cols["bucket"]] = counts.get(cols["bucket"], 0) + 1
            if progress is not None:
                progress.update(1)
        return counts

    @staticmethod
    def _unique(dest: Path, taken: set[str]) -> Path:
        if str(dest) not in taken:
            taken.add(str(dest))
            return dest
        stem, suffix = (dest.name.split(".", 1) + [""])[:2]  # smartnames never contain dots
        n = 1
        while True:
            cand = dest.with_name(f"{stem}-{n}" + (f".{suffix}" if suffix else ""))
            if str(cand) not in taken:
                taken.add(str(cand))
                return cand
            n += 1


def plan_rows(cfg: Config, t: tdb.TriageDB, where: str = "", params: tuple = (), quiet: bool = False) -> dict:
    """Plan every catalogued/planned file matching `where`. Returns counts per bucket."""
    tc = cfg.triage
    folder_of = {s.name: s.path.name for s in tc.sources}
    base = f"status IN ('{tdb.CATALOGUED}', '{tdb.PLANNED}')" + (f" AND ({where})" if where else "")
    total = t.db.execute(f"SELECT COUNT(*) FROM files WHERE {base}", params).fetchone()[0]
    groups = t.db.execute(f"SELECT DISTINCT source, parent_id, dir FROM files WHERE {base}", params).fetchall()
    planner = Planner(cfg, t)
    counts: dict[str, int] = {}
    skipped_sources: set[str] = set()

    def work(progress):
        for g in groups:
            if g["source"] not in folder_of:
                skipped_sources.add(g["source"])
                continue
            rows = t.db.execute(
                f"SELECT * FROM files WHERE {base} AND source=? AND parent_id=? AND dir=? ORDER BY rel_path",
                (*params, g["source"], g["parent_id"], g["dir"])).fetchall()
            try:
                for k, v in planner.plan_group(rows, folder_of[g["source"]], progress).items():
                    counts[k] = counts.get(k, 0) + v
                t.db.commit()
            except Exception as exc:
                # a folder that can't be planned is marked and skipped; the rest carry on
                t.db.rollback()
                log.exception("could not plan folder %s", g["dir"])
                say(f"Problem planning folder '{g['dir']}': {exc} - its files are marked 'error', the rest carry on")
                for r in rows:
                    t.update(r["id"], status=tdb.ERROR)
                    t.action(None, r, "error", r["current_path"], None, None, detail=f"plan: {exc}"[:500])
                t.db.commit()
                counts["error"] = counts.get("error", 0) + len(rows)
                if progress is not None:
                    progress.update(len(rows))

    if quiet:
        work(None)
    else:
        with Progress("Plan", total=total) as p:
            work(p)
    for s in skipped_sources:
        say(f"Note: files from source '{s}' were skipped - it is no longer in triage.sources.")
    return counts


def run_plan(cfg: Config) -> dict:
    from imagepipe.triage.reports import write_plan_report

    tc = cfg.triage
    t = tdb.TriageDB(tc.database, cfg.tz, tc.reports_dir / "actions.csv")
    run_id = t.start_run("triage-plan")
    status = "failed"
    summary: dict = {"run_id": run_id}
    try:
        counts = plan_rows(cfg, t)
        summary["planned"] = counts
        status = "finished"
        out = write_plan_report(cfg, t, run_id)
        summary["report"] = str(out)
        return summary | {"status": status}
    except KeyboardInterrupt:
        status = "interrupted"
        say("Interrupted. Run plan again - it starts over and keeps every ID already given.")
        return summary | {"status": status}
    finally:
        t.finish_run(run_id, status, summary)
        t.close()
