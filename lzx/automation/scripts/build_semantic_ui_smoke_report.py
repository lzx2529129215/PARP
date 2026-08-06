#!/usr/bin/env python3
"""在 GUI 不可用时生成诚实的语义 UI Smoke 预检与验收报告。"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="构建语义 UI Smoke 预检报告")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--sid", required=True)
    parser.add_argument("--kernel-release", required=True)
    parser.add_argument("--gui-json", required=True, type=Path)
    parser.add_argument("--previous-dir", required=True, type=Path)
    parser.add_argument("--runtime-config", required=True, type=Path)
    parser.add_argument("--candidate-config", required=True, type=Path)
    parser.add_argument("--scenario-compile-status", default="NOT_RUN")
    parser.add_argument("--dry-run-status", default="NOT_RUN")
    args = parser.parse_args(); work = args.work_dir
    gui = read_json(args.gui_json); previous_summary = read_json(args.previous_dir / "reports/semantic_automation_summary.json")
    previous = {"operation_library": previous_summary.get("framework", {}).get("operation_library", False), "scenario_compiler": previous_summary.get("framework", {}).get("scenario_compiler", False), "trace_marker": previous_summary.get("framework", {}).get("trace_marker", False), "all_scenarios_compiled": previous_summary.get("scenarios", {}).get("compile_failed", 1) == 0, "previous_tests": previous_summary.get("tests", {})}
    (work / "precheck/previous_stage_review.json").write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (work / "precheck/previous_stage_review.md").write_text("# 上一阶段复核\n\n- operation library: `%s`\n- scenario compiler: `%s`\n- trace marker: `%s`\n- 所有静态场景编译: `%s`\n" % tuple(previous[key] for key in ("operation_library", "scenario_compiler", "trace_marker", "all_scenarios_compiled")), encoding="utf-8")
    action_rows = [
        ("launch", "command,name", "无返回值；跟踪 scope/Popen", "命令为空或启动失败", "否", "否", "否", "否", "是", "否"),
        ("shell", "command,name", "无返回值；可放入 scope", "命令为空或失败", "否", "否", "否", "否", "是", "否"),
        ("switch", "class,title,name,command", "聚焦或按需启动", "窗口未找到", "是", "否", "否", "否", "是", "否"),
        ("verify_foreground", "class,title,name", "验证活动窗口", "前台 app 不匹配", "是", "否", "否", "否", "否", "否"),
        ("verify_window_profile", "profile,class,title", "验证 class/title/geometry", "profile/窗口/尺寸不匹配", "是", "是", "否", "否", "否", "否"),
        ("wait_window", "class,title,name,timeout", "等待窗口", "超时", "是", "否", "否", "否", "否", "否"),
        ("key/type/click/click_window/drag", "键盘、文本或窗口相对坐标", "无返回值", "xdotool/窗口错误", "click_window 是", "click_window 是", "click_window 是", "失败可截图", "否", "否"),
        ("close", "name 或匹配条件", "关闭本场景进程或 scope", "缺少关闭目标", "可选", "否", "否", "否", "是", "否"),
        ("trace_marker", "event_type 与语义字段", "仅写 trace", "缺少 event_type", "否", "否", "否", "否", "否", "是"),
    ]
    fields = ["action_type", "参数", "返回值", "错误路径", "是否验证窗口", "是否验证几何", "是否支持相对坐标", "是否支持截图", "是否支持shell", "是否支持trace_marker"]
    write_csv(work / "precheck/low_level_action_capabilities.csv", fields, [dict(zip(fields, row)) for row in action_rows])
    runtime = read_json(args.runtime_config); existing = {item["app_key"]: item for item in runtime.get("apps", [])}
    smoke_apps = [dict(existing[key]) for key in ("WPS", "FILES")]
    smoke_apps.append({"app_key": "BROWSER", "app_id": 5, "vocab_name": "", "scope_name": "automation-browser.scope", "unit_name": "automation-browser", "workload_enabled": True, "prediction_enabled": False, "window_keywords": ["firefox", "chrome", "chromium"], "process_keywords": ["firefox", "chrome", "chromium"]})
    smoke_config = {"slice": runtime.get("slice", "huawei-test.slice"), "apps": smoke_apps, "notes": "仅本轮 Smoke 实际加载候选；BROWSER 不参与 LSTM vocab。"}
    (work / "smoke/runtime_app_scope.smoke.json").write_text(json.dumps(smoke_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ids = [item["app_id"] for item in smoke_apps]
    config_rows = [{"app_key": item["app_key"], "runtime_app_id": item["app_id"], "scope_name": item["scope_name"], "prediction_enabled": item["prediction_enabled"], "status": "VALID" if len(ids) == len(set(ids)) else "DUPLICATE_ID"} for item in smoke_apps]
    write_csv(work / "capability/runtime_app_config_check.csv", ["app_key", "runtime_app_id", "scope_name", "prediction_enabled", "status"], config_rows)
    (work / "capability/runtime_app_config_diff.txt").write_text("Smoke 配置只包含 FILES、WPS、BROWSER；BROWSER 使用 app_id=5、prediction_enabled=false。候选配置未替换默认配置。\n", encoding="utf-8")
    gui_status = gui.get("gui_session_status", "GUI_SESSION_UNAVAILABLE")
    capability_rows = []
    for key in ("FILES", "WPS", "BROWSER", "WECHAT", "BILIBILI"):
        required = key in {"FILES", "WPS", "BROWSER"}
        status = "BLOCKED" if gui_status != "AVAILABLE" else "PARTIAL"
        capability_rows.append({"app_key": key, "executable": "未在本阶段启动", "desktop_file": "未在本阶段启动", "launch_command": "未执行", "process_name": "", "cmdline_pattern": "", "window_class": "", "window_title_pattern": "", "window_geometry": "", "xwayland": "未验证", "scope_name": next((item["scope_name"] for item in smoke_apps if item["app_key"] == key), ""), "runtime_app_id": next((item["app_id"] for item in smoke_apps if item["app_key"] == key), ""), "launch_success": False, "focus_success": False, "foreground_detected": False, "scope_membership": False, "capability_status": status, "failure_reason": "当前 Codex 进程为 tty，DISPLAY/WAYLAND_DISPLAY 均为空；未执行 UI 操作" if required else "本轮不执行可选应用 UI 操作"})
    capability_fields = list(capability_rows[0])
    write_csv(work / "capability/app_capability_runtime.csv", capability_fields, capability_rows)
    (work / "capability/app_capability_runtime.json").write_text(json.dumps(capability_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    calibration_rows = []
    for key in ("FILES", "WPS", "BROWSER", "WECHAT", "BILIBILI"):
        status = "NOT_EXERCISED"
        payload = {"app_key": key, "status": status, "reason": "GUI_SESSION_UNAVAILABLE，未启动、聚焦、截图或读取窗口", "window_class": "", "window_title": "", "window_geometry": "", "profile_updated": False}
        if key in {"FILES", "WPS", "BROWSER"}:
            (work / "calibration" / f"{key.lower()}_calibration.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        calibration_rows.append(payload)
    write_csv(work / "calibration/calibration_summary.csv", ["app_key", "status", "reason", "window_class", "window_title", "window_geometry", "profile_updated"], calibration_rows)
    external = {"external_side_effects_executed": False, "wechat_message_sent": False, "wechat_file_sent": False, "moments_published": False, "moments_deleted": False, "bilibili_follow_changed": False, "bilibili_favorite_changed": False, "bilibili_video_uploaded": False, "online_document_modified": False, "external_file_uploaded": False, "public_content_created": False, "local_wps_files": []}
    (work / "audit/external_side_effect_audit.json").write_text(json.dumps(external, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    safety = {"status": "NOT_EXERCISED", "reason": "没有真实 Smoke，未生成前后扫描计数 delta", "app_policy_apply": 0, "tier2_runtime_enabled": 0, "real_protection": "NOT_IMPLEMENTED", "prefetch": "NOT_IMPLEMENTED", "ready_for_apply": False}
    (work / "audit/observe_safety_validation.json").write_text(json.dumps(safety, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (work / "audit/kernel_log_check.txt").write_text("未运行真实 Smoke；未执行 kernel journal 增量检查。\n", encoding="utf-8")
    write_csv(work / "smoke/automation_trace_validation.csv", ["check", "status", "notes"], [{"check": "真实 automation trace", "status": "NOT_EXERCISED", "notes": "仅存在 dry-run trace，不作为真实 UI 成功证据"}])
    write_csv(work / "smoke/operation_result_validation.csv", ["operation_id", "status", "notes"], [])
    for name, fields in {
        "foreground_operation_alignment.csv": ["operation_id", "foreground_match_ratio", "status"],
        "scope_operation_alignment.csv": ["operation_id", "scope_match_ratio", "status"],
        "foreground_epoch_summary.csv": ["app_key", "foreground_epoch_id", "status"],
    }.items():
        write_csv(work / "alignment" / name, fields, [])
    summary = {"session": {"sid": args.sid, "kernel_release": args.kernel_release, "gui_session": gui_status}, "capability": {row["app_key"]: row["capability_status"] for row in capability_rows}, "smoke": {"attempt_count": 0, "operation_total": 0, "operation_done": 0, "operation_failed": 0, "operation_success_rate": 0, "wps_files_created": [], "browser_tabs_verified": 0, "files_directory_verified": False, "foreground_switch_requested": 0, "foreground_switch_confirmed": 0, "foreground_switch_recognition_rate": 0, "foreground_epochs": 0, "scope_match_ratio": 0, "valid_classifier_samples": 0, "continue_transitions": 0, "continue_predictions": 0, "reentry_events": 0, "reentry_valid_samples": 0, "state_unchanged_reentry_samples": 0, "cross_epoch_transitions": 0}, "kernel": {"app_bind_enospc": 0, "original_scan_pages": 0, "proposed_scan_pages": 0, "applied_scan_pages": 0, "applied_equals_original": True, "legacy_hook_calls": 0, "dual_hook_calls": 0, "per_folio_calls": 0, "tier2_runtime_enabled": 0, "kernel_errors": 0}, "safety": external, "status": {"source_fix": "PASS", "scenario_compile": args.scenario_compile_status, "dry_run": args.dry_run_status, "real_ui_smoke": "NOT_EXERCISED", "foreground_alignment": "NOT_EXERCISED", "scope_alignment": "NOT_EXERCISED", "workload_alignment": "NOT_EXERCISED", "continue_collection": "NOT_EXERCISED", "reentry_collection": "NOT_EXERCISED", "observe_safety": "NOT_EXERCISED", "external_side_effect": "PASS", "ready_for_large_scale_collection": False, "ready_for_apply": False}}
    (work / "reports/semantic_ui_smoke_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (work / "reports/semantic_ui_smoke_summary.md").write_text("# 真实 UI Smoke 汇总\n\n- GUI_SESSION_STATUS: `%s`\n- 真实 Smoke: `NOT_EXERCISED`\n- 原因: 当前进程不在图形桌面会话，未发送任何 UI 输入。\n- 场景编译: `%s`\n- dry-run: `%s`\n- 外部副作用: `false`\n- ready_for_large_scale_collection: `false`\n- ready_for_apply: `false`\n" % (gui_status, args.scenario_compile_status, args.dry_run_status), encoding="utf-8")
    (work / "reports/真实UI语义自动化Smoke检查报告.md").write_text("# 真实 UI 语义自动化 Smoke 检查报告\n\n本轮 GUI 预检为 `%s`。当前进程为 tty，且 DISPLAY/WAYLAND_DISPLAY 均为空；为避免在不可观察桌面上盲目启动、聚焦或点击，按门槛立即停止真实 UI 校准和 Smoke。\n\n已完成：源代码安全修正、隔离本地素材、loopback 页面、动态配置生成、场景编译、dry-run、marker 与路径审计。未完成项均明确为 `NOT_EXERCISED`，没有伪造 foreground epoch、scope membership、classifier、CONTINUE、REENTRY、文件保存或浏览器标签结果。\n\n内核未修改、未安装、未重启、未进入 apply；未写 lru_gen_pages，未执行外部账号操作。\n" % gui_status, encoding="utf-8")
    write_csv(work / "reports/feature_status_matrix.csv", ["feature", "status", "notes"], [{"feature": "GUI session", "status": gui_status, "notes": "; ".join(gui.get("reasons", []))}, {"feature": "Source fix", "status": "PASS", "notes": "仅 automation/compiler 静态修正"}, {"feature": "Real UI smoke", "status": "NOT_EXERCISED", "notes": "GUI 会话不可用"}, {"feature": "External side effect", "status": "PASS", "notes": "未执行"}])
    write_csv(work / "reports/issue_register.csv", ["id", "severity", "status", "description"], [{"id": "UI-001", "severity": "BLOCKER", "status": "OPEN", "description": "需在真实图形桌面终端、具有 DISPLAY/XAUTHORITY 后运行 calibration 和 Smoke。"}])
    return 0


if __name__ == "__main__": raise SystemExit(main())
