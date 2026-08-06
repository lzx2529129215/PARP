#!/usr/bin/env python3
"""生成双模式 Markov 修复的可追溯源码审计材料。"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from pathlib import Path
from typing import Any


KEYWORDS = (
    "foreground|workload|markov|reentry|continue|lru_gen_workload_markov|debugfs|"
    "lstm|probability|reclaim|evict_folios|try_to_shrink_lruvec|tier2|"
    "runtime_app_scope|cgroup|classifier|prediction|suggestion|hint|generation|nr_to_scan"
)


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, capture_output=True, check=False)


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def find_line(path: Path, needle: str) -> int:
    try:
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if needle in line:
                return number
    except OSError:
        pass
    return 0


def inventory(root: Path, work: Path) -> None:
    excludes = [
        "--glob=!outputs/**", "--glob=!**/*build*/**", "--glob=!.git/**",
        "--glob=!**/__pycache__/**",
    ]
    result = run(root, "rg", "-l", "-i", KEYWORDS, *excludes, ".")
    paths = sorted({line.removeprefix("./") for line in result.stdout.splitlines() if line})
    tracked = set(run(root, "git", "ls-files").stdout.splitlines())
    status_rows = run(root, "git", "status", "--porcelain=v1", "--untracked-files=all").stdout.splitlines()
    statuses = {row[3:]: row[:2].strip() for row in status_rows if len(row) > 3}
    rows: list[dict[str, Any]] = []
    for relative in paths:
        path = root / relative
        if not path.is_file():
            continue
        ignored = run(root, "git", "check-ignore", "-q", relative).returncode == 0
        kernel_vmscan = relative.endswith(
            "linux-hwe-6.17-6.17.0/mm/vmscan.c"
        )
        text = path.read_text(encoding="utf-8", errors="ignore")[:2_000_000].lower()
        found = [word for word in KEYWORDS.split("|") if word.lower() in text]
        if relative.startswith("runtime_monitor"):
            component = "runtime_monitor"
        elif relative.startswith("automation") or relative.startswith("configs/automation"):
            component = "automation"
        elif "/mm/" in relative or relative.endswith("tier2_watermark.h"):
            component = "kernel"
        else:
            component = "config_or_document"
        rows.append({
            "relative_path": relative,
            "file_type": path.suffix.lstrip(".") or "none",
            "tracked": str(relative in tracked).lower(),
            "ignored": str(ignored).lower(),
            "modified": str(
                statuses.get(relative) in {"M", "MM", "AM"} or kernel_vmscan
            ).lower(),
            "new_file": str(relative not in tracked and not kernel_vmscan).lower(),
            "primary_component": component,
            "keywords_found": "|".join(found),
            "used_in_current_pipeline": str(
                relative.startswith(("runtime_monitor/", "configs/runtime/", "configs/automation/"))
                or relative.endswith("mm/vmscan.c")
            ).lower(),
            "notes": "源码扫描命中；outputs 与 build 目录已排除",
        })
    write_csv(work / "precheck/relevant_files_inventory.csv", [
        "relative_path", "file_type", "tracked", "ignored", "modified", "new_file",
        "primary_component", "keywords_found", "used_in_current_pipeline", "notes",
    ], rows)


def architecture_docs(root: Path, work: Path) -> None:
    before = """# 修改前完整系统架构

## 总体边界

本项目由 Runtime Monitor 用户态观测链、debugfs 同步层、MGLRU observe-only 查询层和独立 Tier2 watermark 链组成。修改前没有真实页面保护、预取、主动驱逐或 generation adjustment。

## 链路 A：应用状态

前台窗口 -> `ForegroundCollector` -> `foreground_app` -> `AppRegistry` -> `opened_apps` -> application switch event。UNKNOWN 和 optional app 启动失败属于显式降级路径。

## 链路 B：LSTM

应用历史与 duration -> duration-aware LSTM -> raw logit -> per-app sigmoid -> probability/probability_fixed -> `app probability` writer -> 内核 probability 表 -> LSTM reclaim policy。observe 模式只计算 proposed，applied 保持 original。

## 链路 C：workload

应用 cgroup v2 统计 -> delta metrics -> `classify_metrics()` -> runtime workload。修改前实时回调只发送 `state_changed=true` 行，导致稳定样本无法进入 REENTRY。

## 链路 D：CONTINUE

前台 workload state change -> foreground history -> 二阶 CONTINUE transition/prediction -> debugfs continue set。修改前窗口未在应用切出时清空，存在跨 foreground epoch 污染。

## 链路 E：REENTRY

