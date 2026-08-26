"""只读 cgroup workload 实时采样，并复用现有 workload classifier。"""

from __future__ import annotations

import csv
import time
from pathlib import Path
from typing import Any

from scripts.collect_cgroup_memory_workload import (
    DELTA_FIELDS,
    EVENT_FIELDS,
    RAW_FIELDS,
    STAT_FIELDS,
    ScopeSample,
    delta_row,
    raw_row,
    resolve_user_slice_path,
    sample_scope,
)
from core.workload_classifier import WORKLOAD_NAMES, classify_metrics


LIVE_METRIC_FIELDS = list(dict.fromkeys([
    "session_id", "timestamp_ns", "timestamp", "scope_name", "app_key", "app_id",
    "cgroup_id", "cgroup_path", "status", "error",
    *[field for field in RAW_FIELDS if field not in {"session_id", "timestamp", "scope_name", "status", "error"}],
    *[field for field in DELTA_FIELDS if field not in {"session_id", "timestamp", "scope_name", "status", "error"}],
]))

CLASSIFIER_FIELDS = [
    "session_id", "timestamp_ns", "timestamp", "app_key", "app_id", "scope_name", "cgroup_id",
    "requested_phase", "requested_workload_id", "observed_workload_id", "observed_workload_name",
    "classifier_rule", "state_changed", "status", "error",
    "memory_current", "memory_current_delta", "anon", "anon_delta", "file", "file_delta",
    "pgfault", "pgfault_delta", "pgmajfault", "pgmajfault_delta",
    "workingset_refault_file", "workingset_refault_file_delta", "workingset_refault_anon",
    "workingset_refault_anon_delta", "pgscan", "pgscan_delta", "pgsteal", "pgsteal_delta",
]

REQUESTED_OBSERVED_FIELDS = [
    "session_id", "timestamp_ns", "timestamp", "scope_name", "app_key", "app_id", "cgroup_id",
    "requested_phase", "requested_workload_id", "observed_workload_id", "observed_workload_name",
    "state_changed", "source",
]


def _int(value: Any) -> int:
    try:
        return int(float(str(value or 0)))
    except (TypeError, ValueError):
        return 0


