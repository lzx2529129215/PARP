"""Explicit, output-audited p180 projection into the existing PARP bin ABI.

The model's two probabilities remain unchanged. The scalar ABI transports p180;
this module does not disguise it as the legacy next-event probability.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lzx/service/runtime_monitor"))
from core.parp_myfs import PARPMyfsBridge, Q15_ONE, ENTRY_FOREGROUND, MAX_APPS

MODEL_VERSION = 501
COLD_Q15 = 6553


def project(bundle, scope, foreground, opened, now=None):
    if bundle.get("prediction_format") != "visit_window" or bundle.get("status") != "success":
        raise ValueError("requires a successful visit_window prediction")
    by_key = {a.app_key: a for a in scope.apps if a.prediction_enabled}
    opened = set(opened)
    if not opened or foreground not in opened or not opened <= by_key.keys():
        raise ValueError("unknown foreground or incomplete running-app mapping")
    if len(opened) > MAX_APPS:
        raise ValueError("too many running apps for atomic ABI")
    by_name = {a.vocab_name: a for a in by_key.values()}
    seen, entries, audit, remaining = set(), [], [], []
    for row in bundle.get("all_probabilities", []):
        app = by_name.get(row.get("app"))
        if app is None or app.app_key not in opened:
            continue
        if app.app_key in seen:
            raise ValueError("duplicate application prediction")
        seen.add(app.app_key)
        p30, p180 = float(row["p_visit_30s"]), float(row["p_visit_180s"])
        if not (math.isfinite(p30) and math.isfinite(p180) and 0 <= p30 <= p180 <= 1):
            raise ValueError("invalid nested probabilities")
        predicted = dt.datetime.fromisoformat(row["predicted_at"])
        expires = dt.datetime.fromisoformat(row["expires_at"])
        when = now if now is not None else dt.datetime.now(tz=predicted.tzinfo)
        if not predicted <= when < expires or not 0 < (expires - predicted).total_seconds() <= 30:
            raise ValueError("expired, future, or invalid prediction lifetime")
        if row.get("probability_source", "unavailable") == "unavailable":
            raise ValueError("unavailable prediction")
        remaining.append((expires - when).total_seconds())
        fg = app.app_key == foreground
        # Ceil avoids making p180 >= .20 cold at the Q15 boundary. At most one
        # quantization step of genuinely cold probabilities is conservative.
        score = Q15_ONE if fg else math.ceil(p180 * Q15_ONE)
        entries.append((int(app.app_id), score, ENTRY_FOREGROUND if fg else 0))
        audit.append({"app_key": app.app_key, "runtime_app_id": int(app.app_id),
                      "p_visit_30s": p30, "p_visit_180s": p180,
                      "thermal_state": "foreground" if fg else "hot" if p30 >= .9 else "cold" if p180 < .2 else "neutral",
                      "kernel_score_q15": score, "kernel_score_source": "p_visit_180s",
                      "kernel_probability_cold": not fg and score <= COLD_Q15,
                      "foreground_override": fg})
    if seen != opened:
        raise ValueError("missing running-app probabilities; refusing partial state")
    if len({e[0] for e in entries}) != len(entries) or any(e[0] <= 0 for e in entries):
        raise ValueError("invalid or duplicate runtime app IDs")
    entries.sort(key=lambda e: (-bool(e[2]), -e[1], e[0]))
    ttl_ms = math.floor(min(remaining) * 1000)
    if ttl_ms < 1:
        raise ValueError("prediction expires before it can be submitted")
    return [(aid, score, rank, flags) for rank, (aid, score, flags) in enumerate(entries, 1)], audit, ttl_ms


class VisitWindowBinBridge(PARPMyfsBridge):
    def __init__(self, *, policy_root, **kwargs):
        self.policy_root = Path(policy_root).resolve()
        self._visit_entries = []
        self._visit_current = ""
        super().__init__(model_version=MODEL_VERSION, horizon_ms=180000,
                         prior_ttl_ms=30000, **kwargs)
        self.projection_log = self.parp_dir / "visit_bin_projection.jsonl"

    def submit_prediction(self, feature_row, prediction_result, *, process_samples=(), event=None):
        if not prediction_result.get("inference_executed"):
            return  # Never renew a cached prediction's TTL.
        entries, audit, ttl = project(prediction_result, self.runtime_scope,
            feature_row["foreground_app"], feature_row["open_apps"].split("|"))
        self._visit_entries, self._visit_current = entries, feature_row["foreground_app"]
        self.prior_ttl_ms = ttl
        self._deadline_ns = time.monotonic_ns() + ttl * 1000000
        samples = list(process_samples)
        # Resolve twice (validation and submission) so migration outside the
        # controlled memory boundary is caught at the actual submission too.
        self._bindings(samples, {e[0] for e in entries})
        with self.projection_log.open("a") as f:
            f.write(json.dumps({"prediction_id": prediction_result.get("prediction_id"),
                "projection": "visit_window_p180_bin_v1", "model_version": MODEL_VERSION,
                "horizon_ms": 180000, "ttl_ms": ttl, "rows": audit}) + "\n")
        super().submit_prediction(feature_row,
            {**prediction_result, "prediction_format": "visit_window_p180_bin_v1"},
            process_samples=samples, event=event)

    def _prediction_entries(self, feature_row, prediction_result):
        return self._visit_entries, self._visit_current

    def _fill_state(self, state, entries, bindings, event, now_ns, workload_profiles=None):
        super()._fill_state(state, entries, bindings, event, now_ns, workload_profiles)
        if now_ns >= self._deadline_ns:
            raise ValueError("prediction expired while resolving cgroups")
        state.ttl_ns = self._deadline_ns - now_ns

    def _bindings(self, samples, allowed_app_ids):
        result, apps, ambiguous = super()._bindings(samples, allowed_app_ids)
        if ambiguous or {aid for _, aid in result} != allowed_app_ids:
            raise ValueError("missing or ambiguous live cgroup bindings")
        for _, _, path in self._last_binding_paths.values():
            if not path.resolve().is_relative_to(self.policy_root) or path.resolve() == self.policy_root:
                raise ValueError("application migrated outside controlled policy subtree")
        return result, apps, ambiguous
