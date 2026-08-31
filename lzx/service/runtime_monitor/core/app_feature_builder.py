"""Build per-application one-second feature rows — app_state_1s.csv."""

from __future__ import annotations

import datetime as dt
import math
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
    def __init__(
        self,
        session_id: str = "",
        test_slice: str = "",
        *,
        precise_file_events: bool = False,
    ) -> None:
        self.session_id = session_id
        self.test_slice = test_slice
        self.resource_collector = AppResourceCollector()
        self.prev_proc: dict[str, dict[str, int]] = {}
        self.prev_resource: dict[str, dict[str, Any]] = {}
        self.precise_file_events = bool(precise_file_events)
        # 跨窗口保存每个 App/文件最后一次成功 read 的结束 offset。它只保存
        # device+inode+数字位置，不保存原始路径，可识别跨秒连续读取和回绕读取。
        self.last_read_end: dict[tuple[str, int, int], int] = {}

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
            read_pattern = self._read_pattern_stats(record.app_id, app_events)
            read_latencies = self._latencies(app_events, {"read", "pread"})
            write_latencies = self._latencies(app_events, {"write", "pwrite"})

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
                    "file_device_cnt_1s": len({
                        (int(event.get("device_major", 0) or 0),
                         int(event.get("device_minor", 0) or 0))
                        for event in app_events
                        if int(event.get("file_identity_valid", 0) or 0)
                    }),
                    "read_ops_1s": self._count_many(app_events, {"read", "pread"}),
                    "write_ops_1s": self._count_many(app_events, {"write", "pwrite"}),
                    "read_requested_bytes_1s": self._sum_field(
                        app_events, {"read", "pread"}, "requested_size"
                    ),
                    "write_requested_bytes_1s": self._sum_field(
                        app_events, {"write", "pwrite"}, "requested_size"
                    ),
                    # eBPF 模式直接累计成功 read/pread 与 write/pwrite 的返回字节，
                    # 其窗口边界对应真实 syscall；关闭 eBPF 的兼容运行才使用
                    # /proc/<pid>/io 累计值差分（后者还可能包含非文件 I/O）。
                    "read_bytes_1s": (
                        self._sum_field(
                            app_events, {"read", "pread"}, "returned_size"
                        )
                        if self.precise_file_events
                        else _delta(proc, prev_proc, "read_bytes")
                    ),
                    "write_bytes_1s": (
                        self._sum_field(
                            app_events, {"write", "pwrite"}, "returned_size"
                        )
                        if self.precise_file_events
                        else _delta(proc, prev_proc, "write_bytes")
                    ),
                    "read_error_cnt_1s": self._error_count(
                        app_events, {"read", "pread"}
                    ),
                    "write_error_cnt_1s": self._error_count(
                        app_events, {"write", "pwrite"}
                    ),
                    "read_latency_ns_sum_1s": sum(read_latencies),
                    "read_latency_ns_max_1s": max(read_latencies, default=0),
                    "read_latency_ns_p95_1s": self._percentile(read_latencies, 0.95),
                    "write_latency_ns_sum_1s": sum(write_latencies),
                    "write_latency_ns_max_1s": max(write_latencies, default=0),
                    "write_latency_ns_p95_1s": self._percentile(write_latencies, 0.95),
                    "lseek_cnt_1s": self._count(app_events, event="lseek"),
                    "sequential_read_ops_1s": read_pattern["sequential"],
                    "cyclic_read_ops_1s": read_pattern["cyclic"],
                    "random_read_ops_1s": read_pattern["random"],
                    "unknown_offset_read_ops_1s": read_pattern["unknown"],
                    "read_access_pattern": read_pattern["label"],
                    "rchar_1s": _delta(proc, prev_proc, "rchar"),
                    "wchar_1s": _delta(proc, prev_proc, "wchar"),
                    "mmap_cnt_1s": self._count(app_events, event="mmap"),
                    "fsync_cnt_1s": self._count(app_events, event="fsync"),
                    "rename_cnt_1s": self._count(app_events, event="rename"),
                    "unique_inode_cnt_1s": len({
                        (event.get("device"), event.get("inode"))
                        for event in app_events
                        if event.get("inode")
                    }),
                    "page_access_cnt_1s": self._count(
                        app_events, event="page_access"
                    ),
                    "page_access_bytes_1s": self._sum_field(
                        app_events, {"page_access"}, "size"
                    ),
                    "eviction_cnt_1s": self._count(app_events, event="eviction"),
                    "eviction_bytes_1s": self._sum_field(
                        app_events, {"eviction"}, "size"
                    ),
                    "user_page_fault_cnt_1s": self._count(
                        app_events, event="page_fault"
                    ),
                    "attributed_block_io_cnt_1s": self._count(
                        app_events, event="block_io"
                    ),
                    "attributed_block_io_bytes_1s": self._sum_field(
                        app_events, {"block_io"}, "size"
                    ),
                    "offcpu_sleep_ns_1s": self._sum_field(
                        app_events, {"offcpu_sleep"}, "delay_ns"
                    ),
                    "offcpu_blocked_ns_1s": self._sum_field(
                        app_events, {"offcpu_blocked"}, "delay_ns"
                    ),
                    "iowait_ns_1s": self._sum_field(
                        app_events, {"iowait"}, "delay_ns"
                    ),
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

    @staticmethod
    def _count_many(events: list[dict[str, Any]], names: set[str]) -> int:
        return sum(1 for item in events if item.get("event") in names)

    @staticmethod
    def _sum_field(
        events: list[dict[str, Any]], names: set[str], field: str
    ) -> int:
        return sum(
            max(0, int(item.get(field, 0) or 0))
            for item in events
            if item.get("event") in names
        )

    @staticmethod
    def _error_count(events: list[dict[str, Any]], names: set[str]) -> int:
        return sum(
            1 for item in events
            if item.get("event") in names and int(item.get("result", 0) or 0) < 0
        )

    @staticmethod
    def _latencies(events: list[dict[str, Any]], names: set[str]) -> list[int]:
        return [
            max(0, int(item.get("latency_ns", 0) or 0))
            for item in events
            if item.get("event") in names
        ]

    @staticmethod
    def _percentile(values: list[int], quantile: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        index = max(0, math.ceil(len(ordered) * quantile) - 1)
        return int(ordered[index])

    def _read_pattern_stats(
        self, app: str, events: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """按真实 offset 连续性区分顺序、回绕循环、跳跃随机读取。"""
        counts = {"sequential": 0, "cyclic": 0, "random": 0, "unknown": 0}
        reads = sorted(
            (
                item for item in events
                if item.get("event") in {"read", "pread"}
            ),
            key=lambda item: int(item.get("ts_ns", 0) or 0),
        )
        for item in reads:
            returned = max(0, int(item.get("returned_size", 0) or 0))
            valid = bool(
                int(item.get("offset_valid", 0) or 0)
                and int(item.get("file_identity_valid", 0) or 0)
                and int(item.get("inode", 0) or 0)
                and returned > 0
            )
            if not valid:
                counts["unknown"] += 1
                continue
            key = (
                str(app).upper(),
                int(item.get("device", 0) or 0),
                int(item.get("inode", 0) or 0),
            )
            offset = int(item.get("offset", 0) or 0)
            previous_end = self.last_read_end.get(key)
            if previous_end is None:
                counts["unknown"] += 1
            elif offset == previous_end:
                counts["sequential"] += 1
            elif offset < previous_end:
                counts["cyclic"] += 1
            else:
                counts["random"] += 1
            self.last_read_end[key] = offset + returned

        known = sum(counts[name] for name in ("sequential", "cyclic", "random"))
        if not reads:
            label = "IDLE"
        elif known == 0:
            label = "UNKNOWN"
        else:
            dominant = max(
                ("sequential", "cyclic", "random"), key=lambda name: counts[name]
            )
            label = dominant.upper() if counts[dominant] / known >= 0.6 else "MIXED"
        return {**counts, "label": label}

    @staticmethod
    def _sum_size(events: list[dict[str, Any]], event: str) -> int:
        return sum(
            max(0, int(item.get("size", 0) or 0))
            for item in events
            if item.get("event") == event
        )
