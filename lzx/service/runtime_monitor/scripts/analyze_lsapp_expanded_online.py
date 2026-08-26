#!/usr/bin/env python3
"""Evaluate online v3 predictions on held-out LSAPP replay markers."""

# lzx-note: Avoids block-boundary scoring and rejects post-switch leakage.
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def time_value(value: str) -> float:
    return dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()


def trace_time(row: dict[str, str]) -> float:
    if row.get("ts_ns"): return int(row["ts_ns"]) / 1_000_000_000
    return time_value(row.get("ts_iso") or row.get("timestamp", ""))


def scope_maps(path: Path) -> tuple[dict[str, str], set[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    vocab_to_key = {item["vocab_name"]: item["app_key"] for item in payload["apps"] if item.get("prediction_enabled")}
    return vocab_to_key, set(vocab_to_key.values())


def prediction_groups(data: list[dict[str, str]], vocab_to_key: dict[str, str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for index, row in enumerate(data):
        key = row.get("feature_window_id") or row.get("call_id") or f"{row.get('timestamp')}:{index}"
        grouped[key].append(row)
    result: list[dict[str, Any]] = []
    for key, group in grouped.items():
        first = group[0]
        try: when = time_value(first.get("timestamp") or first.get("sample_timestamp") or first.get("wall_time", ""))
        except ValueError: continue
        ranked = sorted(group, key=lambda row: int(row.get("rank") or 999))
        predicted: list[str] = []
        for row in ranked:
            app = row.get("app_key", "").strip() or vocab_to_key.get(row.get("app", "").strip(), "")
            if app and app not in predicted: predicted.append(app)
        current_raw = first.get("mapped_foreground_app", "").strip()
        result.append({"id": key, "time": when, "current": vocab_to_key.get(current_raw, current_raw), "predicted": predicted})
    return sorted(result, key=lambda item: item["time"])


def evaluate(trace_path: Path, predictions_path: Path, scope_path: Path) -> dict[str, Any]:
    vocab_to_key, app_keys = scope_maps(scope_path)
    predictions = prediction_groups(rows(predictions_path), vocab_to_key)
    current: dict[str, Any] | None = None
    samples: list[dict[str, Any]] = []
    misses: Counter[str] = Counter()
    for row in rows(trace_path):
        if row.get("status") != "success": continue
        event = row.get("event_type")
        if event == "LSAPP_BLOCK_START":
            current = {"app": row.get("app_key", ""), "time": trace_time(row), "label": row.get("label", "")}
            continue
        if event != "LSAPP_TRANSITION_DONE" or current is None: continue
        target = row.get("app_key", ""); end = trace_time(row)
        candidates = [item for item in predictions if current["time"] <= item["time"] < end and item["current"] in {"", current["app"]}]
        if not candidates:
            misses["no_pre_switch_prediction"] += 1
        else:
            chosen = candidates[-1]
            samples.append({
                "current": current["app"], "target": target, "prediction_id": chosen["id"],
                "predicted": chosen["predicted"], "hit_at_1": target in chosen["predicted"][:1],
                "hit_at_3": target in chosen["predicted"][:3], "hit_at_5": target in chosen["predicted"][:5],
            })
        current = {"app": target, "time": end, "label": row.get("label", "")}
    possible = len(samples) + misses["no_pre_switch_prediction"]
    by_target: dict[str, Any] = {}
    for app in sorted(app_keys):
        subset = [item for item in samples if item["target"] == app]
        by_target[app] = {
            "samples": len(subset),
            "hit_at_1": sum(item["hit_at_1"] for item in subset) / len(subset) if subset else None,
            "hit_at_3": sum(item["hit_at_3"] for item in subset) / len(subset) if subset else None,
        }
    count = len(samples)
    macro_targets = [item for item in by_target.values() if item["samples"]]
    return {
        "status": "EVALUABLE" if possible and count == possible else "PARTIAL",
        "automation_trace": str(trace_path.resolve()), "predictions": str(predictions_path.resolve()),
        "runtime_scope": str(scope_path.resolve()), "switches_possible": possible, "switches_evaluated": count,
        "coverage": count / max(1, possible),
        "hit_at_1": sum(item["hit_at_1"] for item in samples) / max(1, count),
        "hit_at_3": sum(item["hit_at_3"] for item in samples) / max(1, count),
        "hit_at_5": sum(item["hit_at_5"] for item in samples) / max(1, count),
        "macro_hit_at_1": sum(item["hit_at_1"] for item in macro_targets) / max(1, len(macro_targets)),
        "macro_hit_at_3": sum(item["hit_at_3"] for item in macro_targets) / max(1, len(macro_targets)),
        "random_hit_at_1": 1 / max(1, len(app_keys) - 1), "random_hit_at_3": min(1.0, 3 / max(1, len(app_keys) - 1)),
        "miss_reasons": dict(misses), "by_target": by_target, "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--runtime-scope", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(
        args.session_dir / "model/automation_trace.csv",
        args.session_dir / "model/online_lstm_predictions.csv", args.runtime_scope,
    )
    review = args.session_dir / "review"; review.mkdir(parents=True, exist_ok=True)
    (review / "lsapp-expanded-online-lstm.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (review / "lsapp-expanded-online-lstm.md").write_text(
        "# LSAPP-expanded 在线 LSTM\n\n"
        f"- 状态：`{report['status']}`；覆盖：`{report['switches_evaluated']}/{report['switches_possible']}`（{report['coverage']:.2%}）。\n"
        f"- Top-1 / Top-3 / Top-5：`{report['hit_at_1']:.2%}` / `{report['hit_at_3']:.2%}` / `{report['hit_at_5']:.2%}`。\n"
        f"- 宏平均 Top-1 / Top-3：`{report['macro_hit_at_1']:.2%}` / `{report['macro_hit_at_3']:.2%}`。\n"
        f"- 15 应用随机基准 Top-1 / Top-3：`{report['random_hit_at_1']:.2%}` / `{report['random_hit_at_3']:.2%}`。\n\n"
        "本报告只评价真实窗口切换预测，不把它等同于 PageFault 改善。\n", encoding="utf-8",
    )
    print(json.dumps({key: report[key] for key in ("status", "coverage", "hit_at_1", "hit_at_3", "hit_at_5", "macro_hit_at_1")}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "EVALUABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
