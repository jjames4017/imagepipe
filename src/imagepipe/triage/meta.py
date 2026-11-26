"""Reading the details triage needs from inside files: dates, GPS, titles,
Google Takeout JSON and XMP sidecars."""

from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime, timezone
from xml.etree import ElementTree

from PIL import Image

from imagepipe.metadata import _clean

Image.MAX_IMAGE_PIXELS = None

EXIF_DESCRIPTION = 0x010E
EXIF_XPTITLE = 0x9C9B
EXIF_GPS_IFD = 0x8825
EXIF_IFD = 0x8769

ET_TAGS = (
    "-Make", "-Model", "-DateTimeOriginal", "-CreateDate", "-ImageWidth", "-ImageHeight",
    "-GPSLatitude#", "-GPSLongitude#", "-Title", "-Description", "-ImageDescription", "-Encryption",
)

_DT_RE = re.compile(r"(\d{4})[:\-/.](\d{2})[:\-/.](\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?")

GENERIC_TITLES = {
    "untitled", "untitled document", "document", "document1", "document2", "new document", "scan",
    "scanned document", "image", "picture", "photo", "olympus digital camera", "sony dsc", "default",
    "microsoft word document", "title", "no title", "copy", "print", "page", "doc", "pdf",
}


def norm_date(value) -> str | None:
    """'2014:07:12 10:20:30' and friends -> '2014-07-12 10:20:30'. None if unusable."""
    value = _clean(value)
    if not value:
        return None
    m = _DT_RE.search(value)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return None
    out = f"{y:04d}-{mo:02d}-{d:02d}"
    if m.group(4):
        out += f" {m.group(4)}:{m.group(5)}:{m.group(6)}"
    return out


def useful_title(value) -> str | None:
    value = _clean(value)
    if not value:
        return None
    value = re.sub(r"^microsoft (word|excel|powerpoint) - ", "", value, flags=re.I).strip()
    value = re.sub(r"\.(docx?|xlsx?|pptx?|pdf|odt|rtf|txt)$", "", value, flags=re.I).strip()
    if len(value) < 3 or value.lower() in GENERIC_TITLES or not re.search(r"[^\W\d_]{2}", value):
        return None
    return value[:200]


