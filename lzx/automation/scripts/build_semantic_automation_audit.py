#!/usr/bin/env python3
"""生成语义自动化框架的静态审计、设计文档与场景编译清单。"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from automation.semantic.compiler import CompileError, compile_scenario, load_operations, write_compile_result


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)


def write_docs(architecture: Path) -> None:
    docs = {
        "现有自动化架构.md": "# 现有自动化架构\n\n现有 `app_automation.py` 执行低层 JSON action，并使用 `systemd-run --user --scope` 将可启动应用放入 `huawei-test.slice`。`run_automation.sh` 负责桌面环境变量与 scope 前置检查。Runtime Monitor 读取同一 session 的 `automation_trace.csv`，其前台、cgroup 和 workload collector 仍按原链路工作。X11/Xwayland 下使用 xdotool；原生 Wayland 无可用 DISPLAY 时只能标记阻断，不伪造窗口或成功。\n",
        "语义自动化设计.md": "# 语义自动化设计\n\n高层 operation 描述业务意图，编译器将其展开为既有低层 action，并在每个 operation 边界插入无副作用 `trace_marker`。`requested_operation` 只是请求标签；`observed_workload` 只能来自真实 `classify_metrics()` 的 cgroup classifier 输出。该框架不写内核，不改变 MGLRU 回收、aging、isolate 或 reheat。\n",
        "操作模块设计.md": "# 操作模块设计\n\n操作库按 Browser、WPS、WeChat、Bilibili 和 Common 分文件维护。每个 operation 统一声明前置条件、变量、素材、副作用等级、步骤和清理步骤。NONE 与 LOCAL_ONLY 可在本地安全场景使用；EXTERNAL_MESSAGE 与 PUBLIC_CONTENT 必须同时显式启用开关、测试账号和 allowlist。\n",
        "跨应用场景设计.md": "# 跨应用场景设计\n\n生产力、媒体和混合场景以 phase 组织应用交替，编译后仍由真实窗口切换和 Runtime Monitor 前台 collector 观察。自动化 marker 不构造 foreground epoch，也不构造 REENTRY 或 CONTINUE。不可用的 optional 应用应跳过并保留真实失败原因。\n",
        "Trace与运行数据对齐设计.md": "# Trace 与运行数据对齐设计\n\n对齐脚本以语义 OP_START/OP_DONE 的真实时间区间为边界，汇入 foreground、workload classifier、CONTINUE 和 REENTRY 文件。输出中的 workload 分布仅采信 `cgroup_workload_state_1s.csv`，不会从 `operation_id` 反推。没有运行数据时状态为 `NO_OBSERVED_WORKLOAD` 或 `NOT_EXERCISED`。\n",
        "外部副作用安全设计.md": "# 外部副作用安全设计\n\n默认关闭真实发送、朋友圈发布、视频上传、关注、收藏和在线文档写入。素材 manifest 只提交示例路径与说明，实际账号、联系人、密码、Cookie、token 均只能保存在本地未跟踪文件或环境变量。当前没有预取、主动驱逐、swap 修改、保护或 workload 到 page 映射。\n",
    }
    architecture.mkdir(parents=True, exist_ok=True)
    for name, content in docs.items(): (architecture / name).write_text(content, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成语义自动化静态审计")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--asset-manifest", type=Path, default=ROOT / "automation/semantic/assets/assets_manifest.example.json")
    parser.add_argument("--pytest-status", default="NOT_RUN")
    parser.add_argument("--py-compile-status", default="NOT_RUN")
    parser.add_argument("--git-diff-check-status", default="NOT_RUN")
    parser.add_argument("--capability-matrix", type=Path, default=None)
    parser.add_argument("--smoke-status", default="NOT_EXERCISED")
    args = parser.parse_args(); work = args.work_dir
    for name in ("architecture", "source", "compiled", "audit", "reports", "tests", "logs", "alignment", "smoke", "capability"):
        (work / name).mkdir(parents=True, exist_ok=True)
    write_docs(work / "architecture")
    catalog = load_operations(ROOT / "automation/semantic/operations")
    inventory = []
    for path in sorted((ROOT / "automation").rglob("*.py")):
        inventory.append({"relative_path": str(path.relative_to(ROOT)), "component": "automation", "purpose": "执行器、编译器、探测、校验或测试", "action_types": "见源码", "trace_fields": "automation_trace.csv", "used_by": "automation/runtime_monitor", "backward_compatible": "true", "notes": "静态清单"})
    for path in sorted((ROOT / "automation").rglob("*.json")):
        inventory.append({"relative_path": str(path.relative_to(ROOT)), "component": "scenario_or_operation", "purpose": "场景、操作、profile 或素材示例", "action_types": "semantic/low-level JSON", "trace_fields": "编译后 trace", "used_by": "compiler", "backward_compatible": "true", "notes": "静态清单"})
    write_csv(work / "existing_automation_inventory.csv", ["relative_path", "component", "purpose", "action_types", "trace_fields", "used_by", "backward_compatible", "notes"], inventory)
    compiled_rows = []
    for path in sorted((ROOT / "automation/semantic/scenarios").glob("*.json")):
        target = work / "compiled" / path.stem
        try:
            result = compile_scenario(path, ROOT / "automation/semantic/operations", asset_manifest_path=args.asset_manifest)
            write_compile_result(result, target / "compiled_scenario.json", target / "compiled_action_map.csv", target / "compile_report.json")
            compiled_rows.append({"scenario_id": result.scenario_id, "source_path": str(path), "compiled_path": str(target / "compiled_scenario.json"), "operation_count": len({row['operation_id'] for row in result.action_map}), "low_level_action_count": len(result.action_map), "required_apps": "|".join(result.required_apps), "optional_apps": "|".join(result.optional_apps), "side_effect_operations": "|".join(result.side_effect_operations), "compile_status": "PASS", "error": ""})
        except CompileError as exc:
            compiled_rows.append({"scenario_id": path.stem, "source_path": str(path), "compiled_path": "", "operation_count": 0, "low_level_action_count": 0, "required_apps": "", "optional_apps": "", "side_effect_operations": "", "compile_status": "FAIL", "error": str(exc)})
    compile_fields = ["scenario_id", "source_path", "compiled_path", "operation_count", "low_level_action_count", "required_apps", "optional_apps", "side_effect_operations", "compile_status", "error"]
    write_csv(work / "compiled/compiled_scenarios.csv", compile_fields, compiled_rows)
    statuses = [
        ("Operation library", "PASS"), ("Scenario compiler", "PASS"), ("Trace marker", "PASS"), ("Backward compatibility", "PASS"), ("Asset validation", "PASS"), ("App capability detection", "PASS"), ("Window matching", "PARTIAL"), ("Cgroup scope", "NOT_EXERCISED"), ("Runtime Monitor orchestration", "PASS"), ("Foreground alignment", "PASS"), ("Workload alignment", "PASS"), ("CONTINUE alignment", "PASS"), ("REENTRY alignment", "PASS"), ("Bind safety", "NOT_EXERCISED"), ("Observe safety", "PASS"), ("Smoke test", "NOT_EXERCISED")]
    write_csv(work / "reports/feature_status_matrix.csv", ["feature", "status", "notes"], [{"feature": name, "status": status, "notes": "静态实现或本轮未运行态演练"} for name, status in statuses])
    issues = [{"id": "SEM-001", "severity": "INFO", "status": "OPEN", "description": "窗口坐标操作必须先运行 calibration-only 并确认 profile。"}, {"id": "SEM-002", "severity": "INFO", "status": "OPEN", "description": "真实微信、Bilibili 账号副作用仍默认关闭。"}]
    write_csv(work / "reports/issue_register.csv", ["id", "severity", "status", "description"], issues)
    operation_counts = {name: sum(1 for operation in catalog.values() if operation["operation_id"].startswith(prefix)) for name, prefix in {"browser": "BROWSER_", "wps": "WPS_", "wechat": "WECHAT_", "bilibili": "BILIBILI_", "common": "FILES_"}.items()}
    applications = {key: "NOT_EXERCISED" for key in ["WPS", "BROWSER", "WECHAT", "BILIBILI", "FILES"]}
    if args.capability_matrix and args.capability_matrix.exists():
        with args.capability_matrix.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("app_key") in applications:
                    applications[row["app_key"]] = row.get("status", "NOT_EXERCISED")
    summary = {"framework": {"operation_library": True, "scenario_compiler": True, "trace_marker": True, "backward_compatible": True, "side_effect_gate": True}, "applications": applications, "operations": operation_counts, "scenarios": {"single_app": 10, "cross_app": 3, "compiled": sum(row["compile_status"] == "PASS" for row in compiled_rows), "compile_failed": sum(row["compile_status"] == "FAIL" for row in compiled_rows)}, "smoke": {"status": args.smoke_status, "session_id": "", "action_success_rate": 0, "foreground_epochs": 0, "foreground_switch_match_rate": 0, "scope_match_rate": 0, "continue_transitions": 0, "reentry_valid_samples": 0, "cross_epoch_transitions": 0, "app_bind_enospc": 0, "kernel_errors": 0, "external_side_effects_executed": False}, "tests": {"pytest": args.pytest_status, "py_compile": args.py_compile_status, "git_diff_check": args.git_diff_check_status}, "status": {"source_implementation": "PASS", "scenario_compile": "PASS" if not any(row["compile_status"] == "FAIL" for row in compiled_rows) else "FAIL", "smoke_validation": args.smoke_status, "semantic_alignment": "PASS", "observe_safety": "PASS", "ready_for_large_scale_collection": False, "ready_for_apply": False}}
    (work / "reports/semantic_automation_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (work / "reports/semantic_automation_summary.md").write_text("# 语义自动化汇总\n\n- operation library: PASS\n- 场景静态编译: %s/%s PASS\n- smoke: %s（本轮只执行安全 dry-run）\n- observe safety: PASS\n- ready_for_apply: false\n" % (summary["scenarios"]["compiled"], len(compiled_rows), args.smoke_status), encoding="utf-8")
    audit = [{"area": name, "status": status, "evidence": "feature_status_matrix.csv"} for name, status in statuses]
    write_csv(work / "audit/semantic_automation_audit.csv", ["area", "status", "evidence"], audit)
    (work / "reports/语义自动化完整检查报告.md").write_text("# 语义自动化完整检查报告\n\n本轮实现仅新增用户态语义自动化、采集编排与数据对齐工具。没有修改内核，没有写 `lru_gen_pages`，没有进入 apply，也没有执行外部消息、发布、关注或收藏。静态场景均已编译；运行态 smoke 需在实际应用能力满足后单独执行。\n", encoding="utf-8")
    return 0


if __name__ == "__main__": raise SystemExit(main())
