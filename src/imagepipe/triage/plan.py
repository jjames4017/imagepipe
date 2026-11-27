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
import time
from pathlib import Path
from urllib.parse import unquote

from imagepipe.config import Config
from imagepipe.ids import id_from_name, new_id
from imagepipe.progress import Progress
from imagepipe.triage import db as tdb
from imagepipe.triage.kinds import classify
from imagepipe.layered import special_tiff
from imagepipe.triage.meta import is_generic_title
from imagepipe.triage.naming import (Geocoder, build_name, date_from_mtime, date_from_path,
                                     ddmmyy_in, descriptive_filename, is_default_clock, name_date_of, smartname,
                                     far_apart, with_original_name)

log = logging.getLogger("imagepipe")

IMAGE_SIDECAR_EXT = {"xmp", "xmp_original", "aae"}
EMBEDDED_SOURCES = ("exif", "video", "pdf", "office", "audio", "email")
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
    base = tc.bucket_dir(bucket)
    parts = [p for p in logical_dir.split("/") if p] if logical_dir else []
    return base.joinpath(source_folder, *parts)


def _decoded(text: str) -> str:
    """URL-decode %xx in a name (Cliff%20Edwards -> Cliff Edwards); anything else unchanged."""
    if not re.search(r"%[0-9A-Fa-f]{2}", text):
        return text
    try:
        return unquote(text, errors="strict")
    except UnicodeDecodeError:
        return text


def _office_year(r) -> list[str]:
    return [r["meta_date"][:4]] if r["meta_date_src"] == "office" and r["meta_date"] else []


WEB_BUCKET = "web-pages"
PAGE_EXT = {"htm", "html", "shtml", "xhtml"}
SINGLE_FILE_PAGE_EXT = {"mht", "mhtml"}
WEB_ASSET_EXT = {"css", "js", "htm", "html"}


class WebPages:
    """Saved web pages in one source (or one archive): Page.htm + its Page_files folder.

    A "_files" folder counts when its page is beside it, or - without its page - when
    it holds web files (.css/.js/.htm), so an ordinary folder called "my_files" full
    of photos is not caught.
    """

    def __init__(self, rows):
        pages: set[tuple[str, str]] = set()   # (dir, lower stem) of .htm/.html files
        asset_dirs: set[str] = set()
        dirs: set[str] = set()
        for r in rows:
            d, ext = r["dir"] or "", (r["ext"] or "").lower()
            dirs.add(d)
            name = os.path.basename(r["rel_path"])
            if ext in PAGE_EXT:
                pages.add((d, name[: -(len(ext) + 1)].lower()))
            if ext in WEB_ASSET_EXT:
                asset_dirs.add(d)
        self.page_dirs: set[str] = set()  # lower-cased "_files" folders (full logical dir)
        self.pages = set()                # (dir, lower stem) of pages whose folder exists
        for d in dirs:
            for i, part in enumerate(parts := d.split("/")):
                if not part.lower().endswith("_files") or len(part) <= 6:
                    continue
                folder = "/".join(parts[: i + 1])
                parent, stem = "/".join(parts[:i]), part[:-6].lower()
                has_page = (parent, stem) in pages
                has_assets = any(a == folder or a.startswith(folder + "/") for a in asset_dirs)
                if has_page or has_assets:
                    self.page_dirs.add(folder.lower())
                    if has_page:
                        self.pages.add((parent, stem))

    def in_page_folder(self, d: str) -> bool:
        parts = (d or "").lower().split("/")
        return any("/".join(parts[: i + 1]) in self.page_dirs for i in range(len(parts)))

    def is_page(self, r) -> bool:
        ext = (r["ext"] or "").lower()
        if ext in SINGLE_FILE_PAGE_EXT:
            return True
        name = os.path.basename(r["rel_path"])
        return ext in PAGE_EXT and (r["dir"] or "", name[: -(len(ext) + 1)].lower()) in self.pages


