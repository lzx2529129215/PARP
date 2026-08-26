"""记录 Online Causal Markov 对 debugfs 的实时写入，不改变底层写入。"""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any


FIELDS = [
    "session_id", "timestamp_ns", "event_type", "command", "status", "error",
    "app_key", "app_id", "cgroup_id", "workload_id", "workload_name",
    "prev_workload_id", "current_workload_id", "next_workload_ids", "confidences",
    "boost_levels", "debugfs_path",
]


class OnlineMarkovDebugfsAuditWriter:
    def __init__(self, delegate: Any, session_id: str, model_dir: Path) -> None:
        self.delegate = delegate
        self.session_id = session_id
        self.path = model_dir / "workload_markov_online_debugfs_writes.csv"
        self._file = self.path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDS)
        self._writer.writeheader()
        self._file.flush()
        self._closed = False

    def write_workload_update(self, **kwargs: Any) -> tuple[str, str]:
        result = self.delegate.write_workload_update(**kwargs)
        status, error = result
        values = dict(kwargs)
        values["command"] = "workload update {cgroup_id} {app_id} {workload_id}".format(**{
            "cgroup_id": values.get("cgroup_id", ""), "app_id": values.get("app_id", ""),
            "workload_id": values.get("workload_id", ""),
        })
        self._record("workload_update", status, error, values)
        return result

    def write_markov_set(self, **kwargs: Any) -> tuple[str, str]:
        result = self.delegate.write_markov_set(**kwargs)
        status, error = result
        values = dict(kwargs)
        entries = list(values.get("entries", []))
        values["command"] = "markov set {app_id} {prev_workload_id} {current_workload_id} {entries}".format(
            app_id=values.get("app_id", ""), prev_workload_id=values.get("prev_workload_id", ""),
            current_workload_id=values.get("current_workload_id", ""), entries=" ".join(
                f"{e.get('next_workload_id', '')} {e.get('confidence', '')} {e.get('boost_level', '')}" for e in entries
            ),
        )
        self._record("markov_set", status, error, values)
        return result

    def _record(self, event_type: str, status: str, error: str, values: dict[str, Any]) -> None:
        self._writer.writerow({
            "session_id": self.session_id, "timestamp_ns": time.time_ns(),
            "event_type": event_type, "command": values.get("command", ""), "status": status, "error": error,
            **{field: values.get(field, "") for field in FIELDS if field not in {
                "session_id", "timestamp_ns", "event_type", "command", "status", "error", "debugfs_path"
            }},
            "debugfs_path": str(getattr(self.delegate, "debugfs_path", "")),
        })
        self._file.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._file.flush()
        self._file.close()
        self._closed = True
