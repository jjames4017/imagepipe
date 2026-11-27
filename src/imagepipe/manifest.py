"""The manifest: one SQLite database recording every file ImagePipe has ever seen.

It is the single source of truth for the whole pipeline. Catalogue writes the
catalogue columns; later stages own pipeline_id, status, stage, current_path,
excluded_reason and keeper_pipeline_id.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL,
    source      TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,          -- running | finished | interrupted | failed
    summary     TEXT                    -- JSON
);

CREATE TABLE IF NOT EXISTS files (
    id                 INTEGER PRIMARY KEY,
    source             TEXT NOT NULL,
    rel_path           TEXT NOT NULL,
    top_folder         TEXT NOT NULL,
    size               INTEGER NOT NULL,
    mtime              REAL NOT NULL,
    ext                TEXT,
    kind               TEXT,
    mime               TEXT,
    ext_mismatch       INTEGER NOT NULL DEFAULT 0,
    sha256             TEXT,
    width              INTEGER,
    height             INTEGER,
    has_exif           INTEGER,
    camera_make        TEXT,
    camera_model       TEXT,
    date_taken         TEXT,
    catalogue_error    TEXT,
    -- owned by later pipeline stages
    pipeline_id        TEXT UNIQUE,
    status             TEXT NOT NULL DEFAULT 'catalogued',
    stage              TEXT,
    current_path       TEXT,
    excluded_reason    TEXT,
    keeper_pipeline_id TEXT,
    -- bookkeeping
    first_seen_run     INTEGER,
    last_seen_run      INTEGER,
    catalogued_at      TEXT,
    updated_at         TEXT,
    UNIQUE (source, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files (sha256);
CREATE INDEX IF NOT EXISTS idx_files_kind   ON files (source, kind);
CREATE INDEX IF NOT EXISTS idx_files_status ON files (source, status);
CREATE INDEX IF NOT EXISTS idx_files_top    ON files (source, top_folder);

CREATE TABLE IF NOT EXISTS archive_members (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    size       INTEGER,
    kind_guess TEXT,
    UNIQUE (file_id, name)
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    run_id  INTEGER,
    file_id INTEGER,
    stage   TEXT NOT NULL,
    action  TEXT NOT NULL,
    detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_file ON events (file_id);
"""

# Version 2 (phase 2): batches, copies, conflicts and flags, and phase-2 columns on files.
PHASE2_COLUMNS = {
    "category": "TEXT",        # sorted/<category> the file came from
    "batch": "TEXT",           # processing/<batch>
    "pixel_hash": "TEXT",      # SHA-256 of the decoded pixels
    "date_source": "TEXT",     # where date_taken came from: exif | takeout
    "undated": "INTEGER",      # 1 = no date anywhere; batched as undated-NNN
    "gps_lat": "REAL",
    "gps_lon": "REAL",
    "orientation": "INTEGER",
    "takeout_json": "TEXT",    # the Google Takeout .json read for this file
    "takeout": "TEXT",         # JSON: what the Takeout file said (date, gps)
    "id_source": "TEXT",       # name | metadata | new
    "sha256_written": "TEXT",  # SHA-256 after ImagePipe last wrote into the file (sha256 stays the original)
}

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS batches (
    name       TEXT PRIMARY KEY,       -- YYYY-MM[-n] or undated-NNN
    month      TEXT,                   -- YYYY-MM, NULL for undated
    status     TEXT NOT NULL,          -- planned | processing | processed
    files      INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS copies (     -- every place a photo (or a duplicate of it) came from
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER NOT NULL,
    original_path TEXT NOT NULL,
    role          TEXT NOT NULL,       -- original | duplicate
    dup_file_id   INTEGER,
    created_at    TEXT,
    UNIQUE (file_id, original_path)
);
CREATE INDEX IF NOT EXISTS idx_copies_file ON copies (file_id);

CREATE TABLE IF NOT EXISTS conflicts (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL,      -- the kept file the chosen value is written into
    field       TEXT NOT NULL,         -- date | gps
    signature   TEXT NOT NULL,         -- conflicts with the same signature are "similar"
    candidates  TEXT NOT NULL,         -- JSON list: {key, source, value, ..., file_id}
    recommended TEXT,                  -- JSON: {value, ..., why, confidence}
    status      TEXT NOT NULL,         -- pending | resolved
    chosen      TEXT,                  -- JSON: the value written
    created_at  TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conflicts_status ON conflicts (status, signature);
CREATE INDEX IF NOT EXISTS idx_conflicts_file ON conflicts (file_id);

CREATE TABLE IF NOT EXISTS flags (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL,
    flag       TEXT NOT NULL,          -- burst | low-res | orientation | reentry-changed | ...
    detail     TEXT,
    created_at TEXT,
    UNIQUE (file_id, flag)
);
CREATE INDEX IF NOT EXISTS idx_flags_flag ON flags (flag);
"""

CATALOGUE_COLUMNS = (
    "top_folder", "size", "mtime", "ext", "kind", "mime", "ext_mismatch", "sha256",
    "width", "height", "has_exif", "camera_make", "camera_model", "date_taken",
    "catalogue_error",
)


class Manifest:
    def __init__(self, path: Path, tz: ZoneInfo):
        self.path = Path(path)
        self.tz = tz
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmpdir = os.environ.get("SQLITE_TMPDIR")
        if tmpdir:
            os.makedirs(tmpdir, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA temp_store=FILE")
        self._init_schema()

    # -- setup -----------------------------------------------------------
    def _init_schema(self) -> None:
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is not None and int(row[0]) > SCHEMA_VERSION:
            raise RuntimeError(
                f"Manifest schema version {row[0]} is newer than this ImagePipe ({SCHEMA_VERSION}). Upgrade ImagePipe."
            )
        # version 1 -> 2: only adds tables and columns, nothing is changed or removed
        have = {r[1] for r in self.db.execute("PRAGMA table_info(files)")}
        for col, kind in PHASE2_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE files ADD COLUMN {col} {kind}")
        self.db.executescript(SCHEMA_V2)
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_files_batch ON files (batch, status)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_files_pixel ON files (pixel_hash)")
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        self.db.commit()

    def now(self) -> str:
        return datetime.now(self.tz).isoformat(timespec="seconds")

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # -- runs ------------------------------------------------------------
    def start_run(self, mode: str, source: str | None) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (mode, source, started_at, status) VALUES (?, ?, ?, 'running')",
            (mode, source, self.now()),
        )
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, summary: dict) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
            (self.now(), status, json.dumps(summary), run_id),
        )
        self.db.commit()

    def event(self, run_id: int | None, file_id: int | None, stage: str, action: str, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO events (ts, run_id, file_id, stage, action, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (self.now(), run_id, file_id, stage, action, detail),
        )
