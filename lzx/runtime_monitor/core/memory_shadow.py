"""Read-only Test3 memory-value shadow observation.

This module is deliberately separate from the PARP bridge.  It consumes the
same online v3 LSTM batches and X11 lifecycle events, but only reads procfs and
cgroup-v2 counters.  In particular it never writes memory.reclaim, cgroup
memory controls, vmscan controls, MGLRU controls, or PARP policy state.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from collectors.process import ProcessSample
from core.writer import CsvWriter


MEMORY_FIELDS = [
    "session_id", "timestamp_ns", "timestamp", "sample_reason", "episode_id",
    "prediction_id", "prediction_batch_id", "app_key", "app_id", "memcg_path",
    "foreground_state", "running_state", "pid_count", "pids", "rss_bytes",
    "pss_bytes", "referenced_bytes", "swap_bytes", "memcg_current_bytes",
    "anon_bytes", "file_bytes", "active_anon_bytes", "inactive_anon_bytes",
    "active_file_bytes", "inactive_file_bytes", "pgfault", "pgmajfault",
    "workingset_refault_anon", "workingset_refault_file", "pswpin", "pswpout",
    "pgscan", "pgsteal", "proc_minflt", "proc_majflt", "proc_read_bytes",
    "metric_status", "metric_error", "collection_latency_us",
]

FAULT_FIELDS = [
    "session_id", "timestamp_ns", "episode_id", "prediction_id", "app_key",
    "pgfault_delta", "pgmajfault_delta", "proc_minflt_delta", "proc_majflt_delta",
    "status", "reason",
]
REFAULT_FIELDS = [
    "session_id", "timestamp_ns", "episode_id", "prediction_id", "app_key",
    "workingset_refault_anon_delta", "workingset_refault_file_delta", "status", "reason",
]
SWAP_FIELDS = [
    "session_id", "timestamp_ns", "episode_id", "prediction_id", "app_key",
    "swap_bytes_delta", "pswpin_delta", "pswpout_delta", "status", "reason",
]
RECLAIM_FIELDS = [
    "session_id", "timestamp_ns", "episode_id", "prediction_id", "app_key",
    "pgscan_delta", "pgsteal_delta", "evidence_scope", "status", "reason",
]

BATCH_FIELDS = [
    "session_id", "prediction_id", "prediction_batch_id", "generated_at_ns",
    "valid_until_ns", "trigger_type", "current_app", "current_app_id",
    "current_open_apps", "prediction_format", "prediction_latency_us",
    "candidate_count", "candidate_apps_json", "top1_app", "top3_apps",
    "bridge_batch_id", "snapshot_version_start", "snapshot_version_end", "bridge_status",
]
EPISODE_FIELDS = [
    "session_id", "episode_id", "prediction_id", "prediction_batch_id",
    "generated_at_ns", "valid_until_ns", "current_app", "trigger_type",
    "candidate_apps_json", "monitored_apps", "terminal_time_ns", "terminal_reason",
    "actual_next_app", "time_to_terminal_ms", "recovery_deadline_ns",
]

COUNTER_FIELDS = {
    "pgfault", "pgmajfault", "workingset_refault_anon", "workingset_refault_file",
    "pswpin", "pswpout", "pgscan", "pgsteal", "proc_minflt", "proc_majflt",
    "proc_read_bytes",
}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_key_values(path: Path) -> tuple[dict[str, int], str]:
    try:
        values: dict[str, int] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                values[parts[0].rstrip(":")] = int(parts[1])
            except ValueError:
                continue
        return values, ""
    except OSError as exc:
        return {}, str(exc)


def read_smaps_rollup(pid: int) -> tuple[dict[str, int], str]:
    """Read one process rollup in bytes without parsing individual VMAs."""
    path = Path("/proc") / str(pid) / "smaps_rollup"
    values: dict[str, int] = {}
    try:
        # smaps_rollup uses ``Rss: 123 kB`` (three tokens), unlike the
        # two-token cgroup memory.stat ABI.  Keep the parsers separate so a
        # valid workset can never silently become zero.
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                values[parts[0].rstrip(":")] = int(parts[1]) * 1024
            except ValueError:
                continue
    except OSError as exc:
        return {}, str(exc)
    return {key: values.get(key, 0) for key in ("Rss", "Pss", "Referenced", "Swap")}, ""


def read_cgroup_memory(path: Path) -> tuple[dict[str, int], str]:
    """Read only cgroup-v2 accounting files; missing values remain zero."""
    errors: list[str] = []
    values: dict[str, int] = {}
    try:
        values["memcg_current_bytes"] = int((path / "memory.current").read_text().strip())
    except (OSError, ValueError) as exc:
        values["memcg_current_bytes"] = 0
        errors.append(str(exc))
    stat, error = _read_key_values(path / "memory.stat")
    if error:
        errors.append(error)
    for key in (
        "anon", "file", "active_anon", "inactive_anon", "active_file", "inactive_file",
        "pgfault", "pgmajfault", "workingset_refault_anon", "workingset_refault_file",
        "pswpin", "pswpout", "pgscan", "pgsteal",
    ):
        values[f"{key}_bytes" if key in {"anon", "file", "active_anon", "inactive_anon", "active_file", "inactive_file"} else key] = stat.get(key, 0)
    return values, "; ".join(errors)


def counter_delta(current: int, previous: int) -> tuple[int, str]:
    """Return a conservative delta and expose a counter reset explicitly."""
    delta = int(current) - int(previous)
    if delta < 0:
        return 0, "COUNTER_RESET"
    return delta, "OK"


def memory_value(values: dict[str, Any]) -> int:
    """Use Referenced when present; PSS is the documented fall-back."""
    referenced = _int(values.get("referenced_bytes"))
    return referenced if referenced > 0 else _int(values.get("pss_bytes"))


@dataclass
class Candidate:
    app_key: str
    app_id: str
    app_name: str
    probability: float
    rank: int
    running_state: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "app_key": self.app_key, "app_id": self.app_id, "app": self.app_name,
            "probability": self.probability, "rank": self.rank,
            "running_state": self.running_state,
        }


@dataclass
class Episode:
    episode_id: str
    prediction_id: str
    prediction_batch_id: str
    generated_at_ns: int
    valid_until_ns: int
    current_app: str
    trigger_type: str
    candidates: list[Candidate]
    monitored_apps: set[str]
    terminal_time_ns: int = 0
    terminal_reason: str = ""
    actual_next_app: str = ""
    recovery_deadline_ns: int = 0


class MemoryShadowObserver:
    """Create prediction episodes and record procfs/cgroup memory snapshots.

    The monitor calls ``observe_event`` before it invokes the LSTM for an X11
    edge.  A following successful inference creates a new episode.  Therefore
    the previous batch is ended by the *next real* APP_SWITCH, never by a
    sampling-clock inference.
    """

    def __init__(
        self,
        *,
        session_id: str,
        output_dir: Path,
        runtime_scope: Any,
        sample_interval_s: float = 0.25,
        top_k: int = 3,
        recovery_window_s: float = 3.0,
    ) -> None:
        self.session_id = session_id
        self.output_dir = Path(output_dir)
        self.runtime_scope = runtime_scope
        self.interval_ns = max(50_000_000, int(sample_interval_s * 1_000_000_000))
        self.top_k = max(1, int(top_k))
        self.recovery_window_ns = max(500_000_000, int(recovery_window_s * 1_000_000_000))
        self.memory_dir = self.output_dir / "memory"
        self.prediction_dir = self.output_dir / "prediction"
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.prediction_dir.mkdir(parents=True, exist_ok=True)
        self.memory_writer = CsvWriter(self.memory_dir / "app_memory_shadow_250ms.csv", MEMORY_FIELDS)
        self.fault_writer = CsvWriter(self.memory_dir / "app_fault_deltas.csv", FAULT_FIELDS)
        self.refault_writer = CsvWriter(self.memory_dir / "app_refault_observations.csv", REFAULT_FIELDS)
        self.swap_writer = CsvWriter(self.memory_dir / "app_swap_observations.csv", SWAP_FIELDS)
        self.reclaim_writer = CsvWriter(self.memory_dir / "app_reclaim_observations.csv", RECLAIM_FIELDS)
        self.episode_writer = CsvWriter(self.prediction_dir / "prediction_episodes.csv", EPISODE_FIELDS)
        self.batches: list[dict[str, Any]] = []
        self.episodes: list[Episode] = []
        self.active: Episode | None = None
        self.recovery: list[Episode] = []
        self.previous: dict[str, dict[str, int]] = {}
        self.next_sample_ns = 0
        self.episode_serial = 0
        self.scope_by_app = dict(getattr(runtime_scope, "app_key_to_scope_name", {}) or {})
        self.app_id_by_app = dict(getattr(runtime_scope, "app_key_to_app_id", {}) or {})
        self.vocab_to_app = {
            str(value): str(key)
            for key, value in dict(getattr(runtime_scope, "app_key_to_vocab_name", {}) or {}).items()
        }
        self._slice_cgroup_path = self._resolve_slice_cgroup_path()

    def _resolve_slice_cgroup_path(self) -> Path:
        slice_name = str(getattr(self.runtime_scope, "slice_name", "") or "")
        if not slice_name:
            return Path("/sys/fs/cgroup")
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show", slice_name, "-p", "ControlGroup", "--value"],
                check=False, capture_output=True, text=True, timeout=3,
            )
            control_group = result.stdout.strip()
            if result.returncode == 0 and control_group:
                return Path("/sys/fs/cgroup") / control_group.lstrip("/")
        except (OSError, subprocess.TimeoutExpired):
            pass
        # This fall-back is read-only and only used while a scope does not
        # yet have a process from which its exact cgroup path can be learned.
        return Path("/sys/fs/cgroup") / slice_name

    def observe_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("event_type", ""))
        timestamp_ns = _int(event.get("ts_ns") or event.get("timestamp_ns") or time.time_ns())
        if self.active is None:
            return
        if event_type == "APP_SWITCH":
            next_app = str(event.get("new_app") or event.get("app") or "")
            self._end_active(timestamp_ns, "NEXT_APP_SWITCH", next_app)
        elif event_type == "APP_CLOSE" and str(event.get("app", "")) == self.active.current_app:
            self._end_active(timestamp_ns, "CURRENT_APP_CLOSE", "")

    def record_prediction(
        self,
        *,
        event: dict[str, Any],
        feature_row: dict[str, Any],
        result: dict[str, Any],
        process_samples: list[ProcessSample],
    ) -> None:
        if str(result.get("status", "")) != "success" or not result.get("inference_executed"):
            return
        prediction_id = str(result.get("prediction_id", "")).strip()
        if not prediction_id:
            return
        generated_at_ns = _int(event.get("ts_ns") or time.time_ns())
        # A new batch makes the previous still-pending prediction stale.  It
        # is not a switch outcome and is retained as such for denominator audit.
        if self.active is not None:
            self._end_active(generated_at_ns, "SUPERSEDED_BY_NEW_BATCH", "")
        candidates = self._candidates(result, str(feature_row.get("foreground_app", "")), process_samples)
        if not candidates:
            return
        self.episode_serial += 1
        batch_id = f"{prediction_id}-shadow"
        ttl_ms = _int(result.get("prediction_ttl_ms"), 180_000)
        valid_until_ns = generated_at_ns + max(1, ttl_ms) * 1_000_000
        episode = Episode(
            episode_id=f"{self.session_id}-ep{self.episode_serial:05d}",
            prediction_id=prediction_id,
            prediction_batch_id=batch_id,
            generated_at_ns=generated_at_ns,
            valid_until_ns=valid_until_ns,
            current_app=str(feature_row.get("foreground_app", "")),
            trigger_type=str(result.get("trigger_type", "")),
            candidates=candidates,
            monitored_apps={item.app_key for item in candidates[:self.top_k] if item.app_key},
        )
        self.active = episode
        self.episodes.append(episode)
        self.batches.append({
            "session_id": self.session_id, "prediction_id": prediction_id,
            "prediction_batch_id": batch_id, "generated_at_ns": generated_at_ns,
            "valid_until_ns": valid_until_ns, "trigger_type": episode.trigger_type,
            "current_app": episode.current_app,
            "current_app_id": self.app_id_by_app.get(episode.current_app, ""),
            "current_open_apps": str(feature_row.get("open_apps", "")),
            "prediction_format": str(result.get("prediction_format", "")),
            "prediction_latency_us": int(_float(result.get("predict_latency_ms")) * 1000),
            "candidate_count": len(candidates),
            "candidate_apps_json": json.dumps([item.as_dict() for item in candidates], ensure_ascii=False),
            "top1_app": candidates[0].app_key, "top3_apps": "|".join(item.app_key for item in candidates[:3]),
            "bridge_batch_id": "", "snapshot_version_start": "", "snapshot_version_end": "",
            "bridge_status": "PENDING_BRIDGE_RECONCILIATION",
        })
        self._sample_episode(episode, process_samples, generated_at_ns, "T0_PREDICTION")
        self.next_sample_ns = min(self.next_sample_ns or generated_at_ns + self.interval_ns, generated_at_ns + self.interval_ns)

    def sample_if_due(self, process_samples: list[ProcessSample], timestamp_ns: int | None = None) -> None:
        now = int(timestamp_ns or time.time_ns())
        if self.active is not None and now >= self.active.valid_until_ns:
            self._end_active(now, "TTL_EXPIRED", "")
        if self.next_sample_ns and now < self.next_sample_ns:
            return
        self.next_sample_ns = now + self.interval_ns
        if self.active is not None:
            self._sample_episode(self.active, process_samples, now, "PERIODIC")
        remaining: list[Episode] = []
        for episode in self.recovery:
            if now <= episode.recovery_deadline_ns:
                self._sample_app(
                    episode, episode.actual_next_app, process_samples, now, "T2_RECOVERY_WINDOW"
                )
                remaining.append(episode)
        self.recovery = remaining

    def finish(self, timestamp_ns: int | None = None) -> None:
        if self.active is not None:
            self._end_active(int(timestamp_ns or time.time_ns()), "EXPERIMENT_END", "")

    def _end_active(self, timestamp_ns: int, reason: str, actual_next_app: str) -> None:
        if self.active is None:
            return
        episode = self.active
        self.active = None
        episode.terminal_time_ns = timestamp_ns
        episode.terminal_reason = reason
        episode.actual_next_app = actual_next_app
        if reason == "NEXT_APP_SWITCH" and actual_next_app:
            episode.recovery_deadline_ns = timestamp_ns + self.recovery_window_ns
            self.recovery.append(episode)

    def _candidates(
        self, result: dict[str, Any], current_app: str, samples: list[ProcessSample]
    ) -> list[Candidate]:
        running = {sample.app_id for sample in samples}
        output: list[Candidate] = []
        for row in result.get("all_probabilities", []) or []:
            name = str(row.get("app", ""))
            key = str(row.get("app_key", "")) or self.vocab_to_app.get(name, "")
            if not key:
                continue
            if key == current_app:
                state = "FOREGROUND"
            elif key in running:
                state = "RUNNING_BACKGROUND"
            elif key not in self.scope_by_app:
                state = "UNBOUND"
            else:
                state = "NOT_RUNNING"
            output.append(Candidate(
                app_key=key, app_id=str(row.get("runtime_app_id") or self.app_id_by_app.get(key, "")),
                app_name=name, probability=_float(row.get("probability")), rank=_int(row.get("rank"), 9999),
                running_state=state,
            ))
        return sorted(output, key=lambda item: (item.rank, -item.probability, item.app_key))

    def _sample_episode(self, episode: Episode, samples: list[ProcessSample], now: int, reason: str) -> None:
        for app_key in sorted(episode.monitored_apps):
            self._sample_app(episode, app_key, samples, now, reason)

    def _sample_app(
        self, episode: Episode, app_key: str, samples: list[ProcessSample], now: int, reason: str
    ) -> None:
        row = self._read_app(episode, app_key, samples, now, reason)
        self.memory_writer.write_row(row)
        self._write_deltas(row)

    def _cgroup_path(self, app_key: str, app_samples: list[ProcessSample]) -> Path | None:
        if app_samples:
            raw = str(app_samples[0].identity.cgroup_path or "")
            if raw:
                return Path("/sys/fs/cgroup") / raw.lstrip("/")
        scope_name = self.scope_by_app.get(app_key, "")
        return self._slice_cgroup_path / scope_name if scope_name else None

    def _read_app(
        self, episode: Episode, app_key: str, samples: list[ProcessSample], now: int, reason: str
    ) -> dict[str, Any]:
        start = time.perf_counter_ns()
        app_samples = [sample for sample in samples if sample.app_id == app_key]
        candidate = next((item for item in episode.candidates if item.app_key == app_key), None)
        running_state = candidate.running_state if candidate else "UNKNOWN"
        if app_key == episode.actual_next_app and episode.terminal_reason == "NEXT_APP_SWITCH":
            foreground_state = "FOREGROUND"
        elif app_key == episode.current_app:
            foreground_state = "FOREGROUND"
        else:
            foreground_state = "BACKGROUND"
        totals = {"rss_bytes": 0, "pss_bytes": 0, "referenced_bytes": 0, "swap_bytes": 0}
        errors: list[str] = []
        for sample in app_samples:
            values, error = read_smaps_rollup(sample.identity.pid)
            if error:
                errors.append(f"pid={sample.identity.pid}: {error}")
                continue
            totals["rss_bytes"] += values.get("Rss", 0)
            totals["pss_bytes"] += values.get("Pss", 0)
            totals["referenced_bytes"] += values.get("Referenced", 0)
            totals["swap_bytes"] += values.get("Swap", 0)
        cgroup = self._cgroup_path(app_key, app_samples)
        cgroup_values, cgroup_error = read_cgroup_memory(cgroup) if cgroup is not None else ({}, "missing cgroup")
        if cgroup_error:
            errors.append(cgroup_error)
        values: dict[str, Any] = {
            "session_id": self.session_id, "timestamp_ns": now,
            "timestamp": dt.datetime.fromtimestamp(now / 1_000_000_000).isoformat(timespec="milliseconds"),
            "sample_reason": reason, "episode_id": episode.episode_id,
            "prediction_id": episode.prediction_id, "prediction_batch_id": episode.prediction_batch_id,
            "app_key": app_key, "app_id": self.app_id_by_app.get(app_key, ""), "memcg_path": str(cgroup or ""),
            "foreground_state": foreground_state, "running_state": running_state,
            "pid_count": len(app_samples), "pids": "|".join(str(item.identity.pid) for item in app_samples),
            **totals,
            "memcg_current_bytes": cgroup_values.get("memcg_current_bytes", 0),
            "anon_bytes": cgroup_values.get("anon_bytes", 0), "file_bytes": cgroup_values.get("file_bytes", 0),
            "active_anon_bytes": cgroup_values.get("active_anon_bytes", 0),
            "inactive_anon_bytes": cgroup_values.get("inactive_anon_bytes", 0),
            "active_file_bytes": cgroup_values.get("active_file_bytes", 0),
            "inactive_file_bytes": cgroup_values.get("inactive_file_bytes", 0),
            "pgfault": cgroup_values.get("pgfault", 0), "pgmajfault": cgroup_values.get("pgmajfault", 0),
            "workingset_refault_anon": cgroup_values.get("workingset_refault_anon", 0),
            "workingset_refault_file": cgroup_values.get("workingset_refault_file", 0),
            "pswpin": cgroup_values.get("pswpin", 0), "pswpout": cgroup_values.get("pswpout", 0),
            "pgscan": cgroup_values.get("pgscan", 0), "pgsteal": cgroup_values.get("pgsteal", 0),
            "proc_minflt": sum(_int(item.stat.get("minflt")) for item in app_samples),
            "proc_majflt": sum(_int(item.stat.get("majflt")) for item in app_samples),
            "proc_read_bytes": sum(_int(item.io.get("read_bytes")) for item in app_samples),
            "metric_status": "OK" if not errors else ("PARTIAL" if app_samples else "UNAVAILABLE"),
            "metric_error": "; ".join(errors),
            "collection_latency_us": int((time.perf_counter_ns() - start) / 1000),
        }
        return values

    def _write_deltas(self, row: dict[str, Any]) -> None:
        app_key = str(row["app_key"])
        previous = self.previous.get(app_key)
        current = {field: _int(row.get(field)) for field in COUNTER_FIELDS}
        current["swap_bytes"] = _int(row.get("swap_bytes"))
        self.previous[app_key] = current
        if previous is None:
            return
        deltas: dict[str, int] = {}
        statuses: list[str] = []
        for field in COUNTER_FIELDS:
            deltas[field], status = counter_delta(current[field], previous[field])
            statuses.append(status)
        status = "COUNTER_RESET" if "COUNTER_RESET" in statuses else "OK"
        common = {"session_id": self.session_id, "timestamp_ns": row["timestamp_ns"],
                  "episode_id": row["episode_id"], "prediction_id": row["prediction_id"], "app_key": app_key,
                  "status": status, "reason": row["sample_reason"]}
        self.fault_writer.write_row({**common, "pgfault_delta": deltas["pgfault"],
                                     "pgmajfault_delta": deltas["pgmajfault"],
                                     "proc_minflt_delta": deltas["proc_minflt"],
                                     "proc_majflt_delta": deltas["proc_majflt"]})
        self.refault_writer.write_row({**common, "workingset_refault_anon_delta": deltas["workingset_refault_anon"],
                                       "workingset_refault_file_delta": deltas["workingset_refault_file"]})
        self.swap_writer.write_row({**common, "swap_bytes_delta": _int(row.get("swap_bytes")) - _int(previous.get("swap_bytes")),
                                    "pswpin_delta": deltas["pswpin"], "pswpout_delta": deltas["pswpout"]})
        # These are cgroup accounting counters, not per-folio trace events.
        self.reclaim_writer.write_row({**common, "pgscan_delta": deltas["pgscan"], "pgsteal_delta": deltas["pgsteal"],
                                       "evidence_scope": "CGROUP_COUNTER_ASSOCIATED"})

    def close(self, bridge_audit_path: Path | None = None) -> None:
        self.finish()
        for episode in self.episodes:
            self.episode_writer.write_row({
                "session_id": self.session_id, "episode_id": episode.episode_id,
                "prediction_id": episode.prediction_id, "prediction_batch_id": episode.prediction_batch_id,
                "generated_at_ns": episode.generated_at_ns, "valid_until_ns": episode.valid_until_ns,
                "current_app": episode.current_app, "trigger_type": episode.trigger_type,
                "candidate_apps_json": json.dumps([item.as_dict() for item in episode.candidates], ensure_ascii=False),
                "monitored_apps": "|".join(sorted(episode.monitored_apps)),
                "terminal_time_ns": episode.terminal_time_ns, "terminal_reason": episode.terminal_reason,
                "actual_next_app": episode.actual_next_app,
                "time_to_terminal_ms": max(0, (episode.terminal_time_ns - episode.generated_at_ns) // 1_000_000) if episode.terminal_time_ns else "",
                "recovery_deadline_ns": episode.recovery_deadline_ns,
            })
        self.episode_writer.close()
        self.memory_writer.close(); self.fault_writer.close(); self.refault_writer.close(); self.swap_writer.close(); self.reclaim_writer.close()
        self._write_batches(bridge_audit_path)

    def _write_batches(self, bridge_audit_path: Path | None) -> None:
        bridge: dict[str, list[dict[str, str]]] = {}
        if bridge_audit_path and bridge_audit_path.is_file():
            with bridge_audit_path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    prediction_id = str(row.get("prediction_id", ""))
                    if prediction_id:
                        bridge.setdefault(prediction_id, []).append(row)
        writer = CsvWriter(self.prediction_dir / "prediction_batches.csv", BATCH_FIELDS)
        for row in self.batches:
            rows = bridge.get(str(row["prediction_id"]), [])
            priors = [item for item in rows if item.get("event_type") == "app_prior"]
            successes = [item for item in priors if item.get("write_success") == "true"]
            if priors:
                row = dict(row)
                row["bridge_batch_id"] = priors[0].get("batch_id", "")
                row["snapshot_version_start"] = priors[0].get("snapshot_generation_before", "")
                row["snapshot_version_end"] = priors[-1].get("snapshot_generation", "")
                row["bridge_status"] = "WRITE_SUCCESS" if successes and len(successes) == len(priors) else "BRIDGE_AUDIT_PRESENT"
            writer.write_row(row)
        writer.close()
