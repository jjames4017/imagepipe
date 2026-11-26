"""Building new file names: <ID>_<date>_<smartname>.<ext>

Date, first found of:
  1. embedded metadata (EXIF, video, PDF, Office)
  2. a sidecar or metadata file (Google Takeout JSON, XMP sidecar)
  3. a date in the file name, then the folder names (deepest first)
  4. the file's modified time

Smartname: a place name from GPS (if any), then either a title from the file's
metadata, or the meaningful folder names plus the file name if it is
descriptive (not a camera name like IMG_1234). If none of those exist, the
original file name (without extension).
"""

from __future__ import annotations

import contextlib
import re
import sys
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTHS.update({"january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7, "august": 8,
               "september": 9, "sept": 9, "october": 10, "november": 11, "december": 12})

GENERIC_FOLDERS = {
    "dcim", "camera", "camera roll", "camera uploads", "photos", "photo", "pictures", "picture", "images", "image",
    "img", "imgs", "pics", "pic", "new folder", "untitled folder", "folder", "backup", "backups", "misc", "other",
    "others", "unsorted", "sorted", "temp", "tmp", "download", "downloads", "desktop", "documents", "my documents",
    "my pictures", "my photos", "file cabinet", "export", "exports", "import", "imports", "takeout",
    "google photos", "icloud photos", "whatsapp images", "whatsapp video", "whatsapp", "sent", "private", "media",
    "phone", "iphone", "android", "screenshots", "originals", "original", "edited", "files", "data", "stuff",
    "archive", "archives", "old", "new", "copy", "copies", "upload", "uploads", "shared", "users", "user", "home",
    "videos", "video", "movies", "music", "scans", "scanned", "photos2", "all photos", "photo library",
    "photos library", "masters", "previews", "thumbnails", "thumbs", "resources", "batch", "batches",
}
_GENERIC_FOLDER_RE = re.compile(
    r"^(\d{3}[a-z_]{5}|\d{3}_\d{4}|\d{3}apple|\d{3}cano[n]?|\d{3}msdcf|batch[\s_-]*\d+|part[\s_-]*\d+|"
    r"disk[\s_-]*\d+|cd[\s_-]*\d+|dvd[\s_-]*\d+|vol(ume)?[\s_-]*\d+|[0-9a-f]{8,}|[\d\s._-]+|"
    r"photos from \d{4}|photos? \d{4})$",
    re.I,
)
_CAMERA_NAME_RE = re.compile(
    r"^(img|image|dsc|dscn|dscf|dsci|pxl|mvimg|vid|video|mov|p|pic|pict|picture|photo|sam|gopr|gp|gh\d|dji|wp|"
    r"scan|screenshot|screen shot|screen recording|whatsapp image|whatsapp video|signal|sdc|cimg|imag|hpim|"
    r"pano|burst|cover|edited|copy|file|document|doc|untitled|new|thumb|_mg|mg|mvi|mah|sny|kif|clip|rec)"
    r"[\s_\-.()]*([\d\s_\-.()]|wa|edited|copy|e)*$",
    re.I,
)
_UUIDISH_RE = re.compile(r"^[0-9a-f]{8}(-?[0-9a-f]{4}){3}-?[0-9a-f]{12}$|^[0-9a-f\-_]{16,}$", re.I)
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f\uf780-\uf7ff\udc80-\udcff]')  # incl. bytes that weren't UTF-8


# ---------------------------------------------------------------- dates
def _valid(y: int, m: int = 1, d: int = 1) -> bool:
    if not (1900 <= y <= datetime.now().year + 1 and 1 <= m <= 12 and 1 <= d <= 31):
        return False
    try:
        datetime(y, m, d)
        return True
    except ValueError:
        return False


