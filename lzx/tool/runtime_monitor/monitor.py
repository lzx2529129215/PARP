#!/usr/bin/env python3
"""Runtime Monitor v0: local PC state collector for dataset generation.

Output directory (per session):
  outputs/runtime_monitor/<session_id>/
  ├── model/     — machine-training data (9 CSVs)
  └── review/    — human-inspection data (7 files)

No prefetch, eviction, swap, or kernel policy action is performed.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import queue
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MONITOR_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MONITOR_DIR.parent
if str(MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(MONITOR_DIR))

from collectors.file_events import FileEventCollector
from collectors.foreground import ForegroundCollector, ForegroundState
from collectors.memory import read_meminfo, read_vmstat
from collectors.process import ProcessCollector
from core.app_feature_builder import AppFeatureBuilder
from core.app_mapper import AppMapper, load_config
from core.app_registry import AppRegistry
from core.feature_builder import FeatureBuilder
from core.lifecycle import LifecycleEventBuilder
from core.mglru_markov_debugfs import (
    MGLRUMarkovDebugfsWriter,
    RANK_BASED_CONFIDENCE,
    resolve_scope_cgroup_id,
)
from core.mglru_lstm_reclaim_policy import MGLRULSTMReclaimPolicyController
from core.memory_shadow import MemoryShadowObserver
from core.app_memory_activity import AppMemoryActivityCollector
from core.app_reclaim_controller import AppReclaimController, ReclaimConfig
from core.test4b_ballast import Test4BBallastCoordinator
from core.test4b_reclaim_controller import Test4BReclaimConfig, Test4BReclaimController
from core.online_causal_workload_markov import OnlineCausalWorkloadMarkov
from core.online_cgroup_workload import OnlineCgroupWorkloadSampler
from core.online_markov_debugfs_audit import OnlineMarkovDebugfsAuditWriter
from core.parp_bridge import PARPDebugfsBridge
from core.dual_workload_markov import DualWorkloadMarkov
from core.operation_tracker import OperationTracker
from core.review_builder import ReviewBuilder
from core.runtime_scope import RuntimeAppScope, load_runtime_app_scope
from core.workload_classifier import ClassificationResult, classify_session
from core.workload_markov_builder import MarkovBuildResult, build_workload_markov
from core.schema import (
    APP_LIFECYCLE_EVENT_FIELDS,
    APP_STATE_1S_FIELDS,
    FOREGROUND_DEBUG_FIELDS,
    FOREGROUND_EVENT_FIELDS,
    GLOBAL_STATE_1S_FIELDS,
    OPERATION_EVENT_FIELDS,
    OPERATION_LABEL_FIELDS,
    PROCESS_EVENT_FIELDS,
    DIRECT_APP_EVENT_FIELDS,
)
from core.writer import CsvWriter
from online_duration_lstm import OnlineDurationLSTMRunner
from collectors.x11_events import X11EventCollector
from core.x11_event_state import DIRECT_PREDICTION_EVENTS, X11EventState


class RuntimeMonitorV0:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = load_config(args.config)
        self.runtime_scope = _load_runtime_scope(args)
        if self.runtime_scope is not None:
            self.config = self.runtime_scope.as_process_mapper_config(self.config)
            self.target_apps = self.runtime_scope.target_apps
            if self.target_apps:
                self.args.target_app = self.target_apps[0]
            if self.runtime_scope.slice_name:
                self.args.test_slice = self.runtime_scope.slice_name
                self.args.cgroup_workload_slice = self.runtime_scope.slice_name
            if self.runtime_scope.workload_scopes:
                self.args.cgroup_workload_scopes = ",".join(self.runtime_scope.workload_scopes)
            self.args.app_key_to_vocab_name = self.runtime_scope.app_key_to_vocab_name
            self.args.loaded_runtime_scope = self.runtime_scope
            for warning in self.runtime_scope.vocab_warnings:
                print(f"warning: app scope vocab validation: {warning}", file=sys.stderr)
        else:
            self.target_apps = _parse_app_list(args.target_apps) or [args.target_app]
            self.args.app_key_to_vocab_name = {}
            self.args.loaded_runtime_scope = None
        self.session_id = _resolve_session_id(args)
        self.stop_requested = False

        # Directory structure
        requested_output = Path(args.output_dir)
        self.output_dir = requested_output if requested_output.name == self.session_id else requested_output / self.session_id
        self.model_dir = self.output_dir / "model"
        self.review_dir = self.output_dir / "review"
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)

        # Collectors
        self.mapper = AppMapper(self.config, target_app=args.target_app, target_apps=self.target_apps)
        self.process_collector = ProcessCollector(
            mapper=self.mapper,
            target_app=args.target_app,
            target_apps=self.target_apps,
            target_pid=args.target_pid,
            target_comm=args.target_comm,
            test_slice=args.test_slice,
        )
        manual_pid = args.target_pid or 0
        self.foreground_collector = ForegroundCollector(
            backend=args.foreground_backend,
            manual_app=args.target_app if args.foreground_backend == "manual" else "",
            manual_pid=manual_pid,
            app_window_keywords=self.runtime_scope.window_keywords if self.runtime_scope else None,
        )
        self.direct_x11_events = bool(getattr(args, "direct_x11_events", False))
        if self.direct_x11_events and args.foreground_backend != "x11":
            raise ValueError("--direct-x11-events requires --foreground-backend x11")
        self._direct_event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._direct_event_wake_r, self._direct_event_wake_w = os.pipe()
        # _drain_direct_x11_events may run at a scheduled sampling boundary
        # even when no X11 callback has queued an event.  A blocking read here
        # would prevent both the duration limit and SIGINT from being handled.
        os.set_blocking(self._direct_event_wake_r, False)
        os.set_blocking(self._direct_event_wake_w, False)
        self._direct_event_collector: X11EventCollector | None = None
        self._direct_event_state = X11EventState(self.foreground_collector.resolve_window)
        self._direct_event_id = 0
        self._direct_event_writer: CsvWriter | None = None
        self._direct_event_stats: dict[str, int] = {}
        if self.direct_x11_events:
            self._direct_event_writer = CsvWriter(
                self.model_dir / "direct_app_events.csv", DIRECT_APP_EVENT_FIELDS
            )
        self.file_collector = FileEventCollector(path_mode=args.path_mode)

        # Builders
        self.feature_builder = FeatureBuilder(
            label=args.label, session_id=self.session_id, test_slice=args.test_slice,
        )
        self.app_registry = AppRegistry(
            target_apps=self.target_apps,
            test_slice=args.test_slice,
            close_grace_windows=args.close_grace_windows,
        )
        self.app_feature_builder = AppFeatureBuilder(
            session_id=self.session_id, test_slice=args.test_slice,
        )
        self.lifecycle_builder = LifecycleEventBuilder(
            target_app=args.target_app,
            session_id=self.session_id,
            close_grace_windows=args.close_grace_windows,
            test_slice=args.test_slice,
        )

        # Operation tracker (reads automation_trace.csv if present)
        self.operation_tracker = OperationTracker(self.model_dir, self.session_id)

        # Review builder (generated at session end)
        self.review_builder = ReviewBuilder(self.model_dir, self.review_dir, self.session_id)

        # model/ CSV writers (7 files)
        self.global_state_writer = CsvWriter(
            self.model_dir / "global_state_1s.csv", GLOBAL_STATE_1S_FIELDS,
        )
        self.app_state_writer = CsvWriter(
            self.model_dir / "app_state_1s.csv", APP_STATE_1S_FIELDS,
        )
        self.foreground_events_writer = CsvWriter(
            self.model_dir / "foreground_events.csv", FOREGROUND_EVENT_FIELDS,
        )
        self.process_events_writer = CsvWriter(
            self.model_dir / "process_events.csv", PROCESS_EVENT_FIELDS,
        )
        self.app_lifecycle_writer = CsvWriter(
            self.model_dir / "app_lifecycle_events.csv", APP_LIFECYCLE_EVENT_FIELDS,
        )
        self.operation_events_writer = CsvWriter(
            self.model_dir / "operation_events.csv", OPERATION_EVENT_FIELDS,
        )
        self.operation_labels_writer = CsvWriter(
            self.model_dir / "operation_labels.csv", OPERATION_LABEL_FIELDS,
        )
        self.foreground_debug_writer = CsvWriter(
            self.model_dir / "foreground_debug.csv", FOREGROUND_DEBUG_FIELDS,
        )
        self.online_lstm = OnlineDurationLSTMRunner(args, self.model_dir, self.review_dir) if args.enable_online_lstm else None
        self.parp_bridge = None
        if args.enable_parp_bridge:
            self.parp_bridge = PARPDebugfsBridge(
                mode=args.parp_bridge_mode,
                debugfs_root=args.parp_debugfs_root,
                runtime_scope=self.runtime_scope,
                output_dir=self.output_dir,
                session_id=self.session_id,
                slice_name=args.test_slice,
                app_bind_config=args.parp_app_bind_config,
                model_name=args.parp_model_name,
                model_version=args.parp_model_version,
                schema_version=args.parp_schema_version,
                prior_ttl_ms=args.parp_prior_ttl_ms,
                min_update_interval_ms=args.parp_min_update_interval_ms,
                max_retries=args.parp_max_retries,
            )
        self.memory_shadow: MemoryShadowObserver | None = None
        if args.enable_memory_shadow:
            if not self.direct_x11_events:
                raise ValueError("--enable-memory-shadow requires --direct-x11-events")
            self.memory_shadow = MemoryShadowObserver(
                session_id=self.session_id,
                output_dir=self.output_dir,
                runtime_scope=self.runtime_scope,
                sample_interval_s=args.memory_shadow_interval_s,
                top_k=args.memory_shadow_top_k,
                recovery_window_s=args.memory_shadow_recovery_window_s,
            )
        self.app_memory_activity: AppMemoryActivityCollector | None = None
        self.app_reclaim_controller: AppReclaimController | None = None
        self.test4b_ballast: Test4BBallastCoordinator | None = None
        self.test4b_reclaim_controller: Test4BReclaimController | None = None
        self._latest_app_activity: dict[str, Any] = {}
        if args.enable_app_reclaim_controller:
            if not self.direct_x11_events or self.online_lstm is None or self.parp_bridge is None:
                raise ValueError("Test4 reclaim controller requires direct X11 events, online LSTM, and PARP bridge")
            self.app_memory_activity = AppMemoryActivityCollector(
                session_id=self.session_id, output_dir=self.output_dir, runtime_scope=self.runtime_scope,
                interval_s=args.app_activity_interval_s, ema_rho=args.reclaim_activity_ema_rho,
                activity_threshold=args.reclaim_activity_threshold,
                required_low_windows=args.reclaim_required_low_activity_windows,
                min_rss_bytes=args.reclaim_minimum_rss_bytes,
            )
            self.app_reclaim_controller = AppReclaimController(
                session_id=self.session_id, output_dir=self.output_dir,
                app_ids=self.runtime_scope.app_key_to_app_id if self.runtime_scope else {},
                vocab_to_app={v: k for k, v in (self.runtime_scope.app_key_to_vocab_name if self.runtime_scope else {}).items()},
                config=ReclaimConfig(
                    mode=args.app_reclaim_mode, probability_threshold=args.reclaim_probability_threshold,
                    required_low_probability_batches=args.reclaim_required_low_probability_batches,
                    activity_threshold=args.reclaim_activity_threshold,
                    required_low_activity_windows=args.reclaim_required_low_activity_windows,
                    step_bytes=args.reclaim_step_bytes, per_app_cooldown_ms=args.reclaim_per_app_cooldown_ms,
                    global_cooldown_ms=args.reclaim_global_cooldown_ms,
                    target_headroom_bytes=args.reclaim_target_headroom_bytes,
                    minimum_headroom_deficit_bytes=args.reclaim_minimum_headroom_deficit_bytes,
                    max_ratio=args.reclaim_max_ratio_per_action,
                    minimum_resident_bytes=args.reclaim_minimum_app_resident_bytes,
                    max_actions_per_minute=args.reclaim_max_actions_per_minute,
                    max_actions_per_episode=args.reclaim_max_actions_per_episode,
                    max_per_app_session=args.reclaim_max_bytes_per_app_session,
                    max_global_session=args.reclaim_max_bytes_global_session,
                    hard_min_available_bytes=args.reclaim_hard_min_available_bytes,
                    psi_full_abort_avg10=args.reclaim_psi_full_abort_avg10,
                    preflight_ready=args.reclaim_apply_preflight_ready,
                ),
            )
        if args.enable_test4b_ballast:
            if not self.direct_x11_events or self.online_lstm is None or self.parp_bridge is None or self.runtime_scope is None:
                raise ValueError("Test4B requires direct X11 events, online LSTM, PARP bridge, and runtime scope")
            if not args.test4b_ballast_config:
                raise ValueError("Test4B requires --test4b-ballast-config")
            if self.runtime_scope.slice_name != "test4b-experiment.slice":
                raise ValueError("Test4B runtime scope must target only test4b-experiment.slice")
            # Observed activity is evidence only in Test4B; it cannot veto the
            # synthetic-ground-truth mechanism test.
            if self.app_memory_activity is None:
                self.app_memory_activity = AppMemoryActivityCollector(
                    session_id=self.session_id, output_dir=self.output_dir, runtime_scope=self.runtime_scope,
                    interval_s=args.app_activity_interval_s, ema_rho=args.reclaim_activity_ema_rho,
                    activity_threshold=args.reclaim_activity_threshold,
                    required_low_windows=args.reclaim_required_low_activity_windows,
                    min_rss_bytes=args.reclaim_minimum_rss_bytes,
                )
            self.test4b_ballast = Test4BBallastCoordinator(
                session_id=self.session_id, output_dir=self.output_dir, runtime_scope=self.runtime_scope,
                config_path=_resolve_project_path(args.test4b_ballast_config, legacy_base=MONITOR_DIR),
            )
            self.test4b_reclaim_controller = Test4BReclaimController(
                session_id=self.session_id, output_dir=self.output_dir,
                app_ids=self.runtime_scope.app_key_to_app_id,
                app_name_to_key={app.vocab_name: app.app_key for app in self.runtime_scope.apps},
                config=Test4BReclaimConfig(
                    mode=args.test4b_reclaim_mode, probability_threshold=args.test4b_probability_threshold,
                    required_low_probability_batches=args.test4b_required_low_probability_batches,
                    cold_quiet_s=args.test4b_cold_quiet_s, step_bytes=args.test4b_reclaim_step_bytes,
                    target_headroom_bytes=args.test4b_target_headroom_bytes,
                    minimum_headroom_deficit_bytes=args.test4b_minimum_headroom_deficit_bytes,
                    per_app_cooldown_ms=args.test4b_per_app_cooldown_ms,
                    global_cooldown_ms=args.test4b_global_cooldown_ms,
                    max_reclaims_per_session=args.test4b_max_reclaims_per_session,
                    max_reclaim_bytes_per_session=args.test4b_max_reclaim_bytes_per_session,
                    hard_min_available_bytes=args.test4b_hard_min_available_bytes,
                    psi_full_abort_avg10=args.test4b_psi_full_abort_avg10,
                    preflight_ready=args.test4b_apply_preflight_ready,
                    activation_file=args.test4b_controller_activation_file,
                ),
            )
        self.mglru_markov_writer = MGLRUMarkovDebugfsWriter(
            enabled=(
                args.enable_mglru_markov_debugfs
                or args.enable_mglru_lstm_reclaim_policy
            ),
            strict=args.mglru_markov_strict,
            debugfs_path=args.mglru_markov_debugfs_path,
            session_id=self.session_id,
            model_dir=self.model_dir,
            review_dir=self.review_dir,
            ttl_ms=args.mglru_markov_ttl_ms,
        )
        policy_config_path = _resolve_project_path(
            args.mglru_lstm_reclaim_policy_config, legacy_base=MONITOR_DIR
        )
        self.mglru_lstm_policy = MGLRULSTMReclaimPolicyController(
            enabled=args.enable_mglru_lstm_reclaim_policy,
            strict=args.mglru_lstm_policy_strict,
            config_path=policy_config_path,
            session_id=self.session_id,
            model_dir=self.model_dir,
            review_dir=self.review_dir,
            debugfs_writer=self.mglru_markov_writer,
        )
        self.last_mglru_foreground_app_key = ""
        self.last_mglru_binding_refresh_monotonic = 0.0
        self._scope_cgroup_cache: dict[tuple[str, str], int | None] = {}
        self.cgroup_workload_process: subprocess.Popen[Any] | None = None
        self.cgroup_workload_process_started = False
        self.cgroup_workload_exit_code: int | None = None
        self.cgroup_workload_raw_csv = self.model_dir / "cgroup_memory_workload_1s.csv"
        self.cgroup_workload_delta_csv = self.model_dir / "cgroup_memory_workload_delta_1s.csv"
        self.cgroup_workload_summary = self.review_dir / "cgroup_memory_workload_summary.md"
        self.cgroup_workload_log = self.review_dir / "cgroup_memory_workload_collector.log"
        self.region_monitor_process: subprocess.Popen[Any] | None = None
        self.region_monitor_process_started = False
        self.region_monitor_exit_code: int | None = None
        self.region_monitor_dir = self.output_dir / "region_monitor"
        self.region_monitor_summary = self.region_monitor_dir / "region_monitor_summary.md"
        self.region_monitor_log = self.review_dir / "region_monitor.log"
        self.workload_classifier_enabled = (
            args.enable_cgroup_workload and not args.disable_workload_classifier
        )
        self.workload_classifier_result: ClassificationResult | None = None
        self.cgroup_workload_state_csv = self.model_dir / "cgroup_workload_state_1s.csv"
        self.cgroup_workload_state_summary = self.review_dir / "cgroup_workload_state_summary.md"
        self.workload_markov_builder_enabled = (
            self.workload_classifier_enabled
            and not args.disable_workload_markov_builder
        )
        self.workload_markov_result: MarkovBuildResult | None = None
        self.workload_markov_transitions_csv = (
            self.model_dir / "workload_markov_transitions.csv"
        )
        self.workload_markov_summary = (
            self.review_dir / "workload_markov_summary.md"
        )
        self.online_workload_markov_enabled = bool(
            args.enable_online_workload_markov and self.workload_classifier_enabled
        )
        self.dual_workload_markov_enabled = bool(
            args.enable_dual_workload_markov and self.workload_classifier_enabled
        )
        self.dual_workload_markov = None
        self.current_foreground_app_key = ""
        self.current_foreground_app_id = ""
        if self.dual_workload_markov_enabled:
            self.mglru_markov_writer.write_dual_runtime_mode("dual")
            self.dual_workload_markov = DualWorkloadMarkov(
                enabled=True,
                session_id=self.session_id,
                model_dir=self.model_dir,
                review_dir=self.review_dir,
                # Writer stays attached in disabled mode so the dual audit CSV
                # is still produced without touching debugfs.
                debugfs_writer=self.mglru_markov_writer,
                reentry_window_s=args.dual_markov_reentry_window_s,
                ignore_initial_low_activity_s=args.dual_markov_ignore_initial_low_activity_s,
            )
        self.online_markov_debugfs_writer = None
        self.online_workload_markov = None
        self.live_cgroup_sampler = None
        if self.online_workload_markov_enabled:
            self.online_markov_debugfs_writer = OnlineMarkovDebugfsAuditWriter(
                self.mglru_markov_writer, self.session_id, self.model_dir
            )
            self.online_workload_markov = OnlineCausalWorkloadMarkov(
                enabled=True,
                session_id=self.session_id,
                model_dir=self.model_dir,
                review_dir=self.review_dir,
                debugfs_writer=self.online_markov_debugfs_writer,
            )
        if self.online_workload_markov_enabled or self.dual_workload_markov_enabled:
            scopes = self.runtime_scope.workload_scopes if self.runtime_scope else _parse_app_list(args.cgroup_workload_scopes)
            self.live_cgroup_sampler = OnlineCgroupWorkloadSampler(
                session_id=self.session_id,
                model_dir=self.model_dir,
                slice_name=args.cgroup_workload_slice,
                scopes=scopes,
                runtime_scope=self.runtime_scope,
                on_observed=self._on_live_workload_observed,
            )

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    def run(self) -> int:
        signal.signal(signal.SIGINT, self._request_stop)
        signal.signal(signal.SIGTERM, self._request_stop)
        start = time.monotonic()
        next_tick = start
        print(f"Runtime Monitor v0 started. session_id={self.session_id}")
        print(f"  model: {self.model_dir}")
        print(f"  review: {self.review_dir}")
        if self.online_lstm is not None:
            print("  online duration LSTM: enabled")
            print(f"  online duration checkpoint: {self.args.lstm_checkpoint}")
            print(f"  online LSTM model type: {self.args.lstm_model_type}")
        else:
            print("  online duration LSTM: disabled")
        if self.args.enable_cgroup_workload:
            self._start_cgroup_workload_collector()
            print("  cgroup workload collector: enabled")
            print(f"  cgroup workload raw_csv: {self.cgroup_workload_raw_csv}")
            print(f"  cgroup workload delta_csv: {self.cgroup_workload_delta_csv}")
        else:
            print("  cgroup workload collector: disabled")
        if self.args.enable_region_monitor:
            self._start_region_monitor()
            print("  region monitor: enabled")
            print(f"  region monitor output: {self.region_monitor_dir}")
        else:
            print("  region monitor: disabled")
        print(
            "  online causal workload Markov: "
            + ("enabled" if self.online_workload_markov_enabled else "disabled")
        )
        if self.args.enable_mglru_markov_debugfs:
            print("  MGLRU Markov debugfs writer: enabled")
            print(f"  MGLRU Markov debugfs path: {self.args.mglru_markov_debugfs_path}")
        else:
            print("  MGLRU Markov debugfs writer: disabled")
        if self.args.enable_mglru_lstm_reclaim_policy:
            self.mglru_lstm_policy.configure_kernel()
            self._maybe_refresh_mglru_app_bindings(force=True)
            print("  MGLRU LSTM app reclaim policy: enabled")
            print(f"  policy config: {self.mglru_lstm_policy.config_path}")
        else:
            print("  MGLRU LSTM app reclaim policy: disabled")
        if self.parp_bridge is not None:
            preflight = self.parp_bridge.preflight()
            print(f"  PARP bridge: {self.args.parp_bridge_mode}")
            print(f"  PARP bridge preflight: {preflight['status']}")
            self.parp_bridge.startup_bindings()
        else:
            print("  PARP bridge: disabled")
        if self.direct_x11_events:
            self._start_direct_x11_events()
            print("  native X11 app events: enabled")
            print("  app event source: X11 event queue (not sample polling)")
        else:
            print("  native X11 app events: disabled")
        if self.memory_shadow is not None:
            print("  Test3 memory shadow: enabled (procfs/cgroup read-only)")
        else:
            print("  Test3 memory shadow: disabled")
        if self.app_reclaim_controller is not None:
            print(f"  Test4 app reclaim controller: {self.args.app_reclaim_mode} (fail-closed)")
        if self.test4b_reclaim_controller is not None:
            print(f"  Test4B ballast/controller: {self.args.test4b_reclaim_mode} (foreground allocation only)")
        if self.app_reclaim_controller is not None and self.args.app_reclaim_mode == "apply-bounded":
            print("Only preflight-authorized bounded memory.reclaim may run; no MGLRU, vmscan, swap, generation, or anon/file policy action will be performed.")
        else:
            print("No prefetch, active eviction, swap, generation, or anon/file policy action will be performed.")
        try:
            while not self.stop_requested:
                now = time.monotonic()
                if self.args.duration and now - start >= self.args.duration:
                    break
                if now < next_tick:
                    timeout = next_tick - now
                    if self.direct_x11_events:
                        try:
                            readable, _, _ = select.select(
                                [self._direct_event_wake_r], [], [], timeout
                            )
                        except InterruptedError:
                            continue
                        if readable:
                            self._drain_direct_x11_events()
                    else:
                        time.sleep(min(0.1, timeout))
                    continue
                if self.direct_x11_events:
                    self._drain_direct_x11_events()
                self.sample_once()
                next_tick += self.args.sample_interval
        finally:
            self._stop_direct_x11_events()
            if self.direct_x11_events:
                self._drain_direct_x11_events()
            self._close_writers(close_mglru=False)
            self._stop_cgroup_workload_collector()
            self._stop_region_monitor()
            if self.live_cgroup_sampler is not None:
                self.live_cgroup_sampler.close()
            self._run_workload_classifier()
            self._write_workload_state_changes()
            self._run_workload_markov_builder()
            self._write_workload_markov_sets()
            if self.dual_workload_markov is not None:
                self.dual_workload_markov.close()
            if self.online_workload_markov is not None:
                self.online_workload_markov.close()
            if self.online_markov_debugfs_writer is not None:
                self.online_markov_debugfs_writer.close()
            self.mglru_lstm_policy.close()
            if self.parp_bridge is not None:
                self.parp_bridge.close()
            if self.memory_shadow is not None:
                self.memory_shadow.close(
                    self.parp_bridge.audit_path if self.parp_bridge is not None else None
                )
            if self.app_memory_activity is not None:
                self.app_memory_activity.close()
            if self.app_reclaim_controller is not None:
                self.app_reclaim_controller.close()
            if self.test4b_reclaim_controller is not None:
                self.test4b_reclaim_controller.close()
            if self.test4b_ballast is not None:
                self.test4b_ballast.close()
            self.mglru_markov_writer.close()
            self._write_lstm_debugfs_audit()
            self._generate_review()
            self._append_cgroup_workload_summary()
            print(
                f"Runtime Monitor v0 stopped."
                f" model={self.model_dir} review={self.review_dir}"
            )
        if self.args.mglru_lstm_policy_strict:
            return 0 if self.mglru_lstm_policy.strict_result() == "PASS" else 1
        return 0

    def sample_once(self) -> None:
        self._maybe_refresh_mglru_app_bindings()
        window_start_ns = time.time_ns()
        window_end_ns = window_start_ns + int(self.args.sample_interval * 1_000_000_000)

        # 1. Collect raw data
        samples = self.process_collector.sample()
        if self.memory_shadow is not None:
            self.memory_shadow.sample_if_due(samples, window_start_ns)
        file_events = self.file_collector.poll(samples)  # in-memory only, not written to disk

        meminfo = read_meminfo()
        vmstat = read_vmstat()
        foreground = self._current_foreground_state() if self.direct_x11_events else self.foreground_collector.sample()
        if self.app_memory_activity is not None:
            try:
                self._latest_app_activity = self.app_memory_activity.sample_if_due(
                    samples, foreground.foreground_app, window_start_ns
                )
            except Exception as exc:
                # Activity observation is optional evidence; never let a
                # transient procfs/cgroup failure stop online monitoring.
                print(f"warning: Test4 app activity sample failed: {exc}", file=sys.stderr)
        if self.app_reclaim_controller is not None:
            try:
                self.app_reclaim_controller.tick(window_start_ns)
            except Exception as exc:
                print(f"warning: Test4 reclaim controller tick failed: {exc}", file=sys.stderr)
        if self.test4b_reclaim_controller is not None:
            try:
                self.test4b_reclaim_controller.tick(window_start_ns)
            except Exception as exc:
                print(f"warning: Test4B reclaim controller tick failed: {exc}", file=sys.stderr)
        if self.test4b_ballast is not None:
            try:
                self.test4b_ballast.tick(
                    foreground_app=foreground.foreground_app,
                    bind_ready=self.parp_bridge.successful_binding_apps() if self.parp_bridge is not None else set(),
                    now_ns=window_start_ns,
                )
            except Exception as exc:
                print(f"warning: Test4B ballast tick failed: {exc}", file=sys.stderr)
        feature_window_id = self.feature_builder.feature_window_id

        # 2. Foreground debug CSV
        self.foreground_debug_writer.write_row(
            self.foreground_collector.debug_row(self.session_id, feature_window_id)
        )

        windows = [] if self.direct_x11_events else self.foreground_collector.sample_windows()

        # 3. Lifecycle events → three event streams
        lifecycle_events = self.lifecycle_builder.build_all(
            samples=samples, foreground=foreground, windows=windows,
        )
        self.process_events_writer.write_rows(lifecycle_events.process_events)
        if not self.direct_x11_events:
            self.foreground_events_writer.write_rows(lifecycle_events.foreground_events)
            self.app_lifecycle_writer.write_rows(lifecycle_events.app_lifecycle)
        else:
            # X11 provides normal close notifications.  A forced stop of an
            # automation scope can instead remove the client before that
            # notification reaches us.  The already monitored cgroup-empty
            # lifecycle edge is a reliable close fallback and never triggers
            # LSTM inference.
            for lifecycle_event in lifecycle_events.app_lifecycle:
                if lifecycle_event.get("event_type") == "APP_CLOSE":
                    self._handle_direct_x11_event({
                        "event_type": "CGROUP_APP_EMPTY",
                        "timestamp_ns": lifecycle_event.get("ts_ns", time.time_ns()),
                        "app": lifecycle_event.get("app", ""),
                        "source": "procfs-cgroup",
                    })

        # 4. App registry update
        self.app_registry.update(samples=samples, foreground=foreground)
        registry_summary = self.app_registry.summary()

        # 5. Operation tracking
        self.operation_tracker.refresh()
        op_context = self.operation_tracker.get_current(window_start_ns, window_end_ns)
        new_ops = self.operation_tracker.pop_new_operation_events(
            foreground.foreground_app,
            registry_summary.get("open_apps", ""),
        )
        self.operation_events_writer.write_rows(new_ops)

        # Build per-app operation contexts for app_state rows
        op_app_map: dict[str, dict[str, str]] = {}
        for app_id in self.app_registry.observed_apps:
            op_app_map[app_id] = dict(op_context)

        # 6. App features → app_state_1s.csv
        app_feature_rows = self.app_feature_builder.build_rows(
            feature_window_id=feature_window_id,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            records=self.app_registry.records_for_output(),
            samples=samples,
            file_events=file_events,
            foreground=foreground,
            operation_contexts=op_app_map,
        )
        self.app_state_writer.write_rows(app_feature_rows)

        # 7. Global features → global_state_1s.csv
        test_mem = _read_test_slice_memory(self.args.test_slice)
        feature_row = self.feature_builder.build(
            foreground=foreground,
            meminfo=meminfo,
            vmstat=vmstat,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            registry_summary=registry_summary,
            foreground_window_id=foreground.window_id,
            foreground_pid=foreground.foreground_pid,
            foreground_wm_class=foreground.foreground_app,
            operation_context=op_context,
            test_mem_current=test_mem.get("current", 0),
            test_mem_high=test_mem.get("high", 0),
            test_mem_max=test_mem.get("max", 0),
        )
        self.global_state_writer.write_row(feature_row)
        self.current_foreground_app_key = str(feature_row.get("foreground_app", "")).strip()
        self.current_foreground_app_id = str(
            self.runtime_scope.app_key_to_app_id.get(self.current_foreground_app_key, "")
            if self.runtime_scope else ""
        )
        if self.dual_workload_markov is not None:
            self.dual_workload_markov.observe_foreground(
                foreground_app_key=self.current_foreground_app_key,
                foreground_app_id=self.current_foreground_app_id,
                timestamp_ns=window_start_ns,
            )
        self._maybe_write_mglru_current_app(feature_row)
        if self.online_lstm is not None and not self.direct_x11_events:
            prediction_result = self.online_lstm.process_sample(feature_row)
            self._maybe_write_mglru_predictions(prediction_result)
            if self.parp_bridge is not None:
                try:
                    self.parp_bridge.submit_prediction(feature_row, prediction_result)
                except Exception as exc:
                    print(f"warning: PARP bridge submission failed: {exc}", file=sys.stderr)
        if self.live_cgroup_sampler is not None:
            self.live_cgroup_sampler.sample(window_start_ns)

        if self.args.verbose:
            print(
                f"sample pids={len(samples)} events={len(file_events)} "
                f"mem_available={feature_row.get('global_mem_available_kb')}"
            )

    # ------------------------------------------------------------------
    # Native X11 event path
    # ------------------------------------------------------------------

    def _start_direct_x11_events(self) -> None:
        self._direct_event_collector = X11EventCollector(self._enqueue_direct_x11_event)
        self._direct_event_collector.start()

    def _stop_direct_x11_events(self) -> None:
        collector = self._direct_event_collector
        self._direct_event_collector = None
        if collector is not None:
            collector.stop()
        for fd_name in ("_direct_event_wake_r", "_direct_event_wake_w"):
            fd = getattr(self, fd_name, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_name, -1)

    def _enqueue_direct_x11_event(self, event: dict[str, Any]) -> None:
        self._direct_event_queue.put(event)
        try:
            os.write(self._direct_event_wake_w, b"e")
        except OSError:
            pass

    def _drain_direct_x11_events(self) -> None:
        if self._direct_event_wake_r >= 0:
            try:
                os.read(self._direct_event_wake_r, 65536)
            except (BlockingIOError, OSError):
                pass
        while True:
            try:
                event = self._direct_event_queue.get_nowait()
            except queue.Empty:
                return
            self._handle_direct_x11_event(event)

    def _handle_direct_x11_event(self, raw_event: dict[str, Any]) -> None:
        high_level_events = self._direct_event_state.handle(raw_event)
        for event in high_level_events:
            event_type = str(event.get("event_type", ""))
            self._direct_event_stats[event_type] = self._direct_event_stats.get(event_type, 0) + 1
            event["session_id"] = self.session_id
            self._direct_event_id += 1
            event_id = f"{self.session_id}-e{self._direct_event_id:06d}"
            event["event_id"] = event_id

            if self.memory_shadow is not None:
                self.memory_shadow.observe_event(event)

            if event_type.startswith("APP_FOCUS") or event_type in {
                "APP_SWITCH", "APP_MINIMIZE", "APP_RESTORE",
            }:
                self.foreground_events_writer.write_row(event)
            elif event_type in {"APP_OPEN", "APP_CLOSE"}:
                self.app_lifecycle_writer.write_row(event)

            prediction_result: dict[str, Any] = {
                "status": "not_requested",
                "prediction_id": "",
                "inference_executed": False,
            }
            snapshot = self._direct_event_state.snapshot()
            if self.online_lstm is not None and event_type in DIRECT_PREDICTION_EVENTS:
                feature_row = {
                    "session_id": self.session_id,
                    "feature_window_id": event_id,
                    "timestamp": event.get("timestamp", ""),
                    "foreground_app": snapshot["foreground_app"],
                    "foreground_window_id": snapshot["foreground_window_id"],
                    "foreground_pid": snapshot["foreground_pid"],
                    "foreground_window_title": snapshot["foreground_window_title"],
                    "open_apps": snapshot["open_apps"],
                    "app_history": "",
                    "duration_history_ms": "",
                }
                prediction_result = self.online_lstm.process_event(feature_row, event_type)
                self._maybe_write_mglru_predictions(prediction_result)
                if self.parp_bridge is not None:
                    try:
                        self.parp_bridge.submit_prediction(feature_row, prediction_result)
                    except Exception as exc:
                        print(f"warning: PARP bridge event submission failed: {exc}", file=sys.stderr)
                if self.memory_shadow is not None:
                    # Event-time T0 is intentionally gathered from a fresh
                    # process/cgroup view instead of waiting for the 250 ms
                    # periodic sampler.  This is observation only.
                    self.memory_shadow.record_prediction(
                        event=event,
                        feature_row=feature_row,
                        result=prediction_result,
                        process_samples=self.process_collector.sample(),
                    )
                if self.test4b_ballast is not None:
                    try:
                        self.test4b_ballast.observe_event(
                            event=event, foreground_app=str(snapshot.get("foreground_app", "")),
                            bind_ready=self.parp_bridge.successful_binding_apps() if self.parp_bridge is not None else set(),
                            now_ns=int(event.get("ts_ns") or time.time_ns()),
                        )
                    except Exception as exc:
                        print(f"warning: Test4B ballast event handling failed: {exc}", file=sys.stderr)
                if self.app_reclaim_controller is not None and prediction_result.get("status") == "success":
                    try:
                        fresh_samples = self.process_collector.sample()
                        self._latest_app_activity = self.app_memory_activity.sample_if_due(
                            fresh_samples, str(snapshot.get("foreground_app", "")),
                            int(event.get("ts_ns") or time.time_ns()), force=True,
                        ) if self.app_memory_activity is not None else {}
                        self.app_reclaim_controller.observe_prediction(
                            prediction_id=str(prediction_result.get("prediction_id", "")),
                            trigger_event_id=event_id, trigger_type=str(prediction_result.get("trigger_type", "")),
                            current_app=str(snapshot.get("foreground_app", "")), result=prediction_result,
                            activities=self._latest_app_activity,
                            bind_ready=self.parp_bridge.successful_binding_apps() if self.parp_bridge is not None else set(),
                            now_ns=int(event.get("ts_ns") or time.time_ns()),
                        )
                    except Exception as exc:
                        # A controller bug is fail-closed for reclaim and must
                        # not prevent either subsequent X11 events or LSTM inference.
                        print(f"warning: Test4 reclaim controller event handling failed: {exc}", file=sys.stderr)
                if self.test4b_reclaim_controller is not None and prediction_result.get("status") == "success":
                    try:
                        fresh_samples = self.process_collector.sample()
                        self._latest_app_activity = self.app_memory_activity.sample_if_due(
                            fresh_samples, str(snapshot.get("foreground_app", "")),
                            int(event.get("ts_ns") or time.time_ns()), force=True,
                        ) if self.app_memory_activity is not None else {}
                        self.test4b_reclaim_controller.observe_prediction(
                            prediction_id=str(prediction_result.get("prediction_id", "")), trigger_event_id=event_id,
                            trigger_type=str(prediction_result.get("trigger_type", "")),
                            current_app=str(snapshot.get("foreground_app", "")), result=prediction_result,
                            ballast=self.test4b_ballast.states(int(event.get("ts_ns") or time.time_ns())) if self.test4b_ballast is not None else {},
                            activities=self._latest_app_activity,
                            bind_ready=self.parp_bridge.successful_binding_apps() if self.parp_bridge is not None else set(),
                            now_ns=int(event.get("ts_ns") or time.time_ns()),
                        )
                        if self.test4b_reclaim_controller.last_successful_reclaim_app and self.test4b_ballast is not None:
                            self.test4b_ballast.mark_reclaimed(self.test4b_reclaim_controller.last_successful_reclaim_app)
                            self.test4b_reclaim_controller.last_successful_reclaim_app = ""
                    except Exception as exc:
                        print(f"warning: Test4B reclaim controller event handling failed: {exc}", file=sys.stderr)
            elif self.test4b_ballast is not None:
                # APP_MINIMIZE/APP_CLOSE have no LSTM inference, yet moving a
                # pre-existing sidecar to BACKGROUND_IDLE is still necessary.
                try:
                    self.test4b_ballast.observe_event(
                        event=event, foreground_app=str(snapshot.get("foreground_app", "")),
                        bind_ready=self.parp_bridge.successful_binding_apps() if self.parp_bridge is not None else set(),
                        now_ns=int(event.get("ts_ns") or time.time_ns()),
                    )
                except Exception as exc:
                    print(f"warning: Test4B ballast lifecycle handling failed: {exc}", file=sys.stderr)

            if self._direct_event_writer is not None:
                self._direct_event_writer.write_row({
                    **event,
                    "prediction_triggered": "1" if prediction_result.get("inference_executed") else "0",
                    "prediction_id": prediction_result.get("prediction_id", ""),
                    "prediction_status": prediction_result.get("status", ""),
                })

    def _current_foreground_state(self) -> ForegroundState:
        snapshot = self._direct_event_state.snapshot()
        since_ns = self._direct_event_state.foreground_since_ns
        duration_s = max(0.0, (time.time_ns() - since_ns) / 1_000_000_000) if since_ns else 0.0
        return ForegroundState(
            foreground_app=str(snapshot["foreground_app"]),
            foreground_pid=int(snapshot["foreground_pid"] or 0),
            window_id=str(snapshot["foreground_window_id"]),
            window_title=str(snapshot["foreground_window_title"]),
            foreground_duration=duration_s,
            source="x11-event",
        )

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _close_writers(self, *, close_mglru: bool = True) -> None:
        self.global_state_writer.close()
        self.app_state_writer.close()
        self.foreground_events_writer.close()
        self.process_events_writer.close()
        self.app_lifecycle_writer.close()
        self.operation_tracker.refresh()
        self.operation_events_writer.write_rows(self.operation_tracker.pop_new_operation_events("", ""))
        self.operation_events_writer.close()
        self.operation_labels_writer.write_rows(self.operation_tracker.operation_label_rows())
        self.operation_labels_writer.close()
        self.foreground_debug_writer.close()
        if self._direct_event_writer is not None:
            self._direct_event_writer.close()
            self._direct_event_writer = None
        if self.online_lstm is not None:
            self.online_lstm.close()
        if close_mglru:
            self.mglru_markov_writer.close()

    def _generate_review(self) -> None:
        try:
            self.review_builder.generate()
        except Exception as exc:
            print(f"warning: review generation failed: {exc}", file=sys.stderr)

    def _request_stop(self, _signum: int, _frame: Any) -> None:
        self.stop_requested = True

    def _start_cgroup_workload_collector(self) -> None:
        script = MONITOR_DIR / "scripts" / "collect_cgroup_memory_workload.py"
        cmd = [
            sys.executable,
            str(script),
            "--session-dir",
            str(self.output_dir),
            "--slice",
            str(self.args.cgroup_workload_slice),
            "--interval-s",
            str(self.args.cgroup_workload_interval_s),
            "--scopes",
            str(self.args.cgroup_workload_scopes),
        ]
        self.review_dir.mkdir(parents=True, exist_ok=True)
        log_f = self.cgroup_workload_log.open("w", encoding="utf-8")
        try:
            self.cgroup_workload_process = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                cwd=MONITOR_DIR.parent,
                text=True,
            )
            log_f.close()
            self.cgroup_workload_process_started = True
        except Exception as exc:
            log_f.write(f"failed to start cgroup workload collector: {exc}\n")
            log_f.close()
            self.cgroup_workload_process = None
            self.cgroup_workload_process_started = False
            self.cgroup_workload_exit_code = -1
            print(f"warning: failed to start cgroup workload collector: {exc}", file=sys.stderr)

    def _stop_cgroup_workload_collector(self) -> None:
        proc = self.cgroup_workload_process
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.cgroup_workload_exit_code = proc.returncode

    def _start_region_monitor(self) -> None:
        cmd = [
            sys.executable,
            "-m",
            "runtime_monitor.region_monitor.region_monitor",
            "--session-dir",
            str(self.output_dir),
            "--config",
            str(self.args.region_monitor_config),
            "--app-scope-config",
            str(self.args.app_scope_config),
        ]
        self.review_dir.mkdir(parents=True, exist_ok=True)
        log_f = self.region_monitor_log.open("w", encoding="utf-8")
        try:
            self.region_monitor_process = subprocess.Popen(
                cmd,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                cwd=MONITOR_DIR.parent,
                text=True,
            )
            log_f.close()
            self.region_monitor_process_started = True
        except Exception as exc:
            log_f.write(f"failed to start region monitor: {exc}\n")
            log_f.close()
            self.region_monitor_process = None
            self.region_monitor_process_started = False
            self.region_monitor_exit_code = -1
            print(f"warning: failed to start region monitor: {exc}", file=sys.stderr)

    def _stop_region_monitor(self) -> None:
        proc = self.region_monitor_process
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.region_monitor_exit_code = proc.returncode

    def _run_workload_classifier(self) -> None:
        if not self.workload_classifier_enabled:
            return
        if not self.cgroup_workload_delta_csv.exists():
            print(
                f"warning: workload classifier input does not exist: "
                f"{self.cgroup_workload_delta_csv}",
                file=sys.stderr,
            )
            return
        try:
            self.workload_classifier_result = classify_session(
                self.output_dir,
                self.args.app_scope_config,
            )
            for field in self.workload_classifier_result.missing_fields:
                print(
                    f"warning: workload classifier field missing, using 0: {field}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"warning: workload classifier failed: {exc}", file=sys.stderr)

    def _write_workload_state_changes(self) -> None:
        if self.online_workload_markov_enabled:
            return
        if not self.args.enable_mglru_markov_debugfs:
            return
        if not self.cgroup_workload_state_csv.exists():
            print(
                f"warning: workload state CSV does not exist: "
                f"{self.cgroup_workload_state_csv}",
                file=sys.stderr,
            )
            return
        try:
            with self.cgroup_workload_state_csv.open(
                "r", encoding="utf-8", newline=""
            ) as f:
                for row in csv.DictReader(f):
                    if str(row.get("state_changed", "")).strip().lower() != "true":
                        continue
                    scope_name = str(row.get("scope_name", "")).strip()
                    cgroup_id = _optional_int(row.get("cgroup_id"))
                    if cgroup_id is None:
                        cgroup_id = self._resolve_mglru_scope_cgroup_id(scope_name)
                    self.mglru_markov_writer.write_workload_update(
                        cgroup_id=cgroup_id,
                        app_id=_optional_int(row.get("app_id")),
                        workload_id=_optional_int(row.get("workload_id")),
                        app_key=str(row.get("app_key", "")).strip(),
                        workload_name=str(row.get("workload_name", "")).strip(),
                    )
        except Exception as exc:
            print(
                f"warning: failed to write workload state changes: {exc}",
                file=sys.stderr,
            )

    def _run_workload_markov_builder(self) -> None:
        if self.online_workload_markov_enabled:
            return
        if not self.workload_markov_builder_enabled:
            return
        if not self.cgroup_workload_state_csv.exists():
            print(
                f"warning: workload Markov builder 输入不存在: "
                f"{self.cgroup_workload_state_csv}",
                file=sys.stderr,
            )
            return
        try:
            self.workload_markov_result = build_workload_markov(self.output_dir)
            for field in self.workload_markov_result.missing_fields:
                print(
                    f"warning: workload Markov builder 输入字段缺失: {field}",
                    file=sys.stderr,
                )
        except Exception as exc:
            print(f"warning: workload Markov builder 失败: {exc}", file=sys.stderr)

    def _on_live_workload_observed(self, row: dict[str, Any]) -> None:
        if self.online_workload_markov is None and self.dual_workload_markov is None:
            return
        try:
            workload_id = _optional_int(row.get("observed_workload_id"))
            app_id = _optional_int(row.get("app_id"))
            cgroup_id = _optional_int(row.get("cgroup_id"))
            if workload_id is None or app_id is None:
                return
            timestamp_ns = _optional_int(row.get("timestamp_ns")) or time.time_ns()
            if self.online_workload_markov is not None:
                self.online_workload_markov.observe_workload(
                    app_key=str(row.get("app_key", "")),
                    app_id=str(app_id),
                    scope_name=str(row.get("scope_name", "")),
                    workload_id=workload_id,
                    cgroup_id=cgroup_id,
                    workload_name=str(row.get("observed_workload_name", "")),
                    timestamp_ns=timestamp_ns,
                )
            if self.dual_workload_markov is not None:
                if cgroup_id is not None:
                    self.mglru_markov_writer.write_runtime_workload(
                        cgroup_id=cgroup_id, app_id=app_id, workload_id=workload_id,
                        app_key=str(row.get("app_key", "")),
                        workload_name=str(row.get("observed_workload_name", "")),
                    )
                self.dual_workload_markov.observe_workload(
                    app_key=str(row.get("app_key", "")),
                    app_id=app_id,
                    scope_name=str(row.get("scope_name", "")),
                    workload_id=workload_id,
                    cgroup_id=cgroup_id,
                    workload_name=str(row.get("observed_workload_name", "")),
                    timestamp_ns=timestamp_ns,
                    foreground_app_key=self.current_foreground_app_key,
                    foreground_app_id=self.current_foreground_app_id,
                    state_changed=_bool_text(row.get("state_changed", "true")),
                    sample_valid_scope=str(row.get("status", "ok")).strip().lower() == "ok",
                )
        except Exception as exc:
            print(f"warning: online workload Markov update failed: {exc}", file=sys.stderr)

    def _write_lstm_debugfs_audit(self) -> None:
        """合并 LSTM 相关用户态 debugfs 写入，便于逐行复核。"""
        fields = [
            "session_id", "timestamp_ns", "write_type", "command", "app_id", "cgroup_id",
            "probability_fixed", "ttl_ms", "status", "error",
        ]
        rows: list[dict[str, Any]] = []
        for path in (
            self.model_dir / "mglru_markov_debugfs_writes.csv",
            self.model_dir / "mglru_lstm_reclaim_policy_writes.csv",
        ):
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8", newline="") as f:
                    for row in csv.DictReader(f):
                        event = str(row.get("event_type", "") or row.get("write_type", ""))
                        if event not in {"current_app", "app_bind", "app_probability", "policy_config"}:
                            continue
                        rows.append({
                            "session_id": row.get("session_id", self.session_id),
                            "timestamp_ns": row.get("timestamp_ns", "") or _timestamp_text_to_ns(row.get("timestamp", "")),
                            "write_type": event,
                            "command": row.get("command", "") or row.get("policy_command", ""),
                            "app_id": row.get("app_id", "") or row.get("foreground_app_id", ""),
                            "cgroup_id": row.get("cgroup_id", "") or row.get("foreground_cgroup_id", ""),
                            "probability_fixed": row.get("probability_fixed", ""),
                            "ttl_ms": row.get("ttl_ms", ""),
                            "status": row.get("status", ""), "error": row.get("error", ""),
                        })
            except OSError as exc:
                print(f"warning: failed to audit LSTM debugfs writes: {exc}", file=sys.stderr)
        with (self.model_dir / "lstm_debugfs_writes.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader(); writer.writerows(rows)

    def _write_workload_markov_sets(self) -> None:
        if self.online_workload_markov_enabled:
            return
        if not self.args.enable_mglru_markov_debugfs:
            return
        if not self.workload_markov_transitions_csv.exists():
            print(
                f"warning: workload Markov 转移 CSV 不存在: "
                f"{self.workload_markov_transitions_csv}",
                file=sys.stderr,
            )
            return
        groups: dict[
            tuple[str, str, str, str], list[dict[str, int]]
        ] = {}
        try:
            with self.workload_markov_transitions_csv.open(
                "r", encoding="utf-8", newline=""
            ) as f:
                for row in csv.DictReader(f):
                    key = (
                        str(row.get("app_key", "")).strip(),
                        str(row.get("app_id", "")).strip(),
                        str(row.get("prev_workload_id", "")).strip(),
                        str(row.get("current_workload_id", "")).strip(),
                    )
                    next_workload_id = _optional_int(
                        row.get("next_workload_id")
                    )
                    confidence = _optional_int(row.get("confidence"))
                    boost_level = _optional_int(row.get("boost_level"))
                    if (
                        next_workload_id is None
                        or confidence is None
                        or boost_level is None
                    ):
                        continue
                    groups.setdefault(key, []).append(
                        {
                            "next_workload_id": next_workload_id,
                            "confidence": confidence,
                            "boost_level": boost_level,
                        }
                    )
            for (
                app_key,
                app_id,
                prev_workload_id,
                current_workload_id,
            ), entries in groups.items():
                self.mglru_markov_writer.write_markov_set(
                    app_id=_optional_int(app_id),
                    prev_workload_id=_optional_int(prev_workload_id),
                    current_workload_id=_optional_int(current_workload_id),
                    entries=entries,
                    app_key=app_key,
                )
        except Exception as exc:
            print(
                f"warning: workload Markov set 写入失败: {exc}",
                file=sys.stderr,
            )

    def _append_cgroup_workload_summary(self) -> None:
        summary_path = self.review_dir / "session_summary.md"
        lines = [
            "",
            "## Runtime 应用范围",
        ]
        if self.runtime_scope is not None:
            lines.extend(self.runtime_scope.summary_lines())
        else:
            lines.extend([
                f"- app_scope_config: `{self.args.app_scope_config}`",
                "- loaded_apps: none",
                "- workload_scopes: none",
                "- prediction_apps: none",
                "- app_key_to_vocab_name: none",
                "- app_scope_vocab_warnings: none",
            ])
        lines.extend([
            "",
            "## Cgroup Workload 采集器",
            f"- cgroup_workload_enabled: {str(bool(self.args.enable_cgroup_workload)).lower()}",
            f"- cgroup_workload_process_started: {str(self.cgroup_workload_process_started).lower()}",
            f"- cgroup_workload_raw_csv: `{self.cgroup_workload_raw_csv}`",
            f"- cgroup_workload_delta_csv: `{self.cgroup_workload_delta_csv}`",
            f"- cgroup_workload_summary: `{self.cgroup_workload_summary}`",
            f"- cgroup_workload_exit_code: {'' if self.cgroup_workload_exit_code is None else self.cgroup_workload_exit_code}",
            "",
            "## Region Monitor",
            f"- region_monitor_enabled: {str(bool(self.args.enable_region_monitor)).lower()}",
            f"- region_monitor_config: `{self.args.region_monitor_config}`",
            f"- region_monitor_process_started: {str(self.region_monitor_process_started).lower()}",
            f"- region_monitor_dir: `{self.region_monitor_dir}`",
            f"- region_monitor_summary: `{self.region_monitor_summary}`",
            f"- region_monitor_exit_code: {'' if self.region_monitor_exit_code is None else self.region_monitor_exit_code}",
            "",
            "## Cgroup Workload 分类器",
            f"- workload_classifier_enabled: {str(self.workload_classifier_enabled).lower()}",
            f"- cgroup_workload_state_csv: `{self.cgroup_workload_state_csv}`",
            f"- cgroup_workload_state_summary: `{self.cgroup_workload_state_summary}`",
            f"- workload_classifier_final_result: {self.workload_classifier_result.final_result if self.workload_classifier_result else 'NOT_RUN'}",
            "",
            "## Workload Markov 构建器",
            f"- workload_markov_builder_enabled: {str(self.workload_markov_builder_enabled).lower()}",
            f"- workload_markov_transitions_csv: `{self.workload_markov_transitions_csv}`",
            f"- workload_markov_summary: `{self.workload_markov_summary}`",
            f"- workload_markov_builder_final_result: {self.workload_markov_result.final_result if self.workload_markov_result else 'NOT_RUN'}",
            f"- online_workload_markov_enabled: {str(self.online_workload_markov_enabled).lower()}",
            f"- dual_workload_markov_enabled: {str(self.dual_workload_markov_enabled).lower()}",
            f"- dual_markov_runtime_mode: {'dual' if self.dual_workload_markov_enabled else 'not_configured'}",
            f"- dual_markov_reentry_window_s: {self.args.dual_markov_reentry_window_s}",
            f"- dual_markov_ignore_initial_low_activity_s: {self.args.dual_markov_ignore_initial_low_activity_s}",
            f"- dual_markov_foreground_history: `{self.model_dir / 'foreground_workload_history.csv'}`",
            f"- dual_markov_foreground_epochs: `{self.model_dir / 'foreground_epochs.csv'}`",
            f"- dual_markov_continue_transitions: `{self.model_dir / 'continue_markov_transitions.csv'}`",
            f"- dual_markov_reentry_transitions: `{self.model_dir / 'reentry_markov_transitions.csv'}`",
            f"- dual_markov_reentry_events: `{self.model_dir / 'reentry_events.csv'}`",
            f"- dual_markov_policy_suggestions: `{self.model_dir / 'dual_markov_policy_suggestions.csv'}`",
            f"- dual_markov_debugfs_writes: `{self.model_dir / 'dual_markov_debugfs_writes.csv'}`",
            f"- dual_foreground_workload_update_ok: {self.mglru_markov_writer.foreground_workload_update_ok}",
            f"- dual_continue_markov_set_ok: {self.mglru_markov_writer.continue_markov_set_ok}",
            f"- dual_reentry_markov_set_ok: {self.mglru_markov_writer.reentry_markov_set_ok}",
            f"- dual_markov_result: {self.dual_workload_markov.result().get('final_result', 'NOT_RUN') if self.dual_workload_markov else 'NOT_RUN'}",
            f"- workload_markov_online_updates: `{self.model_dir / 'workload_markov_online_updates.csv'}`",
            f"- workload_markov_online_transitions: `{self.model_dir / 'workload_markov_online_transitions.csv'}`",
            f"- workload_markov_online_predictions: `{self.model_dir / 'workload_markov_online_predictions.csv'}`",
            f"- workload_markov_online_debugfs_writes: `{self.model_dir / 'workload_markov_online_debugfs_writes.csv'}`",
            f"- markov_live_causality_audit: `{self.model_dir / 'markov_live_causality_audit.csv'}`",
            f"- online_markov_final_result: {self.online_workload_markov.result().final_result if self.online_workload_markov else 'NOT_RUN'}",
            f"- markov_set_write_ok: {self.mglru_markov_writer.markov_set_write_ok}",
            "",
            "## MGLRU Markov Debugfs",
            f"- mglru_markov_debugfs_enabled: {str(bool(self.args.enable_mglru_markov_debugfs)).lower()}",
            f"- mglru_markov_debugfs_path: `{self.args.mglru_markov_debugfs_path}`",
            f"- mglru_markov_debugfs_csv: `{self.mglru_markov_writer.csv_path}`",
            f"- mglru_markov_debugfs_summary: `{self.mglru_markov_writer.summary_path}`",
            f"- workload_update_write_attempts: {self.mglru_markov_writer.workload_update_write_attempts}",
            f"- workload_update_write_ok: {self.mglru_markov_writer.workload_update_write_ok}",
            f"- workload_update_write_error: {self.mglru_markov_writer.workload_update_write_error}",
            f"- workload_update_skipped: {self.mglru_markov_writer.workload_update_skipped}",
            f"- markov_set_write_attempts: {self.mglru_markov_writer.markov_set_write_attempts}",
            f"- markov_set_write_ok: {self.mglru_markov_writer.markov_set_write_ok}",
            f"- markov_set_write_error: {self.mglru_markov_writer.markov_set_write_error}",
            f"- markov_set_skipped: {self.mglru_markov_writer.markov_set_skipped}",
            f"- mglru_markov_debugfs_final_result: {self.mglru_markov_writer.final_result()}",
            "",
            "## MGLRU LSTM 应用级回收策略",
            f"- mglru_lstm_reclaim_policy_enabled: {str(bool(self.args.enable_mglru_lstm_reclaim_policy)).lower()}",
            f"- mglru_lstm_reclaim_policy_config: `{self.mglru_lstm_policy.config_path}`",
            f"- mglru_lstm_reclaim_policy_csv: `{self.mglru_lstm_policy.csv_path}`",
            f"- mglru_lstm_reclaim_policy_summary: `{self.mglru_lstm_policy.summary_path}`",
            f"- policy_config_write_ok: {self.mglru_lstm_policy.policy_config_write_ok}",
            f"- app_bind_write_ok: {self.mglru_lstm_policy.app_bind_write_ok}",
            f"- app_probability_write_ok: {self.mglru_lstm_policy.app_probability_write_ok}",
            f"- probability_source: {','.join(sorted(self.mglru_lstm_policy.probability_sources)) or 'none'}",
            f"- probability_is_rank_based: {str(self.mglru_lstm_policy.probability_is_rank_based()).lower()}",
            f"- mglru_lstm_policy_strict_result: {self.mglru_lstm_policy.strict_result()}",
        ])
        try:
            self.review_dir.mkdir(parents=True, exist_ok=True)
            with summary_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as exc:
            print(f"warning: failed to append cgroup workload summary: {exc}", file=sys.stderr)

    def _maybe_write_mglru_current_app(self, feature_row: dict[str, Any]) -> None:
        if not (
            self.args.enable_mglru_markov_debugfs
            or self.args.enable_mglru_lstm_reclaim_policy
        ) or self.runtime_scope is None:
            return
        app_key = str(feature_row.get("foreground_app", "")).strip()
        if not app_key or app_key == self.last_mglru_foreground_app_key:
            return
        app_id = self.runtime_scope.app_key_to_app_id.get(app_key)
        scope_name = self.runtime_scope.app_key_to_scope_name.get(app_key, "")
        if not app_id:
            print(f"warning: MGLRU Markov current app has no app_id: {app_key}", file=sys.stderr)
            return
        cgroup_id = self._resolve_mglru_scope_cgroup_id(scope_name)
        if self.args.enable_mglru_lstm_reclaim_policy and cgroup_id is not None:
            binding_ttl_ms = max(
                self.mglru_lstm_policy.ttl_ms,
                int(self.args.mglru_app_binding_refresh_s * 3000),
            )
            self.mglru_lstm_policy.refresh_bindings(
                [
                    app
                    for app in self.runtime_scope.apps
                    if app.app_key == app_key
                ],
                self._resolve_mglru_scope_cgroup_id,
                binding_ttl_ms,
            )
        self.mglru_markov_writer.write_current_app(
            app_key=app_key,
            app_id=app_id,
            cgroup_id=cgroup_id,
            ttl_ms=self.args.mglru_markov_ttl_ms,
        )
        if cgroup_id is not None:
            self.last_mglru_foreground_app_key = app_key

    def _maybe_write_mglru_predictions(self, prediction_result: dict[str, Any]) -> None:
        if self.runtime_scope is None:
            return
        if prediction_result.get("status") != "success":
            return
        outputs = prediction_result.get("outputs", [])
        if not isinstance(outputs, list):
            return
        if self.args.enable_mglru_markov_debugfs:
            rows = [row for row in outputs if int(row.get("horizon", -1)) == 3]
            rows.sort(key=lambda row: int(row.get("rank", 0)))
            predictions: list[tuple[int, int]] = []
            for row in rows:
                app_name = str(row.get("app", "")).strip()
                app_id = self.runtime_scope.vocab_name_to_app_id.get(app_name)
                if not app_id or app_id not in self.runtime_scope.prediction_enabled_app_ids:
                    self.mglru_markov_writer.skip_prediction(f"missing_or_disabled_app_id:{app_name}")
                    continue
                rank_index = len(predictions)
                confidence = RANK_BASED_CONFIDENCE[rank_index] if rank_index < len(RANK_BASED_CONFIDENCE) else 1000
                predictions.append((app_id, confidence))
                if len(predictions) >= len(RANK_BASED_CONFIDENCE):
                    break
            self.mglru_markov_writer.write_predicted_apps(
                predictions, ttl_ms=self.args.mglru_markov_ttl_ms
            )

        if self.args.enable_mglru_lstm_reclaim_policy:
            all_rows = prediction_result.get("all_probabilities", [])
            probability_rows: list[dict[str, Any]] = []
            for row in all_rows if isinstance(all_rows, list) else []:
                if int(row.get("horizon", -1)) != 3:
                    continue
                app_name = str(row.get("app", "")).strip()
                app_id = self.runtime_scope.vocab_name_to_app_id.get(app_name)
                if not app_id or app_id not in self.runtime_scope.prediction_enabled_app_ids:
                    continue
                app_key = next(
                    (
                        app.app_key
                        for app in self.runtime_scope.apps
                        if app.app_id == app_id
                    ),
                    "",
                )
                probability_rows.append(
                    {
                        "app_key": app_key,
                        "app_id": app_id,
                        "probability": row.get("next_use_probability", row.get("probability")),
                        "probability_source": row.get("probability_source", "unavailable"),
                    }
                )
            self.mglru_lstm_policy.write_probabilities(probability_rows)

    def _maybe_refresh_mglru_app_bindings(self, *, force: bool = False) -> None:
        if not self.args.enable_mglru_lstm_reclaim_policy or self.runtime_scope is None:
            return
        now = time.monotonic()
        if not force and now - self.last_mglru_binding_refresh_monotonic < self.args.mglru_app_binding_refresh_s:
            return
        ttl_ms = max(
            self.mglru_lstm_policy.ttl_ms,
            int(self.args.mglru_app_binding_refresh_s * 3000),
        )
        self.mglru_lstm_policy.refresh_bindings(
            self.runtime_scope.apps,
            self._resolve_mglru_scope_cgroup_id,
            ttl_ms,
        )
        if self.last_mglru_foreground_app_key:
            app_key = self.last_mglru_foreground_app_key
            app_id = self.runtime_scope.app_key_to_app_id.get(app_key)
            scope_name = self.runtime_scope.app_key_to_scope_name.get(app_key, "")
            cgroup_id = self._resolve_mglru_scope_cgroup_id(scope_name)
            if app_id and cgroup_id:
                self.mglru_markov_writer.write_current_app(
                    app_key=app_key,
                    app_id=app_id,
                    cgroup_id=cgroup_id,
                    ttl_ms=self.args.mglru_markov_ttl_ms,
                )
        self.last_mglru_binding_refresh_monotonic = now

    def _resolve_mglru_scope_cgroup_id(self, scope_name: str) -> int | None:
        slice_name = self.args.cgroup_workload_slice or self.args.test_slice
        key = (slice_name, scope_name)
        if key in self._scope_cgroup_cache:
            return self._scope_cgroup_cache[key]
        cgroup_id = resolve_scope_cgroup_id(slice_name, scope_name)
        if cgroup_id is not None:
            self._scope_cgroup_cache[key] = cgroup_id
        return cgroup_id


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Runtime Monitor v0 PC state collector.")
    parser.add_argument("--config", default=PROJECT_ROOT / "configs" / "runtime" / "config.yaml")
    parser.add_argument("--app-scope-config", default=PROJECT_ROOT / "configs" / "runtime" / "runtime_app_scope.json")
    parser.add_argument("--target-app", default="WPS")
    parser.add_argument("--target-apps", default="", help="Comma-separated observed apps, e.g. WPS,QQ,FILES.")
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "outputs" / "runtime_monitor"))
    parser.add_argument("--path-mode", choices=["raw", "hash", "basename"], default="hash")
    parser.add_argument("--target-pid", type=int)
    parser.add_argument("--target-comm")
    parser.add_argument("--duration", type=float, default=0.0)
    parser.add_argument("--enable-ebpf", action="store_true", help="Reserved.")
    parser.add_argument("--disable-ebpf", action="store_true", help="Use procfs polling fallback.")
    parser.add_argument("--foreground-backend", choices=["x11", "wayland", "manual"], default="x11")
    parser.add_argument(
        "--direct-x11-events",
        action="store_true",
        help="Consume native X11 focus/window/state events; do not use X11 polling for app event detection.",
    )
    parser.add_argument("--label", default="", help="Default manual label for all feature rows.")
    parser.add_argument("--session-id", default="", help="Session id; defaults to YYYYMMDD_HHMMSS.")
    parser.add_argument("--test-slice", default="", help="systemd slice to scope monitoring to.")
    parser.add_argument("--close-grace-windows", type=int, default=2, help="Consecutive empty windows before APP_CLOSE.")
    parser.add_argument("--enable-online-lstm", action="store_true", help="Enable online duration-aware switch LSTM prediction.")
    parser.add_argument("--lstm-model-type", choices=["duration", "v2", "v3"], default="duration", help="Online LSTM checkpoint contract.")
    parser.add_argument("--enable-parp-bridge", action="store_true", help="Enable the independent PARP prediction sink bridge.")
    parser.add_argument("--parp-debugfs-root", default="/sys/kernel/debug/parp")
    parser.add_argument("--parp-bridge-mode", choices=["off", "dry-run", "shadow-write"], default="off")
    parser.add_argument("--parp-app-bind-config", default="", help="Optional app_key -> domain_id/memcg_path JSON.")
    parser.add_argument("--parp-model-name", default="AppLSTM")
    parser.add_argument("--parp-model-version", type=int, default=401)
    parser.add_argument("--parp-schema-version", default="parp-app-prior-v1")
    parser.add_argument("--parp-prior-ttl-ms", type=int, default=180000)
    parser.add_argument("--parp-min-update-interval-ms", type=int, default=0)
    parser.add_argument("--parp-max-retries", type=int, default=2)
    parser.add_argument("--enable-app-reclaim-controller", action="store_true")
    parser.add_argument("--app-reclaim-mode", choices=["off", "shadow", "apply-bounded"], default="shadow")
    parser.add_argument("--app-activity-interval-s", type=float, default=.5)
    parser.add_argument("--reclaim-activity-ema-rho", type=float, default=.7)
    parser.add_argument("--reclaim-probability-threshold", type=float, default=.05)
    parser.add_argument("--reclaim-required-low-probability-batches", type=int, default=2)
    parser.add_argument("--reclaim-activity-threshold", type=float, default=.10)
    parser.add_argument("--reclaim-required-low-activity-windows", type=int, default=3)
    parser.add_argument("--reclaim-minimum-rss-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--reclaim-step-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--reclaim-per-app-cooldown-ms", type=int, default=10_000)
    parser.add_argument("--reclaim-global-cooldown-ms", type=int, default=3_000)
    parser.add_argument("--reclaim-target-headroom-bytes", type=int, default=2500 * 1024 * 1024)
    parser.add_argument("--reclaim-minimum-headroom-deficit-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--reclaim-max-ratio-per-action", type=float, default=.10)
    parser.add_argument("--reclaim-minimum-app-resident-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--reclaim-max-actions-per-minute", type=int, default=3)
    parser.add_argument("--reclaim-max-actions-per-episode", type=int, default=1)
    parser.add_argument("--reclaim-max-bytes-per-app-session", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--reclaim-max-bytes-global-session", type=int, default=128 * 1024 * 1024)
    parser.add_argument("--reclaim-hard-min-available-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--reclaim-psi-full-abort-avg10", type=float, default=.20)
    parser.add_argument("--reclaim-apply-preflight-ready", action="store_true", help="Only set by a READY Test4 preflight; apply otherwise fail-closed.")
    parser.add_argument("--enable-test4b-ballast", action="store_true", help="Enable Test4B foreground-only synthetic mixed working sets.")
    parser.add_argument("--test4b-ballast-config", default="", help="Per-session Test4B ballast JSON generated by its runner.")
    parser.add_argument("--test4b-reclaim-mode", choices=["off", "shadow", "apply-bounded"], default="shadow")
    parser.add_argument("--test4b-probability-threshold", type=float, default=.10)
    parser.add_argument("--test4b-required-low-probability-batches", type=int, default=2)
    parser.add_argument("--test4b-cold-quiet-s", type=float, default=3.0)
    parser.add_argument("--test4b-reclaim-step-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--test4b-target-headroom-bytes", type=int, default=2500 * 1024 * 1024)
    parser.add_argument("--test4b-minimum-headroom-deficit-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--test4b-per-app-cooldown-ms", type=int, default=10_000)
    parser.add_argument("--test4b-global-cooldown-ms", type=int, default=3_000)
    parser.add_argument("--test4b-max-reclaims-per-session", type=int, default=1)
    parser.add_argument("--test4b-max-reclaim-bytes-per-session", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--test4b-hard-min-available-bytes", type=int, default=512 * 1024 * 1024)
    parser.add_argument("--test4b-psi-full-abort-avg10", type=float, default=.20)
    parser.add_argument("--test4b-controller-activation-file", default="", help="Optional Test4B marker; predictions before it exists never create reclaim decisions.")
    parser.add_argument("--test4b-apply-preflight-ready", action="store_true", help="Permit a fresh READY Test4B one-shot apply only.")
    parser.add_argument(
        "--enable-memory-shadow", action="store_true",
        help="Test3 observe-only per-app procfs/cgroup memory shadow collector.",
    )
    parser.add_argument("--memory-shadow-interval-s", type=float, default=0.25)
    parser.add_argument("--memory-shadow-top-k", type=int, default=3)
    parser.add_argument("--memory-shadow-recovery-window-s", type=float, default=3.0)
    parser.add_argument("--enable-cgroup-workload", action="store_true", help="Enable lightweight cgroup v2 memory workload collector.")
    parser.add_argument("--enable-region-monitor", action="store_true", help="Enable observe-only cgroup + DAMON region monitor sidecar.")
    parser.add_argument(
        "--region-monitor-config",
        default=MONITOR_DIR / "config" / "region_monitor.json",
        help="Region monitor JSON config. The feature remains disabled unless --enable-region-monitor is set.",
    )
    parser.add_argument(
        "--disable-workload-classifier",
        action="store_true",
        help="Disable offline cgroup workload state classification.",
    )
    parser.add_argument(
        "--disable-workload-markov-builder",
        action="store_true",
        help="禁用离线 workload 二阶 Markov 转移构建。",
    )
    parser.add_argument(
        "--enable-online-workload-markov",
        action="store_true",
        help="在同一 monitor 采样时钟内实时分类 workload 并更新 Online Causal Markov。",
    )
    parser.add_argument(
        "--enable-dual-workload-markov",
        action="store_true",
        help="启用前台 CONTINUE 与后台回切 REENTRY 的双模式 workload Markov 观测。",
    )
    parser.add_argument(
        "--dual-markov-reentry-window-s", type=float, default=5.0,
        help="REENTRY 回切观察窗口，默认 5 秒。",
    )
    parser.add_argument(
        "--dual-markov-ignore-initial-low-activity-s", type=float, default=2.0,
        help="REENTRY 窗口内忽略初始 LOW_ACTIVITY 的时间，默认 2 秒。",
    )
    parser.add_argument("--cgroup-workload-interval-s", type=float, default=1.0, help="Cgroup workload sampling interval in seconds.")
    parser.add_argument(
        "--cgroup-workload-scopes",
        default="automation-wps.scope,automation-qq.scope,automation-files.scope",
        help="Comma-separated app scope names under the cgroup workload slice.",
    )
    parser.add_argument("--cgroup-workload-slice", default="huawei-test.slice", help="User systemd slice for cgroup workload collection.")
    parser.add_argument("--enable-mglru-markov-debugfs", action="store_true", help="Write current app and online LSTM top-k apps to MGLRU Markov debugfs.")
    parser.add_argument("--mglru-markov-debugfs-path", default="/sys/kernel/debug/lru_gen_workload_markov")
    parser.add_argument("--mglru-markov-ttl-ms", type=int, default=180000)
    parser.add_argument("--mglru-markov-strict", action="store_true", help="Mark MGLRU Markov debugfs result FAIL if debugfs writes cannot succeed.")
    parser.add_argument(
        "--enable-mglru-lstm-reclaim-policy",
        action="store_true",
        help="启用基于 LSTM next-use 概率的应用级 MGLRU 扫描预算策略写入。",
    )
    parser.add_argument(
        "--mglru-lstm-reclaim-policy-config",
        default=PROJECT_ROOT / "configs" / "runtime" / "mglru_lstm_reclaim_policy.json",
    )
    parser.add_argument("--mglru-app-binding-refresh-s", type=float, default=30.0)
    parser.add_argument(
        "--mglru-lstm-policy-strict",
        action="store_true",
        help="要求策略配置、至少一个 app bind 和真实概率写入全部成功。",
    )
    parser.add_argument(
        "--lstm-checkpoint",
        default=MONITOR_DIR.parent / "operation_predictor" / "outputs" / "checkpoints" / "app_lstm_duration" / "lsapp_app_lstm_duration_switch.pt",
    )
    parser.add_argument(
        "--app-vocab",
        default=MONITOR_DIR.parent / "operation_predictor" / "data" / "vocab" / "app_vocab_duration.json",
    )
    parser.add_argument("--app-mapping", default=PROJECT_ROOT / "configs" / "runtime" / "app_mapping.json")
    parser.add_argument(
        "--group-vocab",
        default=MONITOR_DIR.parent / "operation_predictor" / "data" / "vocab" / "user_group_vocab.json",
    )
    parser.add_argument("--user-group", default="通用用户")
    parser.add_argument("--history-len", type=int, default=5)
    parser.add_argument("--duration-cap-s", type=float, default=600.0)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--score-mode", choices=["softmax", "sigmoid"], default="sigmoid")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--trigger-mode", choices=["event_plus_ttl", "event_only"], default="event_plus_ttl")
    parser.add_argument("--prediction-ttl-s", type=float, default=180.0)
    parser.add_argument("--periodic-refresh-s", type=float, default=180.0)
    parser.add_argument("--min-event-cooldown-s", type=float, default=5.0)
    if hasattr(argparse, "BooleanOptionalAction"):
        parser.add_argument(
            "--disable-dwell-bucket-trigger",
            action=argparse.BooleanOptionalAction,
            default=True,
        )
    else:
        dwell_group = parser.add_mutually_exclusive_group()
        dwell_group.add_argument(
            "--disable-dwell-bucket-trigger",
            dest="disable_dwell_bucket_trigger",
            action="store_true",
        )
        dwell_group.add_argument(
            "--no-disable-dwell-bucket-trigger",
            dest="disable_dwell_bucket_trigger",
            action="store_false",
        )
        parser.set_defaults(disable_dwell_bucket_trigger=True)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def _load_runtime_scope(args: argparse.Namespace) -> RuntimeAppScope | None:
    if not args.app_scope_config:
        return None
    path = _resolve_project_path(args.app_scope_config, legacy_base=MONITOR_DIR)
    if not path.exists():
        print(f"warning: app scope config not found: {path}", file=sys.stderr)
        return None
    return load_runtime_app_scope(path, args.app_vocab)


def _resolve_project_path(value: str | Path, legacy_base: Path | None = None) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute() or path.exists():
        return path
    project_path = PROJECT_ROOT / path
    if project_path.exists():
        return project_path
    if legacy_base is not None:
        legacy_path = legacy_base / path
        if legacy_path.exists():
            return legacy_path
    return project_path


def _parse_app_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _optional_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bool_text(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _timestamp_text_to_ns(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1_000_000_000)
    except ValueError:
        return 0


def _resolve_session_id(args: argparse.Namespace) -> str:
    if args.session_id:
        return args.session_id
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _read_test_slice_memory(test_slice: str) -> dict[str, int]:
    """Read memory.current, memory.high, memory.max from a cgroup slice."""
    if not test_slice:
        return {"current": 0, "high": 0, "max": 0}
    # Resolve the real cgroup path via systemctl
    base = Path("/sys/fs/cgroup")
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", test_slice, "-p", "ControlGroup"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.startswith("ControlGroup="):
                    cg = line.split("=", 1)[1].strip()
                    if cg:
                        base = Path("/sys/fs/cgroup") / cg.lstrip("/")
    except (OSError, subprocess.TimeoutExpired):
        pass

    out: dict[str, int] = {"current": 0, "high": 0, "max": 0}
    for key in out:
        try:
            text = (base / f"memory.{key}").read_text(encoding="utf-8").strip()
            out[key] = int(text)
        except (OSError, ValueError):
            pass
    return out


# ------------------------------------------------------------------
# entry point
# ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.parp_bridge_mode != "off" and not args.enable_parp_bridge:
        print(
            "error: --parp-bridge-mode requires --enable-parp-bridge",
            file=sys.stderr,
        )
        return 2
    if args.enable_ebpf:
        print("warning: --enable-ebpf is reserved; v0 uses procfs polling fallback.", file=sys.stderr)
    if args.foreground_backend == "wayland":
        print("warning: Wayland foreground collection is not reliable in v0; using manual fallback.", file=sys.stderr)
        args.foreground_backend = "manual"
    monitor = RuntimeMonitorV0(args)
    return monitor.run()


if __name__ == "__main__":
    raise SystemExit(main())
