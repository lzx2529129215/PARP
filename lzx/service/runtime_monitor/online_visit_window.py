"""Output-only visit-window runner; shares existing app mapping/history utilities."""
from __future__ import annotations

import datetime as dt
import json
import time
from collections import Counter
from pathlib import Path

from core.writer import CsvWriter
from online_duration_lstm import (OnlineDurationLSTMRunner, APP_NAME_MAP,
    _load_app_mapping_aliases, parse_time, padded_history)
from predictor_visit_window import OnlineVisitWindowPredictor, classify_rows

ROW_FIELDS = ["session_id", "feature_window_id", "trigger_type", "app_id", "app", "app_key",
    "runtime_app_id", "p_visit_30s", "p_visit_180s", "thermal_state", "prediction_available",
    "predicted_at", "expires_at", "probability_source", "prediction_format"]
CALL_FIELDS = ["prediction_id", "timestamp", "trigger_type", "current_app", "opened_apps",
    "history_apps", "history_durations_s", "history_mask", "hot_apps", "cold_apps", "neutral_apps",
    "predict_latency_ms", "status", "error"]


class OnlineVisitWindowRunner(OnlineDurationLSTMRunner):
    def __init__(self, args, model_dir, review_dir):
        self.args = args
        self.model_type = "visit_window"
        self.session_id = str(getattr(args, "session_id", Path(model_dir).name))
        self.completed_segments = []
        self.current_segment = None
        self.previous_row = None
        self.previous_time = None
        self.last_event_time = None
        self.previous_dwell_s = 0.
        self.last_prediction_time = None
        self.latest_rows = []
        self.call_id = 0
        self.skipped = Counter()
        configured = getattr(args, "app_key_to_vocab_name", {}) or {}
        self.app_name_map = {**APP_NAME_MAP, **_load_app_mapping_aliases(getattr(args, "app_mapping", "")), **configured}
        self.vocab_name_to_app_key = {v: k for k, v in configured.items()}
        scope = getattr(args, "loaded_runtime_scope", None)
        self.app_key_to_runtime_app_id = scope.app_key_to_app_id if scope else {}
        self.hot_threshold = float(getattr(args, "visit_hot_threshold", .90))
        self.cold_threshold = float(getattr(args, "visit_cold_threshold", .20))
        if not 0 <= self.cold_threshold < self.hot_threshold <= 1:
            raise ValueError("visit thresholds must satisfy 0 <= cold < hot <= 1")
        self.predictor = None
        self.predictor_error = ""
        self.vocab = json.loads(Path(args.app_vocab).read_text())
        self.app_name_map.update({a: a for a in self.vocab if not a.startswith("<")})
        try:
            self.predictor = OnlineVisitWindowPredictor(args.lstm_checkpoint, args.app_vocab, args.group_vocab,
                args.user_group, args.device, self.hot_threshold, self.cold_threshold)
            if self.predictor.history_len != args.history_len or self.predictor.duration_cap_s != args.duration_cap_s:
                raise ValueError("runtime history/duration settings differ from checkpoint")
        except Exception as exc:
            self.predictor = None
            self.predictor_error = str(exc)
        self.prediction_writer = CsvWriter(Path(model_dir) / "online_visit_window_predictions.csv", ROW_FIELDS)
        self.call_writer = CsvWriter(Path(review_dir) / "online_visit_window_calls.csv", CALL_FIELDS)
        self.latest_path = Path(review_dir) / "visit_window_latest.json"

    def close(self):
        self.prediction_writer.close()
        self.call_writer.close()

    def reset_history(self):
        self.completed_segments = []
        self.current_segment = None
        self.previous_row = None
        self.previous_time = None
        self.last_event_time = None
        self.last_prediction_time = None
        self.latest_rows = []

    def process_event(self, feature_row, event_type):
        when = parse_time(str(feature_row["timestamp"]))
        # A native edge can arrive after a sample tick that used the old native
        # snapshot. Accept it at its real timestamp if it does not precede an
        # already applied transition; do not lose a switch to delivery latency.
        transition_time = self.current_segment["start_time"] if self.current_segment else None
        if ((self.last_event_time is not None and when < self.last_event_time)
                or (transition_time is not None and when < transition_time)):
            return {"status": "skipped", "skip_reason": "out_of_order", "inference_executed": False,
                    "prediction_format": "visit_window", "outputs": [], "all_probabilities": []}
        if self.previous_time is not None and when < self.previous_time:
            self.previous_time = when
        result = super().process_event(feature_row, event_type)
        self.last_event_time = when
        return result

    def _update_segments(self, row, sample_time, raw_fg, mapped_fg):
        if self.current_segment is not None and mapped_fg != "<UNKNOWN>" and self.current_segment["mapped_app"] != mapped_fg:
            # The transition time closes the old segment, even without intervening
            # periodic samples; last sample time is not the end of its dwell.
            self.current_segment["last_time"] = sample_time
        super()._update_segments(row, sample_time, raw_fg, mapped_fg)
        self.completed_segments = self.completed_segments[-self.args.history_len:]

    def current_result(self, timestamp, current_app, opened_apps):
        rows = self.latest_rows or [{"app_id": aid, "app": app, "p_visit_30s": None,
            "p_visit_180s": None, "predicted_at": "", "expires_at": "", "probability_source": "unavailable"}
            for app, aid in sorted(self.vocab.items(), key=lambda kv: kv[1]) if not app.startswith("<")]
        rows, lists = classify_rows(rows, current_app, opened_apps, timestamp, self.hot_threshold, self.cold_threshold)
        return {"prediction_format": "visit_window", "snapshot_at": timestamp,
                "all_probabilities": rows, **lists}

    def process_sample(self, feature_row, *, trigger_override=""):
        when = parse_time(str(feature_row["timestamp"]))
        if self.previous_time is not None and when < self.previous_time:
            return {"status": "skipped", "skip_reason": "out_of_order", "inference_executed": False,
                    "prediction_format": "visit_window", "outputs": [], "all_probabilities": []}
        if self.previous_time is not None and (when - self.previous_time).total_seconds() > 3600:
            self.reset_history()
        raw_fg = str(feature_row.get("foreground_app", ""))
        fg = self.map_app(raw_fg)
        opened = self.map_open_apps(str(feature_row.get("open_apps", "")))
        previous = self.previous_row or {}
        changed = (fg != self.map_app(previous.get("foreground_app", "")) or
                   set(opened) != set(self.map_open_apps(str(previous.get("open_apps", "")))))
        self._update_segments(feature_row, when, raw_fg, fg)
        segments = self._history_segments(when)
        apps, durations, masks = padded_history([s["mapped_app"] for s in segments],
            [float(s["dwell_s"]) for s in segments], self.args.history_len)
        trigger = trigger_override or ("initial_prediction" if self.last_prediction_time is None else
            "app_state_change" if changed else "periodic_refresh_30s" if
            (when - self.last_prediction_time).total_seconds() >= 30 else "")
        self.previous_row, self.previous_time = dict(feature_row), when
        if not trigger:
            return {"status": "skipped", "skip_reason": "no_prediction_trigger", "inference_executed": False,
                    "outputs": [], **self.current_result(when.isoformat(), fg, opened)}
        started = time.perf_counter()
        error = ""
        executed = False
        try:
            if self.predictor is None:
                raise ValueError(self.predictor_error or "model unavailable")
            executed = True
            bundle = self.predictor.predict_bundle(apps, durations, masks, opened, fg, str(feature_row["timestamp"]))
            self.latest_rows = bundle["all_probabilities"]
            status = "success"
        except Exception as exc:
            self.latest_rows = []
            error, status = str(exc), "error"
        # Retry at the next event or refresh, not every 1s after a failed call.
        self.last_prediction_time = when
        result = self.current_result(when.isoformat(), fg, opened)
        latency = (time.perf_counter() - started) * 1000
        prediction_id = f"{self.session_id}-visit-{self.call_id:06d}"
        self.call_id += 1
        result.update(status=status, skip_reason=error, prediction_id=prediction_id, trigger_type=trigger,
            inference_executed=executed, predict_latency_ms=latency, outputs=result["all_probabilities"],
            mapped_foreground_app=fg, mapped_opened_apps="|".join(opened),
            history_apps="|".join(apps), history_durations_s="|".join(durations), history_mask="|".join(masks))
        for row in result["all_probabilities"]:
            key = self.vocab_name_to_app_key.get(row["app"], "")
            row.update(app_key=key, runtime_app_id=self.app_key_to_runtime_app_id.get(key, ""))
            self.prediction_writer.write_row({**row, "session_id": self.session_id,
                "feature_window_id": feature_row.get("feature_window_id", ""),
                "trigger_type": trigger, "prediction_format": "visit_window"})
        self.call_writer.write_row({"prediction_id": prediction_id, "timestamp": when.isoformat(sep=" "),
            "trigger_type": trigger, "current_app": fg, "opened_apps": "|".join(opened),
            "history_apps": "|".join(apps), "history_durations_s": "|".join(durations), "history_mask": "|".join(masks),
            **{f"{s}_apps": "|".join(result[f"{s}_apps"]) for s in ("hot", "cold", "neutral")},
            "predict_latency_ms": latency, "status": status, "error": error})
        temporary = self.latest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(self.latest_path)
        return result
