"""The `triage:` section of the config file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from imagepipe.config import ConfigError, Source
from imagepipe.triage.kinds import (
    DEFAULT_CATEGORIES,
    DEFAULT_EXT_CATEGORY,
    legacy_bucket,
)

# Where everything triage and ImagePipe work on lives. Set in the compose file only, so it
# can't be changed from the web UI: the sources and the filestack must be inside the same
# container mount, or every move becomes a copy (Linux refuses hard links between mounts).
FILESTACK_ENV = "IMAGEPIPE_FILESTACK"
LEGACY_DESTINATIONS = ("imagepipe", "docupipe", "sorted", "quarantine")  # before 0.4; read by `triage relocate`
QUARANTINE_REASONS = (
    "unknown-type", "corrupt", "encrypted", "archive-failed", "zero-byte", "system-junk",
)
RESERVED_CATEGORIES = ("quarantine", "excluded", "extract")
PIPELINES = ("none", "imagepipe")
EMBED_KINDS = ("image", "pdf", "video")
_CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGT]?B?)\s*$", re.IGNORECASE)
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
    filestack: Path
    sources: list[Source]
    workers: int = 8
    verify: str = "auto"
    embed_metadata: list[str] = field(default_factory=list)  # off: ImagePipe stage 2 writes the ID
    reverse_geocode: bool = True
    day_first: bool = True
    live_photo_max_bytes: int = 20 << 20
    max_archive_depth: int = 5
    max_extract_bytes: int = 200 << 30
    smartname_max_length: int = 80
    categories: dict[str, dict] = field(default_factory=dict)  # name -> {pipeline, extensions}
    overrides: dict[str, str] = field(default_factory=dict)    # ext -> bucket, where it differs from the defaults
    legacy_destinations: dict[str, Path] = field(default_factory=dict)
    legacy_work_dir: Path | None = None

    @property
    def sorted_dir(self) -> Path:
        return self.filestack / "sorted"

    @property
    def work_dir(self) -> Path:
        return self.filestack / "triage" / "work"

    @property
    def imagepipe_dir(self) -> Path:
        return self.filestack / "imagepipe"

    def bucket_dir(self, bucket: str) -> Path:
        return self.sorted_dir.joinpath(*bucket.split("/"))

    def categories_for(self, pipeline: str) -> list[str]:
        return [c for c, v in self.categories.items() if v.get("pipeline") == pipeline]

    def source(self, name: str) -> Source:
        for s in self.sources:
            if s.name == name:
                return s
        names = ", ".join(s.name for s in self.sources) or "(none)"
        raise ConfigError(f"No triage source named '{name}'. Triage sources: {names}")


KNOWN = {
    "database", "log_dir", "reports_dir", "sources", "workers", "verify", "categories",
    "embed_metadata", "reverse_geocode", "day_first", "live_photo_max_bytes", "max_archive_depth",
    "max_extract_bytes", "smartname_max_length",
    # before 0.4: kept so `imagepipe triage relocate` knows where files were; not used otherwise
    "destinations", "work_dir", "bucket_overrides",
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
    for key in ("database", "log_dir", "reports_dir", "sources"):
        if not data.get(key):
            raise ConfigError(f"'triage.{key}' is required")
    filestack = os.environ.get(FILESTACK_ENV, "").strip()
    if not filestack:
        raise ConfigError(f"Triage needs the filestack folder: set {FILESTACK_ENV} in the compose file "
                          "(e.g. /wolf-pool/vault/filestack), inside the same mount as the triage sources")

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

    legacy = data.get("destinations") or {}
    if not isinstance(legacy, dict) or set(legacy) - set(LEGACY_DESTINATIONS):
        raise ConfigError(f"triage.destinations (before 0.4) can only have: {', '.join(LEGACY_DESTINATIONS)}")
    legacy_destinations = {k: Path(v) for k, v in legacy.items() if v}

    categories, overrides = parse_categories(data.get("categories"))
    for ext, bucket in (data.get("bucket_overrides") or {}).items():  # before 0.4
        ext = str(ext).lower().lstrip(".")
        overrides.setdefault(ext, legacy_bucket(str(bucket), ext))

    embed = data.get("embed_metadata") or []
    bad = set(embed) - set(EMBED_KINDS)
    if bad:
        raise ConfigError(f"triage.embed_metadata can only list: {', '.join(EMBED_KINDS)}")

    cfg = TriageConfig(
        database=Path(data["database"]),
        log_dir=Path(data["log_dir"]),
        reports_dir=Path(data["reports_dir"]),
        filestack=Path(filestack),
        sources=sources,
        workers=int(data.get("workers", 8)),
        verify=str(data.get("verify", "auto")),
        embed_metadata=list(embed),
        reverse_geocode=bool(data.get("reverse_geocode", True)),
        day_first=bool(data.get("day_first", True)),
        live_photo_max_bytes=parse_size(data.get("live_photo_max_bytes", "20MB"), "live_photo_max_bytes"),
        max_archive_depth=int(data.get("max_archive_depth", 5)),
        max_extract_bytes=parse_size(data.get("max_extract_bytes", "200GB"), "max_extract_bytes"),
        smartname_max_length=int(data.get("smartname_max_length", 80)),
        categories=categories,
        overrides=overrides,
        legacy_destinations=legacy_destinations,
        legacy_work_dir=Path(data["work_dir"]) if data.get("work_dir") else None,
    )
    if cfg.verify not in ("auto", "hash"):
        raise ConfigError("triage.verify must be 'auto' or 'hash'")
    if cfg.workers < 1:
        raise ConfigError("triage.workers must be at least 1")
    if cfg.smartname_max_length < 10:
        raise ConfigError("triage.smartname_max_length must be at least 10")

    outputs = {"filestack": cfg.filestack}
    for s in sources:
        for name, path in outputs.items():
            if _inside(path, s.path) or _inside(s.path, path):
                raise ConfigError(
                    f"triage: '{name}' ({path}) and source '{s.name}' ({s.path}) overlap - "
                    "the filestack must sit outside every source folder"
                )
    return cfg


def parse_categories(raw) -> tuple[dict[str, dict], dict[str, str]]:
    """triage.categories -> (categories, overrides). Missing: the built-in defaults.

    overrides maps each extension placed in a different category from the default to that
    category, so content checks (empty, corrupt, encrypted ...) still come first.
    """
    if raw is None:
        return {k: {"pipeline": v["pipeline"], "extensions": list(v["extensions"])}
                for k, v in DEFAULT_CATEGORIES.items()}, {}
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("triage.categories must be a mapping of category name -> {pipeline, extensions}")
    categories: dict[str, dict] = {}
    owner: dict[str, str] = {}
    for name, spec in raw.items():
        name = str(name).strip().lower()
        if not _CATEGORY_RE.match(name) or name in RESERVED_CATEGORIES:
            raise ConfigError(f"triage.categories: '{name}' can't be a category name "
                              f"(lower-case letters, digits, hyphens; not {', '.join(RESERVED_CATEGORIES)})")
        spec = spec or {}
        if not isinstance(spec, dict) or set(spec) - {"pipeline", "extensions"}:
            raise ConfigError(f"triage.categories.{name} can only have 'pipeline' and 'extensions'")
        pipeline = str(spec.get("pipeline") or "none")
        if pipeline not in PIPELINES:
            raise ConfigError(f"triage.categories.{name}.pipeline must be one of: {', '.join(PIPELINES)}")
        exts = spec.get("extensions") or []
        if isinstance(exts, str):
            exts = re.split(r"[\s,]+", exts)
        clean = []
        for e in exts:
            e = str(e).strip().lower().lstrip(".")
            if not e:
                continue
            if e in owner and owner[e] != name:
                raise ConfigError(f"triage.categories: extension '{e}' is in both '{owner[e]}' and '{name}'")
            owner[e] = name
            if e not in clean:
                clean.append(e)
        categories[name] = {"pipeline": pipeline, "extensions": sorted(clean)}
    for need in ("images", "raw", "documents"):
        categories.setdefault(need, {"pipeline": DEFAULT_CATEGORIES[need]["pipeline"], "extensions": []})
    overrides = {e: c for e, c in owner.items() if DEFAULT_EXT_CATEGORY.get(e) != c}
    return categories, overrides
