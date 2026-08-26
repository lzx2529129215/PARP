"""Fail-closed, bounded Test4 ``memory.reclaim`` controller.

All policy decisions are made from a complete online v3 probability batch and
read-only App/cgroup observations.  ``shadow`` records the exact decision but
never opens ``memory.reclaim``.  ``apply-bounded`` remains disabled unless its
caller explicitly supplies a READY preflight result.
"""
from __future__ import annotations

import csv
import errno
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from collectors.memory import read_meminfo, read_vmstat
from core.app_memory_activity import AppActivity
from core.memory_shadow import read_cgroup_memory
from core.writer import CsvWriter


DECISION_FIELDS = [
    "timestamp_ns", "decision_id", "prediction_id", "trigger_event_id", "trigger_type",
    "current_app", "candidate_app", "candidate_app_id", "candidate_probability",
    "low_probability_batch_count", "candidate_running", "candidate_background",
    "rss_bytes", "referenced_bytes", "referenced_rss_ratio", "referenced_rss_ema",
    "low_activity_window_count", "mem_available_bytes", "target_headroom_bytes",
    "headroom_deficit_bytes", "last_reclaim_age_ms", "requested_reclaim_bytes",
    "decision", "skip_reason", "mode",
]
ATTEMPT_FIELDS = [
    "decision_id", "timestamp_ns", "app_key", "memcg_path", "requested_bytes",
    "write_attempted", "write_success", "errno", "write_latency_us",
    "memory_current_before", "memory_current_after_500ms", "memory_current_after_1s",
    "pgscan_delta", "pgsteal_delta", "swap_delta", "psi_before", "psi_after",
]
SAFETY_FIELDS = [
    "timestamp_ns", "reason", "mem_available_bytes", "psi_some", "psi_full",
    "root_oom", "root_oom_kill", "mode",
]
EVALUATION_FIELDS = [
    "timestamp_ns", "prediction_id", "trigger_event_id", "app_key", "probability",
    "low_probability_batch_count", "foreground_state", "running_state", "sample_status",
    "referenced_rss_ema", "low_activity_window_count", "memcg_current_bytes",
    "app_bind_ready", "headroom_deficit_bytes", "eligible", "rejection_reason",
]


@dataclass
class ReclaimConfig:
    mode: str = "shadow"
    probability_threshold: float = 0.05
    required_low_probability_batches: int = 2
    activity_threshold: float = 0.10
    required_low_activity_windows: int = 3
    step_bytes: int = 16 * 1024 * 1024
    per_app_cooldown_ms: int = 10_000
    global_cooldown_ms: int = 3_000
    max_actions_per_episode: int = 1
    target_headroom_bytes: int = 2500 * 1024 * 1024
    minimum_headroom_deficit_bytes: int = 16 * 1024 * 1024
    max_ratio: float = 0.10
    minimum_resident_bytes: int = 64 * 1024 * 1024
    max_actions_per_minute: int = 3
    max_per_app_session: int = 64 * 1024 * 1024
    max_global_session: int = 128 * 1024 * 1024
    hard_min_available_bytes: int = 512 * 1024 * 1024
    psi_full_abort_avg10: float = 0.20
    max_no_progress_actions: int = 2
    preflight_ready: bool = False


@dataclass
class _PendingAttempt:
    decision_id: str
    timestamp_ns: int
    due_500_ns: int
    due_1s_ns: int
    app_key: str
    memcg_path: str
    requested_bytes: int
    attempted: bool
    success: bool
    error: str
    latency_us: int
    memory_before: int
    memory_after_500: int | None
    vmstat_before: dict[str, int]
    psi_before: str


def _parse_psi_avg10(line: str) -> float:
    for item in line.split():
        if item.startswith("avg10="):
            try:
                return float(item.split("=", 1)[1])
            except ValueError:
                return 0.0
    return 0.0


