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
    {"key": "dedupe_backup_words", "type": "list", "label": "Duplicates: backup / temporary folder words",
     "help": "Remove duplicates: a folder whose name contains one of these words (whole words, any case; "
             "PhotoTemp counts as Photo + Temp) scores -2, so a copy there is the one taken out"},
    {"key": "dedupe_generic_folders", "type": "list", "label": "Duplicates: extra generic folder names",
     "help": "Folder names that say nothing (score 0), on top of the built-in ones (DCIM, Camera, Documents, "
             "New folder ...). Any other folder name scores +1"},
    {"key": "pdf_text_words", "type": "int", "label": "PDF check: words that make a document", "default": 7,
     "help": "Triage: check PDFs. A PDF with any page holding more words than this (text layer, or OCR when the "
             "text layer has fewer) is a document and never goes to ScanSplitter. A word is 2 or more letters/digits"},
]

TOOLS = [
    {"key": "scansplitter", "type": "group", "label": "ScanSplitter", "test": True,
     "help": "Splits PDF photo pages into photos (triage: Check PDFs). Test calls /api/health. Every project "
             "ImagePipe makes gets the settings below; projects it already made get them before it exports from them",
     "fields": [{"key": "url", "type": "str", "label": "Address", "placeholder": "http://<host>:8010"},
                {"key": "detection_mode", "type": "choice", "label": "Detection mode", "default": "scansplitterv7",
                 "choices": ["scansplitterv7", "scansplitterv5", "scansplitterv4", "scansplitterv3", "album-splitter",
                             "openrouter"]},
                {"key": "album_layout", "type": "choice", "label": "Album layout", "default": "auto",
                 "choices": ["auto", "single", "spread"]},
                {"key": "min_area", "type": "int", "label": "Smallest photo (% of page)", "default": 2},
                {"key": "max_area", "type": "int", "label": "Largest photo (% of page)", "default": 100},
                {"key": "auto_rotate", "type": "bool", "label": "Auto-rotate", "default": True},
                {"key": "edge_cleanup", "type": "choice", "label": "Edge cleanup", "default": "tight",
                 "choices": ["off", "conservative", "tight"]},
                {"key": "auto_deskew", "type": "bool", "label": "Auto-deskew", "default": True},
                {"key": "restore_color", "type": "bool", "label": "Restore colour", "default": True},
                {"key": "upscale_2x", "type": "bool", "label": "2x upscale", "default": True},
                {"key": "format", "type": "choice", "label": "Photo format", "default": "jpeg",
                 "choices": ["jpeg", "png"]},
                {"key": "quality", "type": "int", "label": "JPEG quality", "default": 100},
                {"key": "include_gps", "type": "bool", "label": "Include GPS", "default": False},
                {"key": "max_pdfs_per_project", "type": "int", "label": "Most PDFs per project", "default": 200},
                {"key": "approval", "type": "choice", "label": "Approval", "default": "auto",
                 "choices": ["auto", "review", "project"],
                 "help": "auto: pages ScanSplitter raised no CHECK flag for are approved and exported straight away; "
                         "review: every page waits for you to approve it in ScanSplitter; project: every page waits "
                         "for you, and nothing from a project is exported until every page in it is approved"}]},
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

PHASE2 = [
    {"key": "enabled", "type": "bool", "label": "Phase 2 switched on", "default": False,
     "help": "Off until the PhotoPrism export exists. While off, Phase 2 jobs refuse to run"},
    {"key": "batch_size", "type": "int", "label": "Photos per batch", "default": 2000,
     "help": "A month with more photos than this is split; undated photos go in batches this big"},
    {"key": "burst_gap_seconds", "type": "int", "label": "Burst: seconds between shots", "default": 2,
     "help": "Photos from one camera this close together count as one burst (flagged only)"},
    {"key": "burst_min", "type": "int", "label": "Burst: at least this many photos", "default": 3},
    {"key": "low_res_pixels", "type": "int", "label": "Low resolution below (long edge, pixels)", "default": 1000,
     "help": "Flagged only; whether a bigger copy exists is checked at stage 6"},
]

SCHEMA = [
    {"section": None, "label": "General", "fields": TOP},
    {"section": "catalogue", "label": "Catalogue", "fields": CATALOGUE},
    {"section": "notify", "label": "Notifications", "fields": NOTIFY},
    {"section": "tools", "label": "External tools", "optional": True, "fields": TOOLS,
     "help": "Where each outside tool is. Saved in imagepipe.yml (passwords and keys too). Test checks a tool "
             "from this container, using what is typed in the form, saved or not."},
    {"section": "triage", "label": "Triage", "optional": True, "fields": TRIAGE},
    {"section": "phase2", "label": "Phase 2", "optional": True, "fields": PHASE2,
     "help": "Stages 1, 2 and 4: month batches into <filestack>/imagepipe/processing/, IDs written into the files, "
             "exact duplicates out, conflicts to the Conflicts tab. Uses the triage section's filestack and categories."},
]
