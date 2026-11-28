"""PhotoPrism export: copy PhotoPrism's originals into the pipeline with your PhotoPrism work
written into the copies.

PhotoPrism (its database, read through its API with the admin login from Settings > External
tools) is the source of truth. For each photo ImagePipe copies its original files - never
changing them - and writes into the copy of the image:

- title and caption, only where you set them (source manual/batch/admin); AI and file-name
  guesses are ignored
- keywords you set
- date/time (with the photo's time zone) and location, only where you changed them; PhotoPrism's
  estimates are ignored, so ImagePipe's own checks deal with those photos
- favourite: rating 5 and the tag "Favourite"
- your albums: the album name as a keyword, plus "Albums|<name>" (Lightroom hierarchy) and
  "Albums/<name>" (digiKam tag tree)
- named faces: face areas (MWG regions, as digiKam reads them), merged with any already there
- archived in PhotoPrism: the tag "Review"; private: the tag "Private"

Photos, RAW files and the files that belong to them (Live Photo clips, .xmp/.json/.aae
sidecars) go to <filestack>/imagepipe/intake/photoprism/, keeping their folders; phase 2 takes
that folder first. Anything else in the originals goes to <filestack>/triage/intake/photoprism/
for triage. PhotoPrism's own .yml sidecars are not copied.

A copy is written next to its destination first, checked against PhotoPrism's SHA-1 of the
original, written into, checked again (it must still open), then renamed into place. A run
can be stopped and carried on: each finished file is recorded in the manifest.
"""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import json
import logging
import os
import shutil
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image

from imagepipe.config import ConfigError
from imagepipe.detect import IMAGE_EXT, RAW_EXT, SIDECAR_EXT, extension
from imagepipe.manifest import Manifest
from imagepipe.phase2 import meta
from imagepipe.progress import Progress

log = logging.getLogger("imagepipe")
YOURS = {"manual", "batch", "admin"}       # PhotoPrism sources that mean "you set this"
PAGE = 500
TIMEOUT = 60
COMPANION_EXT = SIDECAR_EXT | {"json", "mov", "mp4"}

PP_SCHEMA = """
CREATE TABLE IF NOT EXISTS pp_export (
    file_name   TEXT PRIMARY KEY,   -- PhotoPrism's name for the file (relative to the originals)
    photo_uid   TEXT,
    dest        TEXT,
    status      TEXT NOT NULL,      -- done | error
    written     TEXT,               -- JSON: what was written into the copy
    note        TEXT,
    exported_at TEXT
);
"""


# -- settings ------------------------------------------------------------------------------
@dataclass
class PPExportConfig:
    originals: str = "/mnt/appdata/imagelibrary"      # PhotoPrism's originals, as ImagePipe sees them
    exclude: list = field(default_factory=lambda: ["imagepipe", "library", ".ppignore", "*.yml"])
    faces: str = "all"                                # all named faces, or only ones you named ("manual")
    favourite_tag: str = "Favourite"
    review_tag: str = "Review"                        # archived in PhotoPrism
    private_tag: str = "Private"
    album_prefix: str = "Albums"


def parse_ppexport(data) -> PPExportConfig:
    if data is None:
        return PPExportConfig()
    if not isinstance(data, dict):
        raise ConfigError("'photoprism_export' must be a mapping")
    known = set(PPExportConfig.__dataclass_fields__)
    unknown = {str(k) for k in data} - known
    if unknown:
        raise ConfigError(f"Unknown key(s) in 'photoprism_export': {', '.join(sorted(unknown))}")
    d = PPExportConfig()
    cfg = PPExportConfig(
        originals=str(data.get("originals") or d.originals),
        exclude=[str(x) for x in (data.get("exclude") if data.get("exclude") is not None else d.exclude)],
        faces=str(data.get("faces") or d.faces),
        favourite_tag=str(data.get("favourite_tag") or d.favourite_tag),
        review_tag=str(data.get("review_tag") or d.review_tag),
        private_tag=str(data.get("private_tag") or d.private_tag),
        album_prefix=str(data.get("album_prefix") or d.album_prefix),
    )
    if cfg.faces not in ("all", "manual"):
        raise ConfigError("photoprism_export.faces must be 'all' or 'manual'")
    return cfg


def say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
    log.info(msg)


# -- PhotoPrism API ---------------------------------------------------------------------------
class PhotoPrismError(Exception):
    pass


