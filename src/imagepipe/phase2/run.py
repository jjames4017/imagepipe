"""Phase 2 run: stage 1 (month batches), stage 2 (ID check + fingerprint, then the ID written into
the file), stage 4 (exact duplicates), Google Takeout dates/GPS and the per-batch checks.

One batch per run:

1. Find   - every photo in the categories whose pipeline is imagepipe (sorted/images,
            sorted/raw): date taken (EXIF, else Google Takeout), recorded in the manifest.
2. Batch  - one batch per calendar month by date taken, split above `batch_size`;
            undated photos in batches of `batch_size`.
3. Stage 1 - the oldest waiting batch moves to <filestack>/imagepipe/processing/<batch>/,
            with the .json/.xmp/.aae files that belong to each photo.
4. Stage 2a - ID check (`<ID>_` name prefix or XMP-dc:Identifier; a known ID on a
            different file is a re-entry), SHA-256, pixel hash (a full decode: a photo
            that doesn't decode goes to excluded/10-corrupt), wrong extensions corrected,
            orientation tag and low-resolution flags.
5. Stage 4 - byte-identical copies: one is kept, the rest go to excluded/01-exact-dup.
            Pixel-identical copies with different metadata: non-conflicting metadata is
            merged into the kept copy (captions joined; keywords and faces merged; highest
            rating; camera, title and orientation from the kept copy), conflicting dates
            and GPS go to the Conflicts tab, the others go to excluded/02-pixel-dup.
6. Stage 2b - the ID (and Takeout dates/GPS where the photo has none) is written into each
            kept file: into a copy first, which must open and read the ID back, then swapped
            in. RAW files get a sidecar <name>.<ext>.xmp instead. The file is renamed
            <ID>_<name>. A Takeout value that disagrees with the photo is a conflict.
7. Checks - burst sequences flagged.

Stopping (Stop button or Ctrl+C) keeps everything done; the next run carries on with the
same batch. Nothing is deleted: every file taken out goes to sorted/excluded/<reason>/ and
a row in <reports_dir>/excluded.csv.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from imagepipe import ids
from imagepipe.detect import EXT_KIND, SIDECAR_EXT, extension
from imagepipe.fspath import to_db
from imagepipe.manifest import Manifest
from imagepipe.phase2 import conflicts as cf
from imagepipe.phase2 import meta
from imagepipe.progress import Progress
from imagepipe.triage.apply import MoveError, free_name, transfer

log = logging.getLogger("imagepipe")
STAGE = "phase2"
FORMAT_RANK = {"tif": 4, "tiff": 4, "png": 4, "heic": 3, "heif": 3, "avif": 3, "jpg": 2, "jpeg": 2}
EXCLUDED_COLUMNS = ("pipeline_id", "original_path", "excluded_path", "reason", "keeper_pipeline_id", "similarity",
                    "width", "height", "size", "timestamp")
TRAVELLING_EXT = {"json"} | SIDECAR_EXT  # files that travel with their photo


class Phase2Error(Exception):
    pass


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    log.info(msg)


def strip_id(name: str) -> str:
    found = ids.id_from_name(name)
    if found and name.startswith(found + "_"):
        return name[len(found) + 1:]
    return name


class Phase2:
    def __init__(self, cfg):
        if cfg.triage is None:
            raise Phase2Error("Phase 2 needs the triage section (it uses the filestack and categories)")
        self.cfg, self.tc, self.p2 = cfg, cfg.triage, cfg.phase2
        self.m = Manifest(cfg.manifest, cfg.tz)
        self.tools = meta.Tools()
        self.run_id = None
        self.stats: dict[str, int] = defaultdict(int)
        self.processing = self.tc.imagepipe_dir / "processing"
        self.excluded = self.tc.sorted_dir / "excluded"
        self.excluded_csv = cfg.reports_dir / "excluded.csv"

    def close(self):
        self.tools.close()
        self.m.close()

    # -- small helpers -------------------------------------------------------------------
    def row(self, file_id: int):
        return self.m.db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()

    def update(self, file_id: int, **cols) -> None:
        cols["updated_at"] = self.m.now()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.m.db.execute(f"UPDATE files SET {sets} WHERE id=?", (*cols.values(), file_id))

    def flag(self, file_id: int, flag: str, detail: str) -> None:
        self.m.db.execute("INSERT OR REPLACE INTO flags (file_id, flag, detail, created_at) VALUES (?, ?, ?, ?)",
                          (file_id, flag, detail, self.m.now()))
        self.stats[f"flag:{flag}"] += 1

    def add_copy(self, file_id: int, original: str, role: str, dup_file_id: int | None = None) -> None:
        self.m.db.execute("INSERT OR IGNORE INTO copies (file_id, original_path, role, dup_file_id, created_at) "
                          "VALUES (?, ?, ?, ?, ?)", (file_id, to_db(original), role, dup_file_id, self.m.now()))

    def original_path(self, file_id: int) -> str | None:
        r = self.m.db.execute("SELECT original_path FROM copies WHERE file_id=? AND role='original' LIMIT 1",
                              (file_id,)).fetchone()
        return r[0] if r else None

    # -- 1. find ------------------------------------------------------------------------
    def find(self) -> None:
        cats = self.tc.categories_for("imagepipe")
        todo = []
        for cat in cats:
            root = self.tc.bucket_dir(cat)
            if not root.is_dir():
                continue
            source = f"sorted/{cat}"
            known = {r["rel_path"]: r for r in self.m.db.execute(
                "SELECT id, rel_path, size, mtime, status FROM files WHERE source=?", (source,))}
            seen = set()
            for dirpath, dirs, files in os.walk(root):
                dirs.sort()
                for name in sorted(files):
                    if extension(name) in TRAVELLING_EXT or name.startswith(".ipw-"):
                        continue
                    p = Path(dirpath) / name
                    rel = to_db(str(p.relative_to(root)))
                    seen.add(rel)
                    st = p.stat()
                    old = known.get(rel)
                    if old is not None and old["status"] not in ("waiting", "missing"):
                        continue  # moved on already; a new file at the same path would be odd - left alone
                    if old is not None and (old["size"], old["mtime"]) == (st.st_size, st.st_mtime) \
                            and old["status"] == "waiting":
                        continue
                    todo.append((cat, source, root, p, rel, st, old))
            for rel, r in known.items():
                if r["status"] == "waiting" and rel not in seen:
                    self.update(r["id"], status="missing")
        self.m.db.commit()
        say(f"Photos to read in {', '.join('sorted/' + c for c in cats)}: {len(todo):,}")
        if not todo:
            return
        with Progress("Find photos", total=len(todo), total_bytes=sum(t[5].st_size for t in todo)) as prog:
            for n, (cat, source, root, p, rel, st, old) in enumerate(todo, 1):
                prog.set_extra(rel)
                md = meta.read_meta(self.tools.r, p)
                tj = meta.find_takeout_json(p)
                take = meta.read_takeout(tj) if tj else None
                date, date_source = md.get("date"), "exif" if md.get("date") else None
                if not date and take and take.get("date"):
                    date, date_source = take["date"], "takeout"
                gps = md.get("gps")
                ext = extension(p.name)
                cols = dict(
                    top_folder=rel.split("/", 1)[0] if "/" in rel else "", size=st.st_size, mtime=st.st_mtime,
                    ext=ext, kind=EXT_KIND.get(ext, "unknown"), date_taken=date, date_source=date_source,
                    undated=0 if date else 1, gps_lat=gps[0] if gps else None, gps_lon=gps[1] if gps else None,
                    orientation=md.get("orientation"), width=md.get("width"), height=md.get("height"),
                    camera_make=md.get("make"), camera_model=md.get("model"), category=cat,
                    takeout_json=to_db(str(tj)) if tj else None, takeout=json.dumps(take) if take else None,
                    current_path=to_db(str(p)), status="waiting", stage="found", last_seen_run=self.run_id,
                    updated_at=self.m.now())
                if old is None:
                    cols.update(source=source, rel_path=rel, first_seen_run=self.run_id, catalogued_at=self.m.now())
                    names = ", ".join(cols)
                    self.m.db.execute(f"INSERT INTO files ({names}) VALUES ({', '.join('?' * len(cols))})",
                                      tuple(cols.values()))
                else:
                    self.update(old["id"], **cols)
                self.stats["found"] += 1
                prog.update(1, st.st_size)
                if n % 200 == 0:
                    self.m.db.commit()
        self.m.db.commit()

    # -- 2. batches ---------------------------------------------------------------------
    def assign_batches(self) -> None:
        rows = self.m.db.execute("SELECT id, date_taken, undated FROM files WHERE status='waiting' AND batch IS NULL "
                                 "AND source LIKE 'sorted/%' ORDER BY date_taken, id").fetchall()
        if not rows:
            return
        groups: dict[str | None, list] = defaultdict(list)
        for r in rows:
            month = r["date_taken"][:7].replace(":", "-") if r["date_taken"] else None
            groups[month].append(r["id"])
        size = self.p2.batch_size
        made = 0
        for month, idlist in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0] or "")):
            chunks = [idlist[i:i + size] for i in range(0, len(idlist), size)]
            for chunk in chunks:
                name = self._next_batch_name(month, len(chunks))
                self.m.db.execute("INSERT INTO batches (name, month, status, files, created_at, updated_at) "
                                  "VALUES (?, ?, 'planned', ?, ?, ?)", (name, month, len(chunk), self.m.now(),
                                                                       self.m.now()))
                self.m.db.executemany("UPDATE files SET batch=? WHERE id=?", [(name, i) for i in chunk])
                made += 1
        self.m.db.commit()
        say(f"New batches: {made:,} ({len(rows):,} photos)")

    def _next_batch_name(self, month: str | None, parts: int) -> str:
        if month is None:
            n = self.m.db.execute("SELECT COUNT(*) FROM batches WHERE month IS NULL").fetchone()[0]
            return f"undated-{n + 1:03d}"
        existing = [r[0] for r in self.m.db.execute("SELECT name FROM batches WHERE month=?", (month,))]
        if not existing and parts == 1:
            return month
        return f"{month}-{len(existing) + 1}"

    def pick(self, name: str | None) -> str | None:
        if name:
            r = self.m.db.execute("SELECT status FROM batches WHERE name=?", (name,)).fetchone()
            if r is None:
                raise Phase2Error(f"No batch named '{name}'")
            if r[0] == "processed":
                raise Phase2Error(f"Batch '{name}' is already processed")
            return name
        r = self.m.db.execute("SELECT name FROM batches WHERE status='processing' ORDER BY name LIMIT 1").fetchone()
        if r:
            return r[0]
        r = self.m.db.execute("SELECT name FROM batches WHERE status='planned' "
                              "ORDER BY month IS NULL, name LIMIT 1").fetchone()
        return r[0] if r else None

    # -- 3. stage 1: into processing/<batch> ----------------------------------------------
    def travelling(self, photo: Path, takeout_json: str | None) -> list[Path]:
        out = []
        if takeout_json and Path(takeout_json).is_file():
            out.append(Path(takeout_json))
        for cand in (photo.with_name(photo.name + ".xmp"), photo.with_suffix(".xmp"), photo.with_suffix(".XMP"),
                     photo.with_suffix(".aae"), photo.with_suffix(".AAE")):
            if cand.is_file() and cand not in out and cand != photo:
                out.append(cand)
        return out

    def stage1(self, batch: str) -> None:
        rows = self.m.db.execute("SELECT * FROM files WHERE batch=? AND status='waiting'", (batch,)).fetchall()
        self.m.db.execute("UPDATE batches SET status='processing', updated_at=? WHERE name=?", (self.m.now(), batch))
        self.m.db.commit()
        if not rows:
            return
        with Progress(f"Stage 1 {batch}", total=len(rows), total_bytes=sum(r["size"] or 0 for r in rows)) as prog:
            for r in rows:
                src = Path(r["current_path"])
                prog.set_extra(r["rel_path"])
                if not src.is_file():
                    self.update(r["id"], status="missing")
                    prog.update()
                    continue
                dest_dir = self.processing / batch / r["category"] / Path(r["rel_path"]).parent
                dest_dir.mkdir(parents=True, exist_ok=True)
                try:
                    tj_new = None
                    for side in self.travelling(src, r["takeout_json"]):
                        sdest = free_name(dest_dir / side.name)
                        transfer(str(side), sdest, None, self.tc.verify)
                        if r["takeout_json"] and Path(r["takeout_json"]) == side:
                            tj_new = to_db(str(sdest))
                    dest = free_name(dest_dir / src.name)
                    transfer(str(src), dest, None, self.tc.verify)
                except (MoveError, OSError) as exc:
                    self.update(r["id"], status="error", stage="stage1")
                    self.m.event(self.run_id, r["id"], STAGE, "move-failed", str(exc))
                    self.stats["errors"] += 1
                    prog.update()
                    continue
                self.add_copy(r["id"], str(src), "original")
                self.update(r["id"], current_path=to_db(str(dest)), status="processing", stage="moved",
                            takeout_json=tj_new or r["takeout_json"])
                self.m.event(self.run_id, r["id"], STAGE, "stage1", f"{src} -> {dest}")
                self.stats["stage1-moved"] += 1
                prog.update(1, r["size"] or 0)
        self.m.db.commit()

    # -- excluding ------------------------------------------------------------------------
    def exclude(self, r, reason: str, detail: str, keeper_id: str | None = None) -> None:
        src = Path(r["current_path"])
        pid = r["pipeline_id"] or ids.new_id()
        dest_dir = self.excluded / reason
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = free_name(dest_dir / f"{pid}_{strip_id(src.name)}")
        transfer(str(src), dest, None, self.tc.verify)
        self.update(r["id"], status="excluded", excluded_reason=f"{reason}: {detail}", current_path=to_db(str(dest)),
                    keeper_pipeline_id=keeper_id, pipeline_id=pid)
        self.m.event(self.run_id, r["id"], STAGE, f"excluded-{reason}", detail)
        self.stats[f"excluded:{reason}"] += 1
        new = not self.excluded_csv.exists() or self.excluded_csv.stat().st_size == 0
        self.excluded_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(self.excluded_csv, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=EXCLUDED_COLUMNS)
            if new:
                w.writeheader()
            w.writerow({"pipeline_id": pid, "original_path": self.original_path(r["id"]) or r["current_path"],
                        "excluded_path": str(dest), "reason": f"{reason} ({detail})",
                        "keeper_pipeline_id": keeper_id or "", "similarity": "", "width": r["width"] or "",
                        "height": r["height"] or "", "size": r["size"] or "", "timestamp": self.m.now()})

    # -- 4. stage 2a: ID check + fingerprint ------------------------------------------------
    def stage2a(self, batch: str) -> None:
        rows = self.m.db.execute("SELECT * FROM files WHERE batch=? AND status='processing' AND stage='moved'",
                                 (batch,)).fetchall()
        if not rows:
            return
        with Progress(f"Stage 2 ID + fingerprint {batch}", total=len(rows),
                      total_bytes=sum(r["size"] or 0 for r in rows)) as prog:
            for r in rows:
                prog.set_extra(os.path.basename(r["current_path"]))
                try:
                    self._fingerprint(r)
                except (MoveError, OSError) as exc:
                    self.update(r["id"], status="error")
                    self.m.event(self.run_id, r["id"], STAGE, "stage2-error", str(exc))
                    self.stats["errors"] += 1
                prog.update(1, r["size"] or 0)
                self.m.db.commit()

    def _fingerprint(self, r) -> None:
        path = Path(r["current_path"])
        md = meta.read_meta(self.tools.r, path)
        sha = meta.sha256_file(path)
        phash, err = meta.pixel_hash(path)
        if err:  # full decode check failed
            self.update(r["id"], sha256=sha)
            self.exclude(self.row(r["id"]), "10-corrupt", f"doesn't decode: {err}")
            return
        found = ids.id_from_name(path.name)
        id_source = "name" if found else None
        if not found and ids.is_id(md.get("identifier")):
            found, id_source = md["identifier"], "metadata"
        pid = found
        if found:
            other = self.m.db.execute("SELECT id, sha256, sha256_written, status FROM files WHERE pipeline_id=? "
                                      "AND id != ?", (found, r["id"])).fetchone()
            if other is not None:  # re-entry: this ID already belongs to a photo
                pid = ids.new_id()
                self.update(r["id"], pipeline_id=pid, id_source="new", sha256=sha, pixel_hash=phash)
                if sha in (other["sha256"], other["sha256_written"]):
                    self.exclude(self.row(r["id"]), "01-exact-dup", "re-entry", keeper_id=found)
                    self.add_copy(other["id"], self.original_path(r["id"]) or str(path), "duplicate", r["id"])
                    return
                self.update(r["id"], status="held", keeper_pipeline_id=found, stage="fingerprinted")
                self.flag(r["id"], "reentry-changed", f"carries the ID of {found} but the file has changed - held "
                                                      "for the similar-duplicates check (stage 6)")
                return
        else:
            pid, id_source = ids.new_id(), "new"
        # wrong extension: corrected now (a PNG called .jpg becomes .png)
        fixed = meta.true_extension(path)
        if fixed:
            new = free_name(path.with_name(f"{path.stem}.{fixed}"))
            os.rename(path, new)
            self.m.event(self.run_id, r["id"], STAGE, "extension-corrected", f"{path.name} -> {new.name}")
            self.stats["extension-corrected"] += 1
            path = new
        cols = dict(pipeline_id=pid, id_source=id_source, sha256=sha, pixel_hash=phash, stage="fingerprinted",
                    current_path=to_db(str(path)), ext=extension(path.name))
        width, height = md.get("width"), md.get("height")
        if phash:
            try:
                from PIL import Image

                with Image.open(path) as im:
                    width, height = im.size
            except Exception:  # noqa: BLE001
                pass
        cols.update(width=width, height=height)
        self.update(r["id"], **cols)
        o = md.get("orientation")
        if o is not None and o not in range(1, 9):
            self.flag(r["id"], "orientation", f"orientation tag {o!r} isn't a valid value (1-8); whether the photo "
                                              "looks the right way up is checked with A-Eye (stage 9)")
        try:
            long_edge = max(int(width or 0), int(height or 0))
        except (TypeError, ValueError):
            long_edge = 0
        if long_edge and long_edge < self.p2.low_res_pixels:
            self.flag(r["id"], "low-res", f"{width}x{height} (long edge under {self.p2.low_res_pixels}px); whether a "
                                          "bigger copy exists is checked by the similar-duplicates stage (6)")

    # -- 5. stage 4: exact duplicates -------------------------------------------------------
    def score(self, r, md: dict) -> tuple:
        try:
            pixels = int(r["width"] or 0) * int(r["height"] or 0)
        except (TypeError, ValueError):
            pixels = 0
        return (pixels, FORMAT_RANK.get((r["ext"] or "").lower(), 1), md.get("tag_count", 0), r["size"] or 0,
                1 if md.get("regions") else 0, -r["id"])

    def stage4(self, batch: str) -> None:
        rows = self.m.db.execute("SELECT * FROM files WHERE batch=? AND status='processing' AND stage='fingerprinted'",
                                 (batch,)).fetchall()
        if not rows:
            return
        with Progress(f"Stage 4 exact duplicates {batch}", total=len(rows)) as prog:
            by_sha = defaultdict(list)
            for r in rows:
                by_sha[r["sha256"]].append(r)
            for sha, group in by_sha.items():
                earlier = self.m.db.execute(
                    "SELECT * FROM files WHERE (sha256=? OR sha256_written=?) AND pipeline_id IS NOT NULL "
                    "AND (batch IS NULL OR batch != ?) "
                    "AND status NOT IN ('excluded', 'missing', 'waiting', 'error', 'held') ORDER BY id LIMIT 1",
                    (sha, sha, batch)).fetchone()
                if earlier is None and len(group) == 1:
                    continue
                if earlier is not None:
                    keeper, losers = earlier, group
                else:
                    scored = sorted(group, key=lambda x: self.score(x, meta.read_meta(self.tools.r, x["current_path"])),
                                    reverse=True)
                    keeper, losers = scored[0], scored[1:]
                for lose in losers:
                    self._takeout_to_keeper(keeper, lose)
                    try:
                        self.exclude(self.row(lose["id"]), "01-exact-dup", "byte-identical",
                                     keeper_id=keeper["pipeline_id"])
                        self.add_copy(keeper["id"], self.original_path(lose["id"]) or lose["current_path"],
                                      "duplicate", lose["id"])
                    except (MoveError, OSError) as exc:
                        self.m.event(self.run_id, lose["id"], STAGE, "exclude-failed", str(exc))
                        self.stats["errors"] += 1
                    prog.update()
                self.m.db.commit()
            # pixel-identical, different bytes
            left = self.m.db.execute("SELECT * FROM files WHERE batch=? AND status='processing' AND "
                                     "stage='fingerprinted' AND pixel_hash IS NOT NULL", (batch,)).fetchall()
            by_px = defaultdict(list)
            for r in left:
                by_px[r["pixel_hash"]].append(r)
            for px, group in by_px.items():
                earlier = self.m.db.execute(
                    "SELECT * FROM files WHERE pixel_hash=? AND pipeline_id IS NOT NULL AND (batch IS NULL OR batch != ?) "
                    "AND status NOT IN ('excluded', 'missing', 'waiting', 'error', 'held') ORDER BY id LIMIT 1",
                    (px, batch)).fetchone()
                if earlier is None and len(group) == 1:
                    continue
                mds = {r["id"]: meta.read_meta(self.tools.r, r["current_path"]) for r in group}
                if earlier is not None:
                    keeper, losers = earlier, group
                    mds[earlier["id"]] = meta.read_meta(self.tools.r, earlier["current_path"]) \
                        if earlier["current_path"] and Path(earlier["current_path"]).is_file() else {}
                else:
                    scored = sorted(group, key=lambda x: self.score(x, mds[x["id"]]), reverse=True)
                    keeper, losers = scored[0], scored[1:]
                self._merge_into(keeper, mds[keeper["id"]], [(lo, mds[lo["id"]]) for lo in losers])
                for lose in losers:
                    self._takeout_to_keeper(keeper, lose)
                    try:
                        self.exclude(self.row(lose["id"]), "02-pixel-dup", "same pixels, different metadata",
                                     keeper_id=keeper["pipeline_id"])
                        self.add_copy(keeper["id"], self.original_path(lose["id"]) or lose["current_path"],
                                      "duplicate", lose["id"])
                    except (MoveError, OSError) as exc:
                        self.m.event(self.run_id, lose["id"], STAGE, "exclude-failed", str(exc))
                        self.stats["errors"] += 1
                    prog.update()
                self.m.db.commit()
            self.m.db.execute("UPDATE files SET stage='deduped' WHERE batch=? AND status='processing' AND "
                              "stage='fingerprinted'", (batch,))
            self.m.db.commit()

    def _takeout_to_keeper(self, keeper, lose) -> None:
        if lose["takeout"] and not self.row(keeper["id"])["takeout"]:
            self.update(keeper["id"], takeout=lose["takeout"], takeout_json=lose["takeout_json"])

    def _merge_into(self, keeper, kmd: dict, losers: list) -> None:
        """Merge the losers' metadata into the keeper: what doesn't conflict is written now."""
        k = self.row(keeper["id"])
        tags: dict = {}
        captions = list(kmd.get("captions") or [])
        keywords = list(kmd.get("keywords") or [])
        rating = kmd.get("rating")
        regions = kmd.get("regions")
        new_regions = None
        date_cands = [{"key": "exif-kept", "source": "kept copy (EXIF)", "date": kmd["date"], "offset": kmd.get("offset"),
                       "file_id": k["id"]}] if kmd.get("date") else []
        gps_cands = [{"key": "gps-kept", "source": "kept copy", "lat": kmd["gps"][0], "lon": kmd["gps"][1],
                      "file_id": k["id"]}] if kmd.get("gps") else []
        for n, (lo, md) in enumerate(losers, 1):
            for c in md.get("captions") or []:
                if c not in captions:
                    captions.append(c)
            for kw in md.get("keywords") or []:
                if kw not in keywords:
                    keywords.append(kw)
            if md.get("rating") is not None and (rating is None or md["rating"] > rating):
                rating = md["rating"]
            merged = meta.merge_regions(new_regions or regions, md.get("regions"))
            if merged:
                new_regions = merged
            if md.get("date"):
                c = {"key": f"exif-copy{n}", "source": f"copy {n} (EXIF)", "date": md["date"], "offset": md.get("offset"),
                     "file_id": lo["id"]}
                if not any(cf.dates_agree(c, d) for d in date_cands):
                    date_cands.append(c)
            if md.get("gps"):
                c = {"key": f"gps-copy{n}", "source": f"copy {n}", "lat": md["gps"][0], "lon": md["gps"][1],
                     "file_id": lo["id"]}
                if not any(cf.gps_agree((c["lat"], c["lon"]), (d["lat"], d["lon"])) for d in gps_cands):
                    gps_cands.append(c)
        raw = meta.is_raw(k["current_path"] or "")
        if captions != list(kmd.get("captions") or []):
            tags["XMP-dc:Description"] = " | ".join(captions)
        if keywords != list(kmd.get("keywords") or []):
            tags["XMP-dc:Subject"] = keywords
        if rating is not None and rating != kmd.get("rating"):
            tags["XMP:Rating"] = rating
        if len(date_cands) == 1 and not kmd.get("date"):
            c = date_cands[0]
            tags.update(meta.date_tags(c["date"], c.get("offset"), raw))
            self.update(k["id"], date_taken=c["date"], date_source="duplicate", undated=0)
        if len(gps_cands) == 1 and not kmd.get("gps"):
            c = gps_cands[0]
            tags.update(meta.gps_tags(c["lat"], c["lon"], raw))
            self.update(k["id"], gps_lat=c["lat"], gps_lon=c["lon"])
        if (tags or new_regions) and k["current_path"] and Path(k["current_path"]).is_file():
            ok, msg = meta.safe_write(self.tools, Path(k["current_path"]), tags, regions_json=new_regions)
            if ok:
                self.update(k["id"], sha256_written=meta.sha256_file(k["current_path"]))
            self.m.event(self.run_id, k["id"], STAGE, "merged" if ok else "merge-failed",
                         json.dumps({"tags": sorted(tags), "faces": bool(new_regions), "result": msg}))
            self.stats["merged" if ok else "merge-failed"] += 1
        if len(date_cands) > 1:
            sig, rec = cf.date_conflict(date_cands)
            cf.add(self.m, k["id"], "date", sig, date_cands, rec)
            self.stats["conflicts:date"] += 1
        if len(gps_cands) > 1:
            ids_ = {c["file_id"] for c in gps_cands}
            sig, rec = cf.gps_conflict(self.m.db, gps_cands, k["camera_model"], k["date_taken"], ids_)
            cf.add(self.m, k["id"], "gps", sig, gps_cands, rec)
            self.stats["conflicts:gps"] += 1
        if len(date_cands) > 1 or len(gps_cands) > 1:
            if k["status"] == "processed":  # an earlier keeper, already through stage 2
                self.update(k["id"], status="pending-conflict")

    # -- 6. stage 2b: write the ID ------------------------------------------------------------
    def stage2b(self, batch: str) -> None:
        rows = self.m.db.execute("SELECT * FROM files WHERE batch=? AND status='processing' AND stage='deduped'",
                                 (batch,)).fetchall()
        if not rows:
            return
        with Progress(f"Stage 2 write ID {batch}", total=len(rows), total_bytes=sum(r["size"] or 0 for r in rows)) as prog:
            for r in rows:
                prog.set_extra(os.path.basename(r["current_path"]))
                try:
                    self._write_id(r)
                except (MoveError, OSError) as exc:
                    self.update(r["id"], status="error")
                    self.m.event(self.run_id, r["id"], STAGE, "write-error", str(exc))
                    self.stats["errors"] += 1
                prog.update(1, r["size"] or 0)
                self.m.db.commit()

    def _write_id(self, r) -> None:
        path = Path(r["current_path"])
        pid = r["pipeline_id"]
        raw = meta.is_raw(path)
        md = meta.read_meta(self.tools.r, path)
        tags: dict = {"XMP-dc:Identifier": pid}
        take = json.loads(r["takeout"]) if r["takeout"] else None
        updates: dict = {}
        if take:
            tdate = take.get("date")
            if tdate:
                tcand = {"key": "takeout", "source": "Google Takeout .json (UTC)", "date": tdate, "offset": "+00:00",
                         "file_id": r["id"]}
                if not md.get("date"):
                    tags.update(meta.date_tags(tdate, "+00:00", raw))
                    updates.update(date_taken=tdate, date_source="takeout", undated=0)
                else:
                    fcand = {"key": "exif", "source": "the photo (EXIF)", "date": md["date"], "offset": md.get("offset"),
                             "file_id": r["id"]}
                    if not cf.dates_agree(fcand, tcand):
                        sig, rec = cf.date_conflict([fcand, tcand])
                        cf.add(self.m, r["id"], "date", sig, [fcand, tcand], rec)
                        self.stats["conflicts:date"] += 1
            tgps = take.get("gps")
            if tgps:
                if not md.get("gps"):
                    tags.update(meta.gps_tags(tgps[0], tgps[1], raw))
                    updates.update(gps_lat=tgps[0], gps_lon=tgps[1])
                elif not cf.gps_agree(tuple(md["gps"]), tuple(tgps)):
                    cands = [{"key": "gps-photo", "source": "the photo", "lat": md["gps"][0], "lon": md["gps"][1],
                              "file_id": r["id"]},
                             {"key": "takeout", "source": "Google Takeout .json", "lat": tgps[0], "lon": tgps[1],
                              "file_id": r["id"]}]
                    sig, rec = cf.gps_conflict(self.m.db, cands, r["camera_model"], md.get("date") or r["date_taken"],
                                               {r["id"]})
                    cf.add(self.m, r["id"], "gps", sig, cands, rec)
                    self.stats["conflicts:gps"] += 1
        ok, msg = meta.safe_write(self.tools, path, tags, ident=pid)
        if not ok:
            self.update(r["id"], status="error", **updates)
            self.m.event(self.run_id, r["id"], STAGE, "write-failed", msg)
            self.stats["write-failed"] += 1
            return
        self.stats["ids-written"] += 1
        # <ID>_<name>; a RAW's sidecar is renamed with it
        if ids.id_from_name(path.name) != pid:
            new = free_name(path.with_name(f"{pid}_{strip_id(path.name)}"))
            os.rename(path, new)
            if raw and meta.sidecar_for(path).is_file():
                os.rename(meta.sidecar_for(path), meta.sidecar_for(new))
            path = new
        pending = self.m.db.execute("SELECT COUNT(*) FROM conflicts WHERE file_id=? AND status='pending'",
                                    (r["id"],)).fetchone()[0]
        self.update(r["id"], current_path=to_db(str(path)), stage="written", sha256_written=meta.sha256_file(path),
                    status="pending-conflict" if pending else "processed", **updates)
        self.m.event(self.run_id, r["id"], STAGE, "id-written", msg)

    # -- 7. checks ------------------------------------------------------------------------
    def burst_check(self, batch: str) -> None:
        rows = self.m.db.execute("SELECT id, camera_model, date_taken FROM files WHERE batch=? AND "
                                 "status IN ('processed', 'pending-conflict') AND date_taken IS NOT NULL "
                                 "ORDER BY camera_model, date_taken, id", (batch,)).fetchall()
        run: list = []

        def flush():
            if len(run) >= self.p2.burst_min:
                start = run[0]["date_taken"]
                for x in run:
                    self.flag(x["id"], "burst", f"one of {len(run)} photos from {run[0]['camera_model'] or 'one camera'} "
                                                f"starting {start}; the best one is picked by A-Eye's quality score "
                                                "(stage 9) - nothing is removed now")

        prev = None
        for r in rows:
            t = meta.to_utc(r["date_taken"], None)
            if prev is not None and r["camera_model"] == prev[0] and t is not None and prev[1] is not None \
                    and (t - prev[1]).total_seconds() <= self.p2.burst_gap_seconds:
                run.append(r)
            else:
                flush()
                run = [r]
            prev = (r["camera_model"], t)
        flush()
        self.m.db.commit()

    def finish_batch(self, batch: str) -> bool:
        left = self.m.db.execute("SELECT COUNT(*) FROM files WHERE batch=? AND status='processing'",
                                 (batch,)).fetchone()[0]
        if left == 0:
            self.m.db.execute("UPDATE batches SET status='processed', updated_at=? WHERE name=?",
                              (self.m.now(), batch))
            self.m.db.commit()
        return left == 0

    def report(self, batch: str | None, status: str) -> Path:
        out = self.cfg.reports_dir / "phase2"
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(self.cfg.tz).strftime("%Y%m%d-%H%M%S")
        path = out / f"phase2-{batch or 'none'}-{stamp}.md"
        lines = [f"# Phase 2 run: batch {batch or '(none)'}", "", f"Status: {status}", ""]
        lines += ["| What | Count |", "|---|---:|"] + [f"| {k} | {v:,} |" for k, v in sorted(self.stats.items())]
        if batch:
            lines += ["", "## Files in this batch by status", "", "| Status | Files |", "|---|---:|"]
            for st, n in self.m.db.execute("SELECT status, COUNT(*) FROM files WHERE batch=? GROUP BY status", (batch,)):
                lines.append(f"| {st} | {n:,} |")
            flags = self.m.db.execute("SELECT fl.flag, COUNT(*) FROM flags fl JOIN files f ON f.id=fl.file_id "
                                      "WHERE f.batch=? GROUP BY fl.flag", (batch,)).fetchall()
            if flags:
                lines += ["", "## Flags", "", "| Flag | Files |", "|---|---:|"] + [f"| {a} | {b:,} |" for a, b in flags]
        lines += ["", "Waiting for later stages: the orientation visual check and the best photo of a burst (A-Eye, "
                  "stage 9), whether a low-resolution photo has a bigger copy (stage 6), image-based GPS estimates "
                  "(stage 8)."]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    # -- the run ----------------------------------------------------------------------------
    def run(self, batch: str | None = None, find_only: bool = False) -> dict:
        if not self.p2.enabled:
            raise Phase2Error("Phase 2 is switched off (phase2.enabled: false). It stays off until the PhotoPrism "
                              "export exists; switch it on in Settings > Phase 2 when you are ready.")
        self.run_id = self.m.start_run("phase2", batch)
        status, chosen = "finished", None
        try:
            self.find()
            self.assign_batches()
            if not find_only:
                chosen = self.pick(batch)
                if chosen is None:
                    say("No batch waiting.")
                else:
                    say(f"Batch: {chosen}")
                    self.stage1(chosen)
                    self.stage2a(chosen)
                    self.stage4(chosen)
                    self.stage2b(chosen)
                    self.burst_check(chosen)
                    done = self.finish_batch(chosen)
                    say(f"Batch {chosen} {'processed' if done else 'not finished (see errors)'}.")
        except KeyboardInterrupt:
            status = "interrupted"
            say("Stopped. Everything done so far is kept; the next run carries on with the same batch.")
        finally:
            self.m.db.commit()
            path = self.report(chosen, status)
            self.m.finish_run(self.run_id, status, dict(self.stats))
            say(f"Report: {path}")
        return {"status": status, "batch": chosen, "stats": dict(self.stats), "report": str(path)}


def run_phase2(cfg, batch: str | None = None, find_only: bool = False) -> dict:
    p = Phase2(cfg)
    try:
        return p.run(batch, find_only)
    finally:
        p.close()


def status_text(cfg) -> str:
    m = Manifest(cfg.manifest, cfg.tz)
    try:
        lines = [f"Phase 2 {'ON' if cfg.phase2 and cfg.phase2.enabled else 'OFF (phase2.enabled: false)'}"]
        rows = m.db.execute("SELECT status, COUNT(*), COALESCE(SUM(files), 0) FROM batches GROUP BY status").fetchall()
        lines.append("Batches: " + (", ".join(f"{n:,} {s} ({f:,} photos)" for s, n, f in rows) or "none yet"))
        rows = m.db.execute("SELECT status, COUNT(*) FROM files WHERE source LIKE 'sorted/%' GROUP BY status").fetchall()
        lines.append("Photos: " + (", ".join(f"{n:,} {s}" for s, n in rows) or "none yet"))
        rows = m.db.execute("SELECT field, COUNT(*) FROM conflicts WHERE status='pending' GROUP BY field").fetchall()
        lines.append("Conflicts waiting: " + (", ".join(f"{n:,} {f}" for f, n in rows) or "none"))
        return "\n".join(lines)
    finally:
        m.close()
