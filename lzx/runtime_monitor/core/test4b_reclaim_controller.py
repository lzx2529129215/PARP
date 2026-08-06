"""Test4B's one-shot synthetic-ground-truth ``memory.reclaim`` controller.

This is deliberately separate from Test4's observed-activity controller.  A
candidate is admitted by the known state of its *synthetic* cold regions, not
by Referenced/RSS.  The output records both values so the limitation remains
explicit.  The writer is bounded to one 16 MiB request per session.
"""
from __future__ import annotations

import csv
import errno
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.app_memory_activity import AppActivity
from core.app_reclaim_controller import _memory_events, _parse_psi_avg10, system_state
from core.memory_shadow import read_cgroup_memory, read_smaps_rollup
from core.test4b_ballast import BallastStatus
from core.writer import CsvWriter


MIB = 1024 * 1024
DECISION_FIELDS = [
    "timestamp_ns", "decision_id", "prediction_id", "trigger_event_id", "trigger_type",
    "current_app", "candidate_app", "candidate_app_id", "candidate_probability",
    "low_probability_batch_count", "candidate_background", "ballast_state",
    "ballast_allocated", "cold_quiet_ms", "ground_truth_inactive",
    "observed_activity_state", "observed_referenced_rss_ema", "memcg_path",
    "memory_current_before", "mem_available_bytes", "target_headroom_bytes",
    "headroom_deficit_bytes", "requested_reclaim_bytes", "decision", "mode",
    "candidate_level", "base_candidate", "if_needed_eligible", "memory_need",
    "apply_label", "skip_reason",
]
EVALUATION_FIELDS = [
    "timestamp_ns", "prediction_id", "app_key", "probability", "low_probability_batch_count",
    "foreground_state", "app_bind_ready", "memcg_path", "ballast_state", "ballast_allocated",
    "cold_quiet_ms", "ground_truth_inactive", "observed_activity_state",
    "observed_referenced_rss_ema", "candidate_level", "base_rejection_reason",
    "if_needed_rejection_reason", "eligible", "rejection_reason",
]
ATTEMPT_FIELDS = [
    "decision_id", "timestamp_ns", "app_key", "memcg_path", "requested_bytes",
    "write_attempted", "write_success", "errno", "write_latency_us", "mode", "apply_label",
]
OBSERVATION_FIELDS = [
    "decision_id", "sample_label", "timestamp_ns", "app_key", "memcg_path", "memory_current",
    "anon", "file", "inactive_anon", "active_anon", "inactive_file", "active_file",
    "file_dirty", "file_writeback", "pgscan", "pgsteal", "workingset_refault_anon",
    "workingset_refault_file", "memory_swap_current", "mem_available", "psi_some", "psi_full",
    "root_oom", "root_oom_kill", "ballast_rss", "ballast_pss", "app_rss", "app_pss", "error",
]
SAFETY_FIELDS = ["timestamp_ns", "reason", "mem_available_bytes", "psi_full", "root_oom", "root_oom_kill", "mode"]
PROBABILITY_INPUT_FIELDS = ["timestamp_ns", "prediction_id", "raw_app", "payload_app_key", "resolved_app_key", "probability", "accepted"]


@dataclass(frozen=True)
class Test4BReclaimConfig:
    mode: str = "shadow"  # off | shadow | apply-bounded
    probability_threshold: float = .10
    required_low_probability_batches: int = 2
    cold_quiet_s: float = 3.0
    step_bytes: int = 16 * MIB
    target_headroom_bytes: int = 2500 * MIB
    minimum_headroom_deficit_bytes: int = 16 * MIB
    per_app_cooldown_ms: int = 10_000
    global_cooldown_ms: int = 3_000
    max_reclaims_per_session: int = 1
    max_reclaim_bytes_per_session: int = 16 * MIB
    hard_min_available_bytes: int = 512 * MIB
    psi_full_abort_avg10: float = .20
    preflight_ready: bool = False
    # Test4B-2 creates all synthetic pages before the scored validation
    # sequence.  A per-session marker prevents bootstrap/preallocation
    # switches from becoming reclaim decisions while retaining event-driven
    # inference for the actual validation sequence.
    activation_file: str = ""


@dataclass
class _Pending:
    decision_id: str
    app: str
    path: Path
    due: list[tuple[str, int]]
    next_index: int = 0