class PhotoPrism:
    def __init__(self, url: str, username: str, password: str):
        if not url:
            raise PhotoPrismError("No PhotoPrism address set (Settings > External tools > PhotoPrism)")
        self.url = url.rstrip("/")
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE
        self.token = None
        self.user, self.password = username, password

    def _call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Auth-Token"] = self.token
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self.ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise PhotoPrismError(f"{method} {path}: HTTP {exc.code} {exc.read(300)!r}") from exc
        except urllib.error.URLError as exc:
            raise PhotoPrismError(f"can't reach PhotoPrism at {self.url}: {exc.reason}") from exc
        return json.loads(raw) if raw else None

    def login(self) -> None:
        if not (self.user and self.password):
            raise PhotoPrismError("No PhotoPrism username/password set (Settings > External tools > PhotoPrism)")
        res = self._call("POST", "/api/v1/session", {"username": self.user, "password": self.password}) or {}
        self.token = res.get("access_token") or res.get("id")
        if not self.token:
            raise PhotoPrismError("PhotoPrism didn't return a session token")

    def logout(self) -> None:
        if self.token:
            try:
                self._call("DELETE", "/api/v1/session")
            except PhotoPrismError:
                pass

    def photo_uids(self, archived: bool) -> list[str]:
        out, offset = [], 0
        while True:
            q = {"count": PAGE, "offset": offset, "order": "added", "merged": "false", "quality": 0}
            if archived:
                q["archived"] = "true"
            page = self._call("GET", "/api/v1/photos?" + urllib.parse.urlencode(q)) or []
            out += [p["UID"] for p in page if p.get("UID")]
            if len(page) < PAGE:
                break
            offset += PAGE
        return list(dict.fromkeys(out))

    def photo(self, uid: str) -> dict:
        return self._call("GET", f"/api/v1/photos/{uid}") or {}


# -- what goes into a copy ----------------------------------------------------------------------
def yours(src) -> bool:
    return str(src or "").lower() in YOURS


