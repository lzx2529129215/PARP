"""Small flushing CSV writer wrappers."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable


class CsvWriter:
    def __init__(self, path: str | Path, fieldnames: list[str]) -> None:
        self.path = Path(path)
        self.fieldnames = fieldnames
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=fieldnames)
        self._writer.writeheader()
        self._file.flush()

    def write_row(self, row: dict[str, Any]) -> None:
        self._writer.writerow({field: row.get(field, "") for field in self.fieldnames})
        self._file.flush()

    def write_rows(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            self.write_row(row)

    def close(self) -> None:
        self._file.flush()
        self._file.close()

    def __enter__(self) -> "CsvWriter":
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()