def _rational(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        num, den = v
        return num / den if den else 0.0


def _dms(values, ref) -> float | None:
    try:
        d, m, s = (_rational(x) for x in values)
    except Exception:
        return None
    val = d + m / 60 + s / 3600
    if isinstance(ref, bytes):
        ref = ref.decode("ascii", "ignore")
    if str(ref).upper().startswith(("S", "W")):
        val = -val
    return val


def valid_gps(lat, lon) -> tuple[float, float] | None:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
        return None
    return lat, lon


def image_meta(path: str) -> dict:
    """Dimensions, camera, date, GPS and title from an image, with Pillow."""
    out: dict = {}
    with Image.open(path) as im:
        out["width"], out["height"] = im.size
        exif = im.getexif()
        out["camera_make"] = _clean(exif.get(0x010F))
        out["camera_model"] = _clean(exif.get(0x0110))
        date = None
        try:
            sub = exif.get_ifd(EXIF_IFD)
            date = norm_date(sub.get(0x9003)) or norm_date(sub.get(0x9004))
        except Exception:
            pass
        date = date or norm_date(exif.get(0x0132))
        if date:
            out["meta_date"], out["meta_date_src"] = date, "exif"
        try:
            gps = exif.get_ifd(EXIF_GPS_IFD)
            if gps:
                lat = _dms(gps.get(2), gps.get(1, "N"))
                lon = _dms(gps.get(4), gps.get(3, "E"))
                good = valid_gps(lat, lon)
                if good:
                    out["gps_lat"], out["gps_lon"] = good
        except Exception:
            pass
        title = None
        xp = exif.get(EXIF_XPTITLE)
        if isinstance(xp, (bytes, tuple, list)):
            try:
                title = bytes(xp).decode("utf-16-le", "ignore")
            except Exception:
                title = None
        title = useful_title(title) or useful_title(exif.get(EXIF_DESCRIPTION))
        if title:
            out["meta_title"] = title
    return out


def exiftool_meta(et, path: str, kind: str) -> dict:
    """Date, GPS, title and encryption from exiftool (RAW, video, audio, PDF)."""
    tags = et.read(path, ET_TAGS)
    if not tags:
        return {}
    out: dict = {}
    w, h = tags.get("ImageWidth"), tags.get("ImageHeight")
    if isinstance(w, int) and isinstance(h, int):
        out["width"], out["height"] = w, h
    out["camera_make"] = _clean(tags.get("Make"))
    out["camera_model"] = _clean(tags.get("Model"))
    date = norm_date(tags.get("DateTimeOriginal")) or norm_date(tags.get("CreateDate"))
    if date:
        out["meta_date"] = date
        out["meta_date_src"] = {"pdf": "pdf", "video": "video", "audio": "audio"}.get(kind, "exif")
    good = valid_gps(tags.get("GPSLatitude"), tags.get("GPSLongitude"))
    if good:
        out["gps_lat"], out["gps_lon"] = good
    title = useful_title(tags.get("Title")) or useful_title(tags.get("Description")) or useful_title(
        tags.get("ImageDescription"))
    if title:
        out["meta_title"] = title
    if tags.get("Encryption"):
        out["encrypted"] = 1
    return {k: v for k, v in out.items() if v is not None}


_NS = {
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
}


def office_meta(path: str) -> dict:
    """Title and created date from .docx/.xlsx/.pptx (core.xml) or .odt/.ods/.odp (meta.xml)."""
    out: dict = {}
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "docProps/core.xml" in names:
            root = ElementTree.fromstring(zf.read("docProps/core.xml"))
            title = root.findtext("dc:title", namespaces=_NS)
            created = root.findtext("dcterms:created", namespaces=_NS)
        elif "meta.xml" in names:
            root = ElementTree.fromstring(zf.read("meta.xml"))
            title = root.findtext(".//dc:title", namespaces=_NS)
            created = root.findtext(".//meta:creation-date", namespaces=_NS)
        else:
            return out
    date = norm_date(created)
    if date:
        out["meta_date"], out["meta_date_src"] = date, "office"
    title = useful_title(title)
    if title:
        out["meta_title"] = title
    return out


def takeout_meta(path: str, size: int) -> dict:
    """Google Takeout photo JSON: the photo's name, taken time and GPS.

    Returns {} for any JSON that isn't a Takeout photo sidecar.
    """
    if size > 1 << 20:
        return {}
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not ("photoTakenTime" in data or ("title" in data and "creationTime" in data)):
        return {}
    out: dict = {"takeout_title": _clean(data.get("title"))}
    for key in ("photoTakenTime", "creationTime"):
        ts = (data.get(key) or {}).get("timestamp") if isinstance(data.get(key), dict) else None
        try:
            if ts and int(ts) > 0:
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                out["meta_date"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                out["meta_date_src"] = "takeout"
                break
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    for key in ("geoDataExif", "geoData"):
        geo = data.get(key)
        if isinstance(geo, dict):
            good = valid_gps(geo.get("latitude"), geo.get("longitude"))
            if good:
                out["gps_lat"], out["gps_lon"] = good
                break
    title = useful_title(data.get("description"))
    if title:
        out["meta_title"] = title
    return out


_XMP_DATE = re.compile(rb"(?:exif:DateTimeOriginal|xmp:CreateDate|photoshop:DateCreated)\s*(?:=\s*[\"']|>)([^\"'<]+)")
_XMP_GPS = re.compile(rb"exif:GPS(Latitude|Longitude)\s*(?:=\s*[\"']|>)([^\"'<]+)")


def _xmp_coord(text: str) -> float | None:
    """XMP GPS like '15,52.766N' or '15,52,46N'."""
    m = re.match(r"^\s*(\d+),(\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?))?([NSEW])\s*$", text)
    if not m:
        return None
    val = int(m.group(1)) + float(m.group(2)) / 60 + (float(m.group(3)) / 3600 if m.group(3) else 0)
    return -val if m.group(4) in "SW" else val


def xmp_sidecar_meta(path: str, size: int) -> dict:
    if size > 4 << 20:
        return {}
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return {}
    out: dict = {}
    m = _XMP_DATE.search(data)
    if m:
        date = norm_date(m.group(1).decode("utf-8", "ignore"))
        if date:
            out["meta_date"], out["meta_date_src"] = date, "xmp"
    coords = {k.decode(): _xmp_coord(v.decode("utf-8", "ignore")) for k, v in _XMP_GPS.findall(data)}
    good = valid_gps(coords.get("Latitude"), coords.get("Longitude"))
    if good:
        out["gps_lat"], out["gps_lon"] = good
    return out
