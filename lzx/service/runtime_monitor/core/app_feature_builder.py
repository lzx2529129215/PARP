"""Build per-application one-second feature rows — app_state_1s.csv."""

from __future__ import annotations

import datetime as dt
from typing import Any

from collectors.cgroup import AppResourceCollector
from collectors.foreground import ForegroundState
from collectors.process import ProcessSample, aggregate_procfs
from core.app_registry import AppRecord


def _delta(now: dict[str, int], prev: dict[str, int] | None, key: str) -> int:
    value = int(now.get(key, 0))
    if not prev:
        return 0
    return max(0, value - int(prev.get(key, 0)))


def _parse_label_app(label: str) -> str:
    """Extract target app name from a label like WPS_LAUNCH or APP_SWITCH_QQ."""
    if not label:
        return ""
    upper = label.upper()
    for candidate in ("WPS", "QQ", "FILES", "FIREFOX"):
        if candidate in upper:
            return candidate
    return ""


class AppFeatureBuilder:
    def __init__(self, session_id: str = "", test_slice: str = "") -> None:
        self.session_id = session_id
        self.test_slice = test_slice
        self.resource_collector = AppResourceCollector()
        self.prev_proc: dict[str, dict[str, int]] = {}
        self.prev_resource: dict[str, dict[str, Any]] = {}

    def build_rows(
        self,
        *,
        feature_window_id: int,
        window_start_ns: int,
        window_end_ns: int,
        records: list[AppRecord],
        samples: list[ProcessSample],
        file_events: list[dict[str, Any]],
        foreground: ForegroundState,
        operation_contexts: dict[str, dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        timestamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        operation_contexts = operation_contexts or {}
        samples_by_app: dict[str, list[ProcessSample]] = {}
        events_by_app: dict[str, list[dict[str, Any]]] = {}
        for sample in samples:
            samples_by_app.setdefault(sample.app_id, []).append(sample)
        for event in file_events:
            events_by_app.setdefault(str(event.get("app", "")), []).append(event)

        rows: list[dict[str, Any]] = []
        for record in records:
            app_samples = samples_by_app.get(record.app_id, [])
            app_events = events_by_app.get(record.app_id, [])
            proc = aggregate_procfs(app_samples)
            resource = self.resource_collector.sample(app_samples)
            prev_proc = self.prev_proc.get(record.app_id)
            prev_resource = self.prev_resource.get(record.app_id)
            op = operation_contexts.get(record.app_id, {})

            # Compute label_app from state_label or manual_label
            raw_label = op.get("state_label") or op.get("manual_label", "")
            label_app = op.get("label_app") or _parse_label_app(raw_label)

            rows.append(
                {
                    "session_id": self.session_id,
                    "feature_window_id": feature_window_id,
                    "window_start_ns": window_start_ns,
                    "window_end_ns": window_end_ns,
                    "timestamp": timestamp,
                    "app_id": record.app_id,
                    "app_display_name": record.display_name or record.app_id,
                    "is_open": int(record.is_open),
                    "is_foreground": int(record.is_foreground),
                    "is_label_target_app": "1" if (label_app and label_app.upper() == record.app_id.upper()) else "0",
                    "closed": int(record.closed),
                    "pid_count": len(record.pid_set),
                    "pids": "|".join(str(pid) for pid in sorted(record.pid_set)),
                    "tgids": "|".join(str(tgid) for tgid in sorted(record.tgid_set)),
                    "comm": record.comm,
                    "exe_path": record.exe_path,
                    "cmdline_hash": record.cmdline_hash,
                    "app_cgroup_unit": record.app_cgroup_unit,
                    "app_cgroup_path": record.cgroup_path,
                    "test_slice": self.test_slice,
                    "in_test_slice": int(record.in_test_slice),
                    "open_cnt_1s": self._count(app_events, event="openat"),
                    "read_bytes_1s": _delta(proc, prev_proc, "read_bytes"),
                    "write_bytes_1s": _delta(proc, prev_proc, "write_bytes"),
                    "rchar_1s": _delta(proc, prev_proc, "rchar"),
                    "wchar_1s": _delta(proc, prev_proc, "wchar"),
                    "mmap_cnt_1s": self._count(app_events, event="mmap"),
                    "fsync_cnt_1s": self._count(app_events, event="fsync"),
                    "rename_cnt_1s": self._count(app_events, event="rename"),
                    "unique_inode_cnt_1s": len({event.get("inode") for event in app_events if event.get("inode")}),
                    "docx_open_cnt_1s": self._count(app_events, event="openat", ext="docx"),
                    "tmp_open_cnt_1s": self._count(app_events, event="openat", ext="tmp"),
                    "so_open_cnt_1s": self._count(app_events, event="openat", ext="so"),
                    "font_open_cnt_1s": self._count_exts(app_events, event="openat", exts={"ttf", "otf"}),
                    "pdf_open_cnt_1s": self._count(app_events, event="openat", ext="pdf"),
                    "mem_current": resource.get("memory.current", 0),
                    "anon": resource.get("memory.stat.anon", 0),
                    "file": resource.get("memory.stat.file", 0),
                    "active_file": resource.get("memory.stat.active_file", 0),
                    "inactive_file": resource.get("memory.stat.inactive_file", 0),
                    "pgmajfault_delta": _delta(
                        {"v": int(resource.get("memory.stat.pgmajfault", 0))},
                        {"v": int(prev_resource.get("memory.stat.pgmajfault", 0))} if prev_resource else None,
                        "v",
                    ),
                    "refault_file_delta": _delta(
                        {"v": int(resource.get("memory.stat.workingset_refault_file", 0))},
                        {"v": int(prev_resource.get("memory.stat.workingset_refault_file", 0))}
                        if prev_resource
                        else None,
                        "v",
                    ),
                    "current_operation_label": op.get("operation_label", ""),
                    "current_operation_app": op.get("operation_app", ""),
                    "state_label": op.get("state_label", ""),
                    "manual_label": op.get("manual_label", ""),
                    "label_app": label_app,
                }
            )
            self.prev_proc[record.app_id] = dict(proc)
            self.prev_resource[record.app_id] = dict(resource)
        return rows

    @staticmethod
    def _count(events: list[dict[str, Any]], event: str, ext: str | None = None) -> int:
        return sum(1 for item in events if item.get("event") == event and (ext is None or item.get("ext") == ext))

    @staticmethod
    def _count_exts(events: list[dict[str, Any]], event: str, exts: set[str]) -> int:
        return sum(1 for item in events if item.get("event") == event and item.get("ext") in exts)
