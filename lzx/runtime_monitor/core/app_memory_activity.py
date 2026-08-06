"""Read-only App-level memory activity collection for Test4.

The collector resolves each configured application scope underneath the Test4
slice, reads its ``cgroup.procs``, and aggregates ``smaps_rollup`` for every
live PID in that cgroup.  It deliberately never uses ``clear_refs``: the
Referenced/RSS ratio is only a conservative low-activity heuristic.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from collectors.process import ProcessSample
from core.memory_shadow import read_cgroup_memory, read_smaps_rollup
from core.writer import CsvWriter


FIELDS = [
    "timestamp_ns", "timestamp", "app_key", "app_id", "memcg_path",
    "foreground_state", "running_state", "process_count", "rss_bytes",
    "pss_bytes", "referenced_bytes", "swap_bytes", "memcg_current_bytes",
    "referenced_rss_ratio", "referenced_rss_ema", "low_activity_window_count",
    "sample_latency_us", "sample_status", "error",
]


@dataclass(frozen=True)
class AppActivity:
    app_key: str
    app_id: int
    memcg_path: str
    foreground_state: str
    running_state: str
    process_count: int
    rss_bytes: int
    pss_bytes: int
    referenced_bytes: int
    swap_bytes: int
    memcg_current_bytes: int
    ratio: float | None
    ema: float | None
    low_windows: int
    status: str
    error: str
    timestamp_ns: int
    latency_us: int = 0


def _read_cgroup_pids(path: Path) -> tuple[list[int], str]:
    try:
        result = sorted({int(line) for line in (path / "cgroup.procs").read_text().split()})
        return result, ""
    except (OSError, ValueError) as exc:
        return [], str(exc)


class AppMemoryActivityCollector:
    def __init__(
        self,
        *,
        session_id: str,
        output_dir: Path,
        runtime_scope: Any,
        interval_s: float = 0.5,
        ema_rho: float = 0.7,
        activity_threshold: float = 0.10,
        required_low_windows: int = 3,
        min_rss_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self.session_id = session_id
        self.scope = runtime_scope
        self.interval_ns = max(100_000_000, int(interval_s * 1_000_000_000))
        self.rho = ema_rho
        self.threshold = activity_threshold
        self.required = required_low_windows
        self.min_rss = min_rss_bytes
        self.ids = dict(getattr(runtime_scope, "app_key_to_app_id", {}) or {})
        self.scope_by_app = dict(getattr(runtime_scope, "app_key_to_scope_name", {}) or {})
        self.slice_path = self._resolve_slice_path(str(getattr(runtime_scope, "slice_name", "") or ""))
        self.last: dict[str, AppActivity] = {}
        self.next_ns = 0
        self.ema: dict[str, float] = {}
        self.low: dict[str, int] = {}
        self.writer = CsvWriter(Path(output_dir) / "activity" / "app_memory_activity.csv", FIELDS)

    @staticmethod
    def _resolve_slice_path(slice_name: str) -> Path:
        if not slice_name:
            return Path("/sys/fs/cgroup")
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", slice_name, "-p", "ControlGroup", "--value"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                return Path("/sys/fs/cgroup") / result.stdout.strip().lstrip("/")
        except (OSError, subprocess.TimeoutExpired):
            pass
        return Path("/sys/fs/cgroup") / slice_name

    def sample_if_due(
        self,
        samples: list[ProcessSample],
        foreground: str,
        now_ns: int | None = None,
        *,
        force: bool = False,
    ) -> dict[str, AppActivity]:
        now = int(now_ns or time.time_ns())
        if not force and self.next_ns and now < self.next_ns:
            return dict(self.last)
        self.next_ns = now + self.interval_ns
        samples_by_app: dict[str, list[ProcessSample]] = {key: [] for key in self.ids}
        for sample in samples:
            if sample.app_id in samples_by_app:
                samples_by_app[sample.app_id].append(sample)
        for app, app_samples in samples_by_app.items():
            activity = self._read(app, app_samples, foreground, now)
            self.last[app] = activity
            self.writer.write_row({
                "timestamp_ns": now,
                "timestamp": dt.datetime.fromtimestamp(now / 1e9).isoformat(timespec="milliseconds"),
                "app_key": app,
                "app_id": activity.app_id,
                "memcg_path": activity.memcg_path,
                "foreground_state": activity.foreground_state,
                "running_state": activity.running_state,
                "process_count": activity.process_count,
                "rss_bytes": activity.rss_bytes,
                "pss_bytes": activity.pss_bytes,
                "referenced_bytes": activity.referenced_bytes,
                "swap_bytes": activity.swap_bytes,
                "memcg_current_bytes": activity.memcg_current_bytes,
                "referenced_rss_ratio": "" if activity.ratio is None else activity.ratio,
                "referenced_rss_ema": "" if activity.ema is None else activity.ema,
                "low_activity_window_count": activity.low_windows,
                "sample_latency_us": activity.latency_us,
                "sample_status": activity.status,
                "error": activity.error,
            })
        return dict(self.last)

    def _scope_path(self, app: str, samples: list[ProcessSample]) -> tuple[Path | None, str]:
        expected_scope = self.scope_by_app.get(app, "")
        if expected_scope:
            path = self.slice_path / expected_scope
            if path.is_dir():
                return path, ""
        for sample in samples:
            cgroup_path = str(sample.identity.cgroup_path or "")
            path = Path("/sys/fs/cgroup") / cgroup_path.lstrip("/")
            if path.is_dir() and (not expected_scope or path.name == expected_scope):
                return path, ""
        if expected_scope:
            return None, f"configured scope missing: {self.slice_path / expected_scope}"
        return None, "no configured scope or matching process cgroup"

    def _read(
        self,
        app: str,
        samples: list[ProcessSample],
        foreground: str,
        now: int,
    ) -> AppActivity:
        started = time.perf_counter_ns()
        errors: list[str] = []
        path, path_error = self._scope_path(app, samples)
        if path_error:
            errors.append(path_error)
        pids, pid_error = _read_cgroup_pids(path) if path is not None else ([], "")
        if pid_error:
            errors.append(pid_error)
        totals = {key: 0 for key in ("Rss", "Pss", "Referenced", "Swap")}
        for pid in pids:
            values, error = read_smaps_rollup(pid)
            if error:
                errors.append(f"pid={pid}:{error}")
                continue
            for key in totals:
                totals[key] += values.get(key, 0)
        memory, memory_error = read_cgroup_memory(path) if path is not None else ({}, "")
        if memory_error:
            errors.append(memory_error)
        foreground_state = "FOREGROUND" if app == foreground else "BACKGROUND"
        running_state = (
            "FOREGROUND" if pids and foreground_state == "FOREGROUND"
            else "RUNNING_BACKGROUND" if pids else "NOT_RUNNING"
        )
        ratio = totals["Referenced"] / totals["Rss"] if totals["Rss"] > 0 else None
        ema = None
        if ratio is not None:
            ema = self.rho * self.ema.get(app, ratio) + (1 - self.rho) * ratio
            self.ema[app] = ema
        status = "OK" if pids and not errors else "PARTIAL" if pids else "UNAVAILABLE"
        inactive = (
            status == "OK"
            and foreground_state == "BACKGROUND"
            and totals["Rss"] >= self.min_rss
            and ema is not None
            and ema < self.threshold
        )
        self.low[app] = self.low.get(app, 0) + 1 if inactive else 0
        return AppActivity(
            app_key=app,
            app_id=self.ids.get(app, 0),
            memcg_path=str(path or ""),
            foreground_state=foreground_state,
            running_state=running_state,
            process_count=len(pids),
            rss_bytes=totals["Rss"],
            pss_bytes=totals["Pss"],
            referenced_bytes=totals["Referenced"],
            swap_bytes=totals["Swap"],
            memcg_current_bytes=int(memory.get("memcg_current_bytes", 0)),
            ratio=ratio,
            ema=ema,
            low_windows=self.low[app],
            status=status,
            error=";".join(errors),
            timestamp_ns=now,
            latency_us=int((time.perf_counter_ns() - started) / 1000),
        )

    def close(self) -> None:
        self.writer.close()