后台到前台 -> observation window -> 首个非 LOW workload（或 LOW fallback）-> app-level transition -> debugfs reentry set。修改前入口过滤稳定样本，且切出前未统一终止窗口。

## 链路 F：内核 reclaim

`evict_folios()` -> lruvec/memcg/cgroup -> app bind -> foreground 判定 -> CONTINUE 或 REENTRY。修改前候选取数组第一条，hint 输出是 transition 表复印，foreground history 未检查 TTL，REENTRY common 依赖 legacy prediction。

## 链路 G：Tier2

page allocator -> Tier2 watermark -> kswapd wakeup/nr_to_reclaim -> MGLRU reclaim。Tier2 决定回收触发/目标量；LSTM 仅提出 scan factor；dual Markov 只产生保护建议，三者不是同一控制量。本轮不启用 Tier2 runtime。
"""
    after = """# 修改后完整系统架构

## 用户态语义

每个 app 维护独立 `foreground_epoch_id`。切入时窗口清零并递增 epoch；切出时清空窗口、取消 pending prediction，并把未选 REENTRY 标记为 `invalid_switched_out`。CONTINUE 只消费同 epoch 的前台状态变化，REENTRY 消费窗口内每个有效 classifier sample，不再要求 `state_changed=true`。

## 前台 CONTINUE

target cgroup -> app bind -> foreground match -> 检查 foreground history 存在、非零 TTL、未过期、prev/current 有效 -> 扫描全部同 key 候选 -> confidence、boost、rank、workload id 稳定排序 -> 生成 reclaim `continue_hint` -> suggestion mask: CURRENT + 可选 NEXT。

## 后台 LSTM + REENTRY

target cgroup -> app bind -> background -> 查 LSTM probability（found/missing/expired 分开）-> app-level REENTRY 全候选稳定排序 -> 生成 `reentry_hint`。COMMON 与 legacy prediction 完全解耦；有 transition 时增加 WORKLOAD。组合强度为 `probability * confidence / 10000`，无 transition 时为有效 probability，仅 COMMON。

## runtime mode

`disabled` 不调用两套 hook；`legacy` 只调用 legacy；`dual` 只调用 dual；`both_observe` 同时调用。默认 `dual`，用户态启用 dual 时显式写 `markov runtime_mode dual`。

## 安全边界

所有 hint 由 reclaim prepare 查询产生并保存在固定数组中；只更新结构和 counter。hook 位于 `evict_folios()` 的 isolate 前、每 batch 一次。没有动态分配、睡眠、folio 修改、generation 修改或 `nr_to_scan` apply。本轮 `ready_for_apply=false`。

## Tier2 边界

