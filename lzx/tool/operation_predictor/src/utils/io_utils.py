"""Shared file I/O helpers for the first-stage prediction pipeline."""

from __future__ import annotations

import csv
import json
import pickle
from pathlib import Path
from typing import Any, Iterable


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def load_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: Any, path: str | Path) -> None:
    target = Path(path)
    if target.parent:
        ensure_dir(target.parent)
    with target.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_pickle(path: str | Path) -> Any:
    with Path(path).open("rb") as f:
        return pickle.load(f)


def save_pickle(data: Any, path: str | Path) -> None:
    target = Path(path)
    if target.parent:
        ensure_dir(target.parent)
    with target.open("wb") as f:
        pickle.dump(data, f)


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def save_csv(rows: Iterable[dict[str, Any]], path: str | Path, fieldnames: list[str] | None = None) -> None:
    target = Path(path)
    if target.parent:
        ensure_dir(target.parent)

    rows = list(rows)
    if fieldnames is None:
        fieldnames = list(rows[0].keys()) if rows else []

    with target.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
