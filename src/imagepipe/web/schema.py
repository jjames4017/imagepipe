"""Description of every config setting, used to draw the settings form.

Each field: key, type, label, help, and for some: choices, default.
Types: str, path, int, bool, choice, multi (several choices), list (lines of
text), size (e.g. 20MB), map (key -> choice), sources (list of records),
group (fixed sub-mapping).
"""

from __future__ import annotations

from imagepipe.triage.config import BUCKETS, EMBED_KINDS

TOP = [
    {"key": "timezone", "type": "str", "label": "Time zone", "default": "Australia/Brisbane",
     "help": "Used for timestamps in the databases, logs and reports"},
    {"key": "manifest", "type": "path", "label": "Manifest database", "help": "ImagePipe's manifest (SQLite file)"},
    {"key": "log_dir", "type": "path", "label": "Log folder"},
    {"key": "reports_dir", "type": "path", "label": "Reports folder"},
    {"key": "sources", "type": "sources", "label": "Catalogue sources",
     "help": "Folders the catalogue reads (read-only)",
     "fields": [
         {"key": "name", "type": "str", "label": "Name"},
         {"key": "path", "type": "path", "label": "Path"},
         {"key": "host_path", "type": "path", "label": "Host path (shown in reports)", "optional": True},
         {"key": "exclude", "type": "list", "label": "Exclude patterns", "optional": True},
     ]},
]

CATALOGUE = [
    {"key": "workers", "type": "int", "label": "Workers", "default": 8, "help": "Parallel readers"},
    {"key": "hash", "type": "choice", "label": "Hash", "choices": ["sha256", "none"], "default": "sha256"},
    {"key": "read_image_metadata", "type": "bool", "label": "Read image metadata", "default": True},
    {"key": "use_exiftool", "type": "bool", "label": "Use exiftool (RAW and video)", "default": True},
    {"key": "list_archive_contents", "type": "bool", "label": "List zip contents", "default": True},
    {"key": "batch_size", "type": "int", "label": "Files per database commit", "default": 500},
]

NOTIFY = [
    {"key": "apprise_url", "type": "str", "label": "Apprise URL", "default": "",
     "help": "Base URL of the Apprise API, e.g. http://10.10.1.32:8001"},
    {"key": "apprise_key", "type": "str", "label": "Apprise config key", "default": ""},
    {"key": "tags", "type": "str", "label": "Apprise tags", "default": ""},
    {"key": "events", "type": "multi", "label": "Notify on", "choices": ["finished", "error"],
     "default": ["finished", "error"]},
]

TRIAGE = [
    {"key": "database", "type": "path", "label": "Triage database"},
    {"key": "log_dir", "type": "path", "label": "Log folder"},
    {"key": "reports_dir", "type": "path", "label": "Reports folder", "help": "Plan reports and actions.csv"},
    {"key": "work_dir", "type": "path", "label": "Archive extraction folder",
     "help": "Same file system as the destinations, so moves are instant"},
    {"key": "sources", "type": "sources", "label": "Triage sources", "help": "Files are MOVED out of these",
     "fields": [
         {"key": "name", "type": "str", "label": "Name"},
         {"key": "path", "type": "path", "label": "Path"},
         {"key": "exclude", "type": "list", "label": "Exclude patterns", "optional": True},
     ]},
    {"key": "destinations", "type": "group", "label": "Destinations", "fields": [
        {"key": "imagepipe", "type": "path", "label": "imagepipe (photos)"},
        {"key": "docupipe", "type": "path", "label": "docupipe (documents)"},
        {"key": "sorted", "type": "path", "label": "sorted (by type)"},
        {"key": "quarantine", "type": "path", "label": "quarantine (by reason)"},
    ]},
    {"key": "workers", "type": "int", "label": "Workers", "default": 8},
    {"key": "verify", "type": "choice", "label": "Verify moves", "choices": ["auto", "hash"], "default": "auto",
     "help": "auto: inode check for same-disk moves, SHA-256 for copies. hash: SHA-256 always"},
    {"key": "embed_metadata", "type": "multi", "label": "Write ID into files", "choices": list(EMBED_KINDS),
     "default": [], "help": "Off by default - ImagePipe stage 2 writes the ID"},
    {"key": "reverse_geocode", "type": "bool", "label": "GPS to place names", "default": True},
    {"key": "day_first", "type": "bool", "label": "Day first in dates (03-04 = 3 April)", "default": True},
    {"key": "live_photo_max_bytes", "type": "size", "label": "Largest Live Photo clip", "default": "20MB"},
    {"key": "max_archive_depth", "type": "int", "label": "Archives inside archives, depth", "default": 5},
    {"key": "max_extract_bytes", "type": "size", "label": "Largest unpacked archive", "default": "200GB"},
    {"key": "smartname_max_length", "type": "int", "label": "Smartname length", "default": 80},
    {"key": "bucket_overrides", "type": "map", "label": "Bucket overrides", "choices": list(BUCKETS),
     "default": {}, "help": "Extension -> bucket, e.g. json -> docupipe"},
]

SCHEMA = [
    {"section": None, "label": "General", "fields": TOP},
    {"section": "catalogue", "label": "Catalogue", "fields": CATALOGUE},
    {"section": "notify", "label": "Notifications", "fields": NOTIFY},
    {"section": "triage", "label": "Triage", "optional": True, "fields": TRIAGE},
]