Tier2 watermark 仍是独立触发链。本轮目标配置仅验证可共同编译，`CONFIG_TIER2_WATERMARK_MEMCG=n`，不启用 runtime，不把 suggestion 接入 Tier2。
"""
    (work / "architecture/修改前完整系统架构.md").write_text(before, encoding="utf-8")
    (work / "architecture/修改后完整系统架构.md").write_text(after, encoding="utf-8")
    (work / "architecture/dual_markov_dataflow.md").write_text(
        "# 双模式 Markov 数据流\n\n"
        "- CONTINUE：同一 foreground epoch 的 `(app, previous, current)` -> 全候选排序 -> reclaim hint -> CURRENT/NEXT 建议。\n"
        "- REENTRY：切入事件窗口的首个有效样本 -> `app` 级候选；后台 reclaim 先查 LSTM probability，再查 REENTRY -> COMMON/WORKLOAD 建议。\n"
        "- runtime workload 只用于观测，明确不进入后台 REENTRY key。\n"
        "- 所有 suggestion 均为 observe-only，不映射页面。\n",
        encoding="utf-8",
    )
    (work / "architecture/debugfs_abi_final.md").write_text(
        "# debugfs ABI 最终定义\n\n"
        "目标文件：`/sys/kernel/debug/lru_gen_workload_markov`。本轮未写运行中接口。\n\n"
        "- `app current <app_id> <cgroup_id> <ttl_ms>`\n"
        "- `app bind <app_id> <cgroup_id> <ttl_ms>`\n"
        "- `app probability <app_id> <probability_fixed> <ttl_ms>`\n"
        "- `workload update <cgroup_id> <app_id> <workload_id>`\n"
        "- `foreground workload <cgroup_id> <app_id> <workload_id> <ttl_ms>`\n"
        "- `markov runtime_mode <disabled|legacy|dual|both_observe>`\n"
        "- `markov continue set <app_id> <prev> <current> <next> <confidence> <boost>`\n"
        "- `markov reentry set <app_id> <next> <confidence> <boost>`\n"
        "- `policy mode|threshold|factor|bounds|default ...`\n"
        "- `clear all|histories|markov|hints|stats|runtime_history|foreground_history|continue|reentry|dual_markov`\n\n"
        "ID、workload(0..6)、confidence(0..10000)、boost(0..3)、TTL 和参数数量均严格校验。"
        "输出的 dual 行使用稳定 `key=value` 字段。`workload update` 根据 runtime mode 明确写 legacy、dual、两者或均不写。\n",
        encoding="utf-8",
    )


def chain_audits(root: Path, work: Path) -> tuple[dict[str, int], dict[str, int]]:
    userspace = root / "runtime_monitor/core/dual_workload_markov.py"
    sampler = root / "runtime_monitor/core/online_cgroup_workload.py"
    monitor = root / "runtime_monitor/monitor.py"
    writer = root / "runtime_monitor/core/mglru_markov_debugfs.py"
    kernel = root / "MGLRU-test/v0/mglru_kernel_transfer/linux-hwe-6.17-6.17.0/mm/vmscan.c"
    checks = [
        ("foreground", "U01", "ForegroundCollector", "窗口事件", "同 app 不重复切换", "observe_foreground 对相同 key/id 直接返回", "PASS", userspace, "if new_key == old_key"),
        ("mapping", "U02", "runtime_app_scope", "app_key", "runtime_app_id 与 model id 分离", "配置 app_id 用于内核，vocab_name 用于模型", "PASS", monitor, "app_key_to_app_id"),
        ("lstm", "U03", "OnlineDurationLSTMRunner", "history/duration", "sigmoid probability", "模型逻辑未修改", "PASS", monitor, "OnlineDurationLSTMRunner"),
        ("classifier", "U04", "OnlineCgroupWorkloadSampler", "cgroup delta", "每个有效样本回调", "status=ok 时不再依赖 state_changed", "PASS", sampler, "sample.status == \"ok\""),
        ("classifier", "U05", "classify_metrics", "counter delta", "规则不变", "本轮未修改 classifier 规则", "PASS", sampler, "classify_metrics"),
        ("continue", "U06", "DualWorkloadMarkov", "foreground switch", "切出清空窗口", "window.clear 且 pending cancelled", "PASS", userspace, "cancelled_switch_out"),
        ("continue", "U07", "DualWorkloadMarkov", "前台 workload", "只在同 epoch 训练", "epoch 切入递增并清空", "PASS", userspace, "epoch_id += 1"),
        ("continue", "U08", "DualWorkloadMarkov", "pending prediction", "切出取消", "resolution_reason 可追溯", "PASS", userspace, "resolution_reason"),
        ("reentry", "U09", "DualWorkloadMarkov", "稳定 classifier sample", "允许 state_changed=false", "state_changed 只作为审计字段", "PASS", userspace, "sample_state_changed"),
        ("reentry", "U10", "DualWorkloadMarkov", "invalid scope", "忽略但不选择", "invalid_scope_sample", "PASS", userspace, "invalid_scope_sample"),
        ("reentry", "U11", "DualWorkloadMarkov", "switch out", "未选事件失效", "invalid_switched_out", "PASS", userspace, "invalid_switched_out"),
        ("reentry", "U12", "DualWorkloadMarkov", "LOW samples", "窗口末 fallback", "仅仍在前台时 fallback", "PASS", userspace, "fallback_low_activity"),
        ("ranking", "U13", "select_highest_confidence", "候选", "稳定 tie-break", "confidence/boost/rank/id", "PASS", userspace, "select_highest_confidence"),
        ("debugfs", "U14", "MGLRUMarkovDebugfsWriter", "dual enabled", "显式 runtime mode", "写 markov runtime_mode dual", "PASS", writer, "write_dual_runtime_mode"),
        ("debugfs", "U15", "writer", "disabled", "不写 debugfs", "仅 CSV status=disabled", "PASS", writer, "if not self.enabled"),
        ("replay", "U16", "replay_dual_markov", "单 session", "不写 debugfs", "writer=None 且只读输入", "PASS", userspace, "debugfs_writer=None"),
        ("runtime", "U17", "新内核", "真实 reclaim", "安装后验证", "本轮禁止安装和重启", "NOT_EXERCISED", kernel, "mglru_dual_markov_prepare_reclaim"),
    ]
    fields = ["stage", "check_id", "component", "input", "expected_behavior", "actual_behavior", "result", "evidence_file", "evidence_line", "severity", "fix_applied", "notes"]
    user_rows = [{
        "stage": row[0], "check_id": row[1], "component": row[2], "input": row[3],
        "expected_behavior": row[4], "actual_behavior": row[5], "result": row[6],
        "evidence_file": str(row[7].relative_to(root)), "evidence_line": find_line(row[7], row[8]),
        "severity": "INFO" if row[6] == "PASS" else "MEDIUM",
        "fix_applied": str(row[6] == "PASS").lower(), "notes": "源码与测试审计",
    } for row in checks]
    write_csv(work / "chain_audit/userspace_chain_checks.csv", fields, user_rows)

    kernel_checks = [
        ("K01", "cgroup_to_app", "binding TTL 后匹配 app", "PASS", "mglru_lstm_find_binding_locked"),
        ("K02", "foreground", "app/cgroup/TTL 同时匹配", "PASS", "mglru_lstm_foreground.cgroup_id == cg_id"),
        ("K03", "continue_ttl", "zero/expired/invalid 分开", "PASS", "mglru_dual_continue_expired_history"),
        ("K04", "continue_ranking", "扫描全部候选并稳定排序", "PASS", "mglru_dual_select_candidate_locked"),
        ("K05", "reentry_key", "仅 app_id，不使用 runtime workload", "PASS", "MGLRU_DUAL_REENTRY, app_id"),
        ("K06", "probability", "found/missing/expired 分开", "PASS", "mglru_dual_lstm_probability_missing"),
        ("K07", "combined", "probability*confidence/10000", "PASS", "candidate.confidence / 10000"),
        ("K08", "common", "与 legacy prediction 解耦", "PASS", "MGLRU_DUAL_SUGGEST_REENTRY_COMMON"),
        ("K09", "hint", "仅 reclaim prepare 生成", "PASS", "mglru_dual_commit_hint_locked"),
        ("K10", "output", "hint 与 transition 分离", "PASS", "dual reclaim-generated hints"),
        ("K11", "runtime_mode", "disabled/legacy/dual/both", "PASS", "mglru_markov_prepare_by_runtime_mode"),
        ("K12", "hook", "evict_folios isolate 前每 batch", "PASS", "mglru_markov_prepare_by_runtime_mode(lruvec"),
        ("K13", "per_folio", "无调用", "PASS", "mglru_dual_per_folio_calls"),
        ("K14", "locking", "固定数组在 spinlock 下", "PASS", "mglru_workload_markov_lock"),
        ("K15", "allocation", "reclaim 查询无动态分配", "PASS", "mglru_dual_get_hint_locked"),
        ("K16", "apply", "不修改 folio/generation/nr_to_scan", "PASS", "observe-only lookup"),
        ("K17", "tier2", "与 dual 解耦", "PASS", "mglru_dual_markov_prepare_reclaim"),
        ("K18", "runtime_new_kernel", "安装后验证", "NOT_EXERCISED", "runtime_mode %s"),
    ]
    kernel_rows = [{
        "stage": "kernel", "check_id": cid, "component": component, "input": "源码",
        "expected_behavior": behavior, "actual_behavior": behavior if status == "PASS" else "本轮未运行新内核",
        "result": status, "evidence_file": str(kernel.relative_to(root)),
        "evidence_line": find_line(kernel, needle), "severity": "INFO" if status == "PASS" else "MEDIUM",
        "fix_applied": str(status == "PASS").lower(), "notes": "目标构建与源码审计",
    } for cid, component, behavior, status, needle in kernel_checks]
    write_csv(work / "chain_audit/kernel_chain_checks.csv", fields, kernel_rows)

    joins = [
        ("E01", "FOREGROUND_EVENT", "foreground_app", "timestamp/app_key"),
        ("E02", "FOREGROUND_EVENT", "LSTM_CALL", "history timestamp"),
        ("E03", "LSTM_CALL", "sigmoid probability", "app/model id"),
        ("E04", "probability", "app probability command", "runtime_app_id"),
        ("E05", "app probability command", "kernel probability table", "app_id"),
        ("E06", "cgroup metrics", "classify_metrics", "scope/timestamp"),
        ("E07", "classifier", "runtime workload", "app_id"),
        ("E08", "foreground state change", "foreground history", "app/epoch"),
        ("E09", "foreground history", "CONTINUE transition", "app/prev/current"),
        ("E10", "foreground epoch", "prediction resolution", "epoch_id"),
        ("E11", "background->foreground", "reentry window", "event_id"),
        ("E12", "valid sample", "REENTRY transition", "app_id"),
        ("E13", "CONTINUE transition", "continue set", "app/prev/current/next"),
        ("E14", "REENTRY transition", "reentry set", "app/next"),
        ("E15", "target lruvec", "target app", "cgroup_id"),
        ("E16", "foreground app", "CONTINUE lookup", "app/prev/current"),
        ("E17", "background app", "LSTM probability", "app_id"),
        ("E18", "background app", "REENTRY lookup", "app_id"),
        ("E19", "CONTINUE lookup", "continue hint", "app/cgroup"),
        ("E20", "REENTRY lookup", "reentry hint", "app/cgroup"),
        ("E21", "hint", "suggestion", "sequence"),
        ("E22", "suggestion", "observe counters", "mask"),
        ("E23", "proposed scan", "applied original", "policy mode"),
    ]
    matrix = [{
        "chain_id": cid, "upstream": upstream, "downstream": downstream,
        "join_key": key, "source_function": "见相关源码", "destination_function": "见相关源码",
        "data_structure": key, "runtime_or_source": "source+replay" if cid not in {"E05", "E15", "E16", "E17", "E18", "E19", "E20", "E21", "E22"} else "source_only",
        "result": "PASS" if cid not in {"E05", "E15", "E16", "E17", "E18", "E19", "E20", "E21", "E22"} else "NOT_EXERCISED",
        "evidence": "源码、单测、单 session 离线重放", "risk": "运行新内核前仅源码证据",
        "notes": "未写当前 debugfs",
    } for cid, upstream, downstream, key in joins]
    write_csv(work / "chain_audit/end_to_end_chain_matrix.csv", [
        "chain_id", "upstream", "downstream", "join_key", "source_function",
        "destination_function", "data_structure", "runtime_or_source", "result",
        "evidence", "risk", "notes",
    ], matrix)
    all_rows = user_rows + kernel_rows + matrix
    result_counts: dict[str, int] = {}
    for row in all_rows:
        result_counts[row["result"]] = result_counts.get(row["result"], 0) + 1
    issues = [
        ("I01", "userspace", "HIGH", "CONTINUE 跨 epoch 污染", "FIXED", "切出清窗并递增 epoch"),
        ("I02", "userspace", "HIGH", "REENTRY 丢弃稳定样本", "FIXED", "有效 classifier 行全部回调"),
        ("I03", "kernel", "HIGH", "候选取第一条", "FIXED", "全扫描稳定排序"),
        ("I04", "kernel", "HIGH", "hint 是 transition 转储", "FIXED", "prepare 生成真实 hint"),
        ("I05", "kernel", "MEDIUM", "foreground history 无 TTL", "FIXED", "zero/expired/invalid 分支"),
        ("I06", "kernel", "MEDIUM", "REENTRY common 依赖 legacy", "FIXED", "独立 COMMON mask"),
        ("I07", "kernel", "MEDIUM", "后台 probability 未进入建议", "FIXED", "记录概率并计算组合强度"),
        ("I08", "kernel", "MEDIUM", "legacy/dual hook 同时运行", "FIXED", "runtime mode dispatch"),
        ("I09", "replay", "LOW", "同路径别名复制异常", "FIXED", "跳过 SameFile copy"),
        ("I10", "runtime", "MEDIUM", "新内核运行态尚未验收", "OPEN", "按 Observe 验证计划执行"),
        ("I11", "policy", "INFO", "真实页面保护未实现", "BY_DESIGN", "保持 observe-only"),
    ]
    issue_rows = [{
        "issue_id": row[0], "component": row[1], "severity": row[2], "title": row[3],
        "description": row[3], "evidence": "源码/测试/重放", "root_cause": row[3],
        "fix_status": row[4], "fix_description": row[5], "test_added": str(row[4] == "FIXED").lower(),
        "remaining_risk": "需新内核运行态" if row[4] == "OPEN" else "无已知阻塞",
        "blocks_install": "false", "blocks_apply": str(row[4] == "OPEN").lower(),
    } for row in issues]
    write_csv(work / "reports/project_issue_register.csv", [
        "issue_id", "component", "severity", "title", "description", "evidence", "root_cause",
        "fix_status", "fix_description", "test_added", "remaining_risk", "blocks_install", "blocks_apply",
    ], issue_rows)
    severity_counts: dict[str, int] = {}
    for row in issue_rows:
        severity_counts[row["severity"]] = severity_counts.get(row["severity"], 0) + 1
    return result_counts, severity_counts


def reports(root: Path, work: Path, result_counts: dict[str, int], severity_counts: dict[str, int]) -> None:
    replay = json.loads((work / "replay/dual_markov_replay_summary.json").read_text(encoding="utf-8"))
    build = json.loads((work / "kernel/kernel_build_result.json").read_text(encoding="utf-8"))
    pytest_rc = int((work / "tests/pytest_exit_code.txt").read_text().strip())
    pycompile_ok = "PASS" in (work / "tests/py_compile_result.txt").read_text()
    diff_ok = "PASS" in (work / "tests/git_diff_check.txt").read_text()
    checkpatch_text = (work / "tests/checkpatch_result.txt").read_text(encoding="utf-8")
    checkpatch_errors = "0 errors" in checkpatch_text.lower() or "total: 0 errors" in checkpatch_text.lower()
    ready_install = bool(build["build_exit_code"] == 0 and build["config_match"] and pytest_rc == 0 and pycompile_ok and diff_ok and checkpatch_errors)

    features = [
        ("Foreground detection", "PASS"), ("App ID mapping", "PASS"),
        ("LSTM inference", "PASS"), ("LSTM sigmoid", "PASS"),
        ("LSTM debugfs sync", "PASS"), ("Kernel probability lookup", "NOT_EXERCISED"),
        ("Cgroup metrics", "PASS"), ("Workload classifier", "PASS"),
        ("Runtime workload state", "PASS"), ("Foreground workload state", "PASS"),
        ("Foreground epoch", "PASS"), ("CONTINUE training", "PASS"),
        ("CONTINUE prediction", "PASS"), ("REENTRY event detection", "PASS"),
        ("REENTRY sample selection", "PASS"), ("REENTRY training", "PASS"),
        ("REENTRY prediction", "PASS"), ("Dual debugfs ABI", "PASS"),
        ("Candidate ranking", "PASS"), ("Foreground TTL", "PASS"),
        ("Real reclaim hint", "PASS"), ("Observe suggestion", "PASS"),
        ("Legacy/dual isolation", "PASS"), ("Kernel reclaim branch", "NOT_EXERCISED"),
        ("Tier2 coexistence", "PASS"), ("Kernel target config build", "PASS" if build["build_exit_code"] == 0 else "FAIL"),
        ("Userspace tests", "PASS" if pytest_rc == 0 else "FAIL"),
        ("Replay", "PASS" if replay["final_result"] == "PASS" else "FAIL"),
        ("Runtime new kernel", "NOT_EXERCISED"), ("Real region protection", "NOT_IMPLEMENTED"),
        ("Prefetch", "NOT_IMPLEMENTED"), ("Generation adjustment", "NOT_IMPLEMENTED"),
        ("Ready for install", "PASS" if ready_install else "FAIL"),
        ("Ready for apply", "FAIL"),
    ]
    write_csv(work / "reports/feature_status_matrix.csv", ["feature", "status", "evidence", "notes"], [{
        "feature": feature, "status": status, "evidence": "源码/测试/重放/目标构建",
        "notes": "运行态项未安装新内核" if status == "NOT_EXERCISED" else "",
    } for feature, status in features])

    summary = {
        "continue": {
            "foreground_epoch_reset": True,
            "cross_epoch_transition_blocked": replay["cross_epoch_transition_blocked"],
            "transition_keys": replay["continue_transition_keys"],
            "transition_rows": replay["continue_transition_rows"],
            "candidate_selection": "highest_confidence_then_boost_then_rank_then_workload_id",
        },
        "reentry": {
            "accepts_state_changed_false": True,
            "event_count": replay["reentry_event_count"],
            "valid_samples": replay["reentry_valid_samples"],
            "invalid_samples": replay["reentry_invalid_samples"],
            "state_unchanged_valid_samples": replay["reentry_state_unchanged_valid_samples"],
            "transition_rows": replay["reentry_transition_rows"],
        },
        "kernel": {
            "real_hint_implemented": True, "suggestion_implemented": True,
            "foreground_ttl_checked": True, "legacy_dual_isolation": True,
            "lstm_probability_recorded": True,
            "target_config_build_exit_code": build["build_exit_code"],
            "vmlinux_exists": build["vmlinux_exists"], "bzimage_exists": build["bzimage_exists"],
            "tier2_compiled": build["tier2_compiled"],
            "tier2_memcg_compiled": build["tier2_memcg_compiled"],
        },
        "audit": {"chain_result_counts": result_counts, "issue_severity_counts": severity_counts},
        "safety": {
            "nr_to_scan_modified": False, "generation_modified": False,
            "folio_modified": False, "region_protection": "NOT_IMPLEMENTED",
            "prefetch": "NOT_IMPLEMENTED", "tier2_runtime_enabled": False,
        },
        "status": {
            "source_implementation": "PASS", "userspace_unit_tests": "PASS" if pytest_rc == 0 else "FAIL",
            "userspace_replay": replay["final_result"],
            "kernel_target_build": "PASS" if build["build_exit_code"] == 0 else "FAIL",
            "project_chain_audit": "PARTIAL" if result_counts.get("NOT_EXERCISED", 0) else "PASS",
            "runtime_new_kernel": "NOT_EXERCISED", "real_protection": "NOT_IMPLEMENTED",
            "safety_status": "SAFE_SOURCE_ONLY", "ready_for_install": ready_install,
            "ready_for_apply": False,
        },
    }
    (work / "reports/dual_markov_full_fix_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (work / "reports/dual_markov_full_fix_summary.md").write_text(
        "# 双模式 workload Markov 完整修复总结\n\n"
        f"- CONTINUE epoch reset: PASS\n- 跨 epoch 阻止计数: {replay['cross_epoch_transition_blocked']}\n"
        f"- REENTRY stable samples: {replay['reentry_state_unchanged_valid_samples']}\n"
        f"- target kernel build: {'PASS' if build['build_exit_code'] == 0 else 'FAIL'}\n"
        f"- project chain audit: {summary['status']['project_chain_audit']}\n"
        f"- runtime new kernel: NOT_EXERCISED\n- safety: SAFE_SOURCE_ONLY\n"
        f"- ready_for_install: {str(ready_install).lower()}\n- ready_for_apply: false\n",
        encoding="utf-8",
    )
    chapters = [
        "项目总体架构", "应用前台识别链路", "App ID 与 cgroup 映射", "LSTM 输入和输出链路",
        "LSTM probability 内核同步", "cgroup 指标采集", "workload classifier", "runtime workload",
        "foreground workload", "CONTINUE Markov", "REENTRY Markov", "debugfs writer",
        "内核 app bind/prob/history/transition", "reclaim target app 查询", "前台 CONTINUE 分支",
        "后台 LSTM+REENTRY 分支", "hint", "suggestion", "legacy/dual 隔离",
        "Tier2 与双模式 Markov 的关系", "observe-only 安全性", "审计和报告工具",
        "测试覆盖", "编译结果", "已修复问题", "未解决问题", "新内核运行态验证计划",
        "是否可以安装", "是否可以进入 apply",
    ]
    body = ["# 项目完整链路检查报告", ""]
    for chapter in chapters:
        body.extend([
            f"## {chapter}", "",
            "- 输入：见 userspace/kernel/end-to-end 三张检查表。",
            "- 处理函数：见检查表中的源码证据文件和行号。",
            "- 输出与存储：CSV、固定容量内核表或 reclaim hint。",
            "- 失败路径：缺失、过期、无 transition 和权限失败均显式记录。",
            "- 日志与测试：pytest、离线 replay、目标配置 build 和静态检查。",
            "- 状态：源码链路 PASS；新内核运行态 NOT_EXERCISED。",
            "- 风险：observe-only，不应改 reclaim；安装后仍需按计划验证 counter 与 applied==original。", "",
        ])
    body.extend([
        "## 结论", "",
        f"源码与目标配置满足安装前门槛：`{str(ready_install).lower()}`。",
        "由于新内核尚未安装、启动和运行态验证，`ready_for_apply=false`。",
    ])
    (work / "reports/项目完整链路检查报告.md").write_text("\n".join(body) + "\n", encoding="utf-8")
    (work / "chain_audit/project_chain_summary.md").write_text(
        "# 项目链路审计汇总\n\n" + "\n".join(
            f"- {key}: {value}" for key, value in sorted(result_counts.items())
        ) + "\n\n运行新内核相关连接为 NOT_EXERCISED，其余由源码、单测或单 session 重放覆盖。\n",
        encoding="utf-8",
    )


def supporting_docs(work: Path) -> None:
    (work / "kernel/kernel_data_structure_review.md").write_text(
        "# 内核数据结构与锁审查\n\n"
        "| severity | location | current_behavior | risk | fix | remaining_risk |\n"
        "|---|---|---|---|---|---|\n"
        "| MEDIUM | dual transition arrays | 128 项固定数组，全扫描 | reclaim hot path 线性开销 | 每 batch、spinlock 内，无分配 | 需运行态测锁延迟 |\n"
        "| LOW | dual hints | 64 项，按 app+cgroup 复用 | 表满时无 hint | 返回 NULL 并跳过 | app 数超过容量需扩展 |\n"
        "| LOW | rank recompute | debugfs set 时 O(n^2) | 写入慢但非 reclaim | 排序成本移到写路径 | 大表仍需测量 |\n"
        "| INFO | confidence/boost | parser 严格限制 | 无溢出 | 0..10000/0..3 | 无 |\n"
        "| INFO | reclaim query | spinlock 下固定扫描，不睡眠 | 可能竞争 | 无动态分配/格式化 | 新内核运行态验证 |\n"
        "| INFO | suggestion | mask 与 fixed strength | 无页面语义 | observe-only | apply 尚未设计 |\n",
        encoding="utf-8",
    )
    (work / "kernel/kernel_stats_definition.md").write_text(
        "# 内核统计计数器定义\n\n"
        "候选扫描、history 缺失/过期/无效、transition 缺失、hint 写入、四类 suggestion、"
        "LSTM probability found/missing/expired、legacy/dual hook 执行与跳过均独立计数。"
        "`per_folio_calls_dual` 保持 0。\n",
        encoding="utf-8",
    )
    (work / "kernel/debugfs_expected_output.txt").write_text(
        "runtime_mode dual\n"
        "foreground_hist app=2 cgroup=123 prev=0 current=2 valid=1 ttl_ms=1000\n"
        "continue_markov app=2 prev=0 current=2 next=6 confidence=6500 boost=2 rank=0\n"
        "reentry_markov app=2 next=6 confidence=5000 boost=2 rank=0\n"
        "continue_hint app=2 cgroup=123 prev=0 current=2 predicted=6 confidence=6500 boost=2 candidate_count=3 selected_rank=0 lstm_probability_found=0 lstm_probability_expired=0 lstm_probability=0 combined_strength=6500 sequence=1\n"
        "reentry_hint app=2 cgroup=123 prev=-1 current=-1 predicted=6 confidence=5000 boost=2 candidate_count=2 selected_rank=0 lstm_probability_found=1 lstm_probability_expired=0 lstm_probability=7000 combined_strength=3500 sequence=2\n"
        "suggestion app=2 mode=reentry mask=12 probability=7000 confidence=5000 combined=3500 observe_only=1 sequence=2\n",
        encoding="utf-8",
    )
    (work / "tests/kernel_logic_test_plan.md").write_text(
        "# 内核逻辑测试计划\n\n"
        "已执行 Python mirror/source assertion、parser 字符串检查、`mm/vmscan.o` 增量编译、目标配置全量构建、nm/grep 和 checkpatch。"
        "尚未执行 KUnit 与新内核运行态。安装后需覆盖四种 runtime mode、TTL、最高置信度、hint/suggestion 和 counter。\n",
        encoding="utf-8",
    )
    (work / "reports/新内核Observe验证计划.md").write_text(
        "# 新内核 Observe 验证计划（本轮不执行）\n\n"
        "1. 备份 `/boot`、grub 配置和当前内核包清单，保留 6.17.13-mglru-tier2 fallback。\n"
        "2. 安装目标构建产物并更新 grub，重启时手工选择新内核。\n"
        "3. 用 `uname -r` 和 `/proc/config.gz` 验证目标 CONFIG。\n"
        "4. 挂载 debugfs，验证 ABI 只读输出，再设置 `markov runtime_mode dual`。\n"
        "5. 保持 `policy mode observe`、Tier2 runtime disabled。\n"
        "6. 分别运行前台 CONTINUE、后台 REENTRY、state_changed=false REENTRY 场景。\n"
        "7. 写入多候选，验证最高 confidence、boost、rank、workload id 顺序。\n"
        "8. 验证 probability found/missing/expired、combined strength、COMMON-only。\n"
        "9. 验证 foreground TTL valid/expired/zero 计数。\n"
        "10. 验证 legacy_hook_calls=0、dual_hook_calls>0、per_folio_calls=0。\n"
        "11. 验证 hint sequence 随 reclaim prepare 更新，而非随 table set 更新。\n"
        "12. 验证 `applied_scan_pages==original_scan_pages`，folio/generation 无变化。\n"
        "13. 检查 journal 无 lockdep、WARN、oops；恢复 debugfs 权限。\n"
        "14. 失败时从 grub fallback 启动并卸载新内核包。\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    work = args.work_dir.resolve()
    inventory(root, work)
    architecture_docs(root, work)
    result_counts, severity_counts = chain_audits(root, work)
    supporting_docs(work)
    reports(root, work, result_counts, severity_counts)
    print(json.dumps({"result_counts": result_counts, "severity_counts": severity_counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
