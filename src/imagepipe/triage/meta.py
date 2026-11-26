"""Reading the details triage needs from inside files: dates, GPS, titles,
Google Takeout JSON, XMP sidecars, Photoshop headers and password-protected
Office files."""

from __future__ import annotations

import json
import re
import struct
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


def psd_meta(path: str) -> dict:
    """Width and height from a Photoshop .psd/.psb header.

    Pillow can't open layered or newer Photoshop files, so the header is read
    directly. Raises ValueError if the file isn't a valid PSD/PSB.
    """
    with open(path, "rb") as fh:
        head = fh.read(26)
    if len(head) < 26 or head[:4] != b"8BPS":
        raise ValueError("not a Photoshop file (no 8BPS header)")
    version, = struct.unpack(">H", head[4:6])
    if version not in (1, 2):
        raise ValueError(f"unknown Photoshop version {version}")
    height, width = struct.unpack(">II", head[14:22])
    if not (0 < width <= 300000 and 0 < height <= 300000):
        raise ValueError(f"bad Photoshop size {width}x{height}")
    return {"width": width, "height": height}


OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_FREESECT, _ENDOFCHAIN = 0xFFFFFFFF, 0xFFFFFFFE


def ole_stream_names(path: str, max_sectors: int = 1 << 22) -> set[str]:
    """Names of the streams and storages in an OLE compound file (old Office
    format, and the container used for password-protected Office files).

    A small, read-only reader of the directory only. Raises ValueError if the
    file isn't a valid compound file.
    """
    with open(path, "rb") as fh:
        hdr = fh.read(512)
        if len(hdr) < 512 or hdr[:8] != OLE_MAGIC:
            raise ValueError("not an OLE compound file")
        shift, = struct.unpack("<H", hdr[0x1E:0x20])
        if shift not in (9, 12):
            raise ValueError(f"bad OLE sector size 2^{shift}")
        ss = 1 << shift
        n_fat, first_dir = struct.unpack("<II", hdr[0x2C:0x34])
        first_difat, n_difat = struct.unpack("<II", hdr[0x44:0x4C])
        per = ss // 4

        def sector(n: int) -> bytes:
            fh.seek((n + 1) * ss)
            data = fh.read(ss)
            if len(data) < ss:
                raise ValueError(f"OLE sector {n} is past the end of the file")
            return data

        # where the FAT sectors are: 109 in the header, the rest in a DIFAT chain
        fat_secs = [x for x in struct.unpack("<109I", hdr[0x4C:0x200]) if x < _ENDOFCHAIN]
        nxt, seen = first_difat, 0
        while nxt < _ENDOFCHAIN and seen < max(n_difat, 1) + 1:
            vals = struct.unpack(f"<{per}I", sector(nxt))
            fat_secs += [x for x in vals[:-1] if x < _ENDOFCHAIN]
            nxt, seen = vals[-1], seen + 1
        fat_secs = fat_secs[:n_fat] if n_fat else fat_secs
        if not fat_secs or len(fat_secs) * per > max_sectors:
            raise ValueError("bad OLE allocation table")
        fat: list[int] = []
        for n in fat_secs:
            fat.extend(struct.unpack(f"<{per}I", sector(n)))

        names: set[str] = set()
        cur, steps = first_dir, 0
        while cur < _ENDOFCHAIN:
            if cur >= len(fat) or steps > len(fat):
                raise ValueError("broken OLE directory chain")
            data = sector(cur)
            for off in range(0, ss, 128):
                entry = data[off:off + 128]
                nlen, otype = struct.unpack("<HB", entry[0x40:0x43])
                if otype in (1, 2, 5) and 2 <= nlen <= 64:
                    names.add(entry[:nlen - 2].decode("utf-16-le", "replace"))
            cur, steps = fat[cur], steps + 1
    return names


def ole_office_meta(path: str) -> dict:
    """An Office file (.docx/.xlsx/...) whose contents are an OLE compound file.

    Password-protected Office files are stored this way (EncryptionInfo +
    EncryptedPackage streams) and are marked encrypted. Anything else is an old
    binary Office file under a newer extension: a normal document, nothing to read.
    """
    if "EncryptedPackage" in ole_stream_names(path):
        return {"encrypted": 1}
    return {}


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
