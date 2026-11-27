"""Reading and writing photo metadata for phase 2.

- read_meta(): the tags phase 2 needs, through one long-running exiftool
- Google Takeout: find the .json beside a photo and read its date and GPS
- fingerprint(): SHA-256 of the file, SHA-256 of the decoded pixels (a full decode check)
- safe_write(): write tags into a copy, check the copy still opens, then swap it in.
  RAW files are never written: their tags go into a sidecar <name>.<ext>.xmp (digiKam's
  default sidecar name).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from imagepipe import metadata as _md  # noqa: F401 - registers the HEIF opener
from imagepipe.detect import HEAD_BYTES, RAW_EXT, detect, extension
from imagepipe.exiftool import ExifTool

CHUNK = 1 << 20
READ_TAGS = (
    "-n", "-DateTimeOriginal", "-SubSecTimeOriginal", "-OffsetTimeOriginal", "-CreateDate",
    "-GPSLatitude", "-GPSLongitude", "-Orientation", "-ImageWidth", "-ImageHeight", "-Make", "-Model",
    "-XMP-dc:Identifier", "-XMP-dc:Description", "-EXIF:ImageDescription", "-IPTC:Caption-Abstract",
    "-XMP-dc:Subject", "-IPTC:Keywords", "-XMP:Rating", "-XMP-dc:Title", "-XMP-mwg-rs:RegionInfo",
    "-struct", "-FileType",
)
MIME_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp",
            "image/heic": "heic", "image/avif": "avif", "image/tiff": "tif", "image/bmp": "bmp",
            "image/jxl": "jxl"}
# What PhotoPrism can index (images and RAW; RAW through its converter)
PHOTOPRISM_EXT = {"jpg", "jpeg", "jpe", "png", "gif", "webp", "heic", "heif", "hif", "avif", "tif", "tiff",
                  "bmp", "jxl"} | RAW_EXT
_EXIF_DATE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def is_raw(path: str | Path) -> bool:
    return extension(str(path)) in RAW_EXT


def sidecar_for(path: str | Path) -> Path:
    return Path(str(path) + ".xmp")


# -- dates ----------------------------------------------------------------------------
def parse_exif_date(value) -> str | None:
    """'YYYY:MM:DD HH:MM:SS' (seconds kept, sub-seconds and offset dropped) or None."""
    if not value:
        return None
    m = _EXIF_DATE.match(str(value).strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1826 or not 1 <= mo <= 12 or not 1 <= d <= 31:
        return None
    try:
        datetime(y, mo, d, int(m.group(4)), int(m.group(5)), int(m.group(6)))
    except ValueError:
        return None
    return m.group(0).replace("T", " ")


def parse_offset(value) -> str | None:
    m = re.match(r"^\s*([+-])(\d{2}):?(\d{2})\s*$", str(value or ""))
    return f"{m.group(1)}{m.group(2)}:{m.group(3)}" if m else None


def to_utc(date: str, offset: str | None) -> datetime | None:
    """A datetime in UTC when the offset is known; a naive datetime otherwise."""
    try:
        dt = datetime.strptime(date, "%Y:%m:%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    if offset:
        sign = 1 if offset[0] == "+" else -1
        hh, mm = int(offset[1:3]), int(offset[4:6])
        dt = (dt - sign * timedelta(hours=hh, minutes=mm)).replace(tzinfo=timezone.utc)
    return dt


def fmt_offset(minutes: int) -> str:
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"


# -- reading ----------------------------------------------------------------------------
def _list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()] if "," in str(value) else [str(value)]


def read_meta(et: ExifTool, path: str | Path) -> dict:
    """The tags phase 2 uses, cleaned up. Unreadable: {}."""
    raw = et.read(str(path), READ_TAGS)
    if not raw:
        return {}
    date = parse_exif_date(raw.get("DateTimeOriginal")) or None
    lat, lon = raw.get("GPSLatitude"), raw.get("GPSLongitude")
    try:
        gps = (float(lat), float(lon)) if lat not in (None, "") and lon not in (None, "") else None
    except (TypeError, ValueError):
        gps = None
    if gps == (0.0, 0.0):
        gps = None
    captions = []
    for k in ("Description", "ImageDescription", "Caption-Abstract"):
        v = str(raw.get(k) or "").strip()
        if v and v not in captions:
            captions.append(v)
    keywords = []
    for v in _list(raw.get("Subject")) + _list(raw.get("Keywords")):
        if v not in keywords:
            keywords.append(v)
    try:
        rating = int(float(raw.get("Rating"))) if raw.get("Rating") not in (None, "") else None
    except (TypeError, ValueError):
        rating = None
    try:
        orientation = int(raw.get("Orientation")) if raw.get("Orientation") not in (None, "") else None
    except (TypeError, ValueError):
        orientation = -1  # present but not a number
    ident = str(raw.get("Identifier") or "").strip() or None
    return {
        "date": date,
        "offset": parse_offset(raw.get("OffsetTimeOriginal")),
        "create_date": parse_exif_date(raw.get("CreateDate")),
        "gps": gps,
        "orientation": orientation,
        "width": raw.get("ImageWidth"), "height": raw.get("ImageHeight"),
        "make": raw.get("Make"), "model": raw.get("Model"),
        "identifier": ident,
        "captions": captions,
        "keywords": keywords,
        "rating": rating,
        "title": raw.get("Title"),
        "regions": raw.get("RegionInfo") if isinstance(raw.get("RegionInfo"), dict) else None,
        "tag_count": sum(1 for v in raw.values() if v not in (None, "")),
    }


# -- Google Takeout -----------------------------------------------------------------------
def find_takeout_json(photo: Path) -> Path | None:
    """The Takeout .json for a photo, if there is one beside it."""
    name = photo.name
    for cand in (f"{name}.json", f"{name}.supplemental-metadata.json", f"{photo.stem}.json"):
        p = photo.with_name(cand)
        if p.is_file():
            return p
    # Takeout shortens long names (to 46-51 characters) and moves "(1)" to the end
    m = re.match(r"^(.*)\((\d+)\)(\.[^.]+)$", name)
    if m:
        p = photo.with_name(f"{m.group(1)}{m.group(3)}({m.group(2)}).json")
        if p.is_file():
            return p
    try:
        for p in photo.parent.iterdir():
            if not p.name.endswith(".json") or p.name == name:
                continue
            stem = p.name[:-5]
            stem = re.sub(r"\.supplemental-metadata.*$|\.supp.*$", "", stem)
            if len(stem) >= 30 and name.startswith(stem):
                data = read_takeout(p)
                if data and data.get("title") == name:
                    return p
    except OSError:
        pass
    return None


def read_takeout(path: Path) -> dict | None:
    """{title, date (UTC, 'YYYY:MM:DD HH:MM:SS'), gps} from a Takeout .json, or None."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    out = {"title": data.get("title"), "date": None, "gps": None}
    ts = (data.get("photoTakenTime") or {}).get("timestamp")
    try:
        if ts not in (None, "", "0"):
            out["date"] = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    for key in ("geoDataExif", "geoData"):
        g = data.get(key) or {}
        try:
            lat, lon = float(g.get("latitude", 0)), float(g.get("longitude", 0))
        except (TypeError, ValueError):
            continue
        if (lat, lon) != (0.0, 0.0):
            out["gps"] = (lat, lon)
            break
    return out


