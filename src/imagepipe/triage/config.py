"""The `triage:` section of the config file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from imagepipe.config import ConfigError, Source

DESTINATIONS = ("imagepipe", "docupipe", "sorted", "quarantine")
SORTED_TYPES = (
    "video", "audio", "software", "disk-images", "email", "ebooks", "code", "fonts", "other-known",
)
QUARANTINE_REASONS = (
    "unknown-type", "corrupt", "encrypted", "archive-failed", "zero-byte", "system-junk",
)
BUCKETS = ("imagepipe", "docupipe", "extract") + tuple(f"sorted/{t}" for t in SORTED_TYPES) + tuple(
    f"quarantine/{r}" for r in QUARANTINE_REASONS
)
EMBED_KINDS = ("image", "pdf", "video")

_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGT]?B?)\s*$", re.I)
_UNITS = {"": 1, "B": 1, "K": 1 << 10, "KB": 1 << 10, "M": 1 << 20, "MB": 1 << 20,
          "G": 1 << 30, "GB": 1 << 30, "T": 1 << 40, "TB": 1 << 40}


def parse_size(value, key: str) -> int:
    if isinstance(value, int):
        return value
    m = _SIZE_RE.match(str(value))
    if not m:
        raise ConfigError(f"triage.{key} must be a size like 500GB, got '{value}'")
    return int(float(m.group(1)) * _UNITS[m.group(2).upper()])


@dataclass
class TriageConfig:
    database: Path
    log_dir: Path
    reports_dir: Path
    work_dir: Path
    sources: list[Source]
    destinations: dict[str, Path]
    workers: int = 8
    verify: str = "auto"
    embed_metadata: list[str] = field(default_factory=lambda: ["image", "pdf"])
    reverse_geocode: bool = True
    day_first: bool = True
    live_photo_max_bytes: int = 20 << 20
    max_archive_depth: int = 5
    max_extract_bytes: int = 200 << 30
    smartname_max_length: int = 80
    bucket_overrides: dict[str, str] = field(default_factory=dict)

    def source(self, name: str) -> Source:
        for s in self.sources:
            if s.name == name:
                return s
        names = ", ".join(s.name for s in self.sources) or "(none)"
        raise ConfigError(f"No triage source named '{name}'. Triage sources: {names}")


KNOWN = {
    "database", "log_dir", "reports_dir", "work_dir", "sources", "destinations", "workers", "verify",
    "embed_metadata", "reverse_geocode", "day_first", "live_photo_max_bytes", "max_archive_depth",
    "max_extract_bytes", "smartname_max_length", "bucket_overrides",
}


def _inside(child: Path, parent: Path) -> bool:
    try:
        Path(os.path.abspath(child)).relative_to(os.path.abspath(parent))
        return True
    except ValueError:
        return False


def parse_triage(data: dict | None) -> TriageConfig | None:
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError("'triage' must be a mapping")
    unknown = {str(k) for k in data} - KNOWN
    if unknown:
        raise ConfigError(f"Unknown key(s) in 'triage': {', '.join(sorted(unknown))}")
    for key in ("database", "log_dir", "reports_dir", "work_dir", "sources", "destinations"):
        if not data.get(key):
            raise ConfigError(f"'triage.{key}' is required")

    sources, seen = [], set()
    for i, raw in enumerate(data["sources"]):
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("path"):
            raise ConfigError(f"triage.sources[{i}] needs 'name' and 'path'")
        extra = set(raw) - {"name", "path", "exclude"}
        if extra:
            raise ConfigError(f"Unknown key(s) in triage.sources[{i}]: {', '.join(sorted(extra))}")
        if raw["name"] in seen:
            raise ConfigError(f"Duplicate triage source name '{raw['name']}'")
        seen.add(raw["name"])
        sources.append(Source(name=str(raw["name"]), path=Path(raw["path"]), exclude=list(raw.get("exclude") or [])))

    dests = data["destinations"]
    if not isinstance(dests, dict) or set(dests) != set(DESTINATIONS):
        raise ConfigError(f"triage.destinations needs exactly these keys: {', '.join(DESTINATIONS)}")
    destinations = {k: Path(v) for k, v in dests.items()}

    overrides = {str(k).lower().lstrip("."): str(v) for k, v in (data.get("bucket_overrides") or {}).items()}
    for ext, bucket in overrides.items():
        if bucket not in BUCKETS:
            raise ConfigError(f"triage.bucket_overrides['{ext}']: '{bucket}' is not one of {', '.join(BUCKETS)}")

    embed = data.get("embed_metadata", ["image", "pdf"]) or []
    bad = set(embed) - set(EMBED_KINDS)
    if bad:
        raise ConfigError(f"triage.embed_metadata can only list: {', '.join(EMBED_KINDS)}")

    cfg = TriageConfig(
        database=Path(data["database"]),
        log_dir=Path(data["log_dir"]),
        reports_dir=Path(data["reports_dir"]),
        work_dir=Path(data["work_dir"]),
        sources=sources,
        destinations=destinations,
        workers=int(data.get("workers", 8)),
        verify=str(data.get("verify", "auto")),
        embed_metadata=list(embed),
        reverse_geocode=bool(data.get("reverse_geocode", True)),
        day_first=bool(data.get("day_first", True)),
        live_photo_max_bytes=parse_size(data.get("live_photo_max_bytes", "20MB"), "live_photo_max_bytes"),
        max_archive_depth=int(data.get("max_archive_depth", 5)),
        max_extract_bytes=parse_size(data.get("max_extract_bytes", "200GB"), "max_extract_bytes"),
        smartname_max_length=int(data.get("smartname_max_length", 80)),
        bucket_overrides=overrides,
    )
    if cfg.verify not in ("auto", "hash"):
        raise ConfigError("triage.verify must be 'auto' or 'hash'")
    if cfg.workers < 1:
        raise ConfigError("triage.workers must be at least 1")
    if cfg.smartname_max_length < 10:
        raise ConfigError("triage.smartname_max_length must be at least 10")

    outputs = dict(destinations, work_dir=cfg.work_dir)
    for s in sources:
        for name, path in outputs.items():
            if _inside(path, s.path) or _inside(s.path, path):
                raise ConfigError(
                    f"triage: '{name}' ({path}) and source '{s.name}' ({s.path}) overlap - "
                    "destinations must sit outside every source folder"
                )
    return cfg
