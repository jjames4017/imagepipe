"""Deciding which bucket a file goes to.

classify(row, overrides) -> (bucket, rule)

Buckets: imagepipe, docupipe, extract, sorted/<type>, quarantine/<reason>.
The rule is a short name for why, recorded against every file.
"""

from __future__ import annotations

from imagepipe.detect import JUNK_NAMES

ARCHIVE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".tbz2", ".txz", ".zip", ".rar", ".7z",
                    ".tar", ".gz", ".bz2", ".xz", ".cab")

DOCUPIPE_EXT = {
    "doc", "docx", "dot", "dotx", "odt", "rtf", "txt", "md", "wpd", "wps", "pages",
    "xls", "xlsx", "xlsm", "ods", "csv", "numbers",
    "ppt", "pptx", "pps", "ppsx", "odp", "key",
    "html", "htm", "mht", "mhtml",
}
# Only these are extracted. Zip (or other archive) content under any other
# extension is not an archive to unpack: .xlsm, .vsdx, .zed ... would lose the original.
ARCHIVE_EXTRACT_EXT = {"zip", "rar", "7z", "tar", "tgz", "tbz2", "txz", "gz", "bz2", "xz", "cab"}
# Design, vector and other "special" image files: kept aside in sorted/specials for review
# one by one, not sent to the photo pipeline (decided 27 Nov 2026, #55). TIFFs that are
# multi-page, layered or unreadable join them - that needs the file itself, see plan.py.
SPECIAL_EXT = {
    "psd", "psb", "xcf", "kra", "afdesign", "afphoto", "indd",            # layered / design
    "svg", "svgz", "eps", "ai", "cdr", "wmf", "emf", "ps", "dxf", "dwg",  # vector / CAD
    "mpo", "ico",                                                         # stereo pairs, icons
}

# Temporary, partial-download and automatic backup files: quarantined as junk.
JUNK_EXT = {"tmp", "crdownload", "asd", "wbk"}

EXT_BUCKET = {}
for _bucket, _exts in {
    "sorted/email": {"eml", "msg", "emlx", "mbox", "mbx", "pst", "ost", "olm"},
    "sorted/ebooks": {"epub", "mobi", "azw", "azw3", "azw4", "kfx", "fb2", "djvu", "lit", "cbz", "cbr", "cb7", "ibooks"},
    "sorted/software": {"exe", "msi", "msp", "dmg", "pkg", "mpkg", "deb", "rpm", "apk", "xapk", "ipa", "appimage",
                        "jar", "xpi", "crx", "whl", "app", "snap", "flatpak", "com", "scr", "dll", "sys"},
    "sorted/disk-images": {"iso", "img", "vmdk", "vhd", "vhdx", "qcow2", "vdi", "bin", "cue", "nrg", "toast",
                           "sparseimage", "sparsebundle", "wim", "esd"},
    "sorted/fonts": {"ttf", "otf", "ttc", "woff", "woff2", "fon", "fnt", "pfb", "pfm", "afm", "dfont"},
    "sorted/code": {"py", "pyw", "ipynb", "js", "mjs", "ts", "tsx", "jsx", "sh", "bash", "zsh", "bat", "cmd", "ps1",
                    "psm1", "vbs", "c", "h", "cpp", "hpp", "cc", "cs", "java", "kt", "go", "rs", "php", "rb", "pl",
                    "pm", "lua", "swift", "m", "r", "sql", "css", "scss", "sass", "less", "yml", "yaml", "toml",
                    "ini", "cfg", "conf", "properties", "gradle", "makefile", "cmake", "asm", "vb", "vba", "bas"},
    "sorted/audio": {"mp3", "m4a", "aac", "wav", "flac", "ogg", "oga", "opus", "wma", "amr", "aif", "aiff",
                     "mid", "midi", "ape", "alac", "m4b", "caf"},
    "sorted/video": {"mov", "mp4", "m4v", "avi", "mkv", "webm", "mts", "m2ts", "ts", "mpg", "mpeg", "3gp", "3g2",
                     "wmv", "asf", "flv", "vob", "mod", "tod", "divx", "ogv", "rm", "rmvb", "swf"},
    "sorted/other-known": {"json", "xml", "log", "vcf", "ics", "kml", "kmz", "gpx", "plist", "db", "sqlite",
                           "sqlite3", "mdb", "accdb", "torrent", "url", "webloc", "lnk", "bak", "dat", "nfo",
                           "srt", "sub", "vtt", "ass", "cue", "m3u", "m3u8", "pls", "reg", "pem", "crt", "key",
                           # application and genealogy files (Family Tree Maker, GenoPro, Project, Visio,
                           # Publisher ...): kept, but not documents for Paperless
                           "ftw", "gno", "fbk", "mpp", "vsd", "pub", "tcw", "hmk", "cl3", "obd", "paf", "ged"},
}.items():
    for _e in _exts:
        EXT_BUCKET.setdefault(_e, _bucket)

