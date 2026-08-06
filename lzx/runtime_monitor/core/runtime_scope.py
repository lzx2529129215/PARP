"""Runtime app scope configuration for Runtime Monitor."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeApp:
    app_key: str
    app_id: int
    vocab_name: str
    scope_name: str
    unit_name: str
    workload_enabled: bool
    prediction_enabled: bool
    window_keywords: list[str]
    process_keywords: list[str]


@dataclass(frozen=True)
class RuntimeAppScope:
    path: Path
    slice_name: str
    apps: list[RuntimeApp]
    vocab_warnings: list[str]

    @property
    def target_apps(self) -> list[str]:
        return [app.app_key for app in self.apps]

    @property
    def workload_scopes(self) -> list[str]:
        return [app.scope_name for app in self.apps if app.workload_enabled and app.scope_name]

    @property
    def prediction_apps(self) -> list[str]:
        return [app.app_key for app in self.apps if app.prediction_enabled]

    @property
    def app_key_to_vocab_name(self) -> dict[str, str]:
        return {app.app_key: app.vocab_name for app in self.apps if app.prediction_enabled}

    @property
    def app_key_to_app_id(self) -> dict[str, int]:
        return {app.app_key: app.app_id for app in self.apps}

    @property
    def vocab_name_to_app_id(self) -> dict[str, int]:
        return {app.vocab_name: app.app_id for app in self.apps if app.prediction_enabled}

    @property
    def app_key_to_scope_name(self) -> dict[str, str]:
        return {app.app_key: app.scope_name for app in self.apps}

    @property
    def prediction_enabled_app_ids(self) -> set[int]:
        return {app.app_id for app in self.apps if app.prediction_enabled}

    @property
    def window_keywords(self) -> dict[str, list[str]]:
        return {app.app_key: list(app.window_keywords) for app in self.apps}

    def as_process_mapper_config(self, base_config: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base_config)
        existing_apps = base_config.get("apps", {})
        apps_config: dict[str, Any] = dict(existing_apps) if isinstance(existing_apps, dict) else {}
        for app in self.apps:
            previous = apps_config.get(app.app_key, {})
            exclude_keywords = previous.get("exclude_keywords", []) if isinstance(previous, dict) else []
            apps_config[app.app_key] = {
                "keywords": list(app.process_keywords),
                "exclude_keywords": list(exclude_keywords),
            }
        merged["apps"] = apps_config
        return merged

    def summary_lines(self) -> list[str]:
        mapping = ", ".join(f"{key}->{value}" for key, value in self.app_key_to_vocab_name.items())
        app_id_mapping = ", ".join(
            f"{app.app_key}->{app.app_id}->{app.vocab_name}->{app.scope_name}"
            for app in self.apps
        )
        lines = [
            f"- app_scope_config: `{self.path}`",
            f"- loaded_apps: {', '.join(self.target_apps)}",
            f"- workload_scopes: {', '.join(self.workload_scopes)}",
            f"- prediction_apps: {', '.join(self.prediction_apps)}",
            f"- app_key_to_vocab_name: {mapping}",
            f"- app_key_to_app_id_vocab_scope: {app_id_mapping}",
        ]
        if self.vocab_warnings:
            lines.append("- app_scope_vocab_warnings:")
            lines.extend(f"  - {warning}" for warning in self.vocab_warnings)
        else:
            lines.append("- app_scope_vocab_warnings: none")
        return lines


def load_runtime_app_scope(path: str | Path, vocab_path: str | Path | None = None) -> RuntimeAppScope:
    config_path = Path(path).expanduser().resolve()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    apps: list[RuntimeApp] = []
    for raw in data.get("apps", []):
        if not isinstance(raw, dict):
            continue
        try:
            app_id = int(raw.get("app_id", 0) or 0)
        except (TypeError, ValueError):
            app_id = 0
        apps.append(
            RuntimeApp(
                app_key=str(raw.get("app_key", "")).strip(),
                app_id=app_id,
                vocab_name=str(raw.get("vocab_name", "")).strip(),
                scope_name=str(raw.get("scope_name", "")).strip(),
                unit_name=str(raw.get("unit_name", "")).strip(),
                workload_enabled=bool(raw.get("workload_enabled", False)),
                prediction_enabled=bool(raw.get("prediction_enabled", False)),
                window_keywords=[str(item) for item in raw.get("window_keywords", [])],
                process_keywords=[str(item) for item in raw.get("process_keywords", [])],
            )
        )
    apps = [app for app in apps if app.app_key]
    app_id_warnings = _validate_app_ids(apps)
    scope = RuntimeAppScope(
        path=config_path,
        slice_name=str(data.get("slice", "")).strip(),
        apps=apps,
        vocab_warnings=app_id_warnings,
    )
    return RuntimeAppScope(
        path=scope.path,
        slice_name=scope.slice_name,
        apps=scope.apps,
        vocab_warnings=scope.vocab_warnings + _validate_vocab(scope, vocab_path),
    )


def _validate_app_ids(apps: list[RuntimeApp]) -> list[str]:
    warnings: list[str] = []
    seen: dict[int, str] = {}
    for app in apps:
        if app.app_id <= 0:
            warnings.append(f"{app.app_key} app_id must be a positive integer")
            continue
        if app.app_id in seen:
            warnings.append(f"duplicate app_id {app.app_id}: {seen[app.app_id]} and {app.app_key}")
        else:
            seen[app.app_id] = app.app_key
    return warnings


def _validate_vocab(scope: RuntimeAppScope, vocab_path: str | Path | None) -> list[str]:
    if not vocab_path:
        return []
    path = Path(vocab_path).expanduser()
    try:
        vocab = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"failed to read app vocab {path}: {exc}"]
    warnings: list[str] = []
    for app in scope.apps:
        if app.prediction_enabled and app.vocab_name not in vocab:
            warnings.append(f"{app.app_key} vocab_name `{app.vocab_name}` not found in {path}")
    return warnings