def _memory_events(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in (path / "memory.events").read_text().splitlines():
            key, value = line.split(maxsplit=1)
            values[key] = int(value)
    except (OSError, ValueError):
        pass
    return values


def system_state() -> dict[str, Any]:
    meminfo = read_meminfo()
    available = int(meminfo.get("MemAvailable", 0)) * 1024
    psi = ""
    try:
        psi = Path("/proc/pressure/memory").read_text(encoding="utf-8")
    except OSError:
        pass
    some = next((line for line in psi.splitlines() if line.startswith("some ")), "")
    full = next((line for line in psi.splitlines() if line.startswith("full ")), "")
    return {
        "available": available,
        "vmstat": read_vmstat(),
        "psi_some": some,
        "psi_full": full,
        "root_memory_events": _memory_events(Path("/sys/fs/cgroup")),
    }


class AppReclaimController:
    """Own Test4 policy state; a controller error is intentionally containable."""

    def __init__(
        self,
        *,
        session_id: str,
        output_dir: Path,
        config: ReclaimConfig,
        app_ids: dict[str, int],
        vocab_to_app: dict[str, str] | None = None,
        test_slice_marker: str = "huawei-test.slice",
    ) -> None:
        self.session_id = session_id
        self.cfg = config
        self.ids = dict(app_ids)
        self.vocab_to_app = vocab_to_app or {}
        self.marker = test_slice_marker
        self.low_probability_batches: dict[str, int] = {}
        self.last_foreground_ns: dict[str, int] = {}
        self.last_app_reclaim_ns: dict[str, int] = {}
        self.last_global_reclaim_ns = 0
        self.action_times_ns: list[int] = []
        self.app_bytes: dict[str, int] = {}
        self.global_bytes = 0
        self.actions_per_episode: dict[str, int] = {}
        self.no_progress_actions = 0
        self.aborted_reason = ""
        self.sequence = 0
        self.pending: list[_PendingAttempt] = []
        self.root_oom_baseline: int | None = None
        self._last_safety_record = ""

        reclaim_dir = Path(output_dir) / "reclaim"
        self.decisions = CsvWriter(reclaim_dir / "app_reclaim_decisions.csv", DECISION_FIELDS)
        self.attempts = CsvWriter(reclaim_dir / "memory_reclaim_attempts.csv", ATTEMPT_FIELDS)
        self.safety = CsvWriter(reclaim_dir / "safety_events.csv", SAFETY_FIELDS)
        self.evaluations = CsvWriter(reclaim_dir / "candidate_evaluations.csv", EVALUATION_FIELDS)

    def observe_prediction(
        self,
        *,
        prediction_id: str,
        trigger_event_id: str,
        trigger_type: str,
        current_app: str,
        result: dict[str, Any],
        activities: dict[str, AppActivity],
        bind_ready: set[str],
        now_ns: int | None = None,
    ) -> None:
        """Update state and make at most one reclaim decision for this batch."""
        if self.cfg.mode == "off":
            return
        now = int(now_ns or time.time_ns())
        self.tick(now)
        self.last_foreground_ns[current_app] = now
        probabilities = self._probabilities(result)
        for app in self.ids:
            if probabilities.get(app, 1.0) < self.cfg.probability_threshold:
                self.low_probability_batches[app] = self.low_probability_batches.get(app, 0) + 1
            else:
                self.low_probability_batches[app] = 0

        state = system_state()
        safety_reason = self._observe_safety(state, now)
        deficit = max(0, self.cfg.target_headroom_bytes - int(state["available"]))
        if safety_reason:
            self._log(
                now, prediction_id, trigger_event_id, trigger_type, current_app, "", 0.0, None,
                -1, state, deficit, "SKIP", safety_reason, 0,
            )
            return

        candidates: list[tuple[float, float, int, int, str, AppActivity, int]] = []
        reasons: list[str] = []
        for app, activity in activities.items():
            probability = probabilities.get(app, 1.0)
            reason = self._candidate_rejection(
                app, activity, current_app, probability, bind_ready, deficit
            )
            self.evaluations.write_row({
                "timestamp_ns": now,
                "prediction_id": prediction_id,
                "trigger_event_id": trigger_event_id,
                "app_key": app,
                "probability": probability,
                "low_probability_batch_count": self.low_probability_batches.get(app, 0),
                "foreground_state": activity.foreground_state,
                "running_state": activity.running_state,
                "sample_status": activity.status,
                "referenced_rss_ema": activity.ema,
                "low_activity_window_count": activity.low_windows,
                "memcg_current_bytes": activity.memcg_current_bytes,
                "app_bind_ready": str(app in bind_ready).lower(),
                "headroom_deficit_bytes": deficit,
                "eligible": str(not bool(reason)).lower(),
                "rejection_reason": reason,
            })
            if reason:
                reasons.append(reason)
                continue
            cap = min(
                int(activity.memcg_current_bytes * self.cfg.max_ratio),
                activity.memcg_current_bytes - self.cfg.minimum_resident_bytes,
                self.cfg.max_per_app_session - self.app_bytes.get(app, 0),
                self.cfg.max_global_session - self.global_bytes,
            )
            if cap <= 0:
                reasons.append("SESSION_BUDGET_EXHAUSTED")
                continue
            request = min(self.cfg.step_bytes, deficit, cap)
            age_since_foreground = now - self.last_foreground_ns.get(app, 0)
            candidates.append((
                probability,
                activity.ema if activity.ema is not None else 1.0,
                -activity.memcg_current_bytes,
                -age_since_foreground,
                app,
                activity,
                request,
            ))

        if not candidates:
            self._log(
                now, prediction_id, trigger_event_id, trigger_type, current_app, "", 0.0, None,
                -1, state, deficit, "SKIP", "|".join(sorted(set(reasons))) or "NO_CANDIDATE", 0,
            )
            return

        _, _, _, _, app, activity, request = sorted(candidates)[0]
        cooldown_age = self._last_reclaim_age_ms(app, now)
        if self._in_cooldown_or_rate_limit(app, now):
            self._log(
                now, prediction_id, trigger_event_id, trigger_type, current_app, app,
                probabilities[app], activity, cooldown_age, state, deficit,
                "SKIP", "COOLDOWN_OR_RATE_LIMIT", 0,
            )
            return
        if self.actions_per_episode.get(trigger_event_id, 0) >= self.cfg.max_actions_per_episode:
            self._log(
                now, prediction_id, trigger_event_id, trigger_type, current_app, app,
                probabilities[app], activity, cooldown_age, state, deficit,
                "SKIP", "EPISODE_ACTION_LIMIT", 0,
            )
            return

        if self.cfg.mode == "shadow":
            decision, reason = "WOULD_RECLAIM", ""
        elif self.cfg.mode == "apply-bounded" and self.cfg.preflight_ready:
            decision, reason = "RECLAIM", ""
        else:
            decision, reason = "SKIP", "APPLY_PREFLIGHT_NOT_READY"
        decision_id = self._log(
            now, prediction_id, trigger_event_id, trigger_type, current_app, app,
            probabilities[app], activity, cooldown_age, state, deficit, decision, reason, request,
        )
        if decision == "RECLAIM":
            self.actions_per_episode[trigger_event_id] = self.actions_per_episode.get(trigger_event_id, 0) + 1
            self._apply(decision_id, now, app, activity, request, state)

    def tick(self, now_ns: int | None = None, *, final: bool = False) -> None:
        """Finish non-blocking 500 ms/1 s post-write observations."""
        now = int(now_ns or time.time_ns())
        remaining: list[_PendingAttempt] = []
        for pending in self.pending:
            path = Path(pending.memcg_path)
            if pending.memory_after_500 is None and now >= pending.due_500_ns:
                values, _ = read_cgroup_memory(path)
                pending.memory_after_500 = int(values.get("memcg_current_bytes", 0))
            if now < pending.due_1s_ns and not final:
                remaining.append(pending)
                continue
            values, _ = read_cgroup_memory(path)
            after_1s = int(values.get("memcg_current_bytes", 0))
            state_after = system_state()
            self.attempts.write_row({
                "decision_id": pending.decision_id,
                "timestamp_ns": pending.timestamp_ns,
                "app_key": pending.app_key,
                "memcg_path": pending.memcg_path,
                "requested_bytes": pending.requested_bytes,
                "write_attempted": str(pending.attempted).lower(),
                "write_success": str(pending.success).lower(),
                "errno": pending.error,
                "write_latency_us": pending.latency_us,
                "memory_current_before": pending.memory_before,
                "memory_current_after_500ms": "" if pending.memory_after_500 is None else pending.memory_after_500,
                "memory_current_after_1s": after_1s,
                "pgscan_delta": max(0, int(state_after["vmstat"].get("pgscan_direct", 0)) - int(pending.vmstat_before.get("pgscan_direct", 0))),
                "pgsteal_delta": max(0, int(state_after["vmstat"].get("pgsteal_direct", 0)) - int(pending.vmstat_before.get("pgsteal_direct", 0))),
                "swap_delta": max(0, int(state_after["vmstat"].get("pswpout", 0)) - int(pending.vmstat_before.get("pswpout", 0))),
                "psi_before": pending.psi_before,
                "psi_after": state_after["psi_full"],
            })
            if pending.success:
                progress = pending.memory_before - after_1s
                self.no_progress_actions = 0 if progress > 0 else self.no_progress_actions + 1
                if self.no_progress_actions >= self.cfg.max_no_progress_actions:
                    self.aborted_reason = "CONSECUTIVE_RECLAIM_NO_PROGRESS"
        self.pending = remaining

    def _probabilities(self, result: dict[str, Any]) -> dict[str, float]:
        probabilities: dict[str, float] = {}
        for row in result.get("all_probabilities", []):
            app = str(row.get("app_key", "") or "")
            if not app:
                app = self.vocab_to_app.get(str(row.get("app", "") or ""), "")
            if not app:
                continue
            try:
                probability = float(row.get("probability", 0.0))
            except (TypeError, ValueError):
                continue
            if 0.0 <= probability <= 1.0:
                probabilities[app] = probability
        return probabilities

    def _observe_safety(self, state: dict[str, Any], now: int) -> str:
        root_oom = int(state.get("root_memory_events", {}).get("oom", 0)) + int(
            state.get("root_memory_events", {}).get("oom_kill", 0)
        )
        if self.root_oom_baseline is None:
            self.root_oom_baseline = root_oom
        elif root_oom > self.root_oom_baseline:
            self.aborted_reason = "OOM_EVENT_INCREASED"
        if int(state["available"]) < self.cfg.hard_min_available_bytes:
            self.aborted_reason = "MEM_AVAILABLE_BELOW_HARD_FLOOR"
        if _parse_psi_avg10(str(state.get("psi_full", ""))) >= self.cfg.psi_full_abort_avg10:
            self.aborted_reason = "MEMORY_PSI_FULL_ABORT_THRESHOLD"
        if self.aborted_reason and self.aborted_reason != self._last_safety_record:
            root_events = state.get("root_memory_events", {})
            self.safety.write_row({
                "timestamp_ns": now,
                "reason": self.aborted_reason,
                "mem_available_bytes": state.get("available", 0),
                "psi_some": state.get("psi_some", ""),
                "psi_full": state.get("psi_full", ""),
                "root_oom": root_events.get("oom", 0),
                "root_oom_kill": root_events.get("oom_kill", 0),
                "mode": self.cfg.mode,
            })
            self._last_safety_record = self.aborted_reason
        return self.aborted_reason

    def _candidate_rejection(
        self,
        app: str,
        activity: AppActivity,
        current_app: str,
        probability: float,
        bind_ready: set[str],
        deficit: int,
    ) -> str:
        path = Path(activity.memcg_path) if activity.memcg_path else None
        if app == current_app or activity.foreground_state == "FOREGROUND":
            return "FOREGROUND_PROTECTED"
        if activity.status != "OK":
            return "ACTIVITY_SAMPLE_UNAVAILABLE"
        if activity.running_state != "RUNNING_BACKGROUND":
            return "NOT_RUNNING_BACKGROUND"
        if app not in bind_ready:
            return "APP_BIND_INVALID"
        if path is None or not path.is_dir() or self.marker not in str(path):
            return "CGROUP_UNAVAILABLE"
        if not (path / "memory.reclaim").is_file():
            return "MEMORY_RECLAIM_UNAVAILABLE"
        if probability >= self.cfg.probability_threshold:
            return "LOW_PROBABILITY_NOT_STABLE"
        if self.low_probability_batches.get(app, 0) < self.cfg.required_low_probability_batches:
            return "LOW_PROBABILITY_NOT_STABLE"
        if activity.ema is None or activity.ema >= self.cfg.activity_threshold:
            return "ACTIVITY_NOT_LOW_STABLE"
        if activity.low_windows < self.cfg.required_low_activity_windows:
            return "ACTIVITY_NOT_LOW_STABLE"
        if activity.memcg_current_bytes <= self.cfg.minimum_resident_bytes:
            return "MINIMUM_RESIDENT_PROTECTED"
        if deficit < self.cfg.minimum_headroom_deficit_bytes:
            return "NO_MEMORY_NEED"
        return ""

    def _in_cooldown_or_rate_limit(self, app: str, now: int) -> bool:
        self.action_times_ns = [item for item in self.action_times_ns if now - item < 60_000_000_000]
        if self.last_global_reclaim_ns and now - self.last_global_reclaim_ns < self.cfg.global_cooldown_ms * 1_000_000:
            return True
        if app in self.last_app_reclaim_ns and now - self.last_app_reclaim_ns[app] < self.cfg.per_app_cooldown_ms * 1_000_000:
            return True
        return len(self.action_times_ns) >= self.cfg.max_actions_per_minute

    def _last_reclaim_age_ms(self, app: str, now: int) -> int:
        last = max(self.last_global_reclaim_ns, self.last_app_reclaim_ns.get(app, 0))
        return int((now - last) / 1_000_000) if last else -1

    def _log(
        self,
        now: int,
        prediction_id: str,
        event_id: str,
        trigger_type: str,
        current_app: str,
        app: str,
        probability: float,
        activity: AppActivity | None,
        last_age_ms: int,
        state: dict[str, Any],
        deficit: int,
        decision: str,
        reason: str,
        requested: int,
    ) -> str:
        self.sequence += 1
        decision_id = f"{self.session_id}-r{self.sequence:05d}"
        if activity is None:
            activity = AppActivity(
                "", 0, "", "", "", 0, 0, 0, 0, 0, 0, None, None, 0, "", "", now
            )
        self.decisions.write_row({
            "timestamp_ns": now,
            "decision_id": decision_id,
            "prediction_id": prediction_id,
            "trigger_event_id": event_id,
            "trigger_type": trigger_type,
            "current_app": current_app,
            "candidate_app": app,
            "candidate_app_id": self.ids.get(app, ""),
            "candidate_probability": probability,
            "low_probability_batch_count": self.low_probability_batches.get(app, 0),
            "candidate_running": activity.running_state,
            "candidate_background": str(activity.foreground_state == "BACKGROUND").lower(),
            "rss_bytes": activity.rss_bytes,
            "referenced_bytes": activity.referenced_bytes,
            "referenced_rss_ratio": activity.ratio,
            "referenced_rss_ema": activity.ema,
            "low_activity_window_count": activity.low_windows,
            "mem_available_bytes": state["available"],
            "target_headroom_bytes": self.cfg.target_headroom_bytes,
            "headroom_deficit_bytes": deficit,
            "last_reclaim_age_ms": last_age_ms,
            "requested_reclaim_bytes": requested,
            "decision": decision,
            "skip_reason": reason,
            "mode": self.cfg.mode,
        })
        return decision_id

    def _safe_target(self, path: Path, activity: AppActivity) -> bool:
        root = Path("/sys/fs/cgroup")
        try:
            path.relative_to(root)
        except ValueError:
            return False
        protected = {root, root / "system.slice", root / "user.slice"}
        return (
            path not in protected
            and self.marker in str(path)
            and activity.foreground_state != "FOREGROUND"
            and (path / "memory.reclaim").is_file()
        )

    def _apply(
        self,
        decision_id: str,
        now: int,
        app: str,
        activity: AppActivity,
        request: int,
        state: dict[str, Any],
    ) -> None:
        path = Path(activity.memcg_path)
        before, _ = read_cgroup_memory(path)
        attempted = False
        success = False
        error = ""
        started = time.perf_counter_ns()
        target = path / "memory.reclaim"
        if not self._safe_target(path, activity):
            error = "SAFE_TARGET_REJECTED"
        else:
            attempted = True
            try:
                command = f"{request}\n"
                with target.open("w", encoding="ascii") as stream:
                    written = stream.write(command)
                    stream.flush()
                if written != len(command):
                    error = "PARTIAL_WRITE"
                else:
                    success = True
            except OSError as exc:
                error = errno.errorcode.get(exc.errno or 0, str(exc.errno or "EIO"))
        latency_us = int((time.perf_counter_ns() - started) / 1000)
        self.pending.append(_PendingAttempt(
            decision_id=decision_id,
            timestamp_ns=now,
            due_500_ns=now + 500_000_000,
            due_1s_ns=now + 1_000_000_000,
            app_key=app,
            memcg_path=str(path),
            requested_bytes=request,
            attempted=attempted,
            success=success,
            error=error,
            latency_us=latency_us,
            memory_before=int(before.get("memcg_current_bytes", 0)),
            memory_after_500=None,
            vmstat_before=dict(state["vmstat"]),
            psi_before=str(state["psi_full"]),
        ))
        if success:
            self.last_app_reclaim_ns[app] = now
            self.last_global_reclaim_ns = now
            self.action_times_ns.append(now)
            self.app_bytes[app] = self.app_bytes.get(app, 0) + request
            self.global_bytes += request

    def close(self) -> None:
        self.tick(final=True)
        self.decisions.close()
        self.attempts.close()
        self.safety.close()
        self.evaluations.close()
