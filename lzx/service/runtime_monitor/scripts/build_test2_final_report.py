#!/usr/bin/env python3
"""Build the scoped Test2 LSTM-to-PARP sink report from one session."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def status_mark(value: bool, blocked: bool = False) -> str:
    if blocked:
        return "BLOCKED"
    return "PASS" if value else "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--test1-session-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    session = args.session_dir
    manifest = read_json(session / "manifest.json", {})
    facts = read_json(session / "kernel_facts.json", {})
    coverage = read_json(session / "review/online_prediction_coverage.json", {})
    sink = read_json(session / "review/prediction_sink_coverage.json", {})
    summary = read_json(session / "parp/parp_bridge_summary.json", {})
    # The verifier augments the bridge's raw funnel with the explicit
    # whole-batch suppression stage, so prefer those derived fields while
    # retaining the bridge counters as the source of base measurements.
    funnel = dict(summary.get("prediction_funnel", {}))
    funnel.update(coverage.get("prediction_funnel", {}))
    event_log = read_json(session / "review/event_coverage.log", {})
    test1 = read_json(
        (args.test1_session_dir or Path("")) / "review/event_coverage.log", {}
    ) if args.test1_session_dir else {}

    bridge_mode = str(manifest.get("bridge_mode", coverage.get("bridge_mode", "")))
    preflight = str(facts.get("status", "MISSING"))
    is_mock = str(facts.get("debugfs_root", "")).startswith("/tmp/")
    implementation_checks = all(
        [
            int(manifest.get("automation_rc", 1)) == 0,
            int(manifest.get("monitor_rc", 1)) == 0,
            int(manifest.get("event_coverage_rc", 1)) == 0,
            int(manifest.get("sink_coverage_rc", 1)) == 0,
            coverage.get("status") in {"PASS", "RUNTIME_BLOCKED"},
            sink.get("status") in {"PASS", "RUNTIME_BLOCKED"},
        ]
    )
    real_write_proof = (
        bridge_mode == "shadow-write"
        and preflight == "READY"
        and not is_mock
        and int(summary.get("app_bind_success", 0)) > 0
        and int(summary.get("app_prior_success", 0)) > 0
        and int(summary.get("snapshot_updates_observed", 0)) > 0
    )
    if real_write_proof and implementation_checks:
        final_status = "PARP_APP_LSTM_SINK_COMPLETE"
    elif implementation_checks:
        final_status = "PARP_APP_LSTM_SINK_IMPLEMENTED_RUNTIME_BLOCKED"
    else:
        final_status = "PARP_APP_LSTM_SINK_INCOMPLETE"

    event_counts = event_log.get("event_counts", {})
    prediction_format = str(funnel.get("prediction_format", coverage.get("prediction_format", "horizon")))
    if prediction_format == "app_probability":
        funnel_description = (
            f"全部应用概率行 {funnel.get('candidate_row_count', 0)} "
            f"→ 有效概率行 {funnel.get('probability_row_count', 0)} "
            f"→ 去除当前 App {funnel.get('current_app_row_count', 0)}、"
            f"保留 `<UNKNOWN>` 前台 batch {funnel.get('unknown_foreground_batch_count', 0)} "
            f"（保留候选行 {funnel.get('unknown_foreground_candidate_row_count_retained', 0)}）、"
            f"重复 {funnel.get('duplicate_row_count', 0)}、非法概率 {funnel.get('invalid_probability_row_count', 0)}、"
            f"非白名单 {funnel.get('non_whitelist_row_count', 0)} "
            f"→ 可下沉候选 {funnel.get('post_filter_candidate_row_count', funnel.get('candidate_row_count_after_filter', 0))} "
            f"→ 整批去重/节流抑制 {funnel.get('duplicate_prediction_batch_count', 0)}+{funnel.get('stale_prediction_batch_count', 0)} batch "
            f"（{funnel.get('suppressed_candidate_row_count', 0)} 行）"
            f"→ `app_prior` 命令 {funnel.get('serialized_prior_command_row_count', funnel.get('prior_command_row_count', 0))}；无 horizon 维度。"
        )
    else:
        funnel_description = (
            f"全部候选行 {funnel.get('candidate_row_count', 0)} → horizon=3 行 "
            f"{funnel.get('target_horizon_row_count', 0)} → 去除当前 App "
            f"{funnel.get('current_app_row_count', 0)}、保留 `<UNKNOWN>` 前台 batch "
            f"{funnel.get('unknown_foreground_batch_count', 0)}（保留候选行 "
            f"{funnel.get('unknown_foreground_candidate_row_count_retained', 0)}）、重复 "
            f"{funnel.get('duplicate_row_count', 0)}、非法概率 "
            f"{funnel.get('invalid_probability_row_count', 0)}、非白名单 "
            f"{funnel.get('non_whitelist_row_count', 0)} → 可下沉候选 "
            f"{funnel.get('candidate_row_count_after_filter', 0)} → `app_prior` 命令 "
            f"{funnel.get('prior_command_row_count', 0)}。"
        )
    model_description = "在线 v3 单步应用切换 LSTM" if prediction_format == "app_probability" else "在线 v2/旧版 LSTM"
    candidate_description = "候选概率行是每个 App 一行，不包含 horizon" if prediction_format == "app_probability" else "候选行是 horizon×candidate，不是推理次数"
    lines = [
        "# Test2：应用间在线 LSTM 预测与 PARP 预测结果下沉",
        "",
        f"最终状态：`{final_status}`",
        "",
        f"本报告只覆盖 Test2 的链路：Runtime Monitor 应用事件 → {model_description} 前向推理 → userspace PARP bridge → `app_bind`/`app_prior` 序列化与写入审计。",
        "本阶段未实现、未修改、未验证 reclaim、MGLRU consumer、`vmscan`、`nr_to_scan`、effective tier 或页面回收行为。",
        "",
        "## 1. 链路结论",
        "",
        "| 链路 | 证据 | 结论 |",
        "|---|---|---|",
        f"| 应用事件 → Runtime Monitor | 自动化动作 {event_log.get('observed_actions', 0)}/{event_log.get('automation_actions_checked', 0)}，APP_OPEN={event_counts.get('APP_OPEN', 0)}，APP_CLOSE={event_counts.get('APP_CLOSE', 0)}，APP_MINIMIZE={event_counts.get('APP_MINIMIZE', 0)}，APP_RESTORE={event_counts.get('APP_RESTORE', 0)} | `{status_mark(event_log.get('status') == 'PASS')}` |",
        f"| 应用事件 → 在线 LSTM | 成功调用 {coverage.get('online_lstm_success_calls', 0)}，成功预测行 {coverage.get('online_lstm_success_prediction_rows', 0)}，预测 ID 关联 {coverage.get('prediction_ids_joined_to_calls', 0)} | `{status_mark(coverage.get('prediction_ids_unique') is True and coverage.get('prediction_ids_joined_to_calls', 0) > 0)}` |",
        f"| LSTM → PARP bridge | bridge 命令 {coverage.get('bridge_command_rows', 0)} 行，`app_bind`={coverage.get('app_bind_command_rows', 0)}，`app_prior`={coverage.get('app_prior_command_rows', 0)} | `{status_mark(coverage.get('status') == 'PASS')}` |",
        f"| bridge → 真实 PARP snapshot | 当前 preflight=`{preflight}`，真实 debugfs 写入模式=`{bridge_mode}`，snapshot 更新={summary.get('snapshot_updates_observed', 0)} | `{status_mark(real_write_proof, blocked=preflight != 'READY' or bridge_mode != 'shadow-write' or is_mock)}` |",
        "",
        "## 2. 预测与下沉统计",
        "",
        f"- 在线 LSTM 成功调用：{coverage.get('online_lstm_success_calls', 0)}；成功预测行：{coverage.get('online_lstm_success_prediction_rows', 0)}。",
        f"- prediction batch 数：{summary.get('prediction_batch_count', funnel.get('prediction_batch_count', 0))}；模型候选行数：{summary.get('prediction_candidate_row_count', funnel.get('candidate_row_count', 0))}。{candidate_description}。",
        f"- prediction format：`{prediction_format}`。{funnel_description}",
        f"- horizon 分布：{json.dumps(funnel.get('horizon_row_counts', {}), ensure_ascii=False, sort_keys=True)}；本 bridge 不因 App 是否已打开而丢弃候选，`candidate_running` 仅作为审计字段。" if prediction_format != "app_probability" else "- v3 输出是每个白名单 App 一个概率；不生成、不使用 horizon 字段。",
        f"- bridge 接受的有效预测 batch：{summary.get('lstm_predictions_valid', 0)}；未形成有效下沉的预测/事件：{summary.get('lstm_predictions_dropped', 0)}。",
        f"- `app_bind`：逻辑 App {summary.get('bind_logical_app_count', 0)}；解析尝试 {summary.get('bind_resolution_attempts', 0)}；缺失 cgroup {summary.get('bind_missing_cgroup', 0)}；序列化命令 {summary.get('bind_serialized_commands', 0)}；真实写入尝试 {summary.get('bind_write_attempts', 0)}；重试 {summary.get('bind_retry_attempts', 0)}；dry-run 未写入 {summary.get('bind_dry_run_not_attempted', 0)}；接口阻塞 {summary.get('bind_blocked_missing_interface', 0)}。",
        f"- `app_prior`：序列化命令 {summary.get('prior_command_row_count', 0)}；真实写入尝试 {summary.get('prior_write_attempts', 0)}；重试 {summary.get('prior_retry_attempts', 0)}；dry-run 未写入 {summary.get('prior_dry_run_not_attempted', 0)}；接口阻塞 {summary.get('prior_blocked_missing_interface', 0)}。",
        f"- 非法概率：{coverage.get('invalid_probability_rows', 0)}；当前前台应用混入候选：{coverage.get('current_app_in_candidates', 0)}；非白名单候选：{coverage.get('non_whitelist_candidates', 0)}。",
        f"- 去重抑制：{summary.get('duplicate_predictions_suppressed', 0)}；过期/节流抑制：{summary.get('stale_predictions_suppressed', 0)}；队列丢弃：{summary.get('queue_drops', 0)}。",
        "",
        "## 3. 接口与运行环境",
        "",
        f"- 当前内核：`{facts.get('kernel_release', 'unknown')}`。",
        f"- 真实 PARP 根目录：`{facts.get('debugfs_root', 'unknown')}`，preflight：`{preflight}`。",
        f"- `app_bind`：exists={facts.get('app_bind', {}).get('exists', False)}；`app_prior`：exists={facts.get('app_prior', {}).get('exists', False)}。",
        f"- 当前 v4-parp patch 未提供可确认的 `app_prior_batch` parser；本实现只报告该接口，不向它写入：`supported_by_current_patch=false`。",
        "- 写入策略为 fail-closed：真实接口不存在或不可写时不伪造 domain ID、不执行 sudo、不把 userspace write success 当作 kernel snapshot proof。",
        "",
        "## 4. dry-run 与 mock 说明",
        "",
        (
            f"- 当前会话：`{session}`；真实内核 preflight 为 `{preflight}`。"
            "真实 debugfs 写入及其后的 v4.1 snapshot version 更新已逐条确认。"
            if real_write_proof else
            f"- 当前会话：`{session}`；真实内核 preflight 为 `{preflight}`。"
            "userspace 预测链路与命令生成通过，但不能据此声称真实内核 snapshot 已更新。"
        ),
        f"- 是否为 mock shadow-write：`{('是' if is_mock else '否')}`。若使用 `/tmp` 下的普通文件，它只证明 worker、重试、精确命令格式、写入审计和 mock marker 逻辑，不证明真实内核消费。",
        f"- snapshot 审计：updates={summary.get('snapshot_updates_observed', 0)}，prior matched={summary.get('prediction_to_snapshot_matched', 0)}。",
        "",
        "## 5. 可复核产物",
        "",
        f"- [manifest.json]({session / 'manifest.json'})",
        f"- [kernel_facts.json]({session / 'kernel_facts.json'})",
        f"- [online_prediction_coverage.json]({session / 'review/online_prediction_coverage.json'})",
        f"- [prediction_sink_coverage.json]({session / 'review/prediction_sink_coverage.json'})",
        f"- [prediction_funnel.json]({session / 'review/prediction_funnel.json'})",
        f"- [parp_bridge_events.csv]({session / 'parp/parp_bridge_events.csv'})",
        f"- [parp_bridge_summary.json]({session / 'parp/parp_bridge_summary.json'})",
    ]
    if args.test1_session_dir and test1:
        lines.extend([
            "",
            f"Test1 回归会话：`{args.test1_session_dir}`，事件覆盖状态：`{test1.get('status', 'MISSING')}`。",
        ])
    output = args.output or session / "review/FINAL_REPORT.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(output)
    print(final_status)
    return 0 if final_status != "PARP_APP_LSTM_SINK_INCOMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
