"""Pure inference adapter for per-app 30s/180s entry probabilities."""
from __future__ import annotations

import datetime as dt
import json
import math
import sys
from pathlib import Path

from layout import OPERATION_PREDICTOR_ROOT

if str(OPERATION_PREDICTOR_ROOT) not in sys.path:
    sys.path.insert(0, str(OPERATION_PREDICTOR_ROOT))


def thermal_state(p30, p180, *, foreground=False, running=True, available=True,
                  hot_threshold=.90, cold_threshold=.20):
    if foreground:
        return "foreground"
    if not running:
        return "not_running"
    if not available or p30 is None or p180 is None:
        return "unavailable"
    if not (math.isfinite(p30) and math.isfinite(p180) and 0 <= p30 <= p180 <= 1):
        return "unavailable"
    if p30 >= hot_threshold:
        return "hot"
    if p180 < cold_threshold:
        return "cold"
    return "neutral"


def classify_rows(rows, current_app, opened_apps, timestamp, hot_threshold=.90, cold_threshold=.20):
    if not 0 <= cold_threshold < hot_threshold <= 1:
        raise ValueError("thresholds must satisfy 0 <= cold < hot <= 1")
    when = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    output = []
    for row in rows:
        item = dict(row)
        expires = item.get("expires_at")
        valid = bool(expires and when < dt.datetime.fromisoformat(expires))
        valid = valid and item.get("probability_source", "unavailable") != "unavailable"
        item["thermal_state"] = thermal_state(item.get("p_visit_30s"), item.get("p_visit_180s"),
            foreground=item["app"] == current_app, running=item["app"] in opened_apps,
            available=valid, hot_threshold=hot_threshold, cold_threshold=cold_threshold)
        item["prediction_available"] = valid
        output.append(item)
    lists = {f"{state}_apps": [r["app"] for r in output if r["thermal_state"] == state]
             for state in ("hot", "cold", "neutral")}
    return output, lists


class OnlineVisitWindowPredictor:
    def __init__(self, checkpoint, app_vocab, group_vocab, user_group="通用用户",
                 device_name="auto", hot_threshold=.90, cold_threshold=.20):
        import torch
        from v3.models.app_lstm_visit_window import (AppLSTMVisitWindow, MODEL_TYPE,
            FORMAT, HORIZONS, VISIT_DEFINITION, encode_features, tensor_features, visit_probabilities)
        self.torch = torch
        self.device = torch.device("cuda" if device_name == "auto" and torch.cuda.is_available() else
                                   "cpu" if device_name == "auto" else device_name)
        self.app_vocab = json.loads(Path(app_vocab).read_text())
        self.group_vocab = json.loads(Path(group_vocab).read_text())
        ckpt = torch.load(checkpoint, map_location=self.device, weights_only=False)
        if (ckpt.get("model_type") != MODEL_TYPE or ckpt.get("schema_version") != 1
                or ckpt.get("prediction_format") != FORMAT or ckpt.get("horizons_s") != list(HORIZONS)
                or ckpt.get("visit_definition") != VISIT_DEFINITION
                or ckpt.get("probability_parameterization") != "p30_plus_survival30_times_sigmoid_z2"):
            raise ValueError("checkpoint is not a supported nested visit-window model")
        if ckpt.get("app_vocab") != self.app_vocab or ckpt.get("group_vocab") != self.group_vocab:
            raise ValueError("checkpoint vocabulary mapping mismatch")
        if user_group not in self.group_vocab:
            raise ValueError("unknown user group")
        if not 0 <= cold_threshold < hot_threshold <= 1:
            raise ValueError("thresholds must satisfy 0 <= cold < hot <= 1")
        self.history_len = int(ckpt["history_len"])
        self.duration_cap_s = float(ckpt["model_args"].get("duration_cap_s", 600.))
        self.user_group = user_group
        self.hot_threshold, self.cold_threshold = hot_threshold, cold_threshold
        self.encode_features, self.tensor_features = encode_features, tensor_features
        self.visit_probabilities = visit_probabilities
        self.model = AppLSTMVisitWindow(**ckpt["model_args"]).to(self.device).eval()
        self.model.load_state_dict(ckpt["model_state_dict"])

    def encode_inputs(self, history_apps, history_durations, history_mask, opened_apps, current_app, timestamp):
        values = self.encode_features(history_apps, history_durations, history_mask, opened_apps,
            current_app, timestamp, self.user_group, self.app_vocab, self.group_vocab, self.history_len)
        return self.tensor_features(values, self.device)

    def predict_bundle(self, history_apps, history_durations, history_mask, opened_apps, current_app, timestamp):
        if not any(float(m) for m in history_mask):
            raise ValueError("no valid history")
        batch = self.encode_inputs(history_apps, history_durations, history_mask, opened_apps, current_app, timestamp)
        with self.torch.no_grad():
            p = self.visit_probabilities(self.model(**batch))[0].cpu().tolist()
        when = dt.datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        rows = []
        for app, aid in sorted(self.app_vocab.items(), key=lambda kv: kv[1]):
            if app.startswith("<"):
                continue
            p30, p180 = map(float, p[aid])
            if not (math.isfinite(p30) and math.isfinite(p180) and 0 <= p30 <= p180 <= 1):
                raise ValueError("invalid nested probabilities")
            rows.append({"app_id": aid, "app": app, "p_visit_30s": p30, "p_visit_180s": p180,
                "predicted_at": when.isoformat(sep=" "),
                "expires_at": (when + dt.timedelta(seconds=30)).isoformat(sep=" "),
                "probability_source": "nested_sigmoid_uncalibrated"})
        rows, lists = classify_rows(rows, current_app, opened_apps, timestamp, self.hot_threshold, self.cold_threshold)
        return {"prediction_format": "visit_window", "all_probabilities": rows, "outputs": rows,
                "probability_source": "nested_sigmoid_uncalibrated", **lists}