# -- fingerprints -------------------------------------------------------------------------
def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def pixel_hash(path: str | Path) -> tuple[str | None, str | None]:
    """(hash of the decoded pixels, error). Decodes the whole image: that is the decode check.

    RAW files are not decoded here (no RAW decoder in the image); (None, None).
    """
    if is_raw(path):
        return None, None
    try:
        with Image.open(path) as im:
            im.load()
            h = hashlib.sha256(f"{im.mode}|{im.size[0]}x{im.size[1]}|".encode())
            h.update(im.tobytes())
            return h.hexdigest(), None
    except Exception as exc:  # noqa: BLE001 - any failure is a failed decode
        return None, f"{type(exc).__name__}: {exc}"[:300]


def true_extension(path: str | Path) -> str | None:
    """The right extension when the current one names a different image format, else None."""
    with open(path, "rb") as fh:
        head = fh.read(HEAD_BYTES)
    kind, mime, mismatch = detect(head, Path(path).name, os.path.getsize(path))
    if mismatch and kind == "image" and mime in MIME_EXT:
        return MIME_EXT[mime]
    return None


# -- writing ---------------------------------------------------------------------------------
def tag_args(values: dict) -> list[str]:
    """exiftool arguments for {tag: value or [values]}. A list replaces the whole list tag."""
    args = []
    for tag, value in values.items():
        if isinstance(value, (list, tuple)):
            if not value:
                args.append(f"-{tag}=")
            for v in value:
                args.append(f"-{tag}={_one_line(v)}")
        else:
            args.append(f"-{tag}={_one_line(value)}")
    return args


def _one_line(v) -> str:
    return str(v).replace("\r", " ").replace("\n", " ")


