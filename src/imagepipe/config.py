"""Loading and validating the YAML config file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

if TYPE_CHECKING:  # pragma: no cover
    from imagepipe.phase2.config import Phase2Config
    from imagepipe.tools import ToolsConfig
    from imagepipe.triage.config import TriageConfig

DEFAULT_CONFIG_PATH = "/config/imagepipe.yml"


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid."""


@dataclass
class Source:
    name: str
    path: Path
    host_path: str = ""
    exclude: list[str] = field(default_factory=list)

    def display_path(self, rel_path: str) -> str:
        """Path as it should appear in reports (the host's view, if given)."""
        base = self.host_path or str(self.path)
        return f"{base.rstrip('/')}/{rel_path}"


@dataclass
class CatalogueConfig:
    workers: int = 8
    hash: str = "sha256"
    read_image_metadata: bool = True
    use_exiftool: bool = True
    list_archive_contents: bool = True
    batch_size: int = 500


@dataclass
class NotifyConfig:
    apprise_url: str = ""
    apprise_key: str = ""
    tags: str = ""
    events: list[str] = field(default_factory=lambda: ["finished", "error"])

    @property
    def enabled(self) -> bool:
        return bool(self.apprise_url and self.apprise_key)


@dataclass
class Config:
    timezone: str
    manifest: Path
    log_dir: Path
    reports_dir: Path
    sources: list[Source]
    catalogue: CatalogueConfig
    notify: NotifyConfig
    path: Path | None = None
    triage: "TriageConfig | None" = None
    tools: "ToolsConfig | None" = None
    phase2: "Phase2Config | None" = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def source(self, name: str) -> Source:
        for s in self.sources:
            if s.name == name:
                return s
        names = ", ".join(s.name for s in self.sources) or "(none)"
        raise ConfigError(f"No source named '{name}'. Configured sources: {names}")


def _section(data: dict, key: str) -> dict:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be a mapping")
    return value


def _known(cls, values: dict, section: str):
    allowed = set(cls.__dataclass_fields__)
    unknown = {str(k) for k in values} - allowed
    if unknown:
        hint = ""
        if {"True", "False"} & unknown:
            hint = " (YAML reads unquoted on/off/yes/no as true/false - check the key names)"
        raise ConfigError(f"Unknown key(s) in '{section}': {', '.join(sorted(unknown))}{hint}")
    return cls(**values)


def load_config(path: str | os.PathLike | None = None) -> Config:
    path = Path(path or os.environ.get("IMAGEPIPE_CONFIG") or DEFAULT_CONFIG_PATH)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("Config file must be a mapping at the top level")

    for key in ("manifest", "log_dir", "reports_dir"):
        if not data.get(key):
            raise ConfigError(f"'{key}' is required")

    timezone = data.get("timezone", "Australia/Brisbane")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"Unknown timezone '{timezone}'") from exc

    sources = []
    seen = set()
    for i, raw in enumerate(data.get("sources") or []):
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("path"):
            raise ConfigError(f"sources[{i}] needs at least 'name' and 'path'")
        if raw["name"] in seen:
            raise ConfigError(f"Duplicate source name '{raw['name']}'")
        seen.add(raw["name"])
        raw = dict(raw)
        raw["path"] = Path(raw["path"])
        sources.append(_known(Source, raw, f"sources[{i}]"))

    catalogue = _known(CatalogueConfig, _section(data, "catalogue"), "catalogue")
    if catalogue.hash not in ("sha256", "none"):
        raise ConfigError("catalogue.hash must be 'sha256' or 'none'")
    if catalogue.workers < 1:
        raise ConfigError("catalogue.workers must be at least 1")

    notify = _known(NotifyConfig, _section(data, "notify"), "notify")
    if not isinstance(notify.events, list) or not all(isinstance(e, str) for e in notify.events):
        raise ConfigError("notify.events must be a list of event names, e.g. [finished, error]")

    known_top = {"timezone", "manifest", "log_dir", "reports_dir", "sources", "catalogue", "notify", "triage",
                 "tools", "phase2"}
    unknown = {str(k) for k in data} - known_top
    if unknown:
        raise ConfigError(f"Unknown top-level key(s): {', '.join(sorted(unknown))}")

    from imagepipe.phase2.config import parse_phase2
    from imagepipe.tools import parse_tools
    from imagepipe.triage.config import parse_triage

    return Config(
        timezone=timezone,
        manifest=Path(data["manifest"]),
        log_dir=Path(data["log_dir"]),
        reports_dir=Path(data["reports_dir"]),
        sources=sources,
        catalogue=catalogue,
        notify=notify,
        path=path,
        triage=parse_triage(data.get("triage")),
        tools=parse_tools(data.get("tools")),
        phase2=parse_phase2(data.get("phase2")),
    )
