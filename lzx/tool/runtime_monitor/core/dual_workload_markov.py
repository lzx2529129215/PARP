"""双模式 workload Markov 的用户态观测实现。

该模块与旧的 ``OnlineCausalWorkloadMarkov`` 分离。它只维护前台连续
历史和回切后的首个 workload 统计，并将建议写成 CSV；debugfs 写入由
传入的 writer 完成，内核侧仍保持 observe-only。
"""

from __future__ import annotations

import csv
import datetime as dt
import shutil
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


CONTINUE = 0
REENTRY = 1
LOW_ACTIVITY = 0
TOP_K = 4

FOREGROUND_HISTORY_FIELDS = [
    "session_id", "timestamp_ns", "app_key", "app_id", "runtime_app_id", "scope_name",
    "cgroup_id", "foreground_app_key", "foreground_app_id", "workload_id",
    "workload_name", "previous_foreground_workload_id", "current_foreground_workload_id",
    "mode", "state_changed", "history_valid", "foreground_epoch_id",
]
FOREGROUND_EPOCH_FIELDS = [
    "session_id", "app_key", "app_id", "foreground_epoch_id", "switch_in_time_ns",
    "switch_out_time_ns", "status", "continue_samples", "pending_cancelled",
]
CONTINUE_UPDATE_FIELDS = [
    "session_id", "timestamp_ns", "mode", "app_key", "app_id", "scope_name",
    "previous_workload_id", "current_workload_id", "next_workload_id",
    "count_before", "count_after", "total_after", "confidence_fixed", "boost_level",
]
CONTINUE_TRANSITION_FIELDS = [
    "session_id", "timestamp_ns", "mode", "app_key", "app_id", "scope_name",
    "previous_workload_id", "current_workload_id", "next_workload_id", "count",
    "total_count", "confidence", "confidence_fixed", "boost_level", "rank",
]
CONTINUE_PREDICTION_FIELDS = [
    "session_id", "timestamp_ns", "mode", "app_key", "app_id", "scope_name",
    "previous_workload_id", "current_workload_id", "predicted_next_workload_id",
    "rank", "confidence_fixed", "boost_level", "prediction_status",
    "prediction_epoch_id", "resolution_reason",
]
REENTRY_EVENT_FIELDS = [
    "session_id", "reentry_event_id", "app_key", "app_id",
    "previous_foreground_app_key", "previous_foreground_app_id",
    "switch_in_time_ns", "window_end_time_ns", "selected_time_ns",
    "selected_workload_id", "sample_count", "state_unchanged_sample_count",
    "valid", "resolution_reason",
]
REENTRY_SAMPLE_FIELDS = [
    "session_id", "timestamp_ns", "mode", "reentry_event_id", "app_key", "app_id", "runtime_app_id", "scope_name",
    "previous_foreground_app_key", "current_foreground_app_key",
    "previous_foreground_app_id", "current_foreground_app_id", "workload_id",
    "workload_name", "elapsed_ms", "switch_in_time_ns", "window_end_time_ns",
    "candidate_sample_count", "ignored_low_activity_count", "first_valid_workload_id",
    "first_valid_workload_name", "selection_reason", "resolution_reason",
    "sample_state_changed", "sample_valid_scope", "sample_time_from_switch_in_ms",
    "app_still_foreground", "valid", "sample_valid",
]
REENTRY_UPDATE_FIELDS = [
    "session_id", "timestamp_ns", "mode", "app_key", "app_id",
    "next_workload_id", "count_before", "count_after", "total_after",
    "confidence_fixed", "boost_level", "selection_reason",
]
REENTRY_TRANSITION_FIELDS = [
    "session_id", "timestamp_ns", "mode", "app_key", "app_id",
    "next_workload_id", "count", "total_count", "confidence",
    "confidence_fixed", "boost_level", "rank",
]
REENTRY_PREDICTION_FIELDS = [
    "session_id", "timestamp_ns", "mode", "app_key", "app_id",
    "predicted_next_workload_id", "rank", "confidence_fixed", "boost_level",
    "prediction_status",
]
DUAL_POLICY_FIELDS = [
    "session_id", "timestamp_ns", "mode", "target_app_key", "target_app_id",
    "target_is_foreground", "lstm_probability_fixed", "previous_workload_id",
    "current_workload_id", "predicted_workload_id", "prediction_confidence_fixed",
    "suggest_current_workload_protect", "suggest_next_workload_protect",
    "suggest_reentry_common_protect", "suggest_reentry_workload_protect",
    "status", "reason",
    "foreground_app_key", "foreground_app_id", "workload_id", "confidence_fixed",
    "boost_level", "suggestion", "observe_only",
]


def _confidence_fixed(count: int, total: int) -> int:
    return max(0, min(10000, int(round(count * 10000 / total)))) if total else 0


def _boost(confidence_fixed: int) -> int:
    if confidence_fixed >= 8000:
        return 3
    if confidence_fixed >= 5000:
        return 2
    if confidence_fixed > 0:
        return 1
    return 0


