"""Conservative near-horizon working-set prediction for PARP Tier2.

The application LSTM predicts *which* application is likely to be used next.
This module combines that probability with cgroup-v2 resident/active memory
observations to predict how much of the next working set is already resident
and how much new allocation it may need.  It never writes cgroup controls;
the result is submitted atomically through ``/dev/myfs``.  lzx-note
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


Q15_ONE = 32767
ESTIMATOR_VERSION = 1
MIN_SAMPLE_COUNT = 4
FIELDS = [
    "timestamp_ns", "prediction_id", "valid", "policy_domain_id",
    "predicted_workingset_bytes", "predicted_resident_bytes",
    "predicted_growth_bytes", "confidence_q15", "action_hint",
    "policy_limit_bytes", "binding_domains", "known_candidate_weight_q15",
    "total_candidate_weight_q15", "top3_weight_q15", "apps_json", "reason",
]


@dataclass
class AppWorkingSet:
    app_id: int
    app_key: str
    samples: int = 0
    observed_bytes: int = 0
    resident_bytes: int = 0
    ema_bytes: int = 0
    decaying_peak_bytes: int = 0
    timestamp_ns: int = 0

    @property
    def estimate_bytes(self) -> int:
        return max(self.observed_bytes, self.ema_bytes, self.decaying_peak_bytes)

    @property
    def maturity_q15(self) -> int:
        return min(Q15_ONE, self.samples * Q15_ONE // MIN_SAMPLE_COUNT)


@dataclass(frozen=True)
class WorkingSetPrediction:
    valid: bool = False
    policy_domain_id: int = 0
    predicted_workingset_bytes: int = 0
    predicted_resident_bytes: int = 0
    confidence_q15: int = 0
    estimator_version: int = ESTIMATOR_VERSION
    action_hint: str = "FALLBACK"
    reason: str = ""

    @property
    def predicted_growth_bytes(self) -> int:
        return max(0, self.predicted_workingset_bytes - self.predicted_resident_bytes)


def _read_int(path: Path) -> int:
    try:
        return max(0, int(path.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return 0


def _read_memory_stat(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = (path / "memory.stat").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return values
    for line in lines:
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            values[parts[0]] = max(0, int(parts[1]))
        except ValueError:
            continue
    return values


class WorkingSetPredictor:
    """Maintain bounded online WSS estimates and produce one aggregate batch."""

    def __init__(self, *, output_dir: Path) -> None:
        self.states: dict[int, AppWorkingSet] = {}
        self.live_app_ids: set[int] = set()
        self.policy_path: Path | None = None
        self.binding_domains = 0
        output_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = output_dir / "workingset_predictions.csv"
        self._file = self.audit_path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDS)
        self._writer.writeheader()
        self._file.flush()

    def observe(
        self,
        bindings: dict[int, tuple[int, str, Path]],
        timestamp_ns: int,
    ) -> None:
        """Aggregate GUI and fixture domains that share the same App ID."""
        totals: dict[int, dict[str, Any]] = {}
        policy_paths: set[Path] = set()
        for _domain_id, (app_id, app_key, path) in bindings.items():
            stat = _read_memory_stat(path)
            if not stat and not (path / "memory.current").exists():
                continue
            anon = stat.get("anon", 0)
            file_bytes = stat.get("file", 0)
            active_file = min(file_bytes, stat.get("active_file", 0))
            inactive_file = min(
                max(0, file_bytes - active_file), stat.get("inactive_file", 0)
            )
            # Count all anon and active file pages.  At most one eighth of the
            # inactive file cache is retained as an uncertainty allowance;
            # the decaying peak below preserves a previously active file set.
            inactive_allowance = min(inactive_file, file_bytes // 8)
            resident = anon + file_bytes
            if resident <= 0:
                resident = _read_int(path / "memory.current")
            observed = min(resident, anon + active_file + inactive_allowance)
            item = totals.setdefault(
                app_id,
                {"app_key": app_key, "observed": 0, "resident": 0},
            )
            item["observed"] += observed
            item["resident"] += resident
            policy = self._enabled_policy_ancestor(path)
            if policy is not None:
                policy_paths.add(policy)

        self.live_app_ids = set(totals)
        self.binding_domains = len(bindings)
        self.policy_path = next(iter(policy_paths)) if len(policy_paths) == 1 else None
        for app_id, values in totals.items():
            observed = int(values["observed"])
            state = self.states.get(app_id)
            if state is None:
                state = AppWorkingSet(app_id=app_id, app_key=str(values["app_key"]))
                self.states[app_id] = state
            state.samples += 1
            state.observed_bytes = observed
            state.resident_bytes = int(values["resident"])
            state.ema_bytes = observed if state.ema_bytes <= 0 else (
                state.ema_bytes * 7 + observed
            ) // 8
            decayed = state.decaying_peak_bytes * 31 // 32
            state.decaying_peak_bytes = max(observed, decayed)
            state.timestamp_ns = int(timestamp_ns)

    def predict(
        self,
        entries: Iterable[tuple[int, int, int, int]],
        *,
        prediction_id: str,
        timestamp_ns: int,
        foreground_flag: int,
    ) -> WorkingSetPrediction:
        rows = list(entries)
        policy_domain_id = 0
        if self.policy_path is not None:
            try:
                policy_domain_id = int(self.policy_path.stat().st_ino)
            except OSError:
                policy_domain_id = 0

        future = 0
        resident = 0
        candidate_total = 0
        candidate_known = 0
        maturity_weighted = 0
        top3_weight = 0
        app_rows: list[dict[str, Any]] = []
        for app_id, score_q15, rank, flags in rows:
            state = self.states.get(int(app_id))
            is_foreground = bool(flags & foreground_flag)
            if is_foreground:
                weight = Q15_ONE
            else:
                weight = max(0, min(Q15_ONE, int(score_q15)))
                candidate_total += weight
                if rank <= 4:  # foreground occupies rank 1 in the kernel batch.
                    top3_weight += weight
            estimate = state.estimate_bytes if state is not None else 0
            live_resident = (
                min(estimate, state.resident_bytes)
                if state is not None and app_id in self.live_app_ids
                else 0
            )
            if state is not None:
                if not is_foreground:
                    candidate_known += weight
                    maturity_weighted += weight * state.maturity_q15
                future += estimate if is_foreground else estimate * weight // Q15_ONE
                resident += (
                    live_resident
                    if is_foreground
                    else live_resident * weight // Q15_ONE
                )
            app_rows.append({
                "app_id": app_id,
                "app_key": state.app_key if state is not None else "",
                "rank": rank,
                "score_q15": score_q15,
                "foreground": is_foreground,
                "estimate_bytes": estimate,
                "resident_bytes": live_resident,
                "samples": state.samples if state is not None else 0,
            })

        reason = ""
        confidence = 0
        if candidate_total > 0 and candidate_known > 0:
            coverage = min(Q15_ONE, candidate_known * Q15_ONE // candidate_total)
            maturity = min(
                Q15_ONE, maturity_weighted // max(1, candidate_known)
            )
            concentration = min(Q15_ONE, top3_weight)
            confidence = coverage * maturity // Q15_ONE
            confidence = confidence * concentration // Q15_ONE
        if not policy_domain_id:
            reason = "NO_UNIQUE_ENABLED_TIER2_POLICY_DOMAIN"
        elif future <= 0:
            reason = "NO_WORKINGSET_OBSERVATION"
        elif candidate_total <= 0 or candidate_known <= 0:
            reason = "NO_KNOWN_FUTURE_CANDIDATE"
        elif confidence <= 0:
            reason = "ZERO_CONFIDENCE"

        valid = not reason
        action, limit = self._action_hint(future, min(future, resident), confidence)
        result = WorkingSetPrediction(
            valid=valid,
            policy_domain_id=policy_domain_id if valid else 0,
            predicted_workingset_bytes=future if valid else 0,
            predicted_resident_bytes=min(future, resident) if valid else 0,
            confidence_q15=confidence if valid else 0,
            action_hint=action if valid else "FALLBACK",
            reason=reason,
        )
        self._writer.writerow({
            "timestamp_ns": int(timestamp_ns),
            "prediction_id": prediction_id,
            "valid": str(result.valid).lower(),
            "policy_domain_id": result.policy_domain_id,
            "predicted_workingset_bytes": result.predicted_workingset_bytes,
            "predicted_resident_bytes": result.predicted_resident_bytes,
            "predicted_growth_bytes": result.predicted_growth_bytes,
            "confidence_q15": result.confidence_q15,
            "action_hint": result.action_hint,
            "policy_limit_bytes": limit,
            "binding_domains": self.binding_domains,
            "known_candidate_weight_q15": candidate_known,
            "total_candidate_weight_q15": candidate_total,
            "top3_weight_q15": top3_weight,
            "apps_json": json.dumps(app_rows, ensure_ascii=False),
            "reason": result.reason,
        })
        self._file.flush()
        return result

    def _action_hint(self, future: int, resident: int, confidence: int) -> tuple[str, int]:
        if self.policy_path is None:
            return "FALLBACK", 0
        try:
            raw = (self.policy_path / "memory.max").read_text(
                encoding="utf-8", errors="replace"
            ).strip()
        except OSError:
            return "FALLBACK", 0
        try:
            limit = int(raw)
        except ValueError:
            return "FALLBACK", 0
        alloc = max(4096, limit // 100)
        demote = max(alloc, limit * 3 // 100)
        growth = max(0, future - resident)
        if confidence < (Q15_ONE + 1) // 2:
            return "FALLBACK", limit
        if future > max(0, limit - demote) or growth > demote:
            return "STRENGTHEN", limit
        if growth <= alloc:
            return "RELAX", limit
        return "NORMAL", limit

    @staticmethod
    def _enabled_policy_ancestor(path: Path) -> Path | None:
        for candidate in (path, *path.parents):
            control = candidate / "memory.tier2_enabled"
            try:
                if control.read_text(encoding="utf-8").strip() == "1":
                    return candidate
            except OSError:
                continue
        return None

    def close(self) -> None:
        self._file.close()
