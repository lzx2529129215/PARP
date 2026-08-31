"""Online duration-aware LSTM trigger and CSV output for Runtime Monitor v0."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from core.writer import CsvWriter
from predictor import OnlineLSTMPredictor
from predictor_duration import OnlineLSTMDurationPredictor
from predictor_v3 import OnlineLSTMNextV3Predictor


MODEL_CALL_TRACE_FIELDS = [
    "call_id",
    "session_id",
    "feature_window_id",
    "sample_timestamp",
    "wall_time",
    "latency_from_sample_ms",
    "trigger_type",
    "transition_from_raw",
    "transition_to_raw",
    "transition_from_mapped",
    "transition_to_mapped",
    "raw_foreground_app",
    "mapped_foreground_app",
    "raw_open_apps",
    "mapped_opened_apps",
    "raw_history_apps",
    "mapped_history_apps",
    "input_history_apps",
    "input_history_durations_s",
    "input_history_mask",
    "current_app_dwell_s",
    "user_group",
    "top_k",
    "score_mode",
    "device",
    "model_type",
    "checkpoint",
    "predict_latency_ms",
    "output_horizon_3",
    "output_horizon_5",
    "output_horizon_10",
    "output_app_probabilities",
    "status",
    "error",
]

REVIEW_CALL_TRACE_FIELDS = [
    "call_id",
    "time",
    "trigger_type",
    "foreground_app",
    "mapped_foreground_app",
    "opened_apps",
    "mapped_opened_apps",
    "history",
    "history_durations_s",
    "history_mask",
    "current_app_dwell_s",
    "top3_3min",
    "top3_5min",
    "top3_10min",
    "top_apps",
    "predict_latency_ms",
    "status",
    "note",
]

PREDICTION_FIELDS = [
    "session_id",
    "feature_window_id",
    "timestamp",
    "trigger_type",
    "raw_foreground_app",
    "mapped_foreground_app",
    "raw_open_apps",
    "mapped_opened_apps",
    "history_apps",
    "history_durations_s",
    "history_mask",
    "horizon",
    "rank",
    "app_id",
    "app_key",
    "runtime_app_id",
    "app",
    "raw_score",
    "raw_logit",
    "probability",
    "probability_fixed",
    "next_use_probability",
    "next_use_probability_fixed",
    "probability_source",
    "prediction_horizon_ms",
    "prediction_ttl_ms",
    "score_mode",
    "status",
    "skip_reason",
]

V3_PREDICTION_FIELDS = [
    field
    for field in PREDICTION_FIELDS
    if field not in {"horizon", "prediction_horizon_ms"}
] + ["prediction_format"]

TIMELINE_FIELDS = [
    "time",
    "trigger_type",
    "foreground_app",
    "mapped_foreground_app",
    "opened_apps",
    "mapped_opened_apps",
    "history_apps",
    "history_durations_s",
    "horizon",
    "top1",
    "top3",
    "status",
    "note",
]

V3_MODEL_CALL_TRACE_FIELDS = [
    field for field in MODEL_CALL_TRACE_FIELDS
    if field not in {"output_horizon_3", "output_horizon_5", "output_horizon_10"}
]
V3_REVIEW_CALL_TRACE_FIELDS = [
    field for field in REVIEW_CALL_TRACE_FIELDS
    if field not in {"top3_3min", "top3_5min", "top3_10min"}
]
V3_TIMELINE_FIELDS = [field for field in TIMELINE_FIELDS if field != "horizon"]

APP_NAME_MAP = {
    "WPS": "WPS",
    "QQ": "腾讯QQ",
    "FILES": "图库",
    "BILIBILI": "哔哩哔哩",
}
UNKNOWN_VALUES = {"", "UNKNOWN", "None", "none", "null", "NULL"}


def parse_time(value: str) -> dt.datetime:
    text = str(value).strip()
    for parser in (
        dt.datetime.fromisoformat,
        lambda item: dt.datetime.strptime(item, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            return parser(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
    raise ValueError(f"unsupported timestamp: {value!r}")


def format_time(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def fmt_duration(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}".rstrip("0").rstrip(".")


def split_apps(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split("|") if item.strip()]


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def map_app(raw_app: str | None) -> str:
    raw = "" if raw_app is None else str(raw_app).strip()
    if raw in UNKNOWN_VALUES:
        return "<UNKNOWN>"
    return APP_NAME_MAP.get(raw, "<UNKNOWN>")


def map_open_apps(raw_open_apps: str) -> list[str]:
    # DESKTOP 等运行时可观测但未进入训练词表的状态会映射为 <UNKNOWN>；它们
    # 保留在 raw_open_apps 审计中，但不能污染模型的 opened-app multi-hot。
    return dedupe_keep_order([
        mapped for mapped in (map_app(app) for app in split_apps(raw_open_apps))
        if mapped != "<UNKNOWN>"
    ])


def padded_history(apps: list[str], durations: list[float], history_len: int) -> tuple[list[str], list[str], list[str]]:
    apps = apps[-history_len:]
    durations = durations[-history_len:]
    pad_count = max(0, history_len - len(apps))
    return (
        ["<PAD>"] * pad_count + apps,
        ["0"] * pad_count + [fmt_duration(duration) for duration in durations],
        ["0"] * pad_count + ["1"] * len(apps),
    )


def json_rows_for_horizon(outputs: list[dict[str, Any]], horizon: int) -> str:
    rows = [
        {
            "rank": int(row["rank"]),
            "app_id": int(row["app_id"]),
            "app": str(row["app"]),
            "probability": float(row["probability"]),
            "raw_logit": float(row.get("raw_logit", row.get("raw_score", 0.0))),
            "probability_fixed": int(row.get("next_use_probability_fixed", 0)),
        }
        for row in outputs
        if int(row.get("horizon", -1)) == horizon
    ]
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))


def top_names(outputs: list[dict[str, Any]], horizon: int, limit: int = 3) -> str:
    rows = [row for row in outputs if int(row.get("horizon", -1)) == horizon]
    rows.sort(key=lambda row: int(row.get("rank", 0)))
    return "|".join(str(row.get("app", "")) for row in rows[:limit] if row.get("app"))


def top_names_any(outputs: list[dict[str, Any]], limit: int = 3) -> str:
    rows = sorted(outputs, key=lambda row: int(row.get("rank", 0)))
    return "|".join(str(row.get("app", "")) for row in rows[:limit] if row.get("app"))


class OnlineDurationLSTMRunner:
    """在线 LSTM 的状态维护、触发判定、推理调用和审计输出总入口。

    Predictor 本身只接受张量并返回概率，不知道桌面事件。Runner 负责把 runtime
    app_key 映射成训练词表名称，维护最近应用段及停留时间，区分事件触发与采样
    触发，并把同一次调用写入模型 CSV、人工 review CSV 和内存返回值。返回值会
    直接交给 ``PARPMyfsBridge``，因此其中的 ``app_key``、``runtime_app_id`` 和
    ``prediction_id`` 也是用户态模型与内核批次之间的关联键。
    """

    def __init__(self, args: argparse.Namespace, model_dir: Path, review_dir: Path) -> None:
        self.args = args
        self.model_type = str(getattr(args, "lstm_model_type", "duration"))
        self.session_id = str(getattr(args, "session_id", model_dir.name))
        self.model_dir = model_dir
        self.review_dir = review_dir
        # 映射优先级：内置兼容别名 < app_mapping.json < runtime scope。生产服务的
        # 15 App 映射来自 runtime scope，确保它与 myfs 使用的稳定 App ID 同源。
        configured_map = getattr(args, "app_key_to_vocab_name", {}) or {}
        self.app_name_map = dict(APP_NAME_MAP)
        self.app_name_map.update(_load_app_mapping_aliases(getattr(args, "app_mapping", "")))
        self.app_name_map.update({str(key): str(value) for key, value in configured_map.items()})
        self.vocab_name_to_app_key = {
            str(value): str(key) for key, value in configured_map.items()
        }
        runtime_scope = getattr(args, "loaded_runtime_scope", None)
        self.app_key_to_runtime_app_id = (
            runtime_scope.app_key_to_app_id if runtime_scope is not None else {}
        )
        self.call_id = 0
        self.skipped: Counter[str] = Counter()
        self.completed_segments: list[dict[str, Any]] = []
        self.current_segment: dict[str, Any] | None = None
        self.previous_row: dict[str, Any] | None = None
        self.previous_dwell_s = 0.0
        self.last_prediction_time: dt.datetime | None = None
        self.predictor_error = ""
        self.predictor: Any | None = None

        call_fields = V3_MODEL_CALL_TRACE_FIELDS if self.model_type == "v3" else MODEL_CALL_TRACE_FIELDS
        review_call_fields = V3_REVIEW_CALL_TRACE_FIELDS if self.model_type == "v3" else REVIEW_CALL_TRACE_FIELDS
        self.model_call_writer = CsvWriter(model_dir / "online_lstm_duration_call_trace.csv", call_fields)
        self.review_call_writer = CsvWriter(review_dir / "online_lstm_duration_call_trace.csv", review_call_fields)
        prediction_name = "online_lstm_predictions.csv" if self.model_type in {"v2", "v3"} else "online_app_predictions_duration_1s.csv"
        prediction_fields = V3_PREDICTION_FIELDS if self.model_type == "v3" else PREDICTION_FIELDS
        self.prediction_writer = CsvWriter(model_dir / prediction_name, prediction_fields)
        timeline_fields = V3_TIMELINE_FIELDS if self.model_type == "v3" else TIMELINE_FIELDS
        self.timeline_writer = CsvWriter(review_dir / "online_app_predictions_duration_timeline.csv", timeline_fields)

        # 模型加载失败不会阻止常驻采集：predictor 保持 None，之后每次触发都会以
        # predictor_error 写一条 skipped 审计，而不是让 monitor 启动失败。
        try:
            if self.model_type == "v3":
                self.predictor = OnlineLSTMNextV3Predictor(
                    checkpoint=args.lstm_checkpoint,
                    app_vocab=args.app_vocab,
                    group_vocab=args.group_vocab,
                    user_group=args.user_group,
                    top_k=args.top_k,
                    device_name=args.device,
                )
            elif self.model_type == "v2":
                self.predictor = OnlineLSTMPredictor(
                    checkpoint=args.lstm_checkpoint,
                    app_vocab=args.app_vocab,
                    group_vocab=args.group_vocab,
                    user_group=args.user_group,
                    top_k=args.top_k,
                    score_mode=args.score_mode,
                    device_name=args.device,
                )
            else:
                self.predictor = OnlineLSTMDurationPredictor(
                    checkpoint=args.lstm_checkpoint,
                    app_vocab=args.app_vocab,
                    group_vocab=args.group_vocab,
                    user_group=args.user_group,
                    history_len=args.history_len,
                    duration_cap_s=args.duration_cap_s,
                    top_k=args.top_k,
                    score_mode=args.score_mode,
                    device_name=args.device,
                )
        except Exception as exc:
            self.predictor_error = f"predictor_error:{exc}"
            print(f"warning: online duration LSTM disabled: {exc}")

    def close(self) -> None:
        self.model_call_writer.close()
        self.review_call_writer.close()
        self.prediction_writer.close()
        self.timeline_writer.close()

    def process_sample(
        self,
        feature_row: dict[str, Any],
        *,
        trigger_override: str = "",
    ) -> dict[str, Any]:
        """处理一个统一特征快照，并在满足条件时执行一次在线推理。

        ``trigger_override`` 为空时用于采样兼容路径，由相邻快照变化/TTL 决定是否
        推理；非空时表示来自原生 APP_* 边沿，直接采用该触发类型。无论最后推理、
        跳过还是报错，本函数都会返回结构稳定的字典供事件审计和 myfs bridge 使用。
        """
        # 阶段 1：将运行时 App key 归一化为训练词表 token。未知桌面/helper 窗口
        # 映射为 <UNKNOWN>，不会作为一个真实应用段挤占固定长度历史。
        sample_time = parse_time(str(feature_row["timestamp"]))
        raw_fg = str(feature_row.get("foreground_app", ""))
        mapped_fg = self.map_app(raw_fg)
        raw_open_apps = str(feature_row.get("open_apps", ""))
        mapped_opened = self.map_open_apps(raw_open_apps)

        # 阶段 2：更新“连续前台应用段”。只有映射后的 App 变化才结束上一段；
        # 每段至少记 1 秒，避免事件时间戳几乎相同时向模型输入零时长。
        self._update_segments(feature_row, sample_time, raw_fg, mapped_fg)
        history_segments = self._history_segments(sample_time)
        raw_history = [str(item["raw_app"]) for item in history_segments]
        mapped_history = [str(item["mapped_app"]) for item in history_segments]
        durations = [float(item["dwell_s"]) for item in history_segments]
        if self.model_type == "v2":
            valid_history = mapped_history[-self.args.history_len:]
            valid_durations = durations[-self.args.history_len:]
            input_history_apps = list(valid_history)
            input_durations = [fmt_duration(value) for value in valid_durations]
            input_mask = ["1"] * len(input_history_apps)
        else:
            # v3/时长模型需要固定 history_len。左侧不足部分由 <PAD>、0 秒和 mask=0
            # 补齐；有效历史位 mask=1，模型据此忽略 padding。
            input_history_apps, input_durations, input_mask = padded_history(
                mapped_history, durations, self.args.history_len
            )

        # 阶段 3：采样模式检查前台/打开集合变化和 TTL；直接事件模式用 override
        # 覆盖这个结果，避免一个真实 APP_SWITCH 又被 5 秒 cooldown 吞掉。
        trigger_type, skip_reason = self._trigger(feature_row, sample_time, mapped_fg, mapped_opened)
        if trigger_override:
            # Native lifecycle events are already edge-triggered.  Do not
            # reapply the sample-clock cooldown or infer a second transition
            # from a later feature snapshot.
            trigger_type = trigger_override
            skip_reason = ""
        if trigger_type == "event_cooldown":
            skip_reason = "event_cooldown"
        # 即使是直接事件，完全没有有效历史或模型未加载也不能构造可信输入；这些
        # 情况统一标为 skipped，并明确记录原因。
        if not any(mask == "1" for mask in input_mask):
            skip_reason = "no_valid_history"
        if trigger_type == "":
            skip_reason = "no_prediction_trigger"
        if self.predictor is None and not skip_reason:
            skip_reason = self.predictor_error

        base_prediction = {
            "session_id": feature_row.get("session_id", ""),
            "feature_window_id": feature_row.get("feature_window_id", ""),
            "timestamp": feature_row.get("timestamp", ""),
            "trigger_type": "" if trigger_type == "event_cooldown" else trigger_type,
            "raw_foreground_app": raw_fg,
            "mapped_foreground_app": mapped_fg,
            "raw_open_apps": raw_open_apps,
            "mapped_opened_apps": "|".join(mapped_opened),
            "history_apps": "|".join(input_history_apps),
            "history_durations_s": "|".join(input_durations),
            "history_mask": "|".join(input_mask),
            "score_mode": self.args.score_mode,
        }

        if skip_reason:
            self.skipped[skip_reason] += 1
            self.prediction_writer.write_row({**base_prediction, "status": "skipped", "skip_reason": skip_reason})
            self.previous_row = dict(feature_row)
            self.previous_dwell_s = self._current_dwell(sample_time)
            return {
                "status": "skipped",
                "skip_reason": skip_reason,
                "trigger_type": trigger_type,
                "inference_executed": False,
                "outputs": [],
                "mapped_foreground_app": mapped_fg,
                "raw_foreground_app": raw_fg,
            }

        assert self.predictor is not None
        inference_executed = True
        status = "success"
        error = ""
        outputs: list[dict[str, Any]] = []
        all_probabilities: list[dict[str, Any]] = []
        bundle: dict[str, Any] = {"probability_source": "unavailable"}
        wall_dt = dt.datetime.now()
        wall_mono = time.perf_counter()
        # 阶段 4：真正的模型调用。v3 返回单步“下一 App”概率；旧模型返回多个
        # 时间 horizon。异常被收敛到当前调用的 error 字段，不向上传播到主循环。
        try:
            if self.model_type == "v3":
                bundle = self.predictor.predict_bundle(
                    input_history_apps,
                    input_durations,
                    input_mask,
                    mapped_opened,
                    mapped_fg,
                    str(feature_row["timestamp"]),
                )
            elif self.model_type == "v2":
                bundle = self.predictor.predict_bundle(
                    input_history_apps,
                    mapped_opened,
                    str(feature_row["timestamp"]),
                )
            else:
                bundle = self.predictor.predict_bundle(
                    mapped_history,
                    durations,
                    mapped_opened,
                    str(feature_row["timestamp"]),
                )
            outputs = list(bundle.get("top_k_outputs", []))
            all_probabilities = list(bundle.get("all_probabilities", []))
            if not outputs:
                status = "skipped"
                error = "predictor_returned_no_rows"
                self.skipped[error] += 1
        except Exception as exc:
            status = "error"
            error = f"predictor_error:{exc}"
            self.skipped[error] += 1
        predict_latency_ms = (time.perf_counter() - wall_mono) * 1000.0
        latency_from_sample_ms = max(0.0, (wall_dt - sample_time).total_seconds() * 1000.0)

        if status == "success":
            self.last_prediction_time = sample_time
            normalized_probabilities: list[dict[str, Any]] = []
            for output in all_probabilities:
                app_name = str(output.get("app", ""))
                app_key = self.vocab_name_to_app_key.get(app_name, "")
                # The CSV has always carried the runtime key, but downstream
                # online consumers receive this in-memory bundle rather than
                # rereading the CSV.  Preserve the same mapping here so
                # Test4B's event-time controller does not silently default
                # every candidate to probability 1.0.
                # Predictor 使用词表名称；下游 bridge/controller 需要 runtime app_key
                # 和内核稳定 App ID，因此在内存 bundle 与 CSV 两处都补齐映射。
                normalized = {
                    **output,
                    "app_key": app_key,
                    "runtime_app_id": self.app_key_to_runtime_app_id.get(app_key, ""),
                }
                normalized_probabilities.append(normalized)
                row = {
                    **base_prediction,
                    "rank": output.get("rank", ""),
                    "app_id": output.get("app_id", ""),
                    "app_key": app_key,
                    "runtime_app_id": self.app_key_to_runtime_app_id.get(app_key, ""),
                    "app": output.get("app", ""),
                    "raw_score": output.get("raw_score", ""),
                    "raw_logit": output.get("raw_logit", output.get("raw_score", "")),
                    "probability": output.get("probability", ""),
                    "probability_fixed": output.get("probability_fixed", output.get("next_use_probability_fixed", "")),
                    "next_use_probability": output.get("next_use_probability", ""),
                    "next_use_probability_fixed": output.get("next_use_probability_fixed", ""),
                    "probability_source": output.get("probability_source", "unavailable"),
                    "prediction_ttl_ms": int(round(self.args.prediction_ttl_s * 1000)),
                    "score_mode": output.get("score_mode", self.args.score_mode),
                    "status": "success",
                    "skip_reason": "",
                }
                if self.model_type == "v3":
                    row["prediction_format"] = "app_probability"
                else:
                    row["horizon"] = output.get("horizon", "")
                    row["prediction_horizon_ms"] = int(output.get("horizon", 0)) * 60_000
                self.prediction_writer.write_row(row)
            all_probabilities = normalized_probabilities
            if self.model_type == "v3":
                self.timeline_writer.write_row({
                    "time": feature_row.get("timestamp", ""),
                    "trigger_type": trigger_type,
                    "foreground_app": raw_fg,
                    "mapped_foreground_app": mapped_fg,
                    "opened_apps": raw_open_apps,
                    "mapped_opened_apps": "|".join(mapped_opened),
                    "history_apps": "|".join(input_history_apps),
                    "history_durations_s": "|".join(input_durations),
                    "horizon": "",
                    "top1": top_names_any(outputs, 1),
                    "top3": top_names_any(outputs, 3),
                    "status": "success",
                    "note": "online v3 single-step app probability",
                })
            else:
                for horizon in [3, 5, 10]:
                    self.timeline_writer.write_row({
                        "time": feature_row.get("timestamp", ""),
                        "trigger_type": trigger_type,
                        "foreground_app": raw_fg,
                        "mapped_foreground_app": mapped_fg,
                        "opened_apps": raw_open_apps,
                        "mapped_opened_apps": "|".join(mapped_opened),
                        "history_apps": "|".join(input_history_apps),
                        "history_durations_s": "|".join(input_durations),
                        "horizon": horizon,
                        "top1": top_names(outputs, horizon, 1),
                        "top3": top_names(outputs, horizon, 3),
                        "status": "success",
                        "note": "online duration-aware switch LSTM",
                    })
        else:
            self.prediction_writer.write_row({**base_prediction, "status": status, "skip_reason": error})

        # 阶段 5：写调用级证据。prediction CSV 是逐候选概率，call trace 是一次
        # 推理一行，二者通过 feature_window_id/prediction_id 对齐。
        previous_raw = self.previous_row.get("foreground_app", "") if self.previous_row else ""
        previous_mapped = self.map_app(previous_raw) if self.previous_row else ""
        model_row = {
            "call_id": self.call_id,
            "session_id": feature_row.get("session_id", ""),
            "feature_window_id": feature_row.get("feature_window_id", ""),
            "sample_timestamp": feature_row.get("timestamp", ""),
            "wall_time": format_time(wall_dt),
            "latency_from_sample_ms": f"{latency_from_sample_ms:.3f}",
            "trigger_type": trigger_type,
            "transition_from_raw": previous_raw,
            "transition_to_raw": raw_fg,
            "transition_from_mapped": previous_mapped,
            "transition_to_mapped": mapped_fg,
            "raw_foreground_app": raw_fg,
            "mapped_foreground_app": mapped_fg,
            "raw_open_apps": raw_open_apps,
            "mapped_opened_apps": "|".join(mapped_opened),
            "raw_history_apps": "|".join(raw_history[-self.args.history_len:]),
            "mapped_history_apps": "|".join(mapped_history[-self.args.history_len:]),
            "input_history_apps": "|".join(input_history_apps),
            "input_history_durations_s": "|".join(input_durations),
            "input_history_mask": "|".join(input_mask),
            "current_app_dwell_s": fmt_duration(self._current_dwell(sample_time)),
            "user_group": self.args.user_group,
            "top_k": self.args.top_k,
            "score_mode": self.args.score_mode,
            "device": str(getattr(self.predictor, "device", self.args.device)),
            "model_type": "AppLSTM-v2" if self.model_type == "v2" else ("AppLSTMNextV3" if self.model_type == "v3" else "AppLSTMDurationV3"),
            "checkpoint": self.args.lstm_checkpoint,
            "predict_latency_ms": f"{predict_latency_ms:.3f}",
            "output_horizon_3": "" if self.model_type == "v3" else json_rows_for_horizon(outputs, 3),
            "output_horizon_5": "" if self.model_type == "v3" else json_rows_for_horizon(outputs, 5),
            "output_horizon_10": "" if self.model_type == "v3" else json_rows_for_horizon(outputs, 10),
            "output_app_probabilities": json.dumps(outputs, ensure_ascii=False, separators=(",", ":")) if self.model_type == "v3" else "",
            "status": status,
            "error": error,
        }
        self.model_call_writer.write_row(model_row)
        self.review_call_writer.write_row({
            "call_id": self.call_id,
            "time": feature_row.get("timestamp", ""),
            "trigger_type": trigger_type,
            "foreground_app": raw_fg,
            "mapped_foreground_app": mapped_fg,
            "opened_apps": raw_open_apps,
            "mapped_opened_apps": "|".join(mapped_opened),
            "history": "|".join(input_history_apps),
            "history_durations_s": "|".join(input_durations),
            "history_mask": "|".join(input_mask),
            "current_app_dwell_s": fmt_duration(self._current_dwell(sample_time)),
            "top3_3min": "" if self.model_type == "v3" else top_names(outputs, 3),
            "top3_5min": "" if self.model_type == "v3" else top_names(outputs, 5),
            "top3_10min": "" if self.model_type == "v3" else top_names(outputs, 10),
            "top_apps": top_names_any(outputs, 3) if self.model_type == "v3" else "",
            "predict_latency_ms": f"{predict_latency_ms:.3f}",
            "status": status,
            "note": "online app switch LSTM v2" if self.model_type == "v2" else "online duration-aware switch LSTM",
        })
        self.call_id += 1
        self.previous_row = dict(feature_row)
        self.previous_dwell_s = self._current_dwell(sample_time)
        return {
            "status": status,
            "skip_reason": error,
            "outputs": outputs,
            "all_probabilities": all_probabilities if status == "success" else [],
            "probability_source": (
                bundle.get("probability_source", "unavailable")
                if status == "success"
                else "unavailable"
            ),
            "prediction_format": bundle.get("prediction_format", "horizon") if status == "success" else "",
            "prediction_id": f"{self.session_id}-p{max(0, self.call_id - 1):05d}",
            "trigger_type": trigger_type,
            "inference_executed": inference_executed,
            "predict_latency_ms": predict_latency_ms,
            "history_apps": "|".join(input_history_apps),
            "history_durations_s": "|".join(input_durations),
            "mapped_opened_apps": "|".join(mapped_opened),
            "mapped_foreground_app": mapped_fg,
            "raw_foreground_app": raw_fg,
        }

    def process_event(self, feature_row: dict[str, Any], event_type: str) -> dict[str, Any]:
        """从原生 APP_* 边沿直接触发推理，并把触发来源标准化进审计字段。"""
        normalized = str(event_type).strip().upper() or "APP_EVENT"
        return self.process_sample(
            feature_row,
            trigger_override=f"direct_{normalized.lower()}",
        )

    def _update_segments(self, row: dict[str, Any], sample_time: dt.datetime, raw_fg: str, mapped_fg: str) -> None:
        """维护连续前台 App 段；段切换时固化上一段的 dwell_s。"""
        # Compositor/helper windows which are outside the LSAPP vocabulary are
        # not application transitions.  Keeping them as a segment would evict
        # one real app from the fixed five-step LSTM history and make an
        # otherwise trained sequence depend on transient desktop timing.
        # The raw event remains in the lifecycle audit; only model history is
        # protected from the unmapped token. lzx-note
        if mapped_fg == "<UNKNOWN>":
            return
        if self.current_segment is None:
            self.current_segment = {
                "raw_app": raw_fg,
                "mapped_app": mapped_fg,
                "start_time": sample_time,
                "last_time": sample_time,
            }
            return
        if self.current_segment["mapped_app"] != mapped_fg:
            completed = dict(self.current_segment)
            completed["dwell_s"] = max(1.0, (completed["last_time"] - completed["start_time"]).total_seconds())
            self.completed_segments.append(completed)
            self.current_segment = {
                "raw_app": raw_fg,
                "mapped_app": mapped_fg,
                "start_time": sample_time,
                "last_time": sample_time,
            }
        else:
            self.current_segment["last_time"] = sample_time

    def _history_segments(self, sample_time: dt.datetime) -> list[dict[str, Any]]:
        segments = list(self.completed_segments)
        if self.current_segment is not None:
            current = dict(self.current_segment)
            current["dwell_s"] = self._current_dwell(sample_time)
            segments.append(current)
        return segments

    def _current_dwell(self, sample_time: dt.datetime) -> float:
        if self.current_segment is None:
            return 0.0
        return max(1.0, (sample_time - self.current_segment["start_time"]).total_seconds())

    def _trigger(
        self,
        row: dict[str, Any],
        sample_time: dt.datetime,
        mapped_fg: str,
        mapped_opened: list[str],
    ) -> tuple[str, str]:
        """仅为采样兼容路径判断触发原因；原生事件会在调用方覆盖该结果。"""
        if self.last_prediction_time is None:
            return "initial_prediction", ""
        previous_mapped_fg = self.map_app(self.previous_row.get("foreground_app", "")) if self.previous_row else mapped_fg
        previous_opened = self.map_open_apps(str(self.previous_row.get("open_apps", ""))) if self.previous_row else mapped_opened
        event_triggers: list[str] = []
        if previous_mapped_fg != mapped_fg:
            event_triggers.append("foreground_transition")
        if set(previous_opened) != set(mapped_opened):
            event_triggers.append("opened_apps_change")
        if event_triggers:
            elapsed = (sample_time - self.last_prediction_time).total_seconds()
            if elapsed < self.args.min_event_cooldown_s:
                return "event_cooldown", "event_cooldown"
            return "+".join(dedupe_keep_order(event_triggers)), ""
        elapsed = (sample_time - self.last_prediction_time).total_seconds()
        if self.args.trigger_mode == "event_plus_ttl" and elapsed >= self.args.prediction_ttl_s:
            return f"periodic_ttl_refresh_{fmt_duration(self.args.periodic_refresh_s)}s", ""
        return "", "no_prediction_trigger"

    def map_app(self, raw_app: str | None) -> str:
        raw = "" if raw_app is None else str(raw_app).strip()
        if raw in UNKNOWN_VALUES:
            return "<UNKNOWN>"
        return self.app_name_map.get(raw, "<UNKNOWN>")

    def map_open_apps(self, raw_open_apps: str) -> list[str]:
        return dedupe_keep_order([
            mapped
            for mapped in (
                self.map_app(app) for app in split_apps(raw_open_apps)
            )
            if mapped != "<UNKNOWN>"
        ])


def _load_app_mapping_aliases(path_value: str | Path | None) -> dict[str, str]:
    if not path_value:
        return {}
    path = Path(path_value).expanduser()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    aliases: dict[str, str] = {}
    for rule in config.get("rules", []):
        if not isinstance(rule, dict):
            continue
        app = str(rule.get("app", "")).strip()
        if not app:
            continue
        aliases[app] = app
        for key in ("gtk_app_id", "wm_class", "process", "title_contains"):
            raw_values = rule.get(key, [])
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            for value in values:
                alias = str(value).strip()
                if alias:
                    aliases[alias] = app
                    aliases[alias.upper()] = app
    return aliases