def select_highest_confidence(candidates: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """按内核一致的稳定规则选择候选，便于用户态和源码镜像测试。"""
    rows = list(candidates)
    if not rows:
        return None
    return min(
        rows,
        key=lambda row: (
            -int(row.get("confidence_fixed", 0)),
            -int(row.get("boost_level", 0)),
            int(row.get("rank", 0)),
            int(row.get("next_workload_id", row.get("workload_id", 0))),
        ),
    )


def reentry_combined_strength(probability_fixed: int, confidence_fixed: int) -> int:
    """返回 0..10000 的 observe-only 后台组合强度。"""
    probability = max(0, min(10000, int(probability_fixed)))
    confidence = max(0, min(10000, int(confidence_fixed)))
    return (probability * confidence + 5000) // 10000


def _bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _timestamp_ns(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1_000_000_000)
    except ValueError:
        return 0


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


@dataclass
class _ContinueState:
    epoch_id: int = 0
    is_foreground: bool = False
    switch_in_ns: int = 0
    window: list[int] = field(default_factory=list)
    sample_count: int = 0
    pending_prediction_indices: list[int] = field(default_factory=list)
    transitions: defaultdict[int, Counter[int]] = field(
        default_factory=lambda: defaultdict(Counter)
    )


@dataclass
class _ReentryWindow:
    event_id: str
    previous_app_key: str
    current_app_key: str
    previous_app_id: str
    current_app_id: str
    started_ns: int
    selected: bool = False
    finalized: bool = False
    samples: list[dict[str, Any]] = field(default_factory=list)


class DualWorkloadMarkov:
    """维护 CONTINUE/REENTRY 两个互不混用的 workload 状态空间。"""

    def __init__(
        self,
        *,
        enabled: bool,
        session_id: str,
        model_dir: Path | str,
        review_dir: Path | str,
        debugfs_writer: Any | None = None,
        reentry_window_s: float = 5.0,
        ignore_initial_low_activity_s: float = 2.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.session_id = session_id
        self.model_dir = Path(model_dir)
        self.review_dir = Path(review_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self.debugfs_writer = debugfs_writer
        self.reentry_window_ns = int(float(reentry_window_s) * 1_000_000_000)
        self.ignore_initial_low_ns = int(float(ignore_initial_low_activity_s) * 1_000_000_000)
        self._continue: dict[str, _ContinueState] = {}
        self._metadata: dict[str, dict[str, str]] = {}
        self._reentry_counts: dict[str, Counter[int]] = defaultdict(Counter)
        self._reentry_windows: dict[str, _ReentryWindow] = {}
        self._foreground_app_key = ""
        self._foreground_app_id = ""
        self._foreground_changed_ns = 0
        self._foreground_history: list[dict[str, Any]] = []
        self._foreground_epochs: list[dict[str, Any]] = []
        self._continue_updates: list[dict[str, Any]] = []
        self._continue_predictions: list[dict[str, Any]] = []
        self._reentry_samples: list[dict[str, Any]] = []
        self._reentry_events: list[dict[str, Any]] = []
        self._reentry_updates: list[dict[str, Any]] = []
        self._reentry_predictions: list[dict[str, Any]] = []
        self._policy_suggestions: list[dict[str, Any]] = []
        self._cross_epoch_transitions_blocked = 0

    def observe_foreground(
        self,
        *,
        foreground_app_key: str,
        foreground_app_id: int | str | None,
        timestamp_ns: int | None = None,
    ) -> None:
        if not self.enabled:
            return
        ts = int(time.time_ns() if timestamp_ns is None else timestamp_ns)
        new_key = str(foreground_app_key or "").strip()
        new_id = "" if foreground_app_id is None else str(foreground_app_id)
        old_key, old_id = self._foreground_app_key, self._foreground_app_id
        if new_key == old_key and new_id == old_id:
            return

        if old_id:
            old_state = self._continue.setdefault(old_id, _ContinueState())
            if old_state.window:
                self._cross_epoch_transitions_blocked += 1
            cancelled = len(old_state.pending_prediction_indices)
            for index in old_state.pending_prediction_indices:
                self._continue_predictions[index]["resolution_reason"] = "cancelled_switch_out"
                self._continue_predictions[index]["prediction_status"] = "cancelled"
            old_state.pending_prediction_indices.clear()
            old_state.window.clear()
            old_state.is_foreground = False
            for epoch in reversed(self._foreground_epochs):
                if epoch["app_id"] == old_id and epoch["switch_out_time_ns"] == "":
                    epoch["switch_out_time_ns"] = ts
                    epoch["status"] = "closed_switch_out"
                    epoch["continue_samples"] = old_state.sample_count
                    epoch["pending_cancelled"] = cancelled
                    break
            self._finalize_reentry(old_id, "invalid_switched_out", ts, allow_low_fallback=False)
        self._foreground_app_key, self._foreground_app_id = new_key, new_id
        self._foreground_changed_ns = ts
        if new_id:
            new_state = self._continue.setdefault(new_id, _ContinueState())
            new_state.epoch_id += 1
            new_state.is_foreground = True
            new_state.switch_in_ns = ts
            new_state.window.clear()
            new_state.sample_count = 0
            new_state.pending_prediction_indices.clear()
            self._foreground_epochs.append({
                "session_id": self.session_id, "app_key": new_key, "app_id": new_id,
                "foreground_epoch_id": new_state.epoch_id, "switch_in_time_ns": ts,
                "switch_out_time_ns": "", "status": "active", "continue_samples": 0,
                "pending_cancelled": 0,
            })
        # Initial foreground discovery is not a background -> foreground
        # reentry event; a real reentry requires a known previous app.
        if old_key and old_key != "UNKNOWN" and new_key and new_key != "UNKNOWN":
            self._reentry_windows[new_id] = _ReentryWindow(
                event_id=f"reentry-{new_id}-{ts}",
                previous_app_key=old_key,
                current_app_key=new_key,
                previous_app_id=old_id,
                current_app_id=new_id,
                started_ns=ts,
            )

    def observe_workload(
        self,
        *,
        app_key: str,
        app_id: int | str,
        scope_name: str,
        workload_id: int,
        cgroup_id: int | None = None,
        workload_name: str = "",
        timestamp_ns: int | None = None,
        foreground_app_key: str | None = None,
        foreground_app_id: int | str | None = None,
        state_changed: bool = True,
        sample_valid_scope: bool = True,
    ) -> None:
        if not self.enabled:
            return
        ts = int(time.time_ns() if timestamp_ns is None else timestamp_ns)
        app_key = str(app_key).strip()
        app_id_s = str(app_id).strip()
        foreground_key = self._foreground_app_key if foreground_app_key is None else str(foreground_app_key).strip()
        foreground_id = self._foreground_app_id if foreground_app_id is None else str(foreground_app_id).strip()
        self._metadata[app_id_s] = {"app_key": app_key, "scope_name": str(scope_name)}

        is_foreground = bool(app_id_s and app_id_s == foreground_id) or (
            bool(app_key) and app_key == foreground_key
        )
        if is_foreground:
            self._observe_reentry(
                app_key, app_id_s, str(scope_name), int(workload_id), ts,
                str(workload_name), state_changed=bool(state_changed),
                sample_valid_scope=bool(sample_valid_scope),
            )
            if state_changed and sample_valid_scope:
                self._observe_continue(
                    app_key, app_id_s, str(scope_name), int(workload_id), cgroup_id,
                    workload_name, ts,
                )
        else:
            # 后台状态仍可记录为 runtime workload，但不能污染 CONTINUE/REENTRY。
            self._policy_suggestions.append({
                "session_id": self.session_id, "timestamp_ns": ts, "mode": REENTRY,
                "target_app_key": app_key, "target_app_id": app_id_s,
                "target_is_foreground": "false", "lstm_probability_fixed": "",
                "previous_workload_id": "", "current_workload_id": "",
                "predicted_workload_id": "", "prediction_confidence_fixed": "",
                "suggest_current_workload_protect": "false", "suggest_next_workload_protect": "false",
                "suggest_reentry_common_protect": "false", "suggest_reentry_workload_protect": "false",
                "status": "observe_only", "reason": "background_runtime_ignored",
                "foreground_app_key": foreground_key, "foreground_app_id": foreground_id,
                "workload_id": workload_id, "confidence_fixed": "", "boost_level": "",
                "suggestion": "background_runtime_ignored", "observe_only": "true",
            })

    def _observe_continue(
        self, app_key: str, app_id: str, scope: str, workload: int,
        cgroup_id: int | None, workload_name: str, ts: int,
    ) -> None:
        state = self._continue.setdefault(app_id, _ContinueState())
        if not state.is_foreground or state.epoch_id <= 0:
            return
        for index in state.pending_prediction_indices:
            self._continue_predictions[index]["resolution_reason"] = "same_epoch_observed"
            self._continue_predictions[index]["prediction_status"] = "resolved"
        state.pending_prediction_indices.clear()
        state.sample_count += 1
        previous = state.window[-1] if state.window else ""
        prior_prior = state.window[-2] if len(state.window) >= 2 else ""
        self._foreground_history.append({
            "session_id": self.session_id, "timestamp_ns": ts, "app_key": app_key,
            "app_id": app_id, "runtime_app_id": app_id, "scope_name": scope,
            "cgroup_id": "" if cgroup_id is None else cgroup_id,
            "foreground_app_key": self._foreground_app_key, "foreground_app_id": self._foreground_app_id,
            "workload_id": workload, "workload_name": workload_name,
            "previous_foreground_workload_id": previous,
            "current_foreground_workload_id": workload, "mode": "CONTINUE", "state_changed": "true",
            "history_valid": "true", "foreground_epoch_id": state.epoch_id,
        })
        if self.debugfs_writer is not None and cgroup_id is not None:
            self.debugfs_writer.write_foreground_workload(
                cgroup_id=cgroup_id, app_id=int(app_id), workload_id=workload,
                app_key=app_key,
            )
        if len(state.window) >= 2:
            counter = state.transitions[(prior_prior, previous)]
            before = counter[workload]
            counter[workload] += 1
            total = sum(counter.values())
            conf = _confidence_fixed(counter[workload], total)
            self._continue_updates.append({
                "session_id": self.session_id, "timestamp_ns": ts, "mode": "CONTINUE",
                "app_key": app_key, "app_id": app_id, "scope_name": scope,
                "previous_workload_id": prior_prior, "current_workload_id": previous,
                "next_workload_id": workload, "count_before": before,
                "count_after": counter[workload], "total_after": total,
                "confidence_fixed": conf, "boost_level": _boost(conf),
            })
            if self.debugfs_writer is not None:
                self.debugfs_writer.write_continue_markov(
                    app_id=int(app_id), previous_workload_id=prior_prior,
                    current_workload_id=previous, next_workload_id=workload,
                    confidence_fixed=conf, boost_level=_boost(conf), app_key=app_key,
                )
        state.window = (state.window + [workload])[-2:]
        counter = state.transitions[(state.window[0], state.window[1])] if len(state.window) == 2 else Counter()
        for rank, (next_id, count) in enumerate(self._ranked(counter), 1):
            total = sum(counter.values())
            conf = _confidence_fixed(count, total)
            self._continue_predictions.append({
                "session_id": self.session_id, "timestamp_ns": ts, "mode": "CONTINUE",
                "app_key": app_key, "app_id": app_id, "scope_name": scope,
                "previous_workload_id": state.window[0], "current_workload_id": state.window[1],
                "predicted_next_workload_id": next_id, "rank": rank,
                "confidence_fixed": conf, "boost_level": _boost(conf),
                "prediction_status": "pending", "prediction_epoch_id": state.epoch_id,
                "resolution_reason": "pending_same_epoch_sample",
            })
            state.pending_prediction_indices.append(len(self._continue_predictions) - 1)
            self._policy_suggestions.append({
                "session_id": self.session_id, "timestamp_ns": ts, "mode": CONTINUE,
                "target_app_key": app_key, "target_app_id": app_id,
                "target_is_foreground": "true", "lstm_probability_fixed": "",
                "previous_workload_id": state.window[0], "current_workload_id": state.window[1],
                "predicted_workload_id": next_id, "prediction_confidence_fixed": conf,
                "suggest_current_workload_protect": "true", "suggest_next_workload_protect": "true",
                "suggest_reentry_common_protect": "false", "suggest_reentry_workload_protect": "false",
                "status": "observe_only", "reason": "continue_observe_only",
                "foreground_app_key": self._foreground_app_key, "foreground_app_id": self._foreground_app_id,
                "workload_id": next_id, "confidence_fixed": conf,
                "boost_level": _boost(conf), "suggestion": "continue_observe_only",
                "observe_only": "true",
            })

    def _observe_reentry(
        self, app_key: str, app_id: str, scope: str, workload: int, ts: int,
        workload_name: str, *, state_changed: bool, sample_valid_scope: bool,
    ) -> None:
        window = self._reentry_windows.get(app_id)
        if window is None or window.selected or window.finalized or window.current_app_key != app_key:
            return
        elapsed = ts - window.started_ns
        if elapsed > self.reentry_window_ns:
            return
        elapsed_ms = max(0, elapsed // 1_000_000)
        app_still_foreground = app_id == self._foreground_app_id and app_key == self._foreground_app_key
        is_low = workload == LOW_ACTIVITY
        if not sample_valid_scope:
            reason, valid = "invalid_scope_sample", False
        elif not app_still_foreground:
            reason, valid = "invalid_switched_out", False
        elif is_low and elapsed < self.ignore_initial_low_ns:
            reason, valid = "ignore_initial_low_activity", False
        elif is_low:
            # Keep the low sample as a fallback candidate. Do not select it
            # yet because a later non-low sample in the same window wins.
            reason, valid = "low_activity_candidate", False
        else:
            reason, valid = "first_non_low_activity", True
        sample = {
            "session_id": self.session_id, "timestamp_ns": ts, "mode": "REENTRY",
            "app_key": app_key, "app_id": app_id, "scope_name": scope,
            "previous_foreground_app_key": window.previous_app_key,
            "current_foreground_app_key": window.current_app_key,
            "previous_foreground_app_id": window.previous_app_id,
            "current_foreground_app_id": window.current_app_id,
            "workload_id": workload, "workload_name": workload_name, "elapsed_ms": elapsed_ms,
            "selection_reason": reason, "valid": str(valid).lower(),
            "sample_state_changed": str(bool(state_changed)).lower(),
            "sample_valid_scope": str(bool(sample_valid_scope)).lower(),
            "sample_time_from_switch_in_ms": elapsed_ms,
            "app_still_foreground": str(app_still_foreground).lower(),
        }
        window.samples.append(sample)
        if not valid:
            return
        self._select_reentry(app_id, window, sample, reason)

    def _select_reentry(
        self, app_id: str, window: _ReentryWindow,
        sample: dict[str, Any], reason: str,
    ) -> None:
        if window.selected:
            return
        window.selected = True
        window.finalized = True
        workload = int(sample["workload_id"])
        ts = int(sample["timestamp_ns"])
        app_key = str(sample["app_key"])
        counter = self._reentry_counts[app_id]
        before = counter[workload]
        counter[workload] += 1
        total = sum(counter.values())
        conf = _confidence_fixed(counter[workload], total)
        self._reentry_updates.append({
            "session_id": self.session_id, "timestamp_ns": ts, "mode": "REENTRY",
            "app_key": app_key, "app_id": app_id, "next_workload_id": workload,
            "count_before": before, "count_after": counter[workload], "total_after": total,
            "confidence_fixed": conf, "boost_level": _boost(conf), "selection_reason": reason,
        })
        self._reentry_samples.append({
            "session_id": self.session_id, "timestamp_ns": ts, "mode": "REENTRY",
            "reentry_event_id": window.event_id, "app_key": app_key, "app_id": app_id,
            "runtime_app_id": app_id, "scope_name": sample.get("scope_name", ""),
            "previous_foreground_app_key": window.previous_app_key,
            "current_foreground_app_key": window.current_app_key,
            "previous_foreground_app_id": window.previous_app_id,
            "current_foreground_app_id": window.current_app_id,
            "workload_id": workload, "workload_name": sample.get("workload_name", ""),
            "elapsed_ms": sample.get("elapsed_ms", 0),
            "switch_in_time_ns": window.started_ns,
            "window_end_time_ns": window.started_ns + self.reentry_window_ns,
            "candidate_sample_count": len(window.samples),
            "ignored_low_activity_count": sum(
                1 for candidate in window.samples
                if candidate.get("selection_reason") == "ignore_initial_low_activity"
            ),
            "first_valid_workload_id": workload,
            "first_valid_workload_name": sample.get("workload_name", ""),
            "selection_reason": reason, "valid": "true", "sample_valid": "true",
            "resolution_reason": reason,
            "sample_state_changed": sample.get("sample_state_changed", ""),
            "sample_valid_scope": sample.get("sample_valid_scope", ""),
            "sample_time_from_switch_in_ms": sample.get("sample_time_from_switch_in_ms", ""),
            "app_still_foreground": sample.get("app_still_foreground", ""),
        })
        self._reentry_events.append({
            "session_id": self.session_id, "reentry_event_id": window.event_id,
            "app_key": app_key, "app_id": app_id,
            "previous_foreground_app_key": window.previous_app_key,
            "previous_foreground_app_id": window.previous_app_id,
            "switch_in_time_ns": window.started_ns,
            "window_end_time_ns": window.started_ns + self.reentry_window_ns,
            "selected_time_ns": ts, "selected_workload_id": workload,
            "sample_count": len(window.samples),
            "state_unchanged_sample_count": sum(
                1 for candidate in window.samples
                if candidate.get("sample_state_changed") == "false"
            ),
            "valid": "true", "resolution_reason": reason,
        })
        if self.debugfs_writer is not None:
            self.debugfs_writer.write_reentry_markov(
                app_id=int(app_id), next_workload_id=workload,
                confidence_fixed=conf, boost_level=_boost(conf), app_key=app_key,
            )
        for rank, (next_id, count) in enumerate(self._ranked(counter), 1):
            ranked_conf = _confidence_fixed(count, total)
            self._reentry_predictions.append({
                "session_id": self.session_id, "timestamp_ns": ts, "mode": "REENTRY",
                "app_key": app_key, "app_id": app_id, "predicted_next_workload_id": next_id,
                "rank": rank, "confidence_fixed": ranked_conf, "boost_level": _boost(ranked_conf),
                "prediction_status": "observe_only",
            })
            self._policy_suggestions.append({
                "session_id": self.session_id, "timestamp_ns": ts, "mode": REENTRY,
                "target_app_key": app_key, "target_app_id": app_id,
                "target_is_foreground": "false", "lstm_probability_fixed": "",
                "previous_workload_id": "", "current_workload_id": "",
                "predicted_workload_id": next_id, "prediction_confidence_fixed": ranked_conf,
                "suggest_current_workload_protect": "false", "suggest_next_workload_protect": "false",
                "suggest_reentry_common_protect": "true", "suggest_reentry_workload_protect": "true",
                "status": "observe_only", "reason": "reentry_observe_only",
                "foreground_app_key": self._foreground_app_key, "foreground_app_id": self._foreground_app_id,
                "workload_id": next_id, "confidence_fixed": ranked_conf,
                "boost_level": _boost(ranked_conf), "suggestion": "reentry_observe_only",
                "observe_only": "true",
            })

    @staticmethod
    def _ranked(counter: Counter[int]) -> list[tuple[int, int]]:
        return sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))[:TOP_K]

    def _finalize_reentry(
        self, app_id: str, reason: str, ts: int, *, allow_low_fallback: bool,
    ) -> None:
        window = self._reentry_windows.get(app_id)
        if window is None or window.selected or window.finalized:
            return
        if allow_low_fallback:
            candidates = [
                sample for sample in window.samples
                if sample.get("sample_valid_scope") == "true"
                and int(sample["workload_id"]) == LOW_ACTIVITY
                and int(sample["timestamp_ns"]) - window.started_ns >= self.ignore_initial_low_ns
            ]
            if candidates:
                sample = candidates[0]
                sample["selection_reason"] = "fallback_low_activity"
                sample["valid"] = "true"
                self._select_reentry(app_id, window, sample, "fallback_low_activity")
                return
        window.finalized = True
        row = {
            "session_id": self.session_id, "timestamp_ns": ts, "mode": "REENTRY",
            "reentry_event_id": window.event_id, "app_key": window.current_app_key,
            "app_id": app_id, "runtime_app_id": app_id, "scope_name": "",
            "previous_foreground_app_key": window.previous_app_key,
            "current_foreground_app_key": window.current_app_key,
            "previous_foreground_app_id": window.previous_app_id,
            "current_foreground_app_id": window.current_app_id,
            "workload_id": "", "workload_name": "", "elapsed_ms": "",
            "switch_in_time_ns": window.started_ns,
            "window_end_time_ns": window.started_ns + self.reentry_window_ns,
            "candidate_sample_count": len(window.samples),
            "ignored_low_activity_count": sum(
                1 for sample in window.samples
                if sample.get("selection_reason") == "ignore_initial_low_activity"
            ),
            "first_valid_workload_id": "", "first_valid_workload_name": "",
            "selection_reason": reason, "resolution_reason": reason,
            "sample_state_changed": "", "sample_valid_scope": "",
            "sample_time_from_switch_in_ms": "",
            "app_still_foreground": str(app_id == self._foreground_app_id).lower(),
            "valid": "false", "sample_valid": "false",
        }
        self._reentry_samples.append(row)
        self._reentry_events.append({
            "session_id": self.session_id, "reentry_event_id": window.event_id,
            "app_key": window.current_app_key, "app_id": app_id,
            "previous_foreground_app_key": window.previous_app_key,
            "previous_foreground_app_id": window.previous_app_id,
            "switch_in_time_ns": window.started_ns,
            "window_end_time_ns": window.started_ns + self.reentry_window_ns,
            "selected_time_ns": "", "selected_workload_id": "",
            "sample_count": len(window.samples),
            "state_unchanged_sample_count": sum(
                1 for sample in window.samples
                if sample.get("sample_state_changed") == "false"
            ),
            "valid": "false", "resolution_reason": reason,
        })

    def close(self) -> None:
        if not self.enabled:
            return
        # 结束仍打开的窗口；只有应用仍在前台时才允许 LOW fallback。
        for app_id, window in sorted(self._reentry_windows.items()):
            self._finalize_reentry(
                app_id, "no_valid_scope_sample", time.time_ns(),
                allow_low_fallback=(app_id == self._foreground_app_id),
            )
        for app_id, state in sorted(self._continue.items()):
            meta = self._metadata.get(app_id, {})
            for (previous, current), counter in sorted(state.transitions.items()):
                total = sum(counter.values())
                for rank, (next_id, count) in enumerate(self._ranked(counter), 1):
                    conf = _confidence_fixed(count, total)
                    self._continue_predictions.append({
                        "session_id": self.session_id, "timestamp_ns": "", "mode": "CONTINUE",
                        "app_key": meta.get("app_key", ""), "app_id": app_id,
                        "scope_name": meta.get("scope_name", ""), "previous_workload_id": previous,
                        "current_workload_id": current, "predicted_next_workload_id": next_id,
                        "rank": rank, "confidence_fixed": conf, "boost_level": _boost(conf),
                        "prediction_status": "table_snapshot",
                        "prediction_epoch_id": state.epoch_id,
                        "resolution_reason": "table_snapshot",
                    })
        for app_id, counter in sorted(self._reentry_counts.items()):
            meta = self._metadata.get(app_id, {})
            total = sum(counter.values())
            for rank, (next_id, count) in enumerate(self._ranked(counter), 1):
                conf = _confidence_fixed(count, total)
                self._reentry_predictions.append({
                    "session_id": self.session_id, "timestamp_ns": "", "mode": "REENTRY",
                    "app_key": meta.get("app_key", ""), "app_id": app_id,
                    "predicted_next_workload_id": next_id, "rank": rank,
                    "confidence_fixed": conf, "boost_level": _boost(conf),
                    "prediction_status": "observe_only",
                })
        _write_csv(self.model_dir / "foreground_workload_history.csv", FOREGROUND_HISTORY_FIELDS, self._foreground_history)
        _write_csv(self.model_dir / "foreground_epochs.csv", FOREGROUND_EPOCH_FIELDS, self._foreground_epochs)
        _write_csv(self.model_dir / "continue_markov_updates.csv", CONTINUE_UPDATE_FIELDS, self._continue_updates)
        transitions: list[dict[str, Any]] = []
        for app_id, state in sorted(self._continue.items()):
            meta = self._metadata.get(app_id, {})
            for (previous, current), counter in sorted(state.transitions.items()):
                total = sum(counter.values())
                for rank, (next_id, count) in enumerate(self._ranked(counter), 1):
                    conf = _confidence_fixed(count, total)
                    transitions.append({
                        "session_id": self.session_id, "timestamp_ns": "", "mode": "CONTINUE",
                        "app_key": meta.get("app_key", ""), "app_id": app_id,
                        "scope_name": meta.get("scope_name", ""), "previous_workload_id": previous,
                        "current_workload_id": current, "next_workload_id": next_id, "count": count,
                        "total_count": total, "confidence": count / total, "confidence_fixed": conf,
                        "boost_level": _boost(conf), "rank": rank,
                    })
        _write_csv(self.model_dir / "continue_markov_transitions.csv", CONTINUE_TRANSITION_FIELDS, transitions)
        _write_csv(self.model_dir / "continue_markov_predictions.csv", CONTINUE_PREDICTION_FIELDS, self._continue_predictions)
        _write_csv(self.model_dir / "reentry_workload_samples.csv", REENTRY_SAMPLE_FIELDS, self._reentry_samples)
        _write_csv(self.model_dir / "reentry_events.csv", REENTRY_EVENT_FIELDS, self._reentry_events)
        _write_csv(self.model_dir / "reentry_markov_updates.csv", REENTRY_UPDATE_FIELDS, self._reentry_updates)
        reentry_transitions: list[dict[str, Any]] = []
        for app_id, counter in sorted(self._reentry_counts.items()):
            meta = self._metadata.get(app_id, {})
            total = sum(counter.values())
            for rank, (next_id, count) in enumerate(self._ranked(counter), 1):
                conf = _confidence_fixed(count, total)
                reentry_transitions.append({
                    "session_id": self.session_id, "timestamp_ns": "", "mode": "REENTRY",
                    "app_key": meta.get("app_key", ""), "app_id": app_id,
                    "next_workload_id": next_id, "count": count, "total_count": total,
                    "confidence": count / total, "confidence_fixed": conf,
                    "boost_level": _boost(conf), "rank": rank,
                })
        _write_csv(self.model_dir / "reentry_markov_transitions.csv", REENTRY_TRANSITION_FIELDS, reentry_transitions)
        _write_csv(self.model_dir / "reentry_markov_predictions.csv", REENTRY_PREDICTION_FIELDS, self._reentry_predictions)
        _write_csv(self.model_dir / "dual_markov_policy_suggestions.csv", DUAL_POLICY_FIELDS, self._policy_suggestions)

    def result(self) -> dict[str, int | str]:
        return {
            "continue_transition_keys": sum(len(state.transitions) for state in self._continue.values()),
            "continue_transition_rows": sum(len(self._ranked(counter)) for state in self._continue.values() for counter in state.transitions.values()),
            "reentry_apps": len(self._reentry_counts),
            "reentry_transition_rows": sum(len(self._ranked(counter)) for counter in self._reentry_counts.values()),
            "foreground_history_rows": len(self._foreground_history),
            "reentry_valid_samples": sum(1 for row in self._reentry_samples if row["valid"] == "true"),
            "reentry_invalid_samples": sum(1 for row in self._reentry_samples if row["valid"] == "false"),
            "reentry_event_count": len(self._reentry_events),
            "reentry_state_unchanged_valid_samples": sum(
                1 for row in self._reentry_samples
                if row["valid"] == "true" and row.get("sample_state_changed") == "false"
            ),
            "foreground_epoch_count": len(self._foreground_epochs),
            "cross_epoch_transition_blocked": self._cross_epoch_transitions_blocked,
            "final_result": "PASS" if self.enabled else "NOT_RUN",
        }


def replay_dual_markov(
    *,
    session_dir: Path | str,
    output_dir: Path | str,
    reentry_window_s: float = 5.0,
    ignore_initial_low_activity_s: float = 2.0,
) -> dict[str, Any]:
    """只读取既有 session，重放双模式逻辑，不写 debugfs。"""
    source = Path(session_dir)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    model = source / "model"
    foreground_path = model / "foreground_events.csv"
    workload_path = model / "cgroup_workload_state_1s.csv"
    foreground_rows: list[dict[str, Any]] = []
    if foreground_path.exists():
        with foreground_path.open(encoding="utf-8", newline="") as f:
            foreground_rows = list(csv.DictReader(f))
    workload_rows: list[dict[str, Any]] = []
    if workload_path.exists():
        with workload_path.open(encoding="utf-8", newline="") as f:
            workload_rows = list(csv.DictReader(f))
    app_ids = {
        str(row.get("app_key", "")).strip(): str(row.get("app_id", "")).strip()
        for row in workload_rows
        if str(row.get("app_key", "")).strip()
    }
    foreground_by_ts: list[tuple[int, str, str]] = []
    for row in foreground_rows:
        ts = _int(row.get("ts_ns")) or _int(row.get("timestamp_ns")) or _timestamp_ns(row.get("timestamp"))
        if ts is None:
            continue
        foreground_by_ts.append((ts, str(row.get("foreground_app", "")).strip(), app_ids.get(str(row.get("foreground_app", "")).strip(), "")))
    foreground_by_ts.sort()
    replay = DualWorkloadMarkov(
        enabled=True, session_id=source.name, model_dir=out, review_dir=out,
        debugfs_writer=None, reentry_window_s=reentry_window_s,
        ignore_initial_low_activity_s=ignore_initial_low_activity_s,
    )
    current_key = ""
    current_id = ""
    for row in sorted(workload_rows, key=lambda item: str(item.get("timestamp", ""))):
        ts = _int(row.get("timestamp_ns")) or _timestamp_ns(row.get("timestamp")) or 0
        if ts == 0:
            # Existing classifier output has second precision; use a deterministic order.
            ts = int(len(foreground_by_ts) + len(workload_rows)) * 1_000_000_000
        while foreground_by_ts and foreground_by_ts[0][0] <= ts:
            foreground_ts, key, app_id = foreground_by_ts.pop(0)
            if key and key != current_key:
                current_key = key
                current_id = app_id
                replay.observe_foreground(
                    foreground_app_key=current_key,
                    foreground_app_id=current_id,
                    timestamp_ns=foreground_ts,
                )
        app_id = _int(row.get("app_id"))
        workload = _int(row.get("workload_id"))
        if app_id is None or workload is None:
            continue
        replay.observe_workload(
            app_key=str(row.get("app_key", "")), app_id=app_id,
            scope_name=str(row.get("scope_name", "")), workload_id=workload,
            cgroup_id=_int(row.get("cgroup_id")), workload_name=str(row.get("workload_name", "")),
            timestamp_ns=ts, foreground_app_key=current_key, foreground_app_id=current_id,
            state_changed=_bool(row.get("state_changed", "true")),
            sample_valid_scope=str(row.get("status", "ok")).strip().lower() == "ok",
        )
    replay.close()
    for source_name, replay_name in (
        ("continue_markov_transitions.csv", "continue_replay_transitions.csv"),
        ("continue_markov_updates.csv", "continue_replay_updates.csv"),
        ("continue_markov_predictions.csv", "continue_replay_predictions.csv"),
        ("foreground_epochs.csv", "foreground_epochs.csv"),
        ("reentry_events.csv", "reentry_events.csv"),
        ("reentry_workload_samples.csv", "reentry_replay_samples.csv"),
        ("reentry_markov_transitions.csv", "reentry_replay_transitions.csv"),
        ("reentry_markov_predictions.csv", "reentry_replay_predictions.csv"),
    ):
        source_path = out / source_name
        target_path = out / replay_name
        if source_path.exists() and source_path != target_path:
            shutil.copyfile(source_path, target_path)
    result = replay.result()
    valid_per_app: Counter[str] = Counter()
    invalid_reasons: Counter[str] = Counter()
    candidate_distribution: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for row in replay._reentry_samples:
        if row.get("valid") == "true":
            valid_per_app[str(row.get("app_key", ""))] += 1
        else:
            invalid_reasons[str(row.get("selection_reason", "unknown"))] += 1
    for app_id, counter in replay._reentry_counts.items():
        app_key = replay._metadata.get(app_id, {}).get("app_key", app_id)
        for workload, count in counter.items():
            candidate_distribution[str(app_key)][str(workload)] += count
    result.update({
        "reentry_valid_samples_per_app": dict(sorted(valid_per_app.items())),
        "reentry_invalid_reasons": dict(sorted(invalid_reasons.items())),
        "reentry_candidate_distribution": {
            app: dict(sorted(counter.items()))
            for app, counter in sorted(candidate_distribution.items())
        },
        "data_sufficient_for_accuracy_conclusion": False,
        "accuracy_conclusion_note": "单 session 样本量仅用于验证链路，不足以估计泛化准确率",
    })
    (out / "dual_markov_replay_summary.json").write_text(
        __import__("json").dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "dual_markov_replay_summary.md").write_text(
        "# 双模式 Markov 重放汇总\n\n"
        + "\n".join(f"- {key}: {value}" for key, value in result.items())
        + "\n- debugfs_write: false\n"
        + "- 结论：该单 session 覆盖 epoch 和稳定 REENTRY 样本，但不足以支撑准确率结论。\n",
        encoding="utf-8",
    )
    return result
