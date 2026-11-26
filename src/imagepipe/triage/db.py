"""The triage catalogue: one SQLite database recording every file triage has
seen, what it decided for each one, and every action it took.

It is separate from the ImagePipe manifest but uses the same pipeline IDs
(see imagepipe.ids), so a photo can be traced from its library record back
to where triage found it.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 1

# Status values
CATALOGUED = "catalogued"   # read, not yet planned
PLANNED = "planned"         # has a destination
MOVING = "moving"           # a move was started (recovered on the next apply)
DONE = "done"               # at its destination
EXTRACTED = "extracted"     # an archive whose contents were extracted and verified; archive deleted
CHANGED = "changed"         # changed on disk after it was read; read again on the next scan
ERROR = "error"             # the action failed; left where it was
MISSING = "missing"         # gone from the source before it was moved
ACTIVE = (CATALOGUED, PLANNED, CHANGED, ERROR, MISSING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    mode        TEXT NOT NULL,
    source      TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    summary     TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id               INTEGER PRIMARY KEY,
    triage_id        TEXT UNIQUE,
    source           TEXT NOT NULL,
    parent_id        INTEGER NOT NULL DEFAULT 0,  -- the archive it came out of (0 = found in the source)
    rel_path         TEXT NOT NULL,               -- path inside the source, or inside the archive
    logical_path     TEXT NOT NULL,               -- original folder structure, relative to the source
    dir              TEXT NOT NULL,               -- folder part of logical_path
    origin           TEXT NOT NULL,               -- where it was found, as a person would read it
    current_path     TEXT NOT NULL,               -- where it is now
    size             INTEGER NOT NULL,
    mtime            REAL NOT NULL,
    ext              TEXT,
    kind             TEXT,
    mime             TEXT,
    ext_mismatch     INTEGER NOT NULL DEFAULT 0,
    sha256           TEXT,
    width            INTEGER,
    height           INTEGER,
    camera_make      TEXT,
    camera_model     TEXT,
    meta_date        TEXT,     -- date from inside the file, YYYY-MM-DD[ HH:MM:SS]
    meta_date_src    TEXT,     -- exif | video | pdf | office | takeout | xmp
    gps_lat          REAL,
    gps_lon          REAL,
    meta_title       TEXT,
    takeout_title    TEXT,     -- Google Takeout JSON: the photo it describes
    encrypted        INTEGER,
    catalogue_error  TEXT,
    status           TEXT NOT NULL DEFAULT 'catalogued',
    bucket           TEXT,     -- imagepipe | docupipe | extract | sorted/<type> | quarantine/<reason>
    rule             TEXT,     -- the rule that chose the bucket
    companion_of     INTEGER,  -- sidecar / Live Photo clip: the file it belongs to
    name_date        TEXT,
    date_source      TEXT,
    smartname        TEXT,
    smartname_source TEXT,
    dest_path        TEXT,
    embed_status     TEXT,     -- pending | done | failed | n/a
    sha256_final     TEXT,     -- after tags were written into the file
    first_seen_run   INTEGER,
    last_seen_run    INTEGER,
    planned_at       TEXT,
    done_at          TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tfiles_path   ON files (source, parent_id, rel_path);
CREATE INDEX IF NOT EXISTS idx_tfiles_dir    ON files (source, parent_id, dir);
CREATE INDEX IF NOT EXISTS idx_tfiles_status ON files (status);
CREATE INDEX IF NOT EXISTS idx_tfiles_sha    ON files (sha256);
CREATE INDEX IF NOT EXISTS idx_tfiles_parent ON files (parent_id);

CREATE TABLE IF NOT EXISTS archive_members (
    id         INTEGER PRIMARY KEY,
    archive_id INTEGER NOT NULL,
    name       TEXT NOT NULL,
    size       INTEGER,
    file_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_members_archive ON archive_members (archive_id);

CREATE TABLE IF NOT EXISTS actions (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    run_id        INTEGER,
    file_id       INTEGER,
    triage_id     TEXT,
    action        TEXT NOT NULL,
    from_path     TEXT,
    to_path       TEXT,
    rule          TEXT,
    sha256_before TEXT,
    sha256_after  TEXT,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_file ON actions (file_id);
"""

ACTION_COLUMNS = ("ts", "run_id", "triage_id", "action", "from_path", "to_path", "rule",
                  "sha256_before", "sha256_after", "detail")


class TriageDB:
    def __init__(self, path: Path, tz: ZoneInfo, actions_csv: Path | None = None):
        self.path = Path(path)
        self.tz = tz
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.executescript(SCHEMA)
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            self.db.commit()
        elif int(row[0]) > SCHEMA_VERSION:
            raise RuntimeError(f"Triage database schema {row[0]} is newer than this ImagePipe. Upgrade ImagePipe.")
        self.actions_csv = actions_csv
        self._csv_fh = None
        self._csv = None

    def now(self) -> str:
        return datetime.now(self.tz).isoformat(timespec="seconds")

    # -- runs --------------------------------------------------------------
    def start_run(self, mode: str, source: str | None = None) -> int:
        cur = self.db.execute("INSERT INTO runs (mode, source, started_at, status) VALUES (?, ?, ?, 'running')",
                              (mode, source, self.now()))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str, summary: dict) -> None:
        self.db.execute("UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
                        (self.now(), status, json.dumps(summary, default=str), run_id))
        self.db.commit()

    # -- actions (audit trail) ---------------------------------------------
    def action(self, run_id: int | None, row, action: str, from_path: str | None = None,
               to_path: str | None = None, rule: str | None = None, sha_before: str | None = None,
               sha_after: str | None = None, detail: str = "") -> None:
        """Record one action, in the database and as a line in actions.csv."""
        rec = {
            "ts": self.now(), "run_id": run_id,
            "triage_id": row["triage_id"] if row is not None else None,
            "action": action, "from_path": from_path, "to_path": to_path, "rule": rule,
            "sha256_before": sha_before, "sha256_after": sha_after, "detail": detail,
        }
        self.db.execute(
            "INSERT INTO actions (ts, run_id, file_id, triage_id, action, from_path, to_path, rule, "
            "sha256_before, sha256_after, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rec["ts"], run_id, row["id"] if row is not None else None, rec["triage_id"], action,
             from_path, to_path, rule, sha_before, sha_after, detail),
        )
        if self.actions_csv is not None:
            if self._csv is None:
                self.actions_csv.parent.mkdir(parents=True, exist_ok=True)
                new = not self.actions_csv.exists() or self.actions_csv.stat().st_size == 0
                self._csv_fh = open(self.actions_csv, "a", newline="", encoding="utf-8")
                self._csv = csv.DictWriter(self._csv_fh, fieldnames=ACTION_COLUMNS)
                if new:
                    self._csv.writeheader()
            self._csv.writerow(rec)
            self._csv_fh.flush()

    def get(self, file_id: int):
        return self.db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()

    def update(self, file_id: int, **cols) -> None:
        cols["updated_at"] = self.now()
        sets = ", ".join(f"{k}=?" for k in cols)
        self.db.execute(f"UPDATE files SET {sets} WHERE id=?", (*cols.values(), file_id))

    def close(self) -> None:
        self.db.commit()
        self.db.close()
        if self._csv_fh is not None:
            self._csv_fh.close()


def file_depth(db: TriageDB, row) -> int:
    """How many archives deep a file is (0 = found in the source)."""
    depth, parent = 0, row["parent_id"]
    while parent:
        depth += 1
        p = db.db.execute("SELECT parent_id FROM files WHERE id=?", (parent,)).fetchone()
        parent = p[0] if p else 0
    return depth

