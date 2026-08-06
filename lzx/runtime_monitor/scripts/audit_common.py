#!/usr/bin/env python3
"""统一审计脚本的无副作用公共工具。"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def resolve_input_path(value: str | Path, project_root: Path = PROJECT_ROOT) -> Path:
    """解析输入路径；相对路径只相对于项目根目录，不做 fallback。"""
    path = Path(value)
    return (path if path.is_absolute() else project_root / path).resolve()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def read_csv_with_fields(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def integer(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value or "").strip()))
    except (TypeError, ValueError):
        return default


def number(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value or "").strip())
    except (TypeError, ValueError):
        return default


def parse_ns(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            # Runtime Monitor 的无时区时间来自 Asia/Shanghai。
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
        return int(parsed.timestamp() * 1_000_000_000)
    except ValueError:
        return 0


def row_ns(row: dict[str, Any]) -> int:
    for key in ("timestamp_ns", "prediction_time_ns", "actual_next_time_ns", "timestamp", "sample_timestamp", "wall_time", "time"):
        value = row.get(key)
        if value not in (None, ""):
            parsed = parse_ns(value)
            if parsed:
                return parsed
    return 0


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def bool_text(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "ok", "pass"}


def event_type(row: dict[str, Any]) -> str:
    return str(row.get("event_type") or row.get("write_type") or row.get("type") or "").strip()


def status_ok(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "").strip().lower() in {"ok", "success", "pass"}


def managed_app_maps(config_path: Path, vocab_path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    vocab = json.loads(vocab_path.read_text(encoding="utf-8"))
    by_key: dict[str, dict[str, Any]] = {}
    by_vocab: dict[str, dict[str, Any]] = {}
    for item in config.get("apps", []):
        app = dict(item)
        app["runtime_app_id"] = app.get("app_id", "")
        app["model_app_id"] = vocab.get(app.get("vocab_name"), "")
        by_key[str(app.get("app_key", ""))] = app
        by_vocab[str(app.get("vocab_name", ""))] = app
    return by_key, by_vocab