def offset_for(local: datetime, tz: str | None) -> str | None:
    if not tz or tz == "Local":
        return None
    try:
        off = local.replace(tzinfo=ZoneInfo(tz)).utcoffset()
    except Exception:  # noqa: BLE001 - unknown zone names
        return None
    if off is None:
        return None
    return meta.fmt_offset(int(off.total_seconds() // 60))


def plan_photo(p: dict, cfg: PPExportConfig, raw: bool = False) -> dict:
    """{tags, keywords, hier, faces, summary} to write into a photo's image copy."""
    tags: dict = {}
    keywords: list[str] = []
    hier: list[str] = []
    summary: list[str] = []
    if yours(p.get("TitleSrc")) and p.get("Title"):
        tags["XMP-dc:Title"] = p["Title"]
        summary.append("title")
    caption = p.get("Caption") if "Caption" in p else p.get("Description")
    caption_src = p.get("CaptionSrc") if "CaptionSrc" in p else p.get("DescriptionSrc")
    if yours(caption_src) and caption:
        tags["XMP-dc:Description"] = caption
        summary.append("caption")
    details = p.get("Details") or {}
    if yours(details.get("KeywordsSrc")) and details.get("Keywords"):
        kws = [k.strip() for k in str(details["Keywords"]).split(",") if k.strip()]
        keywords += kws
        summary.append(f"{len(kws)} keyword(s)")
    if yours(p.get("TakenSrc")) and p.get("TakenAtLocal"):
        try:
            local = datetime.strptime(str(p["TakenAtLocal"])[:19], "%Y-%m-%dT%H:%M:%S")  # noqa: DTZ007 - local time
            date = local.strftime("%Y:%m:%d %H:%M:%S")
            tags.update(meta.date_tags(date, offset_for(local, p.get("TimeZone")), raw=raw))
            summary.append("date")
        except ValueError:
            pass
    lat, lng = p.get("Lat") or 0, p.get("Lng") or 0
    if yours(p.get("PlaceSrc")) and (lat, lng) != (0, 0):
        tags.update(meta.gps_tags(float(lat), float(lng), raw=raw))
        summary.append("location")
    if p.get("Favorite"):
        tags["XMP:Rating"] = 5
        keywords.append(cfg.favourite_tag)
        summary.append("favourite")
    if p.get("DeletedAt"):
        keywords.append(cfg.review_tag)
        summary.append("archived -> " + cfg.review_tag)
    if p.get("Private"):
        keywords.append(cfg.private_tag)
        summary.append("private")
    albums = [a.get("Title") for a in p.get("Albums") or []
              if a.get("Type") == "album" and a.get("Title") and not a.get("DeletedAt")]
    for a in albums:
        keywords.append(a)
        hier.append(f"{cfg.album_prefix}|{a}")
    if albums:
        summary.append(f"{len(albums)} album(s)")
    return {"tags": tags, "keywords": list(dict.fromkeys(keywords)), "hier": list(dict.fromkeys(hier)),
            "summary": summary}


def faces_for(f: dict, cfg: PPExportConfig) -> list[dict]:
    out = []
    for m in f.get("Markers") or []:
        if (m.get("Type") or "face") != "face" or m.get("Invalid") or not m.get("Name"):
            continue
        if cfg.faces == "manual" and not yours(m.get("SubjSrc")):
            continue
        try:
            x, y, w, h = (float(m.get(k) or 0) for k in ("X", "Y", "W", "H"))
        except (TypeError, ValueError):
            continue
        if w <= 0 or h <= 0:
            continue
        out.append({"Name": m["Name"], "Type": "Face",
                    "Area": {"X": round(x + w / 2, 6), "Y": round(y + h / 2, 6), "W": round(w, 6), "H": round(h, 6),
                             "Unit": "normalized"}})
    return out


def merge_faces(existing: dict | None, new: list[dict], width, height) -> dict | None:
    if not new:
        return None
    region = {"AppliedToDimensions": {"W": int(width or 0), "H": int(height or 0), "Unit": "pixel"}, "RegionList": new}
    merged = meta.merge_regions(existing, region)
    return merged


def write_args(plan: dict) -> list[str]:
    args = meta.tag_args(plan["tags"])
    for k in plan["keywords"]:
        args += [f"-XMP-dc:Subject-={k}", f"-XMP-dc:Subject+={k}"]
    for h in plan["hier"]:
        args += [f"-XMP-lr:HierarchicalSubject-={h}", f"-XMP-lr:HierarchicalSubject+={h}"]
        d = h.replace("|", "/")
        args += [f"-XMP-digiKam:TagsList-={d}", f"-XMP-digiKam:TagsList+={d}"]
    return args


def sha1_of(path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# -- the run ---------------------------------------------------------------------------------------
class Exporter:
    def __init__(self, cfg, dry_run: bool = False, limit: int | None = None):
        if cfg.triage is None:
            raise ConfigError("The PhotoPrism export needs the triage section (it uses the filestack)")
        self.cfg, self.pc = cfg, cfg.photoprism_export or PPExportConfig()
        self.dry, self.limit = dry_run, limit
        self.originals = Path(self.pc.originals)
        self.intake = cfg.triage.imagepipe_dir / "intake" / "photoprism"
        self.triage_intake = cfg.triage.filestack / "triage" / "intake" / "photoprism"
        pp = cfg.tools.photoprism if cfg.tools else None
        self.api = PhotoPrism(pp.url if pp else "", pp.username if pp else "", pp.password if pp else "")
        self.m = Manifest(cfg.manifest, cfg.tz)
        self.m.db.executescript(PP_SCHEMA)
        self.tools = None if dry_run else meta.Tools()
        self.stats: dict[str, int] = {}

    def bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    def excluded(self, rel: str) -> bool:
        parts = Path(rel).parts
        for pat in self.pc.exclude:
            if fnmatch.fnmatch(parts[0], pat) or fnmatch.fnmatch(Path(rel).name, pat):
                return True
        return False

    def done(self, name: str) -> bool:
        r = self.m.db.execute("SELECT status FROM pp_export WHERE file_name=?", (name,)).fetchone()
        return r is not None and r[0] == "done"

    def record(self, name: str, uid: str | None, dest: str | None, status: str, written=None, note: str = "") -> None:
        if self.dry:
            return
        self.m.db.execute("INSERT OR REPLACE INTO pp_export (file_name, photo_uid, dest, status, written, note, "
                          "exported_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (name, uid, dest, status, json.dumps(written) if written is not None else None, note,
                           self.m.now()))
        self.m.db.commit()

    def copy_one(self, src: Path, dest: Path, sha1: str | None, plan: dict | None, faces: list | None, f: dict) -> str:
        """Copy src to dest, writing plan/faces into the copy first. Returns a short note."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".ipc-{uuid.uuid4().hex[:8]}-{dest.name}")
        try:
            shutil.copy2(src, tmp)
            got = sha1_of(tmp)
            if sha1 and got != sha1 and got != sha1_of(src):
                raise OSError("the copy doesn't match the original")
            note = "copied"
            args = write_args(plan) if plan else []
            regions = None
            if faces:
                existing = meta.read_meta(self.tools.r, tmp).get("regions")
                regions = merge_faces(existing, faces, f.get("Width"), f.get("Height"))
            if (args or regions) and meta.is_raw(dest):
                os.replace(tmp, dest)
                tmp = None
                self.write_sidecar(src, dest, args, regions)
                return "copied, sidecar written"
            if args or regions:
                if args:
                    out = meta._run(self.tools.w, ["-overwrite_original", "-P", *args, str(tmp)])
                    if "1 image files updated" not in out and "1 files updated" not in out:
                        raise OSError(f"exiftool: {out[:300]}")
                if regions:
                    ok, msg = meta._write_regions(self.tools.w, tmp, regions)
                    if not ok:
                        raise OSError(f"face areas: {msg}")
                if extension(dest.name) != "xmp":
                    with Image.open(tmp) as im:  # the written copy must still open
                        im.load()
                note = "copied, written"
            os.replace(tmp, dest)
            tmp = None
            return note
        finally:
            if tmp is not None and tmp.exists():
                tmp.unlink()

    def write_sidecar(self, src: Path, dest: Path, args: list[str], regions: dict | None) -> None:
        """RAW: write into its .xmp sidecar (the original's sidecar copied first if it has one)."""
        side = meta.sidecar_for(dest)
        orig_side = meta.sidecar_for(src)
        if not side.exists() and orig_side.is_file():
            shutil.copy2(orig_side, side)
        tmp = side.with_name(f".ipc-{uuid.uuid4().hex[:8]}-{side.name}")
        try:
            if side.exists():
                shutil.copy2(side, tmp)
                if args:
                    out = meta._run(self.tools.w, ["-overwrite_original", "-P", *args, str(tmp)])
                    if "1 image files updated" not in out and "1 files updated" not in out:
                        raise OSError(f"exiftool (sidecar): {out[:300]}")
            else:
                out = meta._run(self.tools.w, ["-P", *args, "-o", str(tmp), str(dest)])
                if not tmp.exists():
                    raise OSError(f"sidecar not created: {out[:300]}")
            if regions:
                ok, msg = meta._write_regions(self.tools.w, tmp, regions)
                if not ok:
                    raise OSError(f"face areas (sidecar): {msg}")
            os.replace(tmp, side)
            tmp = None
        finally:
            if tmp is not None and tmp.exists():
                tmp.unlink()

    def run(self) -> dict:
        if not self.originals.is_dir():
            raise ConfigError(f"PhotoPrism's originals aren't visible at {self.originals} - add the read-only mount")
        status = "finished"
        out_dir = self.cfg.reports_dir / f"ppexport-{datetime.now(self.cfg.tz):%Y%m%d-%H%M%S}"
        out_dir.mkdir(parents=True, exist_ok=True)
        report = out_dir / ("files-dry-run.csv" if self.dry else "files.csv")
        copied: set[str] = set()
        try:
            self.api.login()
            say("Listing PhotoPrism's photos...")
            uids = self.api.photo_uids(False)
            archived = self.api.photo_uids(True)
            uids += [u for u in archived if u not in set(uids)]
            say(f"Photos in PhotoPrism: {len(uids):,} (archived {len(archived):,})")
            if self.limit:
                uids = uids[:self.limit]
            with open(report, "w", newline="", encoding="utf-8") as fh, \
                    Progress("PhotoPrism export" + (" (dry run)" if self.dry else ""), total=len(uids),
                             unit="photos") as prog:
                w = csv.writer(fh)
                w.writerow(["photo", "file", "goes to", "outcome", "written"])
                for uid in uids:
                    p = self.api.photo(uid)
                    plan = plan_photo(p, self.pc)
                    files = [f for f in p.get("Files") or [] if (f.get("Root") or "/") == "/" and not f.get("Missing")]
                    for f in files:
                        name = f.get("Name") or ""
                        if not name:
                            continue
                        prog.set_extra(name)
                        copied.add(name)
                        src = self.originals / name
                        dest = self.intake / name
                        ext = extension(name)
                        is_image = (f.get("MediaType") == "image" or ext in IMAGE_EXT or ext in RAW_EXT) \
                            and not f.get("Sidecar") and ext not in COMPANION_EXT
                        fplan = (plan_photo(p, self.pc, raw=True) if ext in RAW_EXT else plan) if is_image else None
                        faces = faces_for(f, self.pc) if is_image else []
                        summary = (plan["summary"] + ([f"{len(faces)} face(s)"] if faces else [])) if is_image else []
                        if self.done(name):
                            self.bump("already-done")
                            continue
                        if not is_image and ext in SIDECAR_EXT and dest.exists():
                            self.record(name, uid, str(dest), "done", None, "already there (written with its RAW)")
                            self.bump("sidecar-with-raw")
                            continue
                        if not src.is_file():
                            w.writerow([uid, name, "", "missing in the originals", ""])
                            self.bump("missing")
                            continue
                        if self.dry:
                            w.writerow([uid, name, str(dest), "would copy", "; ".join(summary)])
                            self.bump("would-write" if summary else "would-copy")
                            if faces:
                                self.bump("faces", len(faces))
                            continue
                        try:
                            note = self.copy_one(src, dest, f.get("Hash"), fplan if summary else None, faces, f)
                            self.record(name, uid, str(dest), "done", summary, note)
                            self.bump("written" if summary else "copied")
                            if faces:
                                self.bump("faces", len(faces))
                            w.writerow([uid, name, str(dest), note, "; ".join(summary)])
                        except Exception as exc:  # noqa: BLE001 - one bad file doesn't stop the run
                            self.record(name, uid, str(dest), "error", None, f"{type(exc).__name__}: {exc}"[:300])
                            self.bump("errors")
                            say(f"  {name}: {exc}")
                            w.writerow([uid, name, str(dest), f"error: {exc}"[:300], ""])
                    fh.flush()
                    prog.update()
            if not self.limit:
                self.rest(copied, report.with_name(report.stem + "-other.csv"))
        except KeyboardInterrupt:
            status = "interrupted"
            say("Stopped. Files finished so far are kept; the next run carries on.")
        finally:
            self.api.logout()
            (out_dir / "summary.md").write_text(
                f"# PhotoPrism export{' - dry run, nothing copied' if self.dry else ''}\n\nStatus: {status}\n\n"
                "| What | Count |\n|---|---:|\n" + "".join(f"| {k} | {v:,} |\n" for k, v in sorted(self.stats.items())),
                encoding="utf-8")
            if self.tools:
                self.tools.close()
            self.m.close()
        say(f"Report: {report}")
        return {"status": status, "stats": dict(self.stats), "report": str(report)}

    def rest(self, copied: set[str], report: Path) -> None:
        """Files in the originals that PhotoPrism doesn't hold as photos: to triage."""
        todo = []
        for root, dirs, files in os.walk(self.originals):
            rel_root = os.path.relpath(root, self.originals)
            if rel_root != "." and self.excluded(rel_root):
                dirs[:] = []
                continue
            dirs.sort()
            for n in sorted(files):
                rel = n if rel_root == "." else f"{rel_root}/{n}"
                if rel in copied or self.excluded(rel) or n.startswith(".ipc-"):
                    continue
                todo.append(rel)
        say(f"Other files in the originals (for triage): {len(todo):,}")
        with open(report, "w", newline="", encoding="utf-8") as fh, \
                Progress("Other files to triage", total=len(todo), unit="files") as prog:
            w = csv.writer(fh)
            w.writerow(["file", "goes to", "outcome"])
            for rel in todo:
                prog.set_extra(rel)
                dest = self.triage_intake / rel
                if self.dry:
                    w.writerow([rel, str(dest), "would copy"])
                    self.bump("other-would-copy")
                elif self.done(rel):
                    self.bump("already-done")
                else:
                    try:
                        self.copy_one(self.originals / rel, dest, None, None, None, {})
                        self.record(rel, None, str(dest), "done", None, "to triage")
                        self.bump("other-to-triage")
                        w.writerow([rel, str(dest), "copied"])
                    except OSError as exc:
                        self.record(rel, None, str(dest), "error", None, str(exc)[:300])
                        self.bump("errors")
                        w.writerow([rel, str(dest), f"error: {exc}"[:300]])
                prog.update()


def run_export(cfg, dry_run: bool = False, limit: int | None = None) -> dict:
    return Exporter(cfg, dry_run, limit).run()