class Planner:
    def __init__(self, cfg: Config, t: tdb.TriageDB):
        self.cfg = cfg
        self.tc = cfg.triage
        self.t = t
        self._geo = None
        self.used_ids: set[str] = set()
        self._web: dict[tuple[str, int], WebPages] = {}
        self._drift: dict | None = None

    def web(self, source: str, parent_id: int) -> WebPages:
        key = (source, parent_id)
        if key not in self._web:
            rows = self.t.db.execute("SELECT dir, rel_path, ext FROM files WHERE source=? AND parent_id=?",
                                     key).fetchall()
            self._web[key] = WebPages(rows)
        return self._web[key]

    @property
    def geo(self) -> Geocoder:
        if self._geo is None:
            self._geo = Geocoder(self.tc.reverse_geocode)
            if self.tc.reverse_geocode and not self._geo.available:
                say("Note: reverse_geocoder is not installed - GPS will not be turned into place names.")
        return self._geo

    def _drift_years(self) -> dict[tuple[str, str], set[int]]:
        """Camera (make, model) -> years whose 1 January it showed as a default clock (#35)."""
        if self._drift is None:
            self._drift = {}
            for row in self.t.db.execute(
                    "SELECT camera_make, camera_model, meta_date FROM files WHERE meta_date_src='exif' "
                    "AND camera_model IS NOT NULL AND substr(meta_date, 5, 6) = '-01-01'"):
                if is_default_clock(row[2]):
                    self._drift.setdefault((row[0] or "", row[1]), set()).add(int(row[2][:4]))
        return self._drift

    def _drifting(self, r, meta: str, src: str) -> bool:
        if src != "exif" or not r["camera_model"] or not meta[:4].isdigit():
            return False
        # a camera whose clock started at a default date and ran on from there
        return int(meta[:4]) in self._drift_years().get((r["camera_make"] or "", r["camera_model"]), ())

    def _pick_date(self, r, name: str, folders: list[str], sidecars: list,
                   bucket: str | None = None) -> tuple[str, str]:
        """(name date, source) for one file. See naming.py for the order.

        A camera default-clock EXIF date (#25) is only used if nothing better is found;
        a date in an Office file's name wins over the Office created date (#23).
        """
        tc = self.tc
        suspect = None
        candidates: list[tuple[str | None, str | None]] = []
        if r["meta_date"] and r["meta_date_src"] in EMBEDDED_SOURCES:
            candidates.append((r["meta_date"], r["meta_date_src"]))
        candidates += [(s["meta_date"], f"sidecar-{s['meta_date_src']}") for s in sidecars if s["meta_date"]]
        if r["meta_date"] and r["meta_date_src"] not in EMBEDDED_SOURCES:
            candidates.append((r["meta_date"], r["meta_date_src"]))
        if r["meta_date_src"] == "office":
            found = date_from_path(name, [], tc.day_first)  # the file name only, not its folders
            if found:
                return found
            # DDMMYY in the name, if within a year of the Office date (#33)
            short = ddmmyy_in(_stem(name), _office_year(r), tc.day_first)
            if short:
                return short[0], "filename"
        # A date from a camera whose clock may have drifted from its default (#35) is only
        # replaced by another date more than a year away from it (#41).
        drift = None
        for meta, src in candidates:
            if is_default_clock(meta):
                suspect = suspect or (name_date_of(meta), f"{src}-default-clock")
                continue
            if drift is None and self._drifting(r, meta, src):
                drift = (name_date_of(meta), src, meta[:10])
                continue
            if drift and not far_apart(meta[:10], drift[2]):
                return drift[0], drift[1]
            return name_date_of(meta), src
        found = date_from_path(name, folders, tc.day_first)
        if found and bucket == "ebooks" and found[1] == "filename" and len(found[0]) == 4:
            found = date_from_path("", folders, tc.day_first)  # a year in a book title isn't its date (#36)
        if found:
            if drift and not far_apart(found[0][:10], drift[2]):
                return drift[0], drift[1]
            return found
        if drift:
            return drift[0], f"{drift[1]}-default-clock"  # nothing to check it against: kept, but marked
        if suspect:
            return suspect  # better than the modified time
        return date_from_mtime(r["mtime"], self.cfg.tz), "mtime"

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
        if rows:
            web = self.web(rows[0]["source"], rows[0]["parent_id"])
            if web.in_page_folder(rows[0]["dir"]):
                # everything in a saved page's "_files" folder: moved with the page, names kept
                now = self.t.now()
                for r in rows:
                    dest = _dest_dir(tc, source_folder, WEB_BUCKET, r["dir"]) / os.path.basename(r["rel_path"])
                    self.t.update(r["id"], triage_id=self._pick_id(r), bucket=WEB_BUCKET, rule="web-page-files",
                                  name_date=None, date_source="kept-name", smartname=None,
                                  smartname_source="kept-name", dest_path=str(dest), companion_of=None,
                                  status=tdb.PLANNED, planned_at=now)
                    if progress is not None:
                        progress.update(1)
                return {WEB_BUCKET: len(rows)} if rows else {}
        else:
            web = None
        by_id = {r["id"]: r for r in rows}
        decided: dict[int, tuple[str, str]] = {r["id"]: classify(r, tc.overrides) for r in rows}
        for r in rows:
            if web is not None and web.is_page(r) and decided[r["id"]][0] not in ("quarantine/zero-byte",
                                                                                 "quarantine/system-junk"):
                decided[r["id"]] = (WEB_BUCKET, "web-page")
        for r in rows:  # multi-page, layered or unreadable TIFFs go to specials too (#55)
            if (r["ext"] or "").lower() in ("tif", "tiff") and decided[r["id"]][0] == "images":
                why = special_tiff(r["current_path"], bool(r["catalogue_error"]))
                if why:
                    decided[r["id"]] = ("specials", f"special-tiff-{why}")
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
            # %20 and friends decoded for reading dates and names; folders on disk are unchanged (#32)
            name = _decoded(os.path.basename(r["rel_path"]))
            folders = [_decoded(p) for p in r["dir"].split("/") if p]
            date, src = self._pick_date(r, name, folders, side_meta.get(r["id"], []), bucket)
            title = r["meta_title"] or next((s["meta_title"] for s in side_meta.get(r["id"], [])
                                             if s["meta_title"]), None)
            if title and is_generic_title(title):
                title = None
            if r["meta_date_src"] == "office" and descriptive_filename(_stem(name)):
                # an Office title is often the template's ("Risk Assessment Register"): a
                # descriptive file name says more about this file (#23)
                title = None
            stem = _stem(name)
            if src == "filename":
                short = ddmmyy_in(stem, folders + _office_year(r), tc.day_first)
                if short and date == short[0]:
                    stem = stem.replace(short[1], " ")  # the DDMMYY date is in the name's date part now
            ebook = bucket == "ebooks"
            smart, smart_src = smartname(place_of.get(r["id"]), title, folders, stem, tc.smartname_max_length,
                                         keep_dates=ebook, used_date=date)
            if bucket and bucket.startswith("quarantine/"):
                smart = with_original_name(smart, _stem(name), tc.smartname_max_length)
                smart_src += "+original-name"
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
                bucket, rule = classify(row, tc.overrides)
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
        # Each folder is its own savepoint, so a folder that fails is undone on its own; the
        # transaction is committed every couple of seconds rather than after every folder.
        last_commit = time.monotonic()
        try:
            for g in groups:
                if g["source"] not in folder_of:
                    skipped_sources.add(g["source"])
                    continue
                rows = t.db.execute(
                    f"SELECT * FROM files WHERE {base} AND source=? AND parent_id=? AND dir=? ORDER BY rel_path",
                    (*params, g["source"], g["parent_id"], g["dir"])).fetchall()
                if progress is not None:
                    progress.set_extra(f"folder: {g['dir'] or '(top)'}")
                t.db.execute("SAVEPOINT plan_folder")
                try:
                    for k, v in planner.plan_group(rows, folder_of[g["source"]], progress).items():
                        counts[k] = counts.get(k, 0) + v
                    t.db.execute("RELEASE plan_folder")
                except KeyboardInterrupt:
                    t.db.execute("ROLLBACK TO plan_folder")
                    t.db.execute("RELEASE plan_folder")
                    raise
                except Exception as exc:
                    # a folder that can't be planned is marked and skipped; the rest carry on
                    t.db.execute("ROLLBACK TO plan_folder")
                    t.db.execute("RELEASE plan_folder")
                    log.exception("could not plan folder %s", g["dir"])
                    say(f"Problem planning folder '{g['dir']}': {exc} - its files are marked 'error', "
                        f"the rest carry on")
                    for r in rows:
                        t.update(r["id"], status=tdb.ERROR)
                        t.action(None, r, "error", r["current_path"], None, None, detail=f"plan: {exc}"[:500])
                    counts["error"] = counts.get("error", 0) + len(rows)
                    if progress is not None:
                        progress.update(len(rows))
                if time.monotonic() - last_commit >= 2:
                    t.db.commit()
                    last_commit = time.monotonic()
        finally:
            t.db.commit()

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