def date_tags(date: str, offset: str | None, raw: bool) -> dict:
    if raw:  # sidecar: XMP only
        return {"XMP-exif:DateTimeOriginal": date + (offset or "")}
    tags = {"EXIF:DateTimeOriginal": date, "XMP-exif:DateTimeOriginal": date + (offset or "")}
    if offset:
        tags["EXIF:OffsetTimeOriginal"] = offset
    return tags


def gps_tags(lat: float, lon: float, raw: bool) -> dict:
    if raw:
        return {"XMP-exif:GPSLatitude": lat, "XMP-exif:GPSLongitude": lon}
    return {"GPSLatitude": abs(lat), "GPSLatitudeRef": "N" if lat >= 0 else "S",
            "GPSLongitude": abs(lon), "GPSLongitudeRef": "E" if lon >= 0 else "W"}


def _run(et: ExifTool, args: list[str]) -> str:
    return " ".join(et._execute(args).decode("utf-8", "replace").split())


class Tools:
    """Two exiftool processes: one reading (JSON), one writing."""

    def __init__(self):
        self.r = ExifTool(read=True)
        self.w = ExifTool(read=False)

    def close(self):
        self.r.close()
        self.w.close()


def safe_write(tools: Tools, path: Path, values: dict, ident: str | None = None,
               regions_json: dict | None = None) -> tuple[bool, str]:
    """Write tags into `path` safely. RAW: into its sidecar instead. Returns (ok, message).

    A copy is written first; it must open (Pillow, for formats Pillow reads) and read back
    the ID; only then does it replace the original. The original is untouched on failure.
    """
    et = tools.w
    path = Path(path)
    if "\n" in str(path):
        return False, "path contains a newline"
    target = sidecar_for(path) if is_raw(path) else path
    if is_raw(path) and not target.exists():  # a new sidecar from the RAW's own XMP + the new tags
        args = ["-P", *tag_args(values), "-o", str(target), str(path)]
        out = _run(et, args)
        if not target.exists():
            return False, f"sidecar not created: {out[:200]}"
        if regions_json:
            return _write_regions(et, target, regions_json)
        return True, f"sidecar {target.name} created"
    tmp = target.with_name(f".ipw-{uuid.uuid4().hex[:8]}-{target.name}")
    try:
        shutil.copy2(target, tmp)
        args = ["-overwrite_original", "-P", *tag_args(values), str(tmp)]
        out = _run(et, args) if values else "1 image files updated"
        if "1 image files updated" not in out and "1 files updated" not in out:
            return False, f"exiftool: {out[:300]}"
        if regions_json:
            ok, msg = _write_regions(et, tmp, regions_json)
            if not ok:
                return False, msg
        if not is_raw(path) and target.suffix.lower() != ".xmp":
            try:
                with Image.open(tmp) as im:
                    im.load()
            except Exception as exc:  # noqa: BLE001
                return False, f"the written copy doesn't open: {type(exc).__name__}: {exc}"[:300]
        if ident:
            back = tools.r.read(str(tmp), ("-XMP-dc:Identifier",))
            if str(back.get("Identifier") or "") != ident:
                return False, "the ID didn't read back from the written copy"
        os.replace(tmp, target)
        tmp = None
        return True, out[:200]
    finally:
        if tmp is not None and tmp.exists():
            tmp.unlink()


def _write_regions(et: ExifTool, target: Path, regions: dict) -> tuple[bool, str]:
    data = target.with_name(f".ipr-{uuid.uuid4().hex[:8]}.json")
    try:
        data.write_text(json.dumps([{"SourceFile": str(target), "RegionInfo": regions}]), encoding="utf-8")
        out = _run(et, ["-overwrite_original", "-P", f"-json={data}", str(target)])
        ok = "1 image files updated" in out or "1 files updated" in out
        return ok, out[:200]
    finally:
        if data.exists():
            data.unlink()


def merge_regions(keeper: dict | None, other: dict | None) -> dict | None:
    """Face regions from both copies, the keeper's first; same name + same area only once."""
    if not other or not other.get("RegionList"):
        return None
    if not keeper or not keeper.get("RegionList"):
        return other
    seen = {(r.get("Name"), json.dumps(r.get("Area"), sort_keys=True)) for r in keeper["RegionList"]}
    extra = [r for r in other["RegionList"]
             if (r.get("Name"), json.dumps(r.get("Area"), sort_keys=True)) not in seen]
    if not extra:
        return None
    merged = dict(keeper)
    merged["RegionList"] = list(keeper["RegionList"]) + extra
    return merged
