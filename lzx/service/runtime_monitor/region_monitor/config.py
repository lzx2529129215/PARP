from __future__ import annotations

import json
from pathlib import Path

from .models import RegionMonitorConfig


def load_region_monitor_config(path: str | Path | None) -> RegionMonitorConfig:
    if not path:
        return RegionMonitorConfig()
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise FileNotFoundError(f"region monitor config not found: {config_path}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("region monitor config must be a JSON object")
    return RegionMonitorConfig.from_dict(data)

