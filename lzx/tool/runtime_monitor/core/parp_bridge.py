"""Fail-closed userspace bridge from online App LSTM to PARP debugfs.

The bridge deliberately targets the parser present in the v4-parp patch:

    app_bind:  domain_id app_id ttl_ms epoch_id model_version
    app_prior: app_id use_score_q15 rank horizon_ms ttl_ms model_version

The checked-in v4-parp patch does not contain an ``app_prior_batch`` parser,
so this module discovers that file for reporting but never guesses its ABI.
Writes are performed by a bounded worker so a debugfs stall cannot stop the
Runtime Monitor sampling loop.  The default mode is ``off`` and all failures
are fail-closed.

Test2 v3 predictions use ``prediction_format=app_probability``: each row is
one whitelist App probability and has no model horizon.  The legacy horizon
format remains accepted for regression tests.  The ``horizon_ms`` token in
the serialized ``app_prior`` ABI is a fixed sink-control value required by
the current kernel parser, not a v3 model output dimension.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

BRIDGE_MODES = {"off", "dry-run", "shadow-write"}
Q15_ONE = 32767
DEFAULT_SCHEMA_VERSION = "parp-app-prior-v1"

AUDIT_FIELDS = [
    "timestamp_ns",
    "event_type",
    "prediction_id",
    "batch_id",
    "current_app",
    "current_app_id",
    "candidate_app",
    "candidate_app_id",
    "probability",
    "rank",
    "candidate_running",
    "opened_apps",
    "history_length",
    "prediction_trigger",
    "prediction_format",
    "prediction_latency_us",
    "memcg_path",
    "domain_id",
    "bind_generation",
    "prior_generation",
    "model_name",
    "model_version",
    "schema_version",
    "horizon_ms",
    "ttl_ms",
    "valid_until_ns",
    "bridge_mode",
    "debugfs_path",
    "serialized_command",
    "write_attempted",
    "write_success",
    "write_errno",
    "write_latency_us",
    "snapshot_update_seen",
    "snapshot_generation_before",
    "snapshot_generation",
    "fallback_reason",
    "status",
    "error",
]


@dataclass(frozen=True)
class PriorCandidate:
    app_key: str
    app_name: str
    app_id: int
    probability: float
    rank: int
    candidate_running: bool


@dataclass(frozen=True)
class _WriteJob:
    event_type: str
    path: Path
    command: str
    values: dict[str, Any]


def _text_bool(value: bool) -> str:
    return "true" if value else "false"


def _finite_probability(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0.0:
        return None
    return result


def _split_apps(value: Any) -> set[str]:
    return {
        item.strip()
        for item in str(value or "").replace(";", "|").split("|")
        if item.strip()
    }


def _q15(value: float) -> int:
    return max(0, min(Q15_ONE, int(round(value * Q15_ONE))))


def resolve_scope_domain_id(slice_name: str, scope_name: str) -> tuple[int | None, str]:
    """Resolve the existing user cgroup inode without creating or changing it."""
    if not slice_name or not scope_name:
        return None, ""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", slice_name, "-p", "ControlGroup", "--value"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    if result.returncode != 0 or not result.stdout.strip():
        return None, ""
    base = Path("/sys/fs/cgroup") / result.stdout.strip().lstrip("/")
    path = base / scope_name
    try:
        return int(path.stat().st_ino), str(path)
    except OSError:
        return None, str(path)


class PARPDebugfsBridge:
    """Bridge online LSTM results into the PARP control surface.

    ``runtime_scope`` is intentionally duck-typed so unit tests can provide a
    small fake scope.  A real scope is the existing RuntimeAppScope loaded by
    Runtime Monitor.
    """

    def __init__(
        self,
        *,
        mode: str,
        debugfs_root: str | Path,
        runtime_scope: Any,
        output_dir: str | Path,
        session_id: str,
        slice_name: str = "",
        app_bind_config: str | Path | None = None,
        model_name: str = "AppLSTM",
        model_version: int = 401,
        schema_version: str = DEFAULT_SCHEMA_VERSION,
        prior_ttl_ms: int = 180_000,
        min_update_interval_ms: int = 0,
        max_retries: int = 2,
        queue_size: int = 64,
    ) -> None:
        if mode not in BRIDGE_MODES:
            raise ValueError(f"invalid PARP bridge mode: {mode}")
        self.mode = mode
        self.debugfs_root = Path(debugfs_root).expanduser()
        self.runtime_scope = runtime_scope
        self.output_dir = Path(output_dir)
        self.parp_dir = self.output_dir / "parp"
        self.parp_dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id
        self.slice_name = slice_name
        self.model_name = model_name
        self.model_version = int(model_version)
        self.schema_version = schema_version
        self.prior_ttl_ms = max(1, int(prior_ttl_ms))
        self.min_update_interval_ns = max(0, int(min_update_interval_ms)) * 1_000_000
        self.max_retries = max(0, int(max_retries))
        self.app_bind_config = self._load_bind_config(app_bind_config)

        self.app_bind_path = self.debugfs_root / "app_bind"
        self.app_prior_path = self.debugfs_root / "app_prior"
        self.app_prior_batch_path = self.debugfs_root / "app_prior_batch"
        self.stats_path = self.debugfs_root / "stats"
        self.snapshot_path = self.debugfs_root / "snapshot"
        self.mode_path = self.debugfs_root / "mode"

        self.debugfs_root_exists = self._is_dir(self.debugfs_root)
        self.app_bind_available = self._exists(self.app_bind_path)
        self.app_prior_available = self._exists(self.app_prior_path)
        self.app_prior_batch_available = self._exists(self.app_prior_batch_path)
        self.shadow_write_ready = (
            self.mode == "shadow-write"
            and self.app_bind_available
            and self.app_prior_available
            and self._is_file(self.app_bind_path)
            and self._is_file(self.app_prior_path)
            and self._writable(self.app_bind_path)
            and self._writable(self.app_prior_path)
        )

        self.audit_path = self.parp_dir / "parp_bridge_events.csv"
        self.summary_path = self.parp_dir / "parp_bridge_summary.json"
        self.bind_commands_path = self.parp_dir / "app_bind_commands.log"
        self.prior_commands_path = self.parp_dir / "app_prior_commands.log"
        self.snapshot_updates_path = self.parp_dir / "snapshot_updates.csv"
        self._audit_file = self.audit_path.open("w", encoding="utf-8", newline="")
        self._audit_writer = csv.DictWriter(self._audit_file, fieldnames=AUDIT_FIELDS)
        self._audit_writer.writeheader()
        self._audit_file.flush()
        self._audit_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._bind_commands_file = self.bind_commands_path.open("w", encoding="utf-8")
        self._prior_commands_file = self.prior_commands_path.open("w", encoding="utf-8")
        self._snapshot_file = self.snapshot_updates_path.open("w", encoding="utf-8", newline="")
        self._snapshot_writer = csv.DictWriter(
            self._snapshot_file,
            fieldnames=["timestamp_ns", "event_type", "prediction_id", "batch_id", "snapshot_generation", "status"],
        )
        self._snapshot_writer.writeheader()
        self._snapshot_file.flush()

        self._queue: queue.Queue[_WriteJob | None] = queue.Queue(maxsize=max(1, queue_size))
        self._worker = threading.Thread(
            target=self._worker_main,
            name="parp-debugfs-bridge",
            daemon=True,
        )
        self._worker.start()
        self._closed = False

        self._prediction_counter = 0
        self._bind_generation = 0
        self._prior_generation = 0
        self._last_prediction_ns = 0
        self._last_prediction_signature = ""
        self._pending_bindings: set[str] = set()
        self._requested_bindings: set[str] = set()
        self._successful_bindings: dict[str, tuple[int, str]] = {}
        self._stats = {
            "online_lstm_triggered": 0,
            "lstm_predictions_total": 0,
            "lstm_predictions_valid": 0,
            "lstm_predictions_dropped": 0,
            "app_bind_attempts": 0,
            "app_bind_success": 0,
            "app_bind_failures": 0,
            "app_prior_attempts": 0,
            "app_prior_success": 0,
            "app_prior_failures": 0,
            "bind_resolution_attempts": 0,
            "bind_missing_cgroup": 0,
            "bind_serialized_commands": 0,
            "bind_write_attempts": 0,
            "bind_retry_attempts": 0,
            "bind_dry_run_not_attempted": 0,
            "bind_blocked_missing_interface": 0,
            "prior_write_attempts": 0,
            "prior_retry_attempts": 0,
            "prior_dry_run_not_attempted": 0,
            "prior_blocked_missing_interface": 0,
            "duplicate_predictions_suppressed": 0,
            "stale_predictions_suppressed": 0,
            "missing_debugfs": int(not self.debugfs_root_exists),
            "missing_cgroup": 0,
            "snapshot_updates_observed": 0,
            "snapshot_binding_updates_observed": 0,
            "snapshot_prior_updates_observed": 0,
            "prediction_to_snapshot_matched": 0,
            "queue_drops": 0,
            "partial_writes": 0,
            "prediction_funnel": {
                "prediction_batch_count": 0,
                "candidate_row_count": 0,
                "horizon_row_counts": {},
                "target_horizon": 3,
                "target_horizon_row_count": 0,
                "non_target_horizon_row_count": 0,
                "prediction_format": "",
                "probability_row_count": 0,
                "unknown_foreground_batch_count": 0,
                "unknown_foreground_candidate_row_count_retained": 0,
                "non_whitelist_row_count": 0,
                "disabled_app_row_count": 0,
                "invalid_probability_row_count": 0,
                "duplicate_row_count": 0,
                "current_app_row_count": 0,
                "candidate_row_count_after_filter": 0,
                "prior_command_row_count": 0,
            },
        }

    @staticmethod
    def _exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    @staticmethod
    def _is_dir(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False

    @staticmethod
    def _is_file(path: Path) -> bool:
        try:
            return path.is_file()
        except OSError:
            return False

    @staticmethod
    def _writable(path: Path) -> bool:
        try:
            return path.exists() and path.is_file() and bool(os.access(path, os.W_OK))
        except OSError:
            return False

    @staticmethod
    def _load_bind_config(path: str | Path | None) -> dict[str, dict[str, Any]]:
        if not path:
            return {}
        try:
            data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        raw_apps = data.get("apps", data) if isinstance(data, dict) else {}
        if not isinstance(raw_apps, dict):
            return {}
        return {
            str(key): value
            for key, value in raw_apps.items()
            if isinstance(value, dict)
        }

    def preflight(self) -> dict[str, Any]:
        result = {
            "bridge_mode": self.mode,
            "debugfs_root": str(self.debugfs_root),
            "debugfs_root_exists": self.debugfs_root_exists,
            "app_bind": {"path": str(self.app_bind_path), "exists": self.app_bind_available, "writable": self._writable(self.app_bind_path)},
            "app_prior": {"path": str(self.app_prior_path), "exists": self.app_prior_available, "writable": self._writable(self.app_prior_path)},
            "app_prior_batch": {"path": str(self.app_prior_batch_path), "exists": self.app_prior_batch_available, "supported": False},
            "stats": {"path": str(self.stats_path), "exists": self._exists(self.stats_path)},
            "snapshot": {"path": str(self.snapshot_path), "exists": self._exists(self.snapshot_path)},
            "mode_file": {"path": str(self.mode_path), "exists": self._exists(self.mode_path)},
            "shadow_write_ready": self.shadow_write_ready,
            "fail_closed": self.mode == "shadow-write" and not self.shadow_write_ready,
            "status": "READY" if self.mode != "shadow-write" or self.shadow_write_ready else "FAIL_CLOSED",
        }
        self.parp_dir.joinpath("preflight.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return result

    def startup_bindings(self) -> None:
        """Attempt idempotent bindings for configured whitelist apps."""
        if self.mode == "off" or self.runtime_scope is None:
            return
        for app in getattr(self.runtime_scope, "apps", []):
            if not getattr(app, "prediction_enabled", True):
                continue
            self.ensure_binding(str(app.app_key), reason="bridge_start")

    def successful_binding_apps(self) -> set[str]:
        """Return only bindings acknowledged by the actual debugfs writer."""
        return set(self._successful_bindings)

    def ensure_binding(self, app_key: str, *, reason: str) -> bool:
        app = self._find_app(app_key)
        now_ns = time.monotonic_ns()
        if app is None or int(getattr(app, "app_id", 0)) <= 0:
            self._audit_base(
                event_type="app_bind",
                timestamp_ns=now_ns,
                app_key=app_key,
                fallback_reason="INVALID_APP_ID",
                status="UNBOUND",
                error="app_not_in_runtime_scope",
            )
            self._stats["app_bind_failures"] += 1
            return False

        self._requested_bindings.add(app_key)
        self._stats["bind_resolution_attempts"] += 1
        domain_id, memcg_path = self._resolve_domain(app)
        if domain_id is None:
            self._stats["missing_cgroup"] += 1
            self._stats["bind_missing_cgroup"] += 1
            self._audit_base(
                event_type="app_bind",
                timestamp_ns=now_ns,
                current_app=str(app_key),
                current_app_id=int(app.app_id),
                memcg_path=memcg_path,
                fallback_reason="MISSING_CGROUP",
                status="UNBOUND",
                error="no reliable cgroup inode/domain id",
            )
            self._stats["app_bind_failures"] += 1
            return False

        binding_hash = hashlib.sha256(
            f"{domain_id}:{app.app_id}:{self.model_version}".encode("utf-8")
        ).hexdigest()[:16]
        previous = self._successful_bindings.get(app_key)
        if previous == (int(domain_id), binding_hash) or app_key in self._pending_bindings:
            return True

        self._bind_generation += 1
        bind_generation = self._bind_generation
        command = f"{int(domain_id)} {int(app.app_id)} {self.prior_ttl_ms} {bind_generation} {self.model_version}"
        values = {
            "timestamp_ns": now_ns,
            "current_app": app_key,
            "current_app_id": int(app.app_id),
            "memcg_path": memcg_path,
            "domain_id": int(domain_id),
            "bind_generation": bind_generation,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "schema_version": self.schema_version,
            "ttl_ms": self.prior_ttl_ms,
            "bridge_mode": self.mode,
            "debugfs_path": str(self.app_bind_path),
            "serialized_command": command,
            "fallback_reason": "" if self.mode != "shadow-write" or self.shadow_write_ready else "FAIL_CLOSED",
            "status": "DRY_RUN" if self.mode == "dry-run" else ("QUEUED" if self.shadow_write_ready else "FAIL_CLOSED"),
            "error": "" if self.mode != "shadow-write" or self.shadow_write_ready else "PARP debugfs preflight failed",
        }
        self._stats["app_bind_attempts"] += 1
        self._pending_bindings.add(app_key)
        self._stats["bind_serialized_commands"] += 1
        if self.mode == "dry-run":
            self._stats["bind_dry_run_not_attempted"] += 1
            self._record({**values, "event_type": "app_bind", "write_attempted": "false", "write_success": "false"})
            self._pending_bindings.discard(app_key)
            self._successful_bindings[app_key] = (int(domain_id), binding_hash)
            return True
        if not self.shadow_write_ready:
            self._stats["bind_blocked_missing_interface"] += 1
            self._record({**values, "event_type": "app_bind", "write_attempted": "false", "write_success": "false"})
            self._pending_bindings.discard(app_key)
            return False
        return self._enqueue(_WriteJob("app_bind", self.app_bind_path, command, values))

    def submit_prediction(self, feature_row: dict[str, Any], prediction_result: dict[str, Any]) -> None:
        """Queue one event-triggered LSTM output for PARP prior serialization."""
        if self.mode == "off":
            return
        trigger_type = str(prediction_result.get("trigger_type", ""))
        prediction_format = str(prediction_result.get("prediction_format", "horizon") or "horizon")
        is_prediction_trigger = bool(trigger_type and trigger_type != "event_cooldown")
        if is_prediction_trigger and prediction_result.get("inference_executed", False):
            self._stats["online_lstm_triggered"] += 1
        if prediction_result.get("status") != "success":
            self._stats["lstm_predictions_dropped"] += int(
                is_prediction_trigger and prediction_result.get("inference_executed", False)
            )
            return
        self._stats["lstm_predictions_total"] += 1

        supplied_valid_until = prediction_result.get("valid_until_ns")
        try:
            if supplied_valid_until not in (None, "") and time.monotonic_ns() >= int(supplied_valid_until):
                self._stats["stale_predictions_suppressed"] += 1
                self._stats["lstm_predictions_dropped"] += 1
                self._audit_base(
                    event_type="app_prior",
                    timestamp_ns=time.monotonic_ns(),
                    prediction_id=prediction_result.get("prediction_id", ""),
                    prediction_trigger=prediction_result.get("trigger_type", ""),
                    prediction_format=prediction_format,
                    bridge_mode=self.mode,
                    fallback_reason="STALE_PREDICTION",
                    status="SUPPRESSED",
                    error="prediction valid_until_ns has expired",
                )
                return
        except (TypeError, ValueError):
            self._stats["lstm_predictions_dropped"] += 1
            self._audit_base(
                event_type="app_prior",
                timestamp_ns=time.monotonic_ns(),
                prediction_id=prediction_result.get("prediction_id", ""),
                bridge_mode=self.mode,
                fallback_reason="INVALID_VALID_UNTIL",
                status="DROPPED",
                error="valid_until_ns is not an integer",
            )
            return

        current_key = str(prediction_result.get("mapped_foreground_app", "") or "").strip()
        current_app = self._find_app_by_vocab(current_key)
        current_id = int(getattr(current_app, "app_id", 0)) if current_app else 0
        opened_keys = _split_apps(prediction_result.get("mapped_opened_apps", ""))
        rows = prediction_result.get("all_probabilities") or prediction_result.get("outputs") or []
        rows = list(rows) if isinstance(rows, Iterable) and not isinstance(rows, (str, bytes, dict)) else []
        funnel = self._stats["prediction_funnel"]
        funnel["prediction_batch_count"] += 1
        funnel["candidate_row_count"] += len(rows)
        if not funnel["prediction_format"]:
            funnel["prediction_format"] = prediction_format
        elif funnel["prediction_format"] != prediction_format:
            funnel["prediction_format"] = "mixed"
        if prediction_format == "app_probability":
            funnel["probability_row_count"] += len(rows)
        if current_id <= 0:
            funnel["unknown_foreground_batch_count"] += 1
        candidates = self._normalize_candidates(rows, current_key, current_id, opened_keys, prediction_format)
        if not candidates:
            self._stats["lstm_predictions_dropped"] += 1
            self._audit_base(
                event_type="app_prior",
                timestamp_ns=time.monotonic_ns(),
                current_app=current_key,
                current_app_id=current_id,
                prediction_trigger=prediction_result.get("trigger_type", ""),
                prediction_format=prediction_format,
                bridge_mode=self.mode,
                fallback_reason="NO_VALID_CANDIDATE",
                status="DROPPED",
                error="no whitelist candidate survived validation",
            )
            return

        now_ns = time.monotonic_ns()
        if self._last_prediction_ns and now_ns - self._last_prediction_ns < self.min_update_interval_ns:
            self._stats["stale_predictions_suppressed"] += 1
            self._audit_base(
                event_type="app_prior",
                timestamp_ns=now_ns,
                current_app=current_key,
                current_app_id=current_id,
                prediction_trigger=prediction_result.get("trigger_type", ""),
                prediction_format=prediction_format,
                bridge_mode=self.mode,
                fallback_reason="MIN_UPDATE_INTERVAL",
                status="SUPPRESSED",
                error="prediction arrived before minimum update interval",
            )
            return

        signature = "|".join(f"{item.app_id}:{_q15(item.probability)}" for item in candidates)
        signature = f"{current_id}:{signature}"
        if signature == self._last_prediction_signature:
            self._stats["duplicate_predictions_suppressed"] += 1
            self._audit_base(
                event_type="app_prior",
                timestamp_ns=now_ns,
                current_app=current_key,
                current_app_id=current_id,
                bridge_mode=self.mode,
                fallback_reason="DUPLICATE_PREDICTION",
                status="SUPPRESSED",
                error="same candidate scores already queued",
            )
            return

        self._last_prediction_ns = now_ns
        self._last_prediction_signature = signature
        self._prediction_counter += 1
        self._prior_generation += 1
        prediction_id = str(
            prediction_result.get("prediction_id")
            or f"{self.session_id}-p{self._prediction_counter:05d}"
        )
        batch_id = f"{prediction_id}-b{self._prior_generation:05d}"
        self._stats["lstm_predictions_valid"] += 1
        prediction_latency_us = int(round(float(prediction_result.get("predict_latency_ms", 0.0)) * 1000.0))
        history_length = len(str(prediction_result.get("history_apps", "")).split("|")) if prediction_result.get("history_apps") else 0
        opened_text = "|".join(sorted(opened_keys))
        horizon_ms = int(prediction_result.get("horizon_ms", 180000) or 180000)

        for item in candidates:
            self.ensure_binding(item.app_key, reason="prediction_candidate")
            if current_app is not None:
                self.ensure_binding(current_app.app_key, reason="prediction_current")
            valid_until_ns = now_ns + self.prior_ttl_ms * 1_000_000
            command = (
                f"{item.app_id} {_q15(item.probability)} {item.rank} "
                f"{horizon_ms} "
                f"{self.prior_ttl_ms} {self.model_version}"
            )
            values = {
                "timestamp_ns": now_ns,
                "event_type": "app_prior",
                "prediction_id": prediction_id,
                "batch_id": batch_id,
                "current_app": current_key,
                "current_app_id": current_id,
                "candidate_app": item.app_name,
                "candidate_app_id": item.app_id,
                "probability": f"{item.probability:.9f}",
                "rank": item.rank,
                "candidate_running": _text_bool(item.candidate_running),
                "opened_apps": opened_text,
                "history_length": history_length,
                "prediction_trigger": prediction_result.get("trigger_type", ""),
                "prediction_format": prediction_format,
                "prediction_latency_us": prediction_latency_us,
                "prior_generation": self._prior_generation,
                "model_name": self.model_name,
                "model_version": self.model_version,
                "schema_version": self.schema_version,
                "horizon_ms": horizon_ms,
                "ttl_ms": self.prior_ttl_ms,
                "valid_until_ns": valid_until_ns,
                "bridge_mode": self.mode,
                "debugfs_path": str(self.app_prior_path),
                "serialized_command": command,
                "fallback_reason": "" if self.mode != "shadow-write" or self.shadow_write_ready else "FAIL_CLOSED",
                "status": "DRY_RUN" if self.mode == "dry-run" else ("QUEUED" if self.shadow_write_ready else "FAIL_CLOSED"),
                "error": "" if self.mode != "shadow-write" or self.shadow_write_ready else "PARP debugfs preflight failed",
            }
            self._stats["app_prior_attempts"] += 1
            self._stats["prediction_funnel"]["prior_command_row_count"] += 1
            if self.mode == "dry-run":
                self._stats["prior_dry_run_not_attempted"] += 1
                self._record({**values, "write_attempted": "false", "write_success": "false"})
            elif self.shadow_write_ready:
                self._enqueue(_WriteJob("app_prior", self.app_prior_path, command, values))
            else:
                self._stats["prior_blocked_missing_interface"] += 1
                self._stats["app_prior_failures"] += 1
                self._record({**values, "write_attempted": "false", "write_success": "false"})

    def _normalize_candidates(
        self,
        rows: Iterable[dict[str, Any]],
        current_key: str,
        current_id: int,
        opened_keys: set[str],
        prediction_format: str = "horizon",
    ) -> list[PriorCandidate]:
        raw: list[tuple[str, str, int, float, bool]] = []
        seen: set[int] = set()
        funnel = self._stats["prediction_funnel"]
        for row in rows:
            if not isinstance(row, dict):
                funnel["non_whitelist_row_count"] += 1
                continue
            if prediction_format == "app_probability":
                horizon = 0
            else:
                try:
                    horizon = int(row.get("horizon", 3) or 3)
                except (TypeError, ValueError):
                    funnel["non_target_horizon_row_count"] += 1
                    continue
                horizon_key = str(horizon)
                horizon_counts = funnel["horizon_row_counts"]
                horizon_counts[horizon_key] = horizon_counts.get(horizon_key, 0) + 1
                if horizon != 3:
                    funnel["non_target_horizon_row_count"] += 1
                    continue
                funnel["target_horizon_row_count"] += 1
            app_name = str(row.get("app", "")).strip()
            app = self._find_app_by_vocab(app_name)
            if app is None:
                funnel["non_whitelist_row_count"] += 1
                continue
            if not getattr(app, "prediction_enabled", True):
                funnel["disabled_app_row_count"] += 1
                continue
            app_id = int(getattr(app, "app_id", 0))
            if app_id <= 0:
                funnel["non_whitelist_row_count"] += 1
                continue
            if app_id == current_id:
                funnel["current_app_row_count"] += 1
                continue
            if app_id in seen:
                funnel["duplicate_row_count"] += 1
                continue
            probability = _finite_probability(row.get("next_use_probability", row.get("probability")))
            if probability is None:
                funnel["invalid_probability_row_count"] += 1
                continue
            seen.add(app_id)
            funnel["candidate_row_count_after_filter"] += 1
            if current_id <= 0:
                funnel["unknown_foreground_candidate_row_count_retained"] += 1
            raw.append((
                str(app.app_key),
                app_name,
                app_id,
                probability,
                app_name in opened_keys or str(app.app_key) in opened_keys,
            ))
        if not raw:
            return []
        total = sum(item[3] for item in raw)
        if not math.isfinite(total) or total <= 0.0:
            return []
        raw.sort(key=lambda item: (-item[3], item[2]))
        result: list[PriorCandidate] = []
        for rank, (app_key, app_name, app_id, probability, running) in enumerate(raw, start=1):
            result.append(PriorCandidate(app_key, app_name, app_id, probability / total, rank, running))
        return result

    def _find_app(self, app_key: str) -> Any | None:
        for app in getattr(self.runtime_scope, "apps", []) if self.runtime_scope is not None else []:
            if str(getattr(app, "app_key", "")) == app_key:
                return app
        return None

    def _find_app_by_vocab(self, vocab_name: str) -> Any | None:
        for app in getattr(self.runtime_scope, "apps", []) if self.runtime_scope is not None else []:
            if str(getattr(app, "vocab_name", "")) == vocab_name:
                return app
        return None

    def _resolve_domain(self, app: Any) -> tuple[int | None, str]:
        configured = self.app_bind_config.get(str(getattr(app, "app_key", "")), {})
        memcg_path = str(configured.get("memcg_path", ""))
        try:
            configured_id = int(configured.get("domain_id", 0) or 0)
        except (TypeError, ValueError):
            configured_id = 0
        if configured_id > 0:
            return configured_id, memcg_path
        configured_scope = str(getattr(app, "scope_name", ""))
        app_key = str(getattr(app, "app_key", "")).strip().lower()
        scope_candidates = [
            configured_scope,
            f"automation-{app_key}.scope" if app_key else "",
        ]
        scope_candidates = [item for item in scope_candidates if item]
        if not self.slice_name or not scope_candidates:
            return None, memcg_path
        for scope_name in scope_candidates:
            domain_id, resolved_path = resolve_scope_domain_id(self.slice_name, scope_name)
            if domain_id is not None:
                return domain_id, resolved_path or memcg_path
        if not memcg_path:
            memcg_path = f"/sys/fs/cgroup/{self.slice_name}/{configured_scope}"
        return None, memcg_path

    def _enqueue(self, job: _WriteJob) -> bool:
        try:
            self._queue.put_nowait(job)
            return True
        except queue.Full:
            self._stats["queue_drops"] += 1
            self._stats["lstm_predictions_dropped"] += int(job.event_type == "app_prior")
            self._record({
                **job.values,
                "event_type": job.event_type,
                "status": "DROPPED",
                "fallback_reason": "QUEUE_FULL",
                "error": "bounded bridge queue is full",
                "write_attempted": "false",
                "write_success": "false",
            })
            return False

    def _worker_main(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                self._queue.task_done()
                return
            try:
                self._perform_write(job)
            except Exception as exc:  # bridge must never kill the monitor
                self._stats["app_bind_failures"] += int(job.event_type == "app_bind")
                self._stats["app_prior_failures"] += int(job.event_type == "app_prior")
                self._pending_bindings.discard(str(job.values.get("current_app", "")))
                self._record({
                    **job.values,
                    "event_type": job.event_type,
                    "status": "WRITE_ERROR",
                    "error": repr(exc),
                    "write_attempted": "true",
                    "write_success": "false",
                })
            finally:
                self._queue.task_done()

    def _perform_write(self, job: _WriteJob) -> None:
        started = time.perf_counter_ns()
        snapshot_generation_before = self._snapshot_generation()
        success = False
        error = ""
        errno_text = ""
        for attempt in range(self.max_retries + 1):
            stat_key = "bind_write_attempts" if job.event_type == "app_bind" else "prior_write_attempts"
            retry_key = "bind_retry_attempts" if job.event_type == "app_bind" else "prior_retry_attempts"
            self._stats[stat_key] += 1
            if attempt > 0:
                self._stats[retry_key] += 1
            try:
                with self._write_lock, job.path.open("w", encoding="utf-8") as stream:
                    written = stream.write(job.command + "\n")
                    stream.flush()
                if written != len(job.command) + 1:
                    self._stats["partial_writes"] += 1
                    error = f"partial_write:{written}/{len(job.command) + 1}"
                    continue
                success = True
                break
            except OSError as exc:
                error = str(exc)
                errno_text = str(getattr(exc, "errno", ""))
                if attempt < self.max_retries:
                    time.sleep(0.01 * (attempt + 1))
        latency_us = int(round((time.perf_counter_ns() - started) / 1000.0))
        values = {
            **job.values,
            "event_type": job.event_type,
            "write_attempted": "true",
            "write_success": _text_bool(success),
            "write_errno": errno_text,
            "write_latency_us": latency_us,
            "status": "OK" if success else "WRITE_ERROR",
            "error": error,
        }
        if success:
            if job.event_type == "app_bind":
                self._stats["app_bind_success"] += 1
                app_key = str(job.values.get("current_app", ""))
                self._pending_bindings.discard(app_key)
                try:
                    self._successful_bindings[app_key] = (
                        int(job.values.get("domain_id", 0)),
                        hashlib.sha256(
                            f"{job.values.get('domain_id')}:{job.values.get('current_app_id')}:{self.model_version}".encode("utf-8")
                        ).hexdigest()[:16],
                    )
                except (TypeError, ValueError):
                    pass
            else:
                self._stats["app_prior_success"] += 1
            snapshot_generation = self._snapshot_generation()
            ack = self._snapshot_ack(snapshot_generation_before, snapshot_generation)
            values["snapshot_generation_before"] = snapshot_generation_before
            values["snapshot_update_seen"] = _text_bool(ack)
            values["snapshot_generation"] = snapshot_generation
            if ack:
                self._stats["snapshot_updates_observed"] += 1
                if job.event_type == "app_bind":
                    self._stats["snapshot_binding_updates_observed"] += 1
                elif job.event_type == "app_prior":
                    self._stats["snapshot_prior_updates_observed"] += 1
                if job.event_type == "app_prior":
                    self._stats["prediction_to_snapshot_matched"] += 1
                self._record_snapshot_update(job, snapshot_generation)
        else:
            if job.event_type == "app_bind":
                self._stats["app_bind_failures"] += 1
                self._pending_bindings.discard(str(job.values.get("current_app", "")))
            else:
                self._stats["app_prior_failures"] += 1
        self._record(values)

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _v41_snapshot_generation(self) -> str:
        """Return v4.1's immutable snapshot version, if this ABI is present."""
        for line in self._read_text(self.snapshot_path).splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "version":
                return value.strip()
        return ""

    def _snapshot_text(self) -> str:
        """Return both v4.1 snapshot data and legacy stats acknowledgements."""
        return "\n".join(
            text for text in (
                self._read_text(self.snapshot_path),
                self._read_text(self.stats_path),
            ) if text
        )

    def _snapshot_ack(self, before: str, after: str) -> bool:
        """Require a v4.1 snapshot generation change after an actual write.

        Older interfaces did not expose a monotonic snapshot version; retain
        their explicit acknowledgement-marker behaviour for compatibility.
        """
        if self._v41_snapshot_generation():
            return bool(after and after != before)
        text = self._snapshot_text()
        return any(key in text for key in ("snapshot_generation", "prior_generation", "app_prior_update"))

    def _snapshot_generation(self) -> str:
        v41_generation = self._v41_snapshot_generation()
        if v41_generation:
            return v41_generation
        for line in self._snapshot_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in {"snapshot_generation", "prior_generation"}:
                return value.strip()
            fields = line.split()
            if len(fields) >= 3 and fields[0] == "snapshot_generation":
                return fields[-1]
        return ""

    def _record_snapshot_update(self, job: _WriteJob, generation: str) -> None:
        with self._audit_lock:
            self._snapshot_writer.writerow({
                "timestamp_ns": job.values.get("timestamp_ns", time.monotonic_ns()),
                "event_type": job.event_type,
                "prediction_id": job.values.get("prediction_id", ""),
                "batch_id": job.values.get("batch_id", ""),
                "snapshot_generation": generation,
                "status": "SEEN",
            })
            self._snapshot_file.flush()

    def _audit_base(self, *, event_type: str, timestamp_ns: int, **values: Any) -> None:
        self._record({"event_type": event_type, "timestamp_ns": timestamp_ns, **values})

    def _record(self, values: dict[str, Any]) -> None:
        row = {field: "" for field in AUDIT_FIELDS}
        row.update({key: value for key, value in values.items() if key in row})
        with self._audit_lock:
            self._audit_writer.writerow(row)
            self._audit_file.flush()
            event_type = str(row.get("event_type", ""))
            command = str(row.get("serialized_command", ""))
            if command and event_type == "app_bind":
                self._bind_commands_file.write(
                    f"{row.get('timestamp_ns', '')} {row.get('status', '')} {command}\n"
                )
                self._bind_commands_file.flush()
            elif command and event_type == "app_prior":
                self._prior_commands_file.write(
                    f"{row.get('timestamp_ns', '')} {row.get('status', '')} "
                    f"{row.get('prediction_id', '')} {command}\n"
                )
                self._prior_commands_file.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.join()
            self._queue.put(None)
            self._worker.join(timeout=5)
        finally:
            with self._audit_lock:
                self._audit_file.flush()
                self._audit_file.close()
                self._bind_commands_file.flush()
                self._bind_commands_file.close()
                self._prior_commands_file.flush()
                self._prior_commands_file.close()
                self._snapshot_file.flush()
                self._snapshot_file.close()
            summary = {
                **self._stats,
                "bind_logical_app_count": len(self._requested_bindings),
                "prediction_batch_count": self._stats["prediction_funnel"]["prediction_batch_count"],
                "prediction_candidate_row_count": self._stats["prediction_funnel"]["candidate_row_count"],
                "prior_command_row_count": self._stats["prediction_funnel"]["prior_command_row_count"],
                "session_id": self.session_id,
                "bridge_mode": self.mode,
                "debugfs_root": str(self.debugfs_root),
                "app_bind_path": str(self.app_bind_path),
                "app_prior_path": str(self.app_prior_path),
                "app_prior_batch_supported": False,
                "shadow_write_ready": self.shadow_write_ready,
                "kernel_snapshot_ack_note": "v4.1 requires the read-only snapshot version to change after each successful write; user-space write success alone is not snapshot proof.",
            }
            self.summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
