from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 这个原型实现了一个用户态的 workload-aware 观测管线。
# 它读取一串原始的内存区域观测数据，将其转换为结构化的特征集合，
# 更新内部的 workload 状态机，预测下一阶段的工作负载行为，并输出与
# PARP shadow-observe 模型兼容的快照。
# 整体逻辑被明确分为四个阶段：
#   1. 输入接收与归一化
#   2. 特征提取与状态评估
#   3. 工作负载预测与快照生成
#   4. 最终产物输出，用于后续校验和审计
#
# 整个执行流程刻意避免直接依赖内核级逻辑，而是基于监控层已准备好的
# 观测记录在用户态完成分析，并把结果输出到指定目录，供后续检查、
# 对比和集成使用。

from runtime_monitor.detector.state_machine import StateMachine
from runtime_monitor.features.engine import Observation, extract_features
from runtime_monitor.output.snapshot import make_snapshot, write_snapshot
from predictor.workload_predictor import WorkloadPredictor
from kernel.adapters.parp_snapshot_adapter import write_hint


def load_observations(path: Path) -> list[Observation]:
    """读取 JSONL 观测流，并将其规范化为带类型的 Observation 对象。

    每一行表示一个采样窗口。该加载器采用保守的类型转换策略，确保
    即使存在格式不完整或字段缺失的情况，也不会让整个流程因为异常而中断。
    缺失的数据会使用安全默认值填充，数值字段会被强制转换为后续特征提取器
    需要的 Python 类型。
    """
    rows: list[Observation] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row: dict[str, Any] = json.loads(line)
        rows.append(Observation(
            scope_type=str(row.get("scope_type", "cgroup")),
            scope_id=str(row.get("scope_id", "")),
            window_start_ns=int(row.get("window_start_ns", 0)),
            window_end_ns=int(row.get("window_end_ns", 0)),
            timestamp_ns=int(row.get("timestamp_ns", 0)),
            sampling_interval_ms=int(row.get("sampling_interval_ms", 1000)),
            region_ids=tuple(str(value) for value in row.get("region_ids", [])),
            region_accesses=tuple(float(value) for value in row.get("region_accesses", [])),
            region_timestamps_ns=tuple(int(value) for value in row.get("region_timestamps_ns", [])),
            counters={str(key): float(value) for key, value in row.get("counters", {}).items()},
        ))
    return rows


def main() -> int:
    """workload-aware observe/shadow 原型的主入口。

    该函数的目标是逐条处理原始访问观测数据，并为每个时间窗口生成完整的
    分析记录，包括：
      - 计算工作负载特征
      - 更新 workload 状态机
      - 学习时间维度上的状态转移
      - 根据趋势规则或二阶 Markov 模型生成预测结果
      - 持久化中间产物和最终分析结果，供后续评估与审计

    该原型支持两种模式：
      * OBSERVE：用于监控和分析场景，观察真实的工作负载行为
      * SHADOW：用于仿真和兼容性验证，通过 shadow 方式生成预测和提示
        信息，供更大范围的 PARP 流程对接使用
    """
    parser = argparse.ArgumentParser(description="Workload-Aware observe/shadow prototype")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--mode", choices=("OBSERVE", "SHADOW"), default="OBSERVE")
    parser.add_argument("--method", choices=("rule_trend", "second_order_markov"), default="rule_trend")
    args = parser.parse_args()
    machine = StateMachine()
    predictor = WorkloadPredictor()
    history = []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "features.jsonl").open("w", encoding="utf-8") as feature_file, (args.output_dir / "predictions.jsonl").open("w", encoding="utf-8") as prediction_file:
        for observation in load_observations(args.input):
            features = extract_features(observation)
            state = machine.update(features)
            if history:
                predictor.observe_transition(history[-1], state)
            history.append(state)
            prediction = predictor.predict_markov(history, state) if args.method == "second_order_markov" else predictor.predict_rule_trend(state)
            snapshot = make_snapshot(prediction, features, mode=args.mode, native_fallback=state.confidence_q15 == 0)
            json.dump({**features.__dict__, "state": state.__dict__}, feature_file, ensure_ascii=False)
            feature_file.write("\n")
            json.dump(snapshot, prediction_file, ensure_ascii=False)
            prediction_file.write("\n")
    last_line = next(reversed((args.output_dir / "predictions.jsonl").read_text(encoding="utf-8").splitlines()), "")
    if last_line:
        snapshot = json.loads(last_line)
        write_snapshot(args.output_dir / "prediction_snapshot.json", snapshot)
        write_hint(args.output_dir / "parp_shadow_hint.json", snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
