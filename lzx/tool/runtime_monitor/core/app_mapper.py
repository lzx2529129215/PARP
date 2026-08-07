"""Map Linux processes to logical applications."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    tgid: int
    comm: str
    exe_path: str
    cgroup_path: str = ""
    start_time: str = ""
    cmdline_hash: str = ""


def _norm(value: object) -> str:
    return str(value or "").lower()


def load_config(path: str | Path) -> dict[str, Any]:
    """Load JSON, or a tiny YAML subset used by the default config."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    data: dict[str, Any] = {}
    current_list: list[str] | None = None
    current_dict: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip()
            if value:
                data[key] = _parse_scalar(value)
                current_list = None
                current_dict = None
            else:
                data[key] = {}
                current_dict = data[key]
                current_list = None
            continue
        if current_dict is not None and line.startswith("  ") and ":" in line:
            key, value = line.strip().split(":", 1)
            value = value.strip()
            if value == "":
                current_dict[key] = []
                current_list = current_dict[key]
            else:
                current_dict[key] = _parse_scalar(value)
                current_list = None
            continue
        if current_list is not None and line.strip().startswith("- "):
            current_list.append(line.strip()[2:].strip().strip('"').strip("'"))
    return data


def _parse_scalar(value: str) -> Any:
    value = value.strip().strip('"').strip("'")
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


class AppMapper:
    def __init__(self, config: dict[str, Any], target_app: str = "WPS", target_apps: list[str] | None = None) -> None:
        self.config = config
        self.target_app = target_app
        self.target_apps = target_apps or [target_app]
        apps = config.get("apps", {})
        if isinstance(apps, dict):
            self.app_rules = apps
        else:
            self.app_rules = {}

    def map_process(self, proc: ProcessIdentity) -> str:
        text = " ".join([proc.comm, proc.exe_path]).lower()
        app_ids = self.target_apps if self.target_apps else list(self.app_rules)
        for app_id in app_ids:
            rules = self.app_rules.get(app_id, {})
            if self._matches_rules(proc, text, rules):
                return app_id
        return ""

    @staticmethod
    def _matches_rules(proc: ProcessIdentity, text: str, rules: Any) -> bool:
        keywords = rules.get("keywords", []) if isinstance(rules, dict) else []
        exclude_keywords = rules.get("exclude_keywords", []) if isinstance(rules, dict) else []
        for keyword in exclude_keywords:
            if _norm(keyword) in text:
                return False
        for keyword in keywords:
            normalized = _norm(keyword)
            if len(normalized) <= 2 and "/" not in normalized:
                exe_name = Path(proc.exe_path).name.lower()
                path_parts = {part for part in proc.exe_path.lower().split("/") if part}
                if normalized == proc.comm.lower() or normalized == exe_name or normalized in path_parts:
                    return True
                continue
            if normalized in text:
                return True
        return False