def _stat(path: Path) -> dict[str, int]:
    values, _ = read_cgroup_memory(path)
    try:
        values["memory_swap_current"] = int((path / "memory.swap.current").read_text().strip())
    except (OSError, ValueError):
        values["memory_swap_current"] = 0
    try:
        raw = {line.split()[0]: int(line.split()[1]) for line in (path / "memory.stat").read_text().splitlines() if len(line.split()) == 2}
    except (OSError, ValueError):
        raw = {}
    values["file_dirty"] = raw.get("file_dirty", 0)
    values["file_writeback"] = raw.get("file_writeback", 0)
    return values


class Test4BReclaimController:
    def __init__(self, *, session_id: str, output_dir: Path, config: Test4BReclaimConfig,
                 app_ids: dict[str, int], app_name_to_key: dict[str, str] | None = None,
                 test_slice_marker: str = "test4b-experiment.slice") -> None:
        self.session_id, self.cfg, self.app_ids, self.marker = session_id, config, dict(app_ids), test_slice_marker
        self.app_name_to_key = dict(app_name_to_key or {})
        self.low: dict[str, int] = {}
        self.last_app_ns: dict[str, int] = {}
        self.last_global_ns = 0
        self.actions = 0
        self.bytes = 0
        self.sequence = 0
        self.pending: list[_Pending] = []
        self.last_successful_reclaim_app = ""
        self.aborted = ""
        self.root_oom_baseline: int | None = None
        directory = Path(output_dir) / "reclaim"
        self.decisions = CsvWriter(directory / "test4b_reclaim_decisions.csv", DECISION_FIELDS)
        self.evaluations = CsvWriter(directory / "test4b_candidate_evaluations.csv", EVALUATION_FIELDS)
        self.attempts = CsvWriter(directory / "test4b_memory_reclaim_attempts.csv", ATTEMPT_FIELDS)
        self.observations = CsvWriter(directory / "test4b_reclaim_observations.csv", OBSERVATION_FIELDS)
        self.safety = CsvWriter(directory / "test4b_safety_events.csv", SAFETY_FIELDS)
        self.probability_inputs = CsvWriter(directory / "test4b_probability_inputs.csv", PROBABILITY_INPUT_FIELDS)

    def _probabilities(self, result: dict[str, Any], *, prediction_id: str = "", timestamp_ns: int = 0) -> dict[str, float]:
        answer: dict[str, float] = {}
        for row in result.get("all_probabilities", []):
            try:
                value = float(row.get("probability", -1))
            except (TypeError, ValueError):
                self.probability_inputs.write_row({"timestamp_ns": timestamp_ns, "prediction_id": prediction_id, "raw_app": str(row.get("app", "")), "payload_app_key": str(row.get("app_key", "")), "resolved_app_key": "", "probability": row.get("probability", ""), "accepted": "false"})
                continue
            raw_name = str(row.get("app", ""))
            app = str(row.get("app_key", "")) or self.app_name_to_key.get(raw_name, "")
            # v3's runtime payload is external evidence, so retain a
            # fail-closed-but-useful canonical fallback for the Linux test
            # vocabulary ("Firefox" -> FIREFOX).  It can only select an
            # app already explicitly in this controller's whitelist.
            if not app:
                canonical = "".join(character for character in raw_name.upper() if character.isalnum())
                if canonical in self.app_ids:
                    app = canonical
            accepted = bool(app and 0 <= value <= 1)
            self.probability_inputs.write_row({"timestamp_ns": timestamp_ns, "prediction_id": prediction_id, "raw_app": raw_name, "payload_app_key": str(row.get("app_key", "")), "resolved_app_key": app, "probability": value, "accepted": str(accepted).lower()})
            if accepted:
                answer[app] = value
        return answer

    def _safe_path(self, path: Path) -> bool:
        root = Path("/sys/fs/cgroup")
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return self.marker in str(path) and path != root and (path / "memory.reclaim").is_file()

    def _safety(self, state: dict[str, Any], now: int) -> str:
        root_events = state.get("root_memory_events", {})
        oom = int(root_events.get("oom", 0)) + int(root_events.get("oom_kill", 0))
        if self.root_oom_baseline is None:
            self.root_oom_baseline = oom
        elif oom > self.root_oom_baseline:
            self.aborted = "OOM_EVENT_INCREASED"
        if int(state.get("available", 0)) < self.cfg.hard_min_available_bytes:
            self.aborted = "MEM_AVAILABLE_BELOW_HARD_FLOOR"
        if _parse_psi_avg10(str(state.get("psi_full", ""))) >= self.cfg.psi_full_abort_avg10:
            self.aborted = "MEMORY_PSI_FULL_ABORT_THRESHOLD"
        if self.aborted:
            self.safety.write_row({"timestamp_ns": now, "reason": self.aborted,
                                   "mem_available_bytes": state.get("available", 0), "psi_full": state.get("psi_full", ""),
                                   "root_oom": root_events.get("oom", 0), "root_oom_kill": root_events.get("oom_kill", 0), "mode": self.cfg.mode})
        return self.aborted

    def observe_prediction(self, *, prediction_id: str, trigger_event_id: str, trigger_type: str,
                           current_app: str, result: dict[str, Any], ballast: dict[str, BallastStatus],
                           activities: dict[str, AppActivity], bind_ready: set[str], now_ns: int | None = None) -> str:
        if self.cfg.mode == "off":
            return ""
        if self.cfg.activation_file and not Path(self.cfg.activation_file).is_file():
            return ""
        now = int(now_ns or time.time_ns())
        self.tick(now)
        probabilities = self._probabilities(result, prediction_id=prediction_id, timestamp_ns=now)
        for app in self.app_ids:
            self.low[app] = self.low.get(app, 0) + 1 if probabilities.get(app, 1.0) < self.cfg.probability_threshold else 0
        state = system_state()
        deficit = max(0, self.cfg.target_headroom_bytes - int(state.get("available", 0)))
        safety = self._safety(state, now)
        base_candidates: list[tuple[float, int, int, str, BallastStatus, Path, str]] = []
        reasons: list[str] = []
        for app, status in ballast.items():
            probability = probabilities.get(app, 1.0)
            path = Path(status.expected_cgroup) if status.expected_cgroup else None
            activity = activities.get(app)
            observed = "UNAVAILABLE" if activity is None else activity.status
            ema = "" if activity is None or activity.ema is None else activity.ema
            quiet_ms = status.quiet_ns(now) // 1_000_000
            base_reason = ""
            if app == current_app: base_reason = "FOREGROUND_PROTECTED"
            elif app not in bind_ready: base_reason = "APP_BIND_INVALID"
            elif status.actual_cgroup != status.expected_cgroup: base_reason = "BALLAST_WRONG_CGROUP"
            elif not path or not self._safe_path(path): base_reason = "CGROUP_UNAVAILABLE"
            elif not status.allocated: base_reason = "BALLAST_NOT_ALLOCATED"
            elif status.state != "BACKGROUND_IDLE": base_reason = "BALLAST_NOT_BACKGROUND_IDLE"
            elif quiet_ms < int(self.cfg.cold_quiet_s * 1000): base_reason = "COLD_QUIET_TIME_NOT_REACHED"
            elif probability >= self.cfg.probability_threshold or self.low.get(app, 0) < self.cfg.required_low_probability_batches: base_reason = "LOW_PROBABILITY_NOT_STABLE"
            if_needed_reason = ""
            if not base_reason:
                if safety: if_needed_reason = safety
                elif self.actions >= self.cfg.max_reclaims_per_session or self.bytes >= self.cfg.max_reclaim_bytes_per_session: if_needed_reason = "SESSION_BUDGET_EXHAUSTED"
                elif self.last_global_ns and now - self.last_global_ns < self.cfg.global_cooldown_ms * 1_000_000: if_needed_reason = "GLOBAL_COOLDOWN"
                elif app in self.last_app_ns and now - self.last_app_ns[app] < self.cfg.per_app_cooldown_ms * 1_000_000: if_needed_reason = "APP_COOLDOWN"
            level = "NONE" if base_reason else ("RECLAIM_CANDIDATE" if if_needed_reason else ("WOULD_RECLAIM" if deficit >= self.cfg.minimum_headroom_deficit_bytes else "WOULD_RECLAIM_IF_NEEDED"))
            self.evaluations.write_row({
                "timestamp_ns": now, "prediction_id": prediction_id, "app_key": app, "probability": probability,
                "low_probability_batch_count": self.low.get(app, 0), "foreground_state": "FOREGROUND" if app == current_app else "BACKGROUND",
                "app_bind_ready": str(app in bind_ready).lower(), "memcg_path": str(path or ""),
                "ballast_state": status.state, "ballast_allocated": str(status.allocated).lower(), "cold_quiet_ms": quiet_ms,
                "ground_truth_inactive": str(status.allocated and status.state == "BACKGROUND_IDLE" and quiet_ms >= int(self.cfg.cold_quiet_s * 1000)).lower(),
                "observed_activity_state": observed, "observed_referenced_rss_ema": ema,
                "candidate_level": level, "base_rejection_reason": base_reason,
                "if_needed_rejection_reason": if_needed_reason,
                "eligible": str(level in {"WOULD_RECLAIM_IF_NEEDED", "WOULD_RECLAIM"}).lower(),
                "rejection_reason": base_reason or if_needed_reason,
            })
            if base_reason:
                reasons.append(base_reason); continue
            base_candidates.append((probability, -quiet_ms, -self._cold_bytes(status), app, status, path, if_needed_reason))
        if not base_candidates:
            return self._decision(now, prediction_id, trigger_event_id, trigger_type, current_app, "", 0.0, None, None, state, deficit, "SKIP", "NONE", "|".join(sorted(set(reasons))) or "NO_CANDIDATE", 0, activities)
        probability, _, _, app, status, path, if_needed_reason = sorted(base_candidates)[0]
        if if_needed_reason:
            return self._decision(now, prediction_id, trigger_event_id, trigger_type, current_app, app, probability, status, path, state, deficit, "RECLAIM_CANDIDATE", "RECLAIM_CANDIDATE", if_needed_reason, 0, activities)
        if deficit < self.cfg.minimum_headroom_deficit_bytes:
            return self._decision(now, prediction_id, trigger_event_id, trigger_type, current_app, app, probability, status, path, state, deficit, "WOULD_RECLAIM_IF_NEEDED", "WOULD_RECLAIM_IF_NEEDED", "NO_MEMORY_NEED", 0, activities)
        request = min(self.cfg.step_bytes, self.cfg.max_reclaim_bytes_per_session - self.bytes, deficit)
        if request <= 0:
            return self._decision(now, prediction_id, trigger_event_id, trigger_type, current_app, app, probability, status, path, state, deficit, "RECLAIM_CANDIDATE", "RECLAIM_CANDIDATE", "SESSION_BUDGET_EXHAUSTED", 0, activities)
        if self.cfg.mode == "shadow":
            decision, reason = "WOULD_RECLAIM", ""
        elif self.cfg.mode == "apply-bounded" and self.cfg.preflight_ready:
            decision, reason = "RECLAIM", ""
        else:
            decision, reason = "SKIP", "APPLY_PREFLIGHT_NOT_READY"
        decision_id = self._decision(now, prediction_id, trigger_event_id, trigger_type, current_app, app, probability, status, path, state, deficit, decision, decision, reason, request, activities)
        if decision == "RECLAIM":
            self._apply(decision_id, app, path, request, now)
        return decision_id

    @staticmethod
    def _cold_bytes(status: BallastStatus) -> int:
        return status.cold_bytes if status.allocated and status.cold_bytes else 80 * MIB if status.allocated else 0

    def _decision(self, now: int, prediction_id: str, event_id: str, trigger: str, current: str,
                  app: str, probability: float, ballast: BallastStatus | None, path: Path | None,
                  state: dict[str, Any], deficit: int, decision: str, level: str, reason: str, requested: int,
                  activities: dict[str, AppActivity]) -> str:
        self.sequence += 1
        decision_id = f"{self.session_id}-b{self.sequence:05d}"
        activity = activities.get(app) if app else None
        memory = _stat(path) if path else {}
        quiet = ballast.quiet_ns(now) // 1_000_000 if ballast else 0
        self.decisions.write_row({
            "timestamp_ns": now, "decision_id": decision_id, "prediction_id": prediction_id,
            "trigger_event_id": event_id, "trigger_type": trigger, "current_app": current,
            "candidate_app": app, "candidate_app_id": self.app_ids.get(app, ""), "candidate_probability": probability,
            "low_probability_batch_count": self.low.get(app, 0), "candidate_background": str(bool(app and app != current)).lower(),
            "ballast_state": ballast.state if ballast else "", "ballast_allocated": str(bool(ballast and ballast.allocated)).lower(),
            "cold_quiet_ms": quiet, "ground_truth_inactive": str(bool(ballast and ballast.allocated and ballast.state == "BACKGROUND_IDLE" and quiet >= int(self.cfg.cold_quiet_s*1000))).lower(),
            "observed_activity_state": activity.status if activity else "UNAVAILABLE",
            "observed_referenced_rss_ema": "" if not activity or activity.ema is None else activity.ema,
            "memcg_path": str(path or ""), "memory_current_before": memory.get("memcg_current_bytes", 0),
            "mem_available_bytes": state.get("available", 0), "target_headroom_bytes": self.cfg.target_headroom_bytes,
            "headroom_deficit_bytes": deficit, "requested_reclaim_bytes": requested, "decision": decision,
            "candidate_level": level, "base_candidate": str(level != "NONE").lower(),
            "if_needed_eligible": str(level in {"WOULD_RECLAIM_IF_NEEDED", "WOULD_RECLAIM"}).lower(),
            "memory_need": str(deficit >= self.cfg.minimum_headroom_deficit_bytes).lower(),
            "mode": self.cfg.mode, "apply_label": "SYNTHETIC_GROUND_TRUTH_APPLY" if decision == "RECLAIM" else "",
            "skip_reason": reason,
        })
        return decision_id

    def _sample(self, decision_id: str, label: str, app: str, path: Path, now: int) -> None:
        values = _stat(path)
        state = system_state(); root_events = state.get("root_memory_events", {})
        ballast_rss = ballast_pss = app_rss = app_pss = 0
        try: pids = [int(line) for line in (path / "cgroup.procs").read_text().split()]
        except (OSError, ValueError): pids = []
        for pid in pids:
            smaps, _ = read_smaps_rollup(pid)
            comm = ""
            try: comm = (Path("/proc") / str(pid) / "comm").read_text().strip()
            except OSError: pass
            if comm.startswith("parp_memory_ba"):
                ballast_rss += smaps.get("Rss", 0); ballast_pss += smaps.get("Pss", 0)
            else:
                app_rss += smaps.get("Rss", 0); app_pss += smaps.get("Pss", 0)
        self.observations.write_row({
            "decision_id": decision_id, "sample_label": label, "timestamp_ns": now, "app_key": app, "memcg_path": path,
            "memory_current": values.get("memcg_current_bytes", 0), "anon": values.get("anon_bytes", 0), "file": values.get("file_bytes", 0),
            "inactive_anon": values.get("inactive_anon_bytes", 0), "active_anon": values.get("active_anon_bytes", 0),
            "inactive_file": values.get("inactive_file_bytes", 0), "active_file": values.get("active_file_bytes", 0),
            "file_dirty": values.get("file_dirty", 0), "file_writeback": values.get("file_writeback", 0), "pgscan": values.get("pgscan", 0),
            "pgsteal": values.get("pgsteal", 0), "workingset_refault_anon": values.get("workingset_refault_anon", 0),
            "workingset_refault_file": values.get("workingset_refault_file", 0), "memory_swap_current": values.get("memory_swap_current", 0),
            "mem_available": state.get("available", 0), "psi_some": state.get("psi_some", ""), "psi_full": state.get("psi_full", ""),
            "root_oom": root_events.get("oom", 0), "root_oom_kill": root_events.get("oom_kill", 0),
            "ballast_rss": ballast_rss, "ballast_pss": ballast_pss, "app_rss": app_rss, "app_pss": app_pss, "error": "",
        })

    def _apply(self, decision_id: str, app: str, path: Path, request: int, now: int) -> None:
        self._sample(decision_id, "T-0", app, path, now)
        started = time.perf_counter_ns(); attempted = success = False; error = ""
        try:
            if not self._safe_path(path):
                error = "SAFE_TARGET_REJECTED"
            else:
                attempted = True
                with (path / "memory.reclaim").open("w", encoding="ascii") as stream:
                    written = stream.write(f"{request}\n"); stream.flush()
                success = written == len(f"{request}\n")
                if not success: error = "PARTIAL_WRITE"
        except OSError as exc:
            error = errno.errorcode.get(exc.errno or 0, f"ERRNO_{exc.errno}")
        self.attempts.write_row({"decision_id": decision_id, "timestamp_ns": now, "app_key": app, "memcg_path": path,
                                 "requested_bytes": request, "write_attempted": str(attempted).lower(), "write_success": str(success).lower(),
                                 "errno": error, "write_latency_us": int((time.perf_counter_ns()-started)/1000), "mode": self.cfg.mode,
                                 "apply_label": "SYNTHETIC_GROUND_TRUTH_APPLY"})
        if success:
            self.actions += 1; self.bytes += request; self.last_app_ns[app] = now; self.last_global_ns = now
            self.last_successful_reclaim_app = app
            self.pending.append(_Pending(decision_id, app, path, [("T+100ms", now+100_000_000), ("T+500ms", now+500_000_000), ("T+1s", now+1_000_000_000), ("T+3s", now+3_000_000_000)]))

    def tick(self, now_ns: int | None = None, *, final: bool = False) -> None:
        now = int(now_ns or time.time_ns()); remaining: list[_Pending] = []
        for item in self.pending:
            while item.next_index < len(item.due) and (final or now >= item.due[item.next_index][1]):
                label, due = item.due[item.next_index]; self._sample(item.decision_id, label, item.app, item.path, due if final else now); item.next_index += 1
            if item.next_index < len(item.due): remaining.append(item)
        self.pending = remaining

    def close(self) -> None:
        self.tick(final=True)
        self.decisions.close(); self.evaluations.close(); self.attempts.close(); self.observations.close(); self.safety.close(); self.probability_inputs.close()
