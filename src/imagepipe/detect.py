"""Work out what a file really is from its contents, not its extension.

detect(head, name) looks at the first few KB of a file and its name, and
returns (kind, mime, ext_mismatch).

Kinds: image, raw, video, audio, pdf, document, archive, sidecar, junk, empty,
unknown.
"""

from __future__ import annotations

HEAD_BYTES = 4096

RAW_EXT = {
    "3fr", "ari", "arw", "bay", "cr2", "cr3", "crw", "dcr", "dng", "erf", "fff", "iiq",
    "k25", "kdc", "mef", "mos", "mrw", "nef", "nrw", "orf", "pef", "raf", "raw", "rw2",
    "rwl", "sr2", "srf", "srw", "x3f",
}
IMAGE_EXT = {
    "jpg", "jpeg", "jpe", "jfif", "png", "gif", "webp", "heic", "heif", "hif", "avif",
    "tif", "tiff", "bmp", "dib", "jxl", "psd",
}
VIDEO_EXT = {
    "mov", "mp4", "m4v", "avi", "mkv", "webm", "mts", "m2ts", "ts", "mpg", "mpeg",
    "3gp", "3g2", "wmv", "asf", "flv", "vob", "mod", "tod",
}
AUDIO_EXT = {"mp3", "m4a", "aac", "wav", "flac", "ogg", "oga", "wma", "amr", "aif", "aiff"}
OFFICE_ZIP_EXT = {"docx", "xlsx", "pptx", "odt", "ods", "odp", "pages", "numbers", "key", "epub"}
DOCUMENT_EXT = OFFICE_ZIP_EXT | {
    "doc", "xls", "ppt", "msg", "rtf", "txt", "csv", "md", "html", "htm", "xml", "json",
    "log", "eml", "vcf", "ics",
}
ARCHIVE_EXT = {"zip", "rar", "7z", "gz", "tgz", "bz2", "xz", "tar", "cab"}
SIDECAR_EXT = {"xmp", "aae", "thm", "xmp_original"}
JUNK_NAMES = {
    ".ds_store", "thumbs.db", "ehthumbs.db", "ehthumbs_vista.db", "desktop.ini",
    ".picasa.ini", "picasa.ini", ".localized", "zbthumbnail.info", ".nomedia",
}

EXT_KIND: dict[str, str] = {}
for _kind, _exts in (
    ("raw", RAW_EXT), ("image", IMAGE_EXT), ("video", VIDEO_EXT), ("audio", AUDIO_EXT),
    ("document", DOCUMENT_EXT), ("archive", ARCHIVE_EXT), ("sidecar", SIDECAR_EXT),
):
    for _e in _exts:
        EXT_KIND.setdefault(_e, _kind)
EXT_KIND["pdf"] = "pdf"

# Image formats where the extension also names the exact format: a PNG saved
# as .jpg is flagged as a mismatch even though both are images.
EXT_MIME = {
    **dict.fromkeys(("jpg", "jpeg", "jpe", "jfif"), "image/jpeg"),
    "png": "image/png", "gif": "image/gif", "webp": "image/webp",
    **dict.fromkeys(("heic", "heif", "hif"), "image/heic"),
    "avif": "image/avif", **dict.fromkeys(("tif", "tiff"), "image/tiff"),
    **dict.fromkeys(("bmp", "dib"), "image/bmp"), "jxl": "image/jxl", "psd": "image/vnd.adobe.photoshop",
}
# Some TIFF-based RAW files have a plain TIFF header; a .nef/.dng that reads as
# TIFF is not a mismatch.

HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"}
AVIF_BRANDS = {b"avif", b"avis"}
AUDIO_BRANDS = {b"M4A ", b"M4B ", b"M4P "}


