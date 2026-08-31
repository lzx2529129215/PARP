from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from predictor.workload_predictor import WorkloadPredictor
from runtime_monitor.detector.state_machine import StateMachine
from runtime_monitor.features.engine import Observation, extract_features

REPORT_DIR = ROOT / "test-reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)


def row(name: str, ids: list[str], accesses: list[float], counters: dict[str, float]) -> dict[str, Any]:
    return {
        "scope_type": "region",
        "scope_id": name,
        "window_start_ns": 0,
        "window_end_ns": 1000,
        "timestamp_ns": 1000,
        "sampling_interval_ms": 1000,
        "region_ids": ids,
        "region_accesses": accesses,
        "region_timestamps_ns": [10 * (index + 1) for index in range(len(ids))],
        "counters": counters,
    }


BASE_COUNTERS = {
    "allocation_delta_pages": 0,
    "pgfault_delta": 0,
    "psi": 0,
    "foreground": 1,
}


def stable_hot(name: str) -> dict[str, Any]:
    return row(name, ["r1", "r1", "r1", "r1"], [6, 6, 6, 6], {
        **BASE_COUNTERS,
        "wss_pages": 128,
        "wss_delta_pages": 0,
        "hotspot_jaccard": 0.9,
        "hotspot_shift_rate": 0.1,
    })


def streaming(name: str) -> dict[str, Any]:
    return row(name, ["r1", "r2", "r3", "r4"], [1, 1, 1, 1], {
        **BASE_COUNTERS,
        "wss_pages": 40,
        "wss_delta_pages": 0,
        "hotspot_jaccard": 0.1,
        "hotspot_shift_rate": 0.1,
    })


def burst(name: str) -> dict[str, Any]:
    return row(name, ["r1", "r2", "r3", "r4"], [2, 3, 4, 5], {
        **BASE_COUNTERS,
        "wss_pages": 500,
        "wss_delta_pages": 120,
        "allocation_delta_pages": 90,
        "pgfault_delta": 8,
        "hotspot_jaccard": 0.6,
        "hotspot_shift_rate": 0.2,
    })


def cold(name: str) -> dict[str, Any]:
    return row(name, ["r1", "r2", "r3", "r4"], [0, 0, 0, 0], {
        **BASE_COUNTERS,
        "wss_pages": 0,
        "wss_delta_pages": 0,
        "hotspot_jaccard": 0,
        "hotspot_shift_rate": 0,
    })


def multi_hotspot(name: str) -> dict[str, Any]:
    return row(name, ["r1", "r2", "r1", "r3"], [6, 5, 6, 5], {
        **BASE_COUNTERS,
        "wss_pages": 320,
        "wss_delta_pages": 0,
        "hotspot_jaccard": 0.6,
        "hotspot_shift_rate": 0.9,
    })


def random_access(name: str) -> dict[str, Any]:
    return row(name, ["r4", "r1", "r3", "r2"], [1, 1, 1, 1], {
        **BASE_COUNTERS,
        "wss_pages": 200,
        "wss_delta_pages": 0,
        "hotspot_jaccard": 0.2,
        "hotspot_shift_rate": 0.2,
    })


def emergency(name: str) -> dict[str, Any]:
    return row(name, ["r1", "r1", "r2", "r2"], [5, 5, 4, 4], {
        **BASE_COUNTERS,
        "wss_pages": 256,
        "wss_delta_pages": 20,
        "pgfault_delta": 30,
        "psi": 0.35,
        "hotspot_jaccard": 0.8,
        "hotspot_shift_rate": 0.1,
    })


SCENARIOS = [
    {"name": "持续稳定热点", "windows": [stable_hot("stable") for _ in range(4)], "expected": ["STABLE_HOT"] * 4},
    {"name": "顺序流式读取", "windows": [streaming("stream") for _ in range(4)], "expected": ["STREAMING"] * 4},
    {"name": "突发工作集扩张", "windows": [burst("burst") for _ in range(4)], "expected": ["BURST_EXPANSION"] * 4},
    {"name": "低价值冷态", "windows": [cold("cold") for _ in range(4)], "expected": ["LOW_VALUE_COLD"] * 4},
    {"name": "多热点轮换", "windows": [multi_hotspot("multi") for _ in range(4)], "expected": ["MULTI_HOTSPOT"] * 4},
    {"name": "随机离散访问", "windows": [random_access("random") for _ in range(4)], "expected": ["UNKNOWN"] * 4},
    {"name": "内存压力场景", "windows": [emergency("pressure") for _ in range(4)], "expected": ["MIXED"] * 4},
    {
        "name": "扩张后进入稳定热点",
        "windows": [burst("transition"), burst("transition"), stable_hot("transition"), stable_hot("transition")],
        "expected": ["BURST_EXPANSION", "BURST_EXPANSION", "STABLE_HOT", "STABLE_HOT"],
    },
]


