"""The `phase2:` section of the config file."""

from __future__ import annotations

from dataclasses import dataclass

from imagepipe.config import ConfigError


@dataclass
class Phase2Config:
    # Off until the PhotoPrism export exists: phase 2 refuses to run while this is false.
    enabled: bool = False
    batch_size: int = 2000          # a month with more photos than this is split; undated go in batches this big
    burst_gap_seconds: int = 2      # photos from one camera this close together are a burst...
    burst_min: int = 3              # ...when there are at least this many in a row
    low_res_pixels: int = 1000      # a photo whose long edge is shorter than this is flagged low resolution


KNOWN = set(Phase2Config.__dataclass_fields__)


def parse_phase2(data) -> Phase2Config:
    if data is None:
        return Phase2Config()
    if not isinstance(data, dict):
        raise ConfigError("'phase2' must be a mapping")
    unknown = {str(k) for k in data} - KNOWN
    if unknown:
        raise ConfigError(f"Unknown key(s) in 'phase2': {', '.join(sorted(unknown))}")
    try:
        cfg = Phase2Config(
            enabled=bool(data.get("enabled", False)),
            batch_size=int(data.get("batch_size", 2000)),
            burst_gap_seconds=int(data.get("burst_gap_seconds", 2)),
            burst_min=int(data.get("burst_min", 3)),
            low_res_pixels=int(data.get("low_res_pixels", 1000)),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"phase2: {exc}") from exc
    if cfg.batch_size < 10:
        raise ConfigError("phase2.batch_size must be at least 10")
    if cfg.burst_min < 2:
        raise ConfigError("phase2.burst_min must be at least 2")
    if cfg.burst_gap_seconds < 0 or cfg.low_res_pixels < 0:
        raise ConfigError("phase2.burst_gap_seconds and phase2.low_res_pixels can't be negative")
    return cfg
