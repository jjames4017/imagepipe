"""Description of every config setting, used to draw the settings form.

Each field: key, type, label, help, and for some: choices, default.
Types: str, path, int, bool, choice, multi (several choices), list (lines of
text), size (e.g. 20MB), map (key -> choice), sources (list of records),
group (fixed sub-mapping; with "test" a Test button checks that tool), secret
(a password or key: typed in a hidden field), readonly (shown, never saved: comes from the compose
file), categories (name -> pipeline + extensions).
"""

from __future__ import annotations

from imagepipe.triage.config import EMBED_KINDS, FILESTACK_ENV, PIPELINES
from imagepipe.triage.kinds import DEFAULT_CATEGORIES

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
    {"key": "filestack", "type": "readonly", "label": "Filestack folder", "env": FILESTACK_ENV,
     "help": f"Set by {FILESTACK_ENV} in the compose file and can only be changed there. Everything triage "
             "sorts goes to <filestack>/sorted/; archives are extracted in <filestack>/triage/work/. It must be "
             "inside the same container mount as the triage sources, or every move becomes a copy."},
    {"key": "sources", "type": "sources", "label": "Triage sources", "help": "Files are MOVED out of these",
     "fields": [
         {"key": "name", "type": "str", "label": "Name"},
         {"key": "path", "type": "path", "label": "Path"},
         {"key": "exclude", "type": "list", "label": "Exclude patterns", "optional": True},
     ]},
    {"key": "categories", "type": "categories", "label": "Categories", "choices": list(PIPELINES),
     "default": DEFAULT_CATEGORIES,
     "help": "Each category is a folder under <filestack>/sorted/. Extensions decide the category (content "
             "checks for empty, corrupt or encrypted files come first). Pipeline: which pipeline picks the "
             "category up, or none (the folder is the files' final place). One pipeline per category."},
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
]

TOOLS = [
    {"key": "scansplitter", "type": "group", "label": "ScanSplitter", "test": True,
     "help": "Splits PDF photo pages into photos (triage). Test calls /api/health",
     "fields": [{"key": "url", "type": "str", "label": "Address", "placeholder": "http://<host>:8010"}]},
    {"key": "photoprism", "type": "group", "label": "PhotoPrism", "test": True,
     "help": "Test calls /api/v1/status and, with a username and password, checks the login (signing out again)",
     "fields": [{"key": "url", "type": "str", "label": "Address", "placeholder": "http://<host>:2342"},
                {"key": "username", "type": "str", "label": "Username"},
                {"key": "password", "type": "secret", "label": "Password"}]},
    {"key": "immich", "type": "group", "label": "Immich", "test": True,
     "help": "Test calls /api/server/ping and, with an API key, checks the key",
     "fields": [{"key": "url", "type": "str", "label": "Address", "placeholder": "http://<host>:2283"},
                {"key": "api_key", "type": "secret", "label": "API key"}]},
    {"key": "a_eye", "type": "group", "label": "A-Eye", "test": True,
     "help": "Test calls /api/health and, with a username and password, signs in to /api/watch/status "
             "(reads only)",
     "fields": [{"key": "url", "type": "str", "label": "Address", "placeholder": "http://<host>:8000"},
                {"key": "username", "type": "str", "label": "Username"},
                {"key": "password", "type": "secret", "label": "Password"}]},
    {"key": "digikam", "type": "group", "label": "digiKam", "test": True,
     "help": "Test opens the web page, signing in with the username and password (HTTP Basic) if set",
     "fields": [{"key": "url", "type": "str", "label": "Address", "placeholder": "https://<host>:3011"},
                {"key": "username", "type": "str", "label": "Username"},
                {"key": "password", "type": "secret", "label": "Password"}]},
]

SCHEMA = [
    {"section": None, "label": "General", "fields": TOP},
    {"section": "catalogue", "label": "Catalogue", "fields": CATALOGUE},
    {"section": "notify", "label": "Notifications", "fields": NOTIFY},
    {"section": "tools", "label": "External tools", "optional": True, "fields": TOOLS,
     "help": "Where each outside tool is. Saved in imagepipe.yml (passwords and keys too). Test checks a tool "
             "from this container, using what is typed in the form, saved or not."},
    {"section": "triage", "label": "Triage", "optional": True, "fields": TRIAGE},
]
