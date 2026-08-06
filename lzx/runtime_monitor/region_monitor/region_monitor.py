from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime_monitor.core.runtime_scope import load_runtime_app_scope
from runtime_monitor.region_monitor.capability_probe import probe_capabilities, write_capability_report
from runtime_monitor.region_monitor.cgroup_feature_adapter import CgroupFeatureAdapter, resolve_user_slice_path
from runtime_monitor.region_monitor.cgroup_pid_tracker import CgroupPidTracker
from runtime_monitor.region_monitor.config import load_region_monitor_config
from runtime_monitor.region_monitor.damon_controller import DamonController
from runtime_monitor.region_monitor.models import CapabilityStatus, ProcessInfo
from runtime_monitor.region_monitor.process_role_resolver import ProcessRoleResolver
from runtime_monitor.region_monitor.region_vocab import RegionVocab
from runtime_monitor.region_monitor.tracefs_event_source import TracefsDamonEventSource
from runtime_monitor.region_monitor.vma_interval_index import VMAIntervalIndex
from runtime_monitor.region_monitor.vma_parser import read_proc_maps
from runtime_monitor.region_monitor.window_aggregator import WindowAggregator


STOP_REQUESTED = False


def _request_stop(_signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


class RegionMonitor:
    def __init__(
        self,
        *,
        session_dir: Path,
        config_path: Path,
        app_scope_config: Path,
        duration_s: float,
        slice_path: Path | None = None,
    ) -> None:
        self.session_dir = session_dir
        self.output_dir = session_dir / "region_monitor"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = load_region_monitor_config(config_path)
        self.runtime_scope = load_runtime_app_scope(app_scope_config)
        self.duration_s = duration_s
        self.slice_path = slice_path
        self.errors_file = (self.output_dir / "region_monitor_errors.jsonl").open("a", encoding="utf-8")
        self.lifecycle_file = (self.output_dir / "process_lifecycle.jsonl").open("a", encoding="utf-8")
        self.vma_stats: dict[str, Any] = {"refresh_attempts": 0, "refresh_ok": 0, "refresh_errors": 0, "pids": {}}
        self.vocab = RegionVocab.load(self.output_dir / "region_vocab.json")
        self.aggregator = WindowAggregator(
            output_dir=self.output_dir,
            vocab=self.vocab,
            bucket_bytes=self.config.region_bucket_bytes,
            window_ms=self.config.region_window_ms,
        )
        self.cgroup_adapter = CgroupFeatureAdapter()
        self.role_resolver = ProcessRoleResolver.for_config(self.config.process_role_rules)
        self.trackers: dict[str, CgroupPidTracker] = {}
        self.indexes: dict[tuple[int, int], VMAIntervalIndex] = {}
        self.app_scope_paths: dict[str, Path] = {}
        self.damon = DamonController(self.config)
        self.event_source: TracefsDamonEventSource | None = None
        self.final_result = "NOT_RUN"
        self.capability_status = "UNKNOWN"
        self.target_pid_count = 0
        self.damon_started = False
        self.damon_kdamond_index: int | None = None
        self.damon_context_index: int | None = None
        self.tracefs_instance = ""

    def run(self) -> int:
        start = time.monotonic()
        report = probe_capabilities()
        self.capability_status = report.status
        write_capability_report(self.output_dir, report)
        self._init_trackers()
        if report.status not in {CapabilityStatus.SUPPORTED.value, CapabilityStatus.SUPPORTED_NEEDS_ROOT.value}:
            self.final_result = "FAIL_CLOSED"
            self._error("capability_fail_closed", f"region monitor not started: {report.status}")
            self.close()
            return 0
        if report.status == CapabilityStatus.SUPPORTED_NEEDS_ROOT.value:
            self.final_result = "FAIL_CLOSED"
            self._error("permission", "DAMON/tracefs supported but needs root; no fake region events generated")
            self.close()
            return 0
        next_pid_refresh = 0.0
        next_vma_refresh = 0.0
        next_cgroup_refresh = 0.0
        cgroup_features: dict[str, dict[str, Any]] = {}
        try:
            while not STOP_REQUESTED:
                now = time.monotonic()
                if self.duration_s and now - start >= self.duration_s:
                    break
                now_ns = time.time_ns()
                if now >= next_pid_refresh:
                    self._refresh_pids(now_ns)
                    next_pid_refresh = now + self.config.pid_refresh_ms / 1000.0
                if now >= next_vma_refresh:
                    self._refresh_vmas()
                    next_vma_refresh = now + self.config.vma_refresh_ms / 1000.0
                if now >= next_cgroup_refresh:
                    cgroup_features = self._sample_cgroup_features()
                    next_cgroup_refresh = now + self.config.cgroup_window_ms / 1000.0
                self._maybe_start_damon_and_tracefs()
                self._drain_tracefs_events(cgroup_features)
                self.aggregator.flush_expired(time.time_ns())
                time.sleep(0.05)
        finally:
            self.final_result = "PASS" if self.aggregator.total_events and self.aggregator.mapped_events else (self.final_result if self.final_result != "NOT_RUN" else "NO_EVENTS")
            self.close()
        return 0

    def close(self) -> None:
        self.damon.stop()
        if self.event_source is not None:
            self.event_source.close()
        self.aggregator.close()
        self.vocab.save()
        (self.output_dir / "vma_refresh_stats.json").write_text(
            json.dumps(self.vma_stats, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self._write_summary()
        self.errors_file.close()
        self.lifecycle_file.close()

    def _init_trackers(self) -> None:
        slice_path = self.slice_path
        if slice_path is None:
            slice_path, error = resolve_user_slice_path(self.runtime_scope.slice_name)
            if error:
                self._error("slice", error)
        apps = [app for app in self.runtime_scope.apps if app.app_key in set(self.config.target_apps)]
        for app in apps:
            cgroup_path = slice_path / app.scope_name
            self.app_scope_paths[app.app_key] = cgroup_path
            self.trackers[app.app_key] = CgroupPidTracker(app.app_key, cgroup_path, self.role_resolver)

    def _refresh_pids(self, now_ns: int) -> None:
        for app_key, tracker in self.trackers.items():
            added, exited = tracker.refresh(now_ns)
            for info in added:
                self._lifecycle(app_key, "PID_ADDED", info)
            for info in exited:
                self._lifecycle(app_key, "PID_EXITED", info)
        if self.damon.started:
            targets = [proc for tracker in self.trackers.values() for proc in tracker.snapshot()]
            if targets:
                if not self.damon.update_targets(targets):
                    self._error("damon_target_update", self.damon.error)
                else:
                    self.target_pid_count = max(self.target_pid_count, len(self.damon.target_pid_map))

    def _refresh_vmas(self) -> None:
        for app_key, tracker in self.trackers.items():
            for proc in tracker.snapshot():
                self.vma_stats["refresh_attempts"] += 1
                records, error = read_proc_maps(proc.pid, proc.process_starttime, proc.process_role)
                key = f"{proc.pid}:{proc.process_starttime}"
                if error:
                    self.vma_stats["refresh_errors"] += 1
                    self.vma_stats["pids"][key] = {"status": "error", "error": error}
                    self._error("maps", f"{app_key} pid={proc.pid}: {error}")
                    continue
                self.indexes[proc.identity] = VMAIntervalIndex(records)
                self.vma_stats["refresh_ok"] += 1
                self.vma_stats["pids"][key] = {"status": "ok", "vma_count": len(records), "app_key": app_key}

    def _sample_cgroup_features(self) -> dict[str, dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        for app_key, path in self.app_scope_paths.items():
            sample = self.cgroup_adapter.sample(app_key, path)
            rows[app_key] = {
                "values": sample.values,
                "availability": sample.availability,
                "status": sample.status,
                "error": sample.error,
            }
        return rows

    def _maybe_start_damon_and_tracefs(self) -> None:
        if self.event_source is not None or self.damon.started:
            return
        targets = [proc for tracker in self.trackers.values() for proc in tracker.snapshot()]
        if not targets:
            return
        if not self.damon.start(targets):
            self._error("damon_start", self.damon.error)
            return
        self.target_pid_count = len(self.damon.target_pid_map)
        self.damon_started = True
        self.damon_kdamond_index = self.damon.kdamond_index
        self.damon_context_index = self.damon.context_index
        try:
            self.event_source = TracefsDamonEventSource(instance_name=f"region_monitor_{self.session_dir.name}")
            self.event_source.start()
            self.tracefs_instance = str(self.event_source.instance_path)
        except Exception as exc:
            self._error("tracefs_start", str(exc))
            self.damon.stop()
            self.event_source = None

    def _drain_tracefs_events(self, cgroup_features: dict[str, dict[str, Any]]) -> None:
        if self.event_source is None:
            return
        try:
            events = self.event_source.read_available(limit=200)
        except Exception as exc:
            self._error("tracefs_read", str(exc))
            return
        for raw_line in self.event_source.drain_unparsed_lines():
            self._error("unparsed_trace_event", raw_line)
        pid_map = self._target_pid_map()
        for event in events:
            proc = pid_map.get(str(event.target_id))
            if proc is None:
                self._error("unmapped_target", f"target_id={event.target_id}")
                continue
            index = self.indexes.get(proc.identity)
            if index is None:
                self._error("missing_vma_index", f"pid={proc.pid} starttime={proc.process_starttime}")
                continue
            app_key = self._app_for_process(proc)
            self.aggregator.add_event(
                app_id=app_key,
                foreground_epoch_id="",
                event=event,
                pid=proc.pid,
                process_starttime=proc.process_starttime,
                process_role=proc.process_role,
                index=index,
                cgroup_features=cgroup_features.get(app_key, {}),
            )

    def _target_pid_map(self) -> dict[str, ProcessInfo]:
        return dict(self.damon.target_pid_map)

    def _app_for_process(self, proc: ProcessInfo) -> str:
        for app_key, tracker in self.trackers.items():
            if proc.identity in {item.identity for item in tracker.snapshot()}:
                return app_key
        return ""

    def _lifecycle(self, app_key: str, event_type: str, info: ProcessInfo) -> None:
        row = {"timestamp_ns": time.time_ns(), "app_key": app_key, "event_type": event_type, **info.to_dict()}
        self.lifecycle_file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.lifecycle_file.flush()

    def _error(self, event_type: str, error: str) -> None:
        self.errors_file.write(json.dumps({"timestamp_ns": time.time_ns(), "event_type": event_type, "error": error}, ensure_ascii=False) + "\n")
        self.errors_file.flush()

    def _write_summary(self) -> None:
        lines = [
            "# Region Monitor 汇总",
            "",
            f"- session_dir: `{self.session_dir}`",
            f"- output_dir: `{self.output_dir}`",
            f"- enabled: true",
            f"- capability_status: {self.capability_status}",
            f"- target_apps: {', '.join(self.config.target_apps)}",
            f"- target_pid_count: {self.target_pid_count}",
            f"- damon_started: {str(self.damon_started).lower()}",
            f"- damon_kdamond_index: {'' if self.damon_kdamond_index is None else self.damon_kdamond_index}",
            f"- damon_context_index: {'' if self.damon_context_index is None else self.damon_context_index}",
            f"- tracefs_instance: `{self.tracefs_instance}`",
            f"- damon_cleanup_ok: {str(self.damon.cleanup_ok).lower()}",
            f"- damon_cleanup_error: {self.damon.cleanup_error}",
            f"- region_bucket_bytes: {self.config.region_bucket_bytes}",
            f"- region_window_ms: {self.config.region_window_ms}",
            f"- total_damon_events: {self.aggregator.total_events}",
            f"- mapped_event_count: {self.aggregator.mapped_events}",
            f"- unmapped_event_count: {self.aggregator.unmapped_events}",
            f"- low_resolution_event_count: {self.aggregator.low_resolution_events}",
            f"- flushed_windows: {self.aggregator.flushed_windows}",
            f"- region_count: {len(self.vocab.regions)}",
            f"- protection_eligible: false",
            f"- ready_for_apply: false",
            f"- final_result: {self.final_result}",
            "",
            "说明：本模块只读取 cgroup、/proc/<pid>/maps、DAMON/tracefs 观测数据，不写 lru_gen_pages，不启用 Tier2，不改变 MGLRU 回收行为。",
        ]
        (self.output_dir / "region_monitor_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Observe-only cgroup region monitor.")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--app-scope-config", required=True)
    parser.add_argument("--duration-s", type=float, default=0.0)
    parser.add_argument("--slice-path", default="", help="Resolved cgroup v2 slice path; useful when running under sudo.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    args = parse_args(argv)
    monitor = RegionMonitor(
        session_dir=Path(args.session_dir),
        config_path=Path(args.config),
        app_scope_config=Path(args.app_scope_config),
        duration_s=args.duration_s,
        slice_path=Path(args.slice_path) if args.slice_path else None,
    )
    return monitor.run()


if __name__ == "__main__":
    raise SystemExit(main())
