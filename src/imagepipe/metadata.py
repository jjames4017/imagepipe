"""Light metadata reading for the catalogue: dimensions, EXIF presence, camera, date."""

from __future__ import annotations

import zipfile

from PIL import Image

from imagepipe.detect import detect

Image.MAX_IMAGE_PIXELS = None  # large scans and panoramas are expected

try:  # HEIC/HEIF support is optional
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF = True
except Exception:  # pragma: no cover - depends on install
    HEIF = False

EXIF_MAKE = 0x010F
EXIF_MODEL = 0x0110
EXIF_DATETIME = 0x0132
EXIF_IFD = 0x8769
EXIF_DATETIME_ORIGINAL = 0x9003
EXIF_DATETIME_DIGITIZED = 0x9004


def _clean(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    value = str(value).replace("\x00", "").strip()
    return value or None


def image_info(path: str) -> dict:
    """Read dimensions and basic EXIF with Pillow, without decoding pixels."""
    with Image.open(path) as im:
        width, height = im.size
        exif = im.getexif()
        has_exif = len(exif) > 0
        make = _clean(exif.get(EXIF_MAKE))
        model = _clean(exif.get(EXIF_MODEL))
        date = None
        try:
            sub = exif.get_ifd(EXIF_IFD)
            date = _clean(sub.get(EXIF_DATETIME_ORIGINAL)) or _clean(sub.get(EXIF_DATETIME_DIGITIZED))
            has_exif = has_exif or len(sub) > 0
        except Exception:
            pass
        date = date or _clean(exif.get(EXIF_DATETIME))
    return {
        "width": width,
        "height": height,
        "has_exif": int(has_exif),
        "camera_make": make,
        "camera_model": model,
        "date_taken": date,
    }


def exiftool_info(et, path: str) -> dict:
    tags = et.read(path)
    if not tags:
        return {}
    width = tags.get("ImageWidth")
    height = tags.get("ImageHeight")
    return {
        "width": width if isinstance(width, int) else None,
        "height": height if isinstance(height, int) else None,
        "has_exif": int(bool(tags.get("Make") or tags.get("Model") or tags.get("DateTimeOriginal"))),
        "camera_make": _clean(tags.get("Make")),
        "camera_model": _clean(tags.get("Model")),
        "date_taken": _clean(tags.get("DateTimeOriginal") or tags.get("CreateDate")),
    }


def zip_members(path: str, limit: int = 100_000) -> list[tuple[str, int, str]]:
    """List a zip's members as (name, size, kind_guess). Kind is a guess from the name only."""
    members = []
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            kind, _, _ = detect(b"", info.filename, size=info.file_size or None)
            if kind == "unknown":
                from imagepipe.detect import EXT_KIND, extension

                kind = EXT_KIND.get(extension(info.filename), "unknown")
            members.append((info.filename, info.file_size, kind))
            if len(members) >= limit:
                break
    return members
