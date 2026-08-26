#!/usr/bin/env python3
"""Align online LSTM outputs with the next real automated foreground switch."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def timestamp(value: str) -> float:
    text = value.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(text)
    return parsed.timestamp()


def load_scope(path: Path) -> tuple[dict[str, str], set[str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    vocab_to_key = {str(item["vocab_name"]): str(item["app_key"]) for item in payload["apps"]}
    return vocab_to_key, set(vocab_to_key.values())


def focus_sequence(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("event_type", "").endswith("_CASE_DONE") or row.get("status") != "success":
            continue
        app = row.get("app_key", "").strip()
        if not app:
            continue
        events.append({"time": int(row["ts_ns"]) / 1_000_000_000, "app": app, "label": row.get("label", "")})
    return events


def prediction_groups(rows: list[dict[str, str]], vocab_to_key: dict[str, str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for index, row in enumerate(rows):
        key = row.get("feature_window_id") or row.get("call_id") or f"{row.get('timestamp', '')}:{index}"
        grouped[key].append(row)
    result: list[dict[str, Any]] = []
    for key, group in grouped.items():
        first = group[0]
        try:
            when = timestamp(first.get("timestamp") or first.get("sample_timestamp") or first.get("wall_time", ""))
        except (ValueError, TypeError):
            continue
        ranked = sorted(group, key=lambda row: int(row.get("rank") or 999))
        predicted = []
        for row in ranked:
            app_key = row.get("app_key", "").strip() or vocab_to_key.get(row.get("app", "").strip(), "")
            if app_key and app_key not in predicted:
                predicted.append(app_key)
        current_raw = first.get("mapped_foreground_app", "").strip()
        current = vocab_to_key.get(current_raw, current_raw)
        result.append({"id": key, "time": when, "current": current, "predicted": predicted})
    return sorted(result, key=lambda item: item["time"])


def evaluate(automation: Path, predictions: Path, runtime_scope: Path) -> dict[str, Any]:
    vocab_to_key, suite_apps = load_scope(runtime_scope)
    focuses = focus_sequence(read_csv(automation))
    groups = prediction_groups(read_csv(predictions), vocab_to_key)
    samples: list[dict[str, Any]] = []
    misses: Counter[str] = Counter()
    for current, following in zip(focuses, focuses[1:]):
        if current["app"] == following["app"]:
            continue
        candidates = [
            group for group in groups
            if current["time"] <= group["time"] < following["time"]
            and group["current"] in {"", current["app"]}
        ]
        if not candidates:
            misses["no_prediction_between_switches"] += 1
            continue
        chosen = candidates[-1]
        target = following["app"]
        samples.append({
            "current": current["app"], "target": target,
            "prediction_id": chosen["id"], "predicted": chosen["predicted"],
            "hit_at_1": target in chosen["predicted"][:1],
            "hit_at_3": target in chosen["predicted"][:3],
            "hit_at_5": target in chosen["predicted"][:5],
        })
    possible = sum(1 for current, following in zip(focuses, focuses[1:]) if current["app"] != following["app"])
    by_target: dict[str, dict[str, Any]] = {}
    for app in sorted(suite_apps):
        subset = [sample for sample in samples if sample["target"] == app]
        if subset:
            by_target[app] = {
                "samples": len(subset),
                "hit_at_1": sum(sample["hit_at_1"] for sample in subset) / len(subset),
                "hit_at_3": sum(sample["hit_at_3"] for sample in subset) / len(subset),
            }
    count = len(samples)
    return {
        "status": "EVALUABLE" if count and count == possible else "PARTIAL",
        "automation_trace": str(automation.resolve()),
        "predictions": str(predictions.resolve()),
        "runtime_scope": str(runtime_scope.resolve()),
        "suite_apps": sorted(suite_apps),
        "switches_possible": possible,
        "switches_evaluated": count,
        "coverage": count / max(1, possible),
        "hit_at_1": sum(sample["hit_at_1"] for sample in samples) / max(1, count),
        "hit_at_3": sum(sample["hit_at_3"] for sample in samples) / max(1, count),
        "hit_at_5": sum(sample["hit_at_5"] for sample in samples) / max(1, count),
        "random_switch_hit_at_1": 1 / max(1, len(suite_apps) - 1),
        "random_switch_hit_at_3": min(1.0, 3 / max(1, len(suite_apps) - 1)),
        "miss_reasons": dict(misses),
        "by_target": by_target,
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--automation-trace", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--runtime-scope", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.automation_trace, args.predictions, args.runtime_scope)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "lstm-online-report-lzx.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# LSTM 真实在线切换预测", "",
        f"- 状态：`{report['status']}`；覆盖：`{report['switches_evaluated']}/{report['switches_possible']}`（{report['coverage']:.2%}）。",
        f"- Top-1 / Top-3 / Top-5：`{report['hit_at_1']:.2%}` / `{report['hit_at_3']:.2%}` / `{report['hit_at_5']:.2%}`。",
        f"- 同应用集合随机切换基准 Top-1 / Top-3：`{report['random_switch_hit_at_1']:.2%}` / `{report['random_switch_hit_at_3']:.2%}`。", "",
        "该报告只评价真实在线应用切换预测；PageFault 改善必须继续使用 OFF/Apply 配对实验单独计算。", "",
    ]
    (args.output_dir / "lstm-online-report-lzx.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "coverage", "hit_at_1", "hit_at_3", "hit_at_5")}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "EVALUABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