class OnlineCgroupWorkloadSampler:
    """在 monitor 的采样时钟内读取 scope，并把 observed 状态交给回调。"""

    def __init__(self, *, session_id: str, model_dir: Path, slice_name: str,
                 scopes: list[str], runtime_scope: Any,
                 on_observed: Any | None = None) -> None:
        self.session_id = session_id
        self.slice_name = slice_name
        self.scopes = list(scopes)
        self.runtime_scope = runtime_scope
        self.on_observed = on_observed
        self.previous: dict[str, ScopeSample] = {}
        self.previous_workload: dict[str, int] = {}
        self.metric_rows = 0
        self.classifier_rows = 0
        self.state_change_rows = 0
        self._metrics_file = (model_dir / "cgroup_metrics_1s.csv").open("w", encoding="utf-8", newline="")
        self._classifier_file = (model_dir / "workload_classifier_results_1s.csv").open("w", encoding="utf-8", newline="")
        self._changes_file = (model_dir / "workload_state_changes.csv").open("w", encoding="utf-8", newline="")
        self._requested_file = (model_dir / "requested_vs_observed_workloads.csv").open("w", encoding="utf-8", newline="")
        self._metrics_writer = csv.DictWriter(self._metrics_file, fieldnames=LIVE_METRIC_FIELDS, extrasaction="ignore")
        self._classifier_writer = csv.DictWriter(self._classifier_file, fieldnames=CLASSIFIER_FIELDS, extrasaction="ignore")
        self._changes_writer = csv.DictWriter(self._changes_file, fieldnames=CLASSIFIER_FIELDS, extrasaction="ignore")
        self._requested_writer = csv.DictWriter(self._requested_file, fieldnames=REQUESTED_OBSERVED_FIELDS, extrasaction="ignore")
        self._metrics_writer.writeheader(); self._classifier_writer.writeheader()
        self._changes_writer.writeheader(); self._requested_writer.writeheader()
        for f in (self._metrics_file, self._classifier_file, self._changes_file, self._requested_file):
            f.flush()

    def sample(self, timestamp_ns: int | None = None) -> None:
        ts_ns = int(timestamp_ns or time.time_ns())
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts_ns / 1_000_000_000))
        _, parent_path, parent_error = resolve_user_slice_path(self.slice_name)
        for scope_name in self.scopes:
            sample = sample_scope(self.session_id, timestamp, parent_path, scope_name, parent_error)
            delta = delta_row(sample, self.previous.get(scope_name))
            identity = self._identity(scope_name)
            cgroup_id = self._cgroup_id(sample)
            metric = {"session_id": self.session_id, "timestamp_ns": ts_ns, "timestamp": timestamp,
                      "scope_name": scope_name, "app_key": identity[0], "app_id": identity[1],
                      "cgroup_id": cgroup_id, "cgroup_path": sample.cgroup_path,
                      **raw_row(sample), **delta}
            self._metrics_writer.writerow(metric)
            self.metric_rows += 1
            values = {field: _int(delta.get(f"{field}_delta", 0)) for field in (
                "memory_current", "anon", "file", "pgfault", "pgmajfault",
                "workingset_refault_file", "workingset_refault_anon")}
            classifier_values = {f"{field}_delta": value for field, value in values.items()}
            observed_id = ""
            observed_name = ""
            reason = ""
            changed = False
            if sample.status == "ok":
                observed_id, reason = classify_metrics(classifier_values)
                observed_name = WORKLOAD_NAMES[observed_id]
                changed = scope_name not in self.previous_workload or self.previous_workload[scope_name] != observed_id
                self.previous_workload[scope_name] = observed_id
                self.previous[sample.scope_name] = sample
            row = {
                "session_id": self.session_id, "timestamp_ns": ts_ns, "timestamp": timestamp,
                "app_key": identity[0], "app_id": identity[1], "scope_name": scope_name,
                "cgroup_id": cgroup_id, "requested_phase": "", "requested_workload_id": "",
                "observed_workload_id": observed_id, "observed_workload_name": observed_name,
                "classifier_rule": reason, "state_changed": str(changed).lower(),
                "status": sample.status, "error": sample.error,
                "memory_current": _int(sample.values.get("memory_current")),
                "memory_current_delta": values["memory_current"],
                "anon": _int(sample.values.get("anon")), "anon_delta": values["anon"],
                "file": _int(sample.values.get("file")), "file_delta": values["file"],
                "pgfault": _int(sample.values.get("pgfault")), "pgfault_delta": values["pgfault"],
                "pgmajfault": _int(sample.values.get("pgmajfault")), "pgmajfault_delta": values["pgmajfault"],
                "workingset_refault_file": _int(sample.values.get("workingset_refault_file")),
                "workingset_refault_file_delta": values["workingset_refault_file"],
                "workingset_refault_anon": _int(sample.values.get("workingset_refault_anon")),
                "workingset_refault_anon_delta": values["workingset_refault_anon"],
                "pgscan": 0, "pgscan_delta": 0, "pgsteal": 0, "pgsteal_delta": 0,
            }
            self._classifier_writer.writerow(row)
            self._requested_writer.writerow({**{field: row.get(field, "") for field in REQUESTED_OBSERVED_FIELDS}, "source": "classifier"})
            self.classifier_rows += 1
            if changed:
                self._changes_writer.writerow(row)
                self.state_change_rows += 1
            # Dual REENTRY needs every valid classifier sample, including a
            # stable workload. The callback still rejects missing app/cgroup.
            if sample.status == "ok" and self.on_observed is not None and cgroup_id:
                self.on_observed(row)
        for f in (self._metrics_file, self._classifier_file, self._changes_file, self._requested_file):
            f.flush()

    def _identity(self, scope_name: str) -> tuple[str, int | str]:
        if self.runtime_scope is None:
            return "", ""
        for app in self.runtime_scope.apps:
            if app.scope_name == scope_name:
                return app.app_key, app.app_id
        return "", ""

    @staticmethod
    def _cgroup_id(sample: ScopeSample) -> int | str:
        if sample.status != "ok":
            return ""
        try:
            return sample.cgroup_path and Path(sample.cgroup_path).stat().st_ino or ""
        except OSError:
            return ""

    def close(self) -> None:
        for f in (self._metrics_file, self._classifier_file, self._changes_file, self._requested_file):
            f.flush(); f.close()
