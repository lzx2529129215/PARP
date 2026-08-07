"""Strictly causal online second-order workload Markov chain."""

from __future__ import annotations

import csv
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MGLRU_MARKOV_TOPK = 4

PREDICTION_FIELDS = [
    "session_id", "prediction_id", "timestamp_ns", "app_key", "app_id",
    "scope_name", "context_prev_workload_id", "context_current_workload_id",
    "predicted_next_workload_id", "rank", "confidence", "confidence_fixed",
    "transition_source", "latest_training_sample_time_ns", "prediction_time_ns",
    "actual_next_workload_id", "actual_next_time_ns", "resolution_status",
    "used_future_information", "causal_valid", "hit",
]

UPDATE_FIELDS = [
    "session_id", "timestamp_ns", "app_key", "app_id", "scope_name",
    "prev_workload_id", "current_workload_id", "observed_next_workload_id",
    "count_before", "count_after", "total_before", "total_after",
    "confidence_after", "debugfs_write_status",
]

TRANSITION_FIELDS = [
    "session_id", "timestamp_ns", "app_key", "app_id", "scope_name",
    "prev_workload_id", "current_workload_id", "next_workload_id", "count",
    "total_count", "confidence", "boost_level", "rank",
]


def _fixed_confidence(value: float) -> int:
    return max(0, min(10000, int(round(float(value) * 10000))))


def _boost_level(confidence_fixed: int) -> int:
    if confidence_fixed >= 8000:
        return 3
    if confidence_fixed >= 5000:
        return 2
    if confidence_fixed > 0:
        return 1
    return 0


@dataclass
class OnlineMarkovResult:
    total_updates: int
    total_predictions: int
    online_predictions_resolved: int
    causal_valid_predictions: int
    prediction_hits: int
    prediction_misses: int
    unresolved_predictions: int
    future_information_rows: int
    debugfs_workload_update_ok: int
    debugfs_markov_set_ok: int
    final_result: str