def make_observation(data: dict[str, Any]) -> Observation:
    return Observation(
        scope_type=str(data["scope_type"]),
        scope_id=str(data["scope_id"]),
        window_start_ns=int(data["window_start_ns"]),
        window_end_ns=int(data["window_end_ns"]),
        timestamp_ns=int(data["timestamp_ns"]),
        sampling_interval_ms=int(data["sampling_interval_ms"]),
        region_ids=tuple(data["region_ids"]),
        region_accesses=tuple(data["region_accesses"]),
        region_timestamps_ns=tuple(data["region_timestamps_ns"]),
        counters=dict(data["counters"]),
    )


def evaluate_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    machine = StateMachine(min_dwell_windows=2)
    predictor = WorkloadPredictor()
    history = []
    actual_states = []
    rule_correct = 0
    markov_correct = 0
    classification_correct = 0
    prediction_samples = 0

    for index, raw in enumerate(scenario["windows"]):
        state = machine.update(extract_features(make_observation(raw)))
        actual_states.append(state.dominant)
        if state.dominant == scenario["expected"][index]:
            classification_correct += 1

        if history:
            predictor.observe_transition(history[-1], state)
        history.append(state)

        if index < len(scenario["windows"]) - 1:
            expected_next = scenario["expected"][index + 1]
            rule_next = predictor.predict_rule_trend(state).next.dominant
            markov_next = predictor.predict_markov(history, state).next.dominant
            rule_correct += rule_next == expected_next
            markov_correct += markov_next == expected_next
            prediction_samples += 1

    return {
        "name": scenario["name"],
        "expected_states": scenario["expected"],
        "actual_states": actual_states,
        "classification_correct": classification_correct,
        "classification_total": len(scenario["windows"]),
        "rule_trend_correct": rule_correct,
        "markov_correct": markov_correct,
        "prediction_total": prediction_samples,
    }


def main() -> int:
    results = [evaluate_scenario(scenario) for scenario in SCENARIOS]
    classification_total = sum(item["classification_total"] for item in results)
    classification_correct = sum(item["classification_correct"] for item in results)
    prediction_total = sum(item["prediction_total"] for item in results)
    rule_correct = sum(item["rule_trend_correct"] for item in results)
    markov_correct = sum(item["markov_correct"] for item in results)
    summary = {
        "scenario_count": len(results),
        "window_count": classification_total,
        "prediction_sample_count": prediction_total,
        "classification_accuracy": round(classification_correct / classification_total * 100, 2),
        "rule_trend_prediction_accuracy": round(rule_correct / prediction_total * 100, 2),
        "second_order_markov_prediction_accuracy": round(markov_correct / prediction_total * 100, 2),
        "results": results,
    }
    (REPORT_DIR / "realistic_backtest_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# 更贴近实际的 Workload 场景回测报告",
        "",
        f"- 场景数量：{summary['scenario_count']}",
        f"- 观测窗口数量：{summary['window_count']}",
        f"- 预测样本数量：{summary['prediction_sample_count']}",
        f"- 状态判断准确率：{summary['classification_accuracy']}%",
        f"- 规则趋势预测准确率：{summary['rule_trend_prediction_accuracy']}%",
        f"- 二阶 Markov 预测准确率：{summary['second_order_markov_prediction_accuracy']}%",
        "",
        "## 场景明细",
        "",
    ]
    for item in results:
        lines.extend([
            f"### {item['name']}",
            f"- 期望状态：{', '.join(item['expected_states'])}",
            f"- 实际状态：{', '.join(item['actual_states'])}",
            f"- 判断：{item['classification_correct']}/{item['classification_total']}",
            f"- 规则预测：{item['rule_trend_correct']}/{item['prediction_total']}",
            f"- Markov 预测：{item['markov_correct']}/{item['prediction_total']}",
            "",
        ])
    (REPORT_DIR / "realistic_backtest_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in summary if key != "results"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