def extension(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    if "." not in base.lstrip("."):
        return ""
    return base.rsplit(".", 1)[-1].lower()


def _content(head: bytes, ext: str) -> tuple[str, str] | None:
    """Identify by magic bytes. Returns (kind, mime) or None."""
    h = head
    if h[:3] == b"\xff\xd8\xff":
        return "image", "image/jpeg"
    if h[:8] == b"\x89PNG\r\n\x1a\n":
        return "image", "image/png"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if h[:4] == b"RIFF" and len(h) >= 12:
        sub = h[8:12]
        if sub == b"WEBP":
            return "image", "image/webp"
        if sub == b"AVI ":
            return "video", "video/x-msvideo"
        if sub == b"WAVE":
            return "audio", "audio/wav"
    if h[:4] in (b"IIRO", b"IIRS", b"IIU\x00"):
        return "raw", "image/x-raw"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        if ext in RAW_EXT:
            return "raw", "image/x-raw"
        return "image", "image/tiff"
    if len(h) >= 12 and h[4:8] == b"ftyp":
        brand = h[8:12]
        brands = {brand} | {h[i:i + 4] for i in range(16, min(len(h), 64), 4)}
        if brand == b"crx ":
            return "raw", "image/x-canon-cr3"
        if brand in AVIF_BRANDS or (brand in (b"mif1", b"msf1") and brands & AVIF_BRANDS):
            return "image", "image/avif"
        if brand in HEIF_BRANDS:
            return "image", "image/heic"
        if brand in AUDIO_BRANDS:
            return "audio", "audio/mp4"
        if brand == b"qt  ":
            return "video", "video/quicktime"
        return "video", "video/mp4"
    if len(h) >= 8 and h[4:8] in (b"moov", b"mdat", b"wide", b"free", b"skip", b"pnot"):
        return "video", "video/quicktime"
    if h[:2] == b"\xff\x0a" or h[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return "image", "image/jxl"
    if h[:15] == b"FUJIFILMCCD-RAW":
        return "raw", "image/x-fuji-raf"
    if h[6:14] == b"HEAPCCDR":
        return "raw", "image/x-canon-crw"
    if h[:4] == b"FOVb":
        return "raw", "image/x-sigma-x3f"
    if h[:4] == b"\x00MRM":
        return "raw", "image/x-minolta-mrw"
    if h[:4] == b"8BPS":
        return "image", "image/vnd.adobe.photoshop"
    if h[:2] == b"BM" and (ext in ("bmp", "dib") or h[6:10] == b"\x00\x00\x00\x00"):
        return "image", "image/bmp"
    if h[:5] == b"%PDF-":
        return "pdf", "application/pdf"
    if h[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        if ext in OFFICE_ZIP_EXT:
            return "document", "application/vnd.openxmlformats"
        return "archive", "application/zip"
    if h[:6] == b"Rar!\x1a\x07":
        return "archive", "application/vnd.rar"
    if h[:6] == b"7z\xbc\xaf\x27\x1c":
        return "archive", "application/x-7z-compressed"
    if h[:2] == b"\x1f\x8b":
        return "archive", "application/gzip"
    if h[:3] == b"BZh":
        return "archive", "application/x-bzip2"
    if h[:6] == b"\xfd7zXZ\x00":
        return "archive", "application/x-xz"
    if h[257:262] == b"ustar":
        return "archive", "application/x-tar"
    if h[:4] == b"MSCF":
        return "archive", "application/vnd.ms-cab-compressed"
    if h[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "document", "application/x-ole-storage"
    if h[:5] == b"{\\rtf":
        return "document", "application/rtf"
    if h[:4] == b"\x1aE\xdf\xa3":
        return "video", "video/x-matroska"
    if h[:4] == b"\x00\x00\x01\xba" or h[:4] == b"\x00\x00\x01\xb3":
        return "video", "video/mpeg"
    if len(h) > 188 and h[0] == 0x47 and h[188] == 0x47:
        return "video", "video/mp2t"
    if len(h) > 196 and h[4] == 0x47 and h[196] == 0x47:
        return "video", "video/mp2t"
    if h[:3] == b"FLV":
        return "video", "video/x-flv"
    if h[:16] == b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c":
        if ext == "wma":
            return "audio", "audio/x-ms-wma"
        return "video", "video/x-ms-asf"
    if h[:3] == b"ID3" or h[:4] == b"fLaC" or h[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio", "audio/mpeg" if h[:4] != b"fLaC" else "audio/flac"
    if h[:4] == b"OggS":
        if ext in VIDEO_EXT:
            return "video", "video/ogg"
        return "audio", "audio/ogg"
    if h[:4] == b"\x00\x05\x16\x07":
        return "junk", "application/applefile"
    # PDFs may have a little junk before the header. Checked after every other
    # signature, so an archive that holds a PDF is not mistaken for one.
    if b"%PDF-" in h[:1024]:
        return "pdf", "application/pdf"
    stripped = h.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if stripped.startswith(b"<!doctype html") or stripped.startswith(b"<html"):
        return "document", "text/html"
    if stripped.startswith(b"<?xpacket") or stripped.startswith(b"<x:xmpmeta"):
        return "sidecar", "application/rdf+xml"
    return None


def _looks_like_text(head: bytes) -> bool:
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        # a multi-byte character may be cut at the end of the head
        try:
            head[:-4].decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False


def detect(head: bytes, name: str, size: int | None = None) -> tuple[str, str, bool]:
    """Return (kind, mime, ext_mismatch) for a file."""
    base = name.rsplit("/", 1)[-1]
    ext = extension(base)
    expected = EXT_KIND.get(ext)

    if size == 0 or (size is None and not head):
        return "empty", "application/x-empty", False
    if base.lower() in JUNK_NAMES or base.startswith("._"):
        return "junk", "application/octet-stream", False
    if ext in SIDECAR_EXT:
        return "sidecar", "application/octet-stream", False

    found = _content(head, ext)
    if found is None:
        if _looks_like_text(head):
            if ext in DOCUMENT_EXT:
                return "document", "text/plain", False
            if expected is None:
                return "unknown", "text/plain", False
            return "unknown", "text/plain", True
        return "unknown", "application/octet-stream", expected is not None

    kind, mime = found
    mismatch = expected is not None and expected != kind and not (expected == "raw" and mime == "image/tiff")
    if not mismatch and kind == "image" and ext in EXT_MIME and EXT_MIME[ext] != mime:
        mismatch = True
    return kind, mime, mismatch
