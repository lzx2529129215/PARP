"""Map Linux desktop window metadata to predictor application names."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MappingResult:
    app: str | None
    source: str
    value: str

    @property
    def known(self) -> bool:
        return self.app is not None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _norm(value: object) -> str:
    return str(value or "").strip().lower()


def read_pid_identity(pid: object) -> dict[str, str]:
    """Read process identity from /proc without requiring psutil."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return {"comm": "", "cmdline": ""}
    if pid_int <= 0:
        return {"comm": "", "cmdline": ""}

    proc_dir = Path("/proc") / str(pid_int)
    comm = ""
    cmdline = ""
    try:
        comm = (proc_dir / "comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pass
    try:
        raw = (proc_dir / "cmdline").read_bytes()
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except OSError:
        pass
    return {"comm": comm, "cmdline": cmdline}


class AppMapper:
    """Rule-based mapper from window metadata to app_vocab names."""

    def __init__(self, mapping_path: str | Path, app_vocab_path: str | Path) -> None:
        self.mapping_path = Path(mapping_path)
        self.app_vocab_path = Path(app_vocab_path)
        self.config = json.loads(self.mapping_path.read_text(encoding="utf-8"))
        self.app_vocab = json.loads(self.app_vocab_path.read_text(encoding="utf-8"))
        self.rules: list[dict[str, Any]] = list(self.config.get("rules", []))

    def map_event(self, event: dict[str, Any]) -> MappingResult:
        identity = read_pid_identity(event.get("pid"))
        fields = {
            "gtk_app_id": _norm(event.get("gtk_app_id")),
            "wm_class": _norm(event.get("wm_class")),
            "process": _norm(identity.get("comm") or identity.get("cmdline")),
            "cmdline": _norm(identity.get("cmdline")),
            "title": _norm(event.get("title")),
        }

        for rule in self.rules:
            app = str(rule.get("app", "")).strip()
            if not app or app not in self.app_vocab:
                continue

            for field in ("gtk_app_id", "wm_class", "process"):
                value = fields[field]
                if value and self._matches_exactish(value, _as_list(rule.get(field))):
                    return MappingResult(app=app, source=field, value=value)

            cmdline = fields["cmdline"]
            if cmdline and self._matches_contains(cmdline, _as_list(rule.get("cmdline_contains"))):
                return MappingResult(app=app, source="cmdline_contains", value=cmdline)

            title = fields["title"]
            if title and self._matches_contains(title, _as_list(rule.get("title_contains"))):
                return MappingResult(app=app, source="title_contains", value=title)

        return MappingResult(app=None, source="unmapped", value="")

    @staticmethod
    def _matches_exactish(value: str, patterns: list[str]) -> bool:
        value = _norm(value)
        for pattern in patterns:
            item = _norm(pattern)
            if item and (value == item or value.endswith("." + item)):
                return True
        return False

    @staticmethod
    def _matches_contains(value: str, patterns: list[str]) -> bool:
        value = _norm(value)
        return any(item and item in value for item in (_norm(pattern) for pattern in patterns))