class OnlineCausalWorkloadMarkov:
    """Maintain independent causal windows and transitions per app/scope.

    For an observed ``w[t]`` the order is fixed: resolve the pending
    prediction for this key, update ``(w[t-2], w[t-1]) -> w[t]``, advance the
    window, then predict ``w[t+1]``.  Prediction rows are kept by unique id and
    rewritten in place, so a prediction is never duplicated as pending/resolved.
    """

    def __init__(self, *, enabled: bool = True, session_id: str = "",
                 model_dir: Path | str = Path("."), review_dir: Path | str = Path("."),
                 debugfs_writer: Any | None = None, top_k: int = MGLRU_MARKOV_TOPK,
                 preload_transitions_csv: str | None = None) -> None:
        self.enabled = bool(enabled)
        self.session_id = session_id
        self.model_dir = Path(model_dir)
        self.review_dir = Path(review_dir)
        self.debugfs_writer = debugfs_writer
        self.top_k = int(top_k)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self._windows: dict[tuple[str, str], list[int]] = {}
        self._last_sample_ns: dict[tuple[str, str], int] = {}
        self._transitions: dict[tuple[str, str, int, int], Counter[int]] = defaultdict(Counter)
        self._preloaded_keys: set[tuple[str, str, int, int]] = set()
        self._predictions: dict[str, dict[str, Any]] = {}
        self._pending: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._prediction_seq = 0
        self.total_updates = 0
        self.total_predictions = 0
        self.future_information_rows = 0
        self.debugfs_workload_update_ok = 0
        self.debugfs_markov_set_ok = 0
        self._app_metadata: dict[tuple[str, str], dict[str, str]] = {}
        self._pred_path = self.model_dir / "workload_markov_online_predictions.csv"
        self._upd_path = self.model_dir / "workload_markov_online_updates.csv"
        self._trans_path = self.model_dir / "workload_markov_online_transitions.csv"
        self._upd_file = self._open_csv(self._upd_path, UPDATE_FIELDS)
        if preload_transitions_csv:
            self._preload_transitions(preload_transitions_csv)

    @staticmethod
    def _open_csv(path: Path, fields: list[str]) -> tuple[Any, csv.DictWriter]:
        f = path.open("w", encoding="utf-8", newline="")
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        f.flush()
        return f, w

    def _preload_transitions(self, path_str: str) -> None:
        path = Path(path_str)
        if not path.exists():
            return
        with path.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    key = (str(row["app_id"]), str(row.get("scope_name", "")),
                           int(row["prev_workload_id"]), int(row["current_workload_id"]))
                    self._transitions[key][int(row["next_workload_id"])] += int(row.get("count", 1))
                    self._preloaded_keys.add(key)
                except (KeyError, TypeError, ValueError):
                    continue

    def observe_workload(self, *, app_key: str, app_id: str, scope_name: str,
                         workload_id: int, cgroup_id: int | None = None,
                         workload_name: str = "", timestamp_ns: int | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        ts = int(timestamp_ns if timestamp_ns is not None else time.time_ns())
        key = (str(app_id), str(scope_name))
        self._app_metadata[key] = {"app_key": app_key, "app_id": str(app_id), "scope_name": scope_name}

        # A. Resolve only this app/scope's previous prediction first.
        self._resolve_pending(key, int(workload_id), ts)

        # B. Update the transition with the newly observed value.
        window = self._windows.get(key, [])
        if len(window) >= 2:
            prev, current = window[-2], window[-1]
            tkey = (str(app_id), scope_name, prev, current)
            counter = self._transitions[tkey]
            before = counter[int(workload_id)]
            total_before = sum(counter.values())
            counter[int(workload_id)] += 1
            total_after = total_before + 1
            self.total_updates += 1
            self._upd_file[1].writerow({
                "session_id": self.session_id, "timestamp_ns": ts, "app_key": app_key,
                "app_id": app_id, "scope_name": scope_name, "prev_workload_id": prev,
                "current_workload_id": current, "observed_next_workload_id": workload_id,
                "count_before": before, "count_after": counter[int(workload_id)],
                "total_before": total_before, "total_after": total_after,
                "confidence_after": counter[int(workload_id)] / total_after,
                "debugfs_write_status": "pending",
            })
            self._upd_file[0].flush()
            if self.debugfs_writer is not None:
                status, _ = self.debugfs_writer.write_markov_set(
                    app_id=int(app_id), prev_workload_id=prev,
                    current_workload_id=current,
                    entries=[{"next_workload_id": n, "confidence": _fixed_confidence(c / total_after),
                              "boost_level": _boost_level(_fixed_confidence(c / total_after))}
                             for n, c in self._ranked(counter)], app_key=app_key)
                if status == "ok":
                    self.debugfs_markov_set_ok += 1

        # C. Advance context, then predict the future next state.
        self._windows[key] = (window + [int(workload_id)])[-2:]
        self._last_sample_ns[key] = ts
        prediction = self._predict(key, ts, app_key)
        if prediction is not None:
            self._pending[key].append(prediction["prediction_id"])

        # The kernel looks up the transition for the app's *current* context
        # after workload update.  Publish that context before changing the
        # kernel's current workload, using only samples already observed by
        # this call.  This is still online incremental sync, not batch replay.
        self._write_current_context_transition(key, app_key)

        # Workload update is tied to this observed state and is written online.
        if self.debugfs_writer is not None and cgroup_id is not None:
            status, _ = self.debugfs_writer.write_workload_update(
                cgroup_id=cgroup_id, app_id=int(app_id), workload_id=int(workload_id),
                app_key=app_key, workload_name=workload_name)
            if status == "ok":
                self.debugfs_workload_update_ok += 1
        return prediction

    def _write_current_context_transition(self, key: tuple[str, str], app_key: str) -> None:
        if self.debugfs_writer is None:
            return
        window = self._windows.get(key, [])
        if len(window) < 2:
            return
        prev, current = window
        counter = self._transitions[(key[0], key[1], prev, current)]
        if not counter:
            return
        total = sum(counter.values())
        status, _ = self.debugfs_writer.write_markov_set(
            app_id=int(key[0]), prev_workload_id=prev,
            current_workload_id=current,
            entries=[{"next_workload_id": n,
                      "confidence": _fixed_confidence(c / total),
                      "boost_level": _boost_level(_fixed_confidence(c / total))}
                     for n, c in self._ranked(counter)],
            app_key=app_key,
        )
        if status == "ok":
            self.debugfs_markov_set_ok += 1

    def _predict(self, key: tuple[str, str], ts: int, app_key: str) -> dict[str, Any] | None:
        window = self._windows.get(key, [])
        if len(window) < 2:
            return None
        prev, current = window
        ranked = self._ranked(self._transitions[(key[0], key[1], prev, current)])
        if not ranked:
            return None
        self._prediction_seq += 1
        prediction_id = f"{self.session_id}:p{self._prediction_seq:06d}"
        next_id, count = ranked[0]
        total = sum(self._transitions[(key[0], key[1], prev, current)].values())
        row = {
            "session_id": self.session_id, "prediction_id": prediction_id,
            "timestamp_ns": ts, "app_key": app_key, "app_id": key[0], "scope_name": key[1],
            "context_prev_workload_id": prev, "context_current_workload_id": current,
            "predicted_next_workload_id": next_id, "rank": 1, "confidence": count / total,
            "confidence_fixed": _fixed_confidence(count / total),
            "transition_source": "historical_preload" if (key[0], key[1], prev, current) in self._preloaded_keys else "online_incremental",
            "latest_training_sample_time_ns": self._last_sample_ns.get(key, 0),
            "prediction_time_ns": ts, "actual_next_workload_id": "", "actual_next_time_ns": "",
            "resolution_status": "PENDING", "used_future_information": "false",
            "causal_valid": "", "hit": "",
        }
        self._predictions[prediction_id] = row
        self.total_predictions += 1
        self._rewrite_predictions()
        return row

    @staticmethod
    def _ranked(counter: Counter[int]) -> list[tuple[int, int]]:
        return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:MGLRU_MARKOV_TOPK]

    def _get_topk_predictions(self, app_id: str, scope_name: str,
                              prev_wl: int, cur_wl: int) -> list[tuple[int, float]]:
        counter = self._transitions[(str(app_id), scope_name, int(prev_wl), int(cur_wl))]
        total = sum(counter.values())
        return [(next_id, count / total) for next_id, count in self._ranked(counter)] if total else []

    def _resolve_pending(self, key: tuple[str, str], actual: int, actual_ts: int) -> None:
        for prediction_id in self._pending.pop(key, []):
            row = self._predictions[prediction_id]
            prediction_ts = int(row["prediction_time_ns"])
            training_ts = int(row["latest_training_sample_time_ns"])
            future = training_ts > prediction_ts
            causal = training_ts <= prediction_ts < actual_ts and not future
            if future:
                self.future_information_rows += 1
            row.update({"actual_next_workload_id": actual, "actual_next_time_ns": actual_ts,
                        "used_future_information": str(future).lower(),
                        "resolution_status": "RESOLVED", "causal_valid": str(causal).lower(),
                        "hit": str(int(row["predicted_next_workload_id"]) == actual).lower()})
        self._rewrite_predictions()

    def _rewrite_predictions(self) -> None:
        tmp = self._pred_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=PREDICTION_FIELDS)
            w.writeheader()
            for row in self._predictions.values():
                w.writerow({field: row.get(field, "") for field in PREDICTION_FIELDS})
        tmp.replace(self._pred_path)

    def close(self) -> None:
        for row in self._predictions.values():
            if row["resolution_status"] == "PENDING":
                row.update({"resolution_status": "UNRESOLVED", "used_future_information": "false",
                            "causal_valid": "false", "hit": ""})
        self._rewrite_predictions()
        with self._trans_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRANSITION_FIELDS)
            w.writeheader()
            for (app_id, scope, prev, current), counter in sorted(self._transitions.items()):
                total = sum(counter.values())
                meta = self._app_metadata.get((app_id, scope), {})
                for rank, (next_id, count) in enumerate(self._ranked(counter), 1):
                    conf = count / total
                    w.writerow({"session_id": self.session_id, "timestamp_ns": self._last_sample_ns.get((app_id, scope), ""),
                                "app_key": meta.get("app_key", ""), "app_id": app_id, "scope_name": scope,
                                "prev_workload_id": prev, "current_workload_id": current, "next_workload_id": next_id,
                                "count": count, "total_count": total, "confidence": conf,
                                "boost_level": _boost_level(_fixed_confidence(conf)), "rank": rank})
        audit_path = self.model_dir / "markov_live_causality_audit.csv"
        audit_fields = ["session_id", "prediction_id", "prediction_time_ns", "actual_next_time_ns",
                        "latest_training_sample_time_ns", "resolution_status", "used_future_information",
                        "causal_valid", "hit"]
        with audit_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=audit_fields)
            writer.writeheader()
            for row in self._predictions.values():
                writer.writerow({field: row.get(field, "") for field in audit_fields})
        self._upd_file[0].close()
        if self.debugfs_writer is not None:
            self.debugfs_writer.close()

    def result(self) -> OnlineMarkovResult:
        rows = list(self._predictions.values())
        resolved = [r for r in rows if r["resolution_status"] == "RESOLVED"]
        valid = [r for r in resolved if r["causal_valid"] == "true"]
        hits = [r for r in valid if r["hit"] == "true"]
        misses = [r for r in valid if r["hit"] == "false"]
        unresolved = [r for r in rows if r["resolution_status"] == "UNRESOLVED"]
        return OnlineMarkovResult(self.total_updates, len(rows), len(resolved), len(valid), len(hits), len(misses),
                                  len(unresolved), self.future_information_rows,
                                  self.debugfs_workload_update_ok, self.debugfs_markov_set_ok,
                                  "PASS" if self.future_information_rows == 0 else "FAIL")