# Content kinds that, if the file's extension promised them, make a file whose
# contents are something else (e.g. a ".jpg" that is really an HTML error page) corrupt.
_PROMISE_KINDS = {"image", "raw", "video", "audio", "pdf"}
_MEDIA_EXT_KIND = {}


def archive_stem(name: str) -> str:
    low = name.lower()
    for suf in ARCHIVE_SUFFIXES:
        if low.endswith(suf) and len(name) > len(suf):
            return name[: -len(suf)]
    return name.rsplit(".", 1)[0] if "." in name.lstrip(".") else name


def _expected_kind(ext: str) -> str | None:
    if not _MEDIA_EXT_KIND:
        from imagepipe.detect import EXT_KIND

        _MEDIA_EXT_KIND.update(EXT_KIND)
    return _MEDIA_EXT_KIND.get(ext)


def classify(row, overrides: dict[str, str]) -> tuple[str, str]:
    """Bucket and rule for one catalogued file (before sidecar/Live Photo pairing)."""
    kind = row["kind"] or "unknown"
    ext = (row["ext"] or "").lower()
    mime = row["mime"] or ""
    err = row["catalogue_error"] or ""

    if kind == "empty":
        return "quarantine/zero-byte", "empty-file"
    try:
        name = (row["rel_path"] or "").rsplit("/", 1)[-1]
    except (KeyError, IndexError):
        name = ""
    if name.startswith("~$"):  # Office owner/lock file beside an open document (#31)
        return "quarantine/system-junk", "office-lock-file"
    if name.lower() in JUNK_NAMES:  # by name, so a name added later applies without a re-scan (#42)
        return "quarantine/system-junk", "system-junk-file"
    if ext in SPECIAL_EXT and kind not in ("empty", "junk"):
        return "sorted/specials", f"special-{ext}"
    if kind == "junk":
        return "quarantine/system-junk", "system-junk-file"
    if err.startswith("read:"):
        return "quarantine/corrupt", "unreadable"
    if ext in overrides:
        return overrides[ext], f"override:{ext}"
    if ext in JUNK_EXT:
        return "quarantine/system-junk", f"temp-or-backup-{ext}"

    if mime == "text/plain" and EXT_BUCKET.get(ext) in ("sorted/code", "sorted/other-known"):
        return EXT_BUCKET[ext], f"ext-{ext}"  # e.g. a TypeScript .ts file, not an MPEG-TS video
    if row["ext_mismatch"] and _expected_kind(ext) in _PROMISE_KINDS and kind in ("unknown", "document", "sidecar"):
        return "quarantine/corrupt", f"content-not-{_expected_kind(ext)}"

    if kind in ("image", "raw"):
        return "imagepipe", kind
    if kind == "sidecar":
        return "imagepipe", "sidecar-unmatched"
    if kind == "pdf":
        return "docupipe", "pdf"
    if kind == "video":
        return "sorted/video", "video"
    if kind == "audio":
        return "sorted/audio", "audio"
    if kind == "archive":
        if ext in EXT_BUCKET:  # zip-based ebooks/software (.cbz, .apk, .jar ...)
            return EXT_BUCKET[ext], f"zip-based-{ext}"
        if ext in ARCHIVE_EXTRACT_EXT:
            return "extract", f"archive-{ext}"
        return "quarantine/unknown-type", "unrecognised-extension"
    if kind == "document":
        if row["encrypted"]:  # password-protected Office file
            return "quarantine/encrypted", "encrypted-document"
        if row["takeout_title"]:
            return "imagepipe", "takeout-json-unmatched"
        if ext in EXT_BUCKET:
            return EXT_BUCKET[ext], f"ext-{ext}"
        if ext in DOCUPIPE_EXT:
            return "docupipe", f"document-{ext}"
        return "quarantine/unknown-type", "unrecognised-extension"
    # unknown content
    if ext in EXT_BUCKET:
        return EXT_BUCKET[ext], f"ext-{ext}"
    if ext in DOCUPIPE_EXT:
        return "docupipe", f"document-{ext}"
    return "quarantine/unknown-type", "unknown-content"