def date_in_text(text: str, day_first: bool = True) -> str | None:
    """Find a date in a file or folder name. Returns YYYY-MM-DD, YYYY-MM or YYYY."""
    t = text
    # YYYY-MM-DD, YYYY_MM_DD, YYYY.MM.DD, YYYYMMDD (e.g. IMG_20140712_101010, PXL_20210101...)
    for m in re.finditer(r"(?<!\d)((?:19|20)\d{2})[-_. ]?(\d{2})[-_. ]?(\d{2})(?!\d)", t):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if _valid(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # DD-MM-YYYY (day first, as used in Australia) or MM-DD-YYYY
    for m in re.finditer(r"(?<!\d)(\d{1,2})[-_. /](\d{1,2})[-_. /]((?:19|20)\d{2})(?!\d)", t):
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        d, mo = (a, b) if day_first else (b, a)
        if not _valid(y, mo, d):
            d, mo = mo, d
        if _valid(y, mo, d):
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # Month name + year: "July 2014", "Jul-2014", "2014 July"
    low = t.lower()
    for m in re.finditer(r"(?<![a-z])([a-z]{3,9})[\s_.-]*((?:19|20)\d{2})(?!\d)", low):
        if m.group(1) in MONTHS and _valid(int(m.group(2)), MONTHS[m.group(1)]):
            return f"{int(m.group(2)):04d}-{MONTHS[m.group(1)]:02d}"
    for m in re.finditer(r"(?<!\d)((?:19|20)\d{2})[\s_.-]*([a-z]{3,9})(?![a-z])", low):
        if m.group(2) in MONTHS and _valid(int(m.group(1)), MONTHS[m.group(2)]):
            return f"{int(m.group(1)):04d}-{MONTHS[m.group(2)]:02d}"
    # YYYY-MM
    for m in re.finditer(r"(?<!\d)((?:19|20)\d{2})[-_.](\d{2})(?!\d)", t):
        if _valid(int(m.group(1)), int(m.group(2))):
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    # A year on its own
    for m in re.finditer(r"(?<![\d])((?:19|20)\d{2})(?![\d])", t):
        if _valid(int(m.group(1))):
            return f"{int(m.group(1)):04d}"
    return None


def date_from_path(filename: str, folders: list[str], day_first: bool = True) -> tuple[str, str] | None:
    """(date, source) from the file name, then folders deepest first.

    A year folder with a month folder below it (2014/07) is combined.
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename.lstrip(".") else filename
    found = date_in_text(stem, day_first)
    if found:
        return found, "filename"
    for i in range(len(folders) - 1, -1, -1):
        found = date_in_text(folders[i], day_first)
        if found:
            if len(found) == 4 and i + 1 < len(folders):
                mm = re.fullmatch(r"(\d{1,2})(?:[\s_.-].*)?", folders[i + 1])
                month = int(mm.group(1)) if mm else MONTHS.get(folders[i + 1].strip().lower()[:9])
                if month and 1 <= month <= 12:
                    found = f"{found}-{month:02d}"
            return found, "folder"
    return None


def date_from_mtime(mtime: float, tz: ZoneInfo) -> str:
    try:
        return datetime.fromtimestamp(mtime, tz).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "0000-00-00"


# ---------------------------------------------------------------- smartname
def clean_part(text: str) -> str:
    """Make text safe for a file name on Linux, Mac and Windows (SMB)."""
    text = unicodedata.normalize("NFC", text)
    text = _BAD_CHARS.sub(" ", text)
    text = re.sub(r"[\s_.]+", "-", text.strip())  # no dots: the first dot starts the extension
    text = re.sub(r"-{2,}", "-", text)
    return text.strip("-. ")


def _strip_dates(text: str) -> str:
    t = re.sub(r"(?<!\d)(?:19|20)\d{2}[-_. ]?\d{2}[-_. ]?\d{2}(?!\d)", " ", text)
    t = re.sub(r"(?<!\d)\d{1,2}[-_. /]\d{1,2}[-_. /](?:19|20)\d{2}(?!\d)", " ", t)
    t = re.sub(r"(?<!\d)(?:19|20)\d{2}(?:[-_.]\d{2})?(?!\d)", " ", t)
    words = [w for w in re.split(r"(\s+)", t) if w.strip().lower() not in MONTHS]
    return re.sub(r"\s+", " ", "".join(words)).strip(" -_.,()[]")


def meaningful_folder(name: str) -> str | None:
    base = name.strip()
    if not base or base.lower() in GENERIC_FOLDERS or _GENERIC_FOLDER_RE.match(base):
        return None
    base = _strip_dates(base)
    if not base or base.lower() in GENERIC_FOLDERS or _GENERIC_FOLDER_RE.match(base):
        return None
    if not re.search(r"[^\W\d_]{2}", base):
        return None
    return base


def descriptive_filename(stem: str) -> str | None:
    s = stem.strip()
    if not s or _CAMERA_NAME_RE.match(s) or _UUIDISH_RE.match(s):
        return None
    s = _strip_dates(s)
    s = re.sub(r"^(img|dsc|pxl|vid|scan|screenshot|whatsapp image)[\s_-]*", "", s, flags=re.I)
    s = re.sub(r"[\s_-]*(\(\d+\)|copy|edited|-e)$", "", s, flags=re.I)
    if not s or _CAMERA_NAME_RE.match(s) or not re.search(r"[^\W\d_]{3}", s):
        return None
    return s


def smartname(place: str | None, title: str | None, folders: list[str], stem: str,
              max_len: int = 80) -> tuple[str, str]:
    """(smartname, source). Source names what it was built from, e.g. 'gps+folder'."""
    pieces: list[tuple[str, str]] = []
    if place:
        pieces.append((place, "gps"))
    if title:
        pieces.append((title, "metadata"))
    else:
        found = [f for f in (meaningful_folder(x) for x in folders) if f]
        if found:
            pieces.append(("-".join(found[-2:]), "folder"))
        desc = descriptive_filename(stem)
        if desc:
            pieces.append((desc, "filename"))
    if not pieces:
        pieces = [(stem, "original-name")]
    parts, seen = [], set()
    for text, _src in pieces:
        c = clean_part(text)
        if c and c.lower() not in seen:
            parts.append(c)
            seen.add(c.lower())
    name = "-".join(parts) or clean_part(stem) or "file"
    if len(name) > max_len:
        name = name[:max_len].rstrip("-. ")
    return name, "+".join(src for _t, src in pieces)


def build_name(pid: str, date: str, smart: str, ext: str) -> str:
    name = f"{pid}_{date}_{smart}"
    suffix = f".{ext}" if ext else ""
    # keep the whole name well inside the 255-byte limit of most file systems
    while len((name + suffix).encode("utf-8")) > 200 and len(smart) > 1:
        smart = smart[:-1].rstrip("-. ")
        name = f"{pid}_{date}_{smart}"
    return name + suffix


# ---------------------------------------------------------------- places
class Geocoder:
    """Offline GPS -> place name, using the reverse_geocoder package (GeoNames cities)."""

    def __init__(self, enabled: bool = True):
        self.rg = None
        if enabled:
            try:
                import reverse_geocoder

                self.rg = reverse_geocoder
            except Exception:  # pragma: no cover - optional dependency
                self.rg = None

    @property
    def available(self) -> bool:
        return self.rg is not None

    def places(self, coords: list[tuple[float, float]]) -> list[str | None]:
        if not coords or self.rg is None:
            return [None] * len(coords)
        # reverse_geocoder prints a loading message on stdout; keep stdout for reports
        with contextlib.redirect_stdout(sys.stderr):
            results = self.rg.search(coords, mode=1)
        out = []
        for r in results:
            name = (r.get("name") or "").strip()
            cc = (r.get("cc") or "").strip()
            out.append(f"{name} {cc}".strip() or None)
        return out
