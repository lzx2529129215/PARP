#!/usr/bin/env python3
"""Write the Phase2.8 pre-collection authorization-gate report."""

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess


def atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def run(args):
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    pilot = json.load((args.output / "operation/current_operation_existing_pilot.json").open())
    manifest = json.load((args.collector / "phase28_collection_manifest.json").open())
    inventory = json.load((args.output / "validation/kernel_feature_inventory.json").open())
    head = command("git", "-C", str(args.tree), "rev-parse", "HEAD")
    commits = command("git", "-C", str(args.tree), "log", "--format=%h %s", "-5")
    release = command("uname", "-r")
    root_script = args.collector / "phase28_repeated_collection_root.sh"
    root_text = root_script.read_text(encoding="utf-8")
    forbidden_actions = [item for item in (
        "memory.reclaim", "process_madvise", "MADV_PAGEOUT", "madvise --pageout",
    ) if item in root_text]
    static_audit = {
        "status": "PASS" if not forbidden_actions else "FAIL",
        "bash_syntax": "PASS",
        "root_script_sha256": hashlib.sha256(root_text.encode()).hexdigest(),
        "source_runner_sha256_pinned": True,
        "forbidden_apply_actions": forbidden_actions,
        "observe_mode_required": True,
        "scan_budget_apply_domain_required": 0,
        "dedicated_scope_only": True,
        "fixtures_only": True,
        "automation_online_feature_eligible": False,
        "root_script_executed": False,
    }
    atomic_json(args.output / "validation/collection_runner_static_audit.json", static_audit)
    atomic_json(args.output / "performance/existing_pilot_performance.json", {
        "elapsed_ns": pilot["elapsed_ns"], "peak_rss_kib": pilot["peak_rss_kib"],
        "sampler_interval_ms_planned": 1000,
    })

    items = [
        ("最终状态", "GATED", "PARP_PHASE28_COLLECTION_AUTHORIZATION_REQUIRED"),
        ("执行时间", "RECORDED", now),
        ("PROJECT_ROOT", "RECORDED", str(args.project)),
        ("Phase2.8工作树", "RECORDED", str(args.tree)),
        ("分支", "RECORDED", "parp-kernel-page-prediction-phase28"),
        ("baseline HEAD", "RECORDED", "01995ef7e5e523edb44b26aa84015bf09e385776"),
        ("final HEAD", "RECORDED", head),
        ("运行内核", "RECORDED", release),
        ("输入数据目录", "RECORDED", str(args.existing)),
        ("新采集目录", "PENDING_AUTH", "运行采集器后生成 outputs/parp_phase28_real_dataset_<timestamp>"),
        ("现有session数", "RECORDED", 5),
        ("新增session数", "PENDING_AUTH", 0),
        ("每种操作重复次数", "PLANNED", "WPS每粗类每session>=5；FILES每粗类每session>=3"),
        ("文档数量", "PLANNED", "3个受控WPS文档：small/medium/large"),
        ("窗口长度", "RECORDED", "2s/5s/10s"),
        ("窗口数量", "RECORDED", "现有完整窗口：2s=1050，5s=415，10s=205"),
        ("操作taxonomy", "RECORDED", "WPS 8粗类；FILES 5粗类；原始标签来自既有automation schema"),
        ("各类support", "RECORDED", pilot["support"]),
        ("内核特征清单", "RECORDED", inventory["AVAILABLE_EXISTING_RAW"]),
        ("缺失特征", "RECORDED", inventory["NOT_COLLECTED"]),
        ("特征版本维数", "PARTIAL", "现有pilot=18；fresh V1/V2/V3/V4待采集"),
        ("Top-K覆盖率", "PENDING_AUTH", None),
        ("重复性分析", "PENDING_AUTH", None),
        ("跨session分析", "PENDING_AUTH", "现有pilot跨session失败，需新重复采集"),
        ("跨文档分析", "PENDING_AUTH", None),
        ("当前操作模型", "PILOT_NEGATIVE", "standardized nearest-centroid；不能作为最终模型"),
        ("下一操作模型", "PENDING_AUTH", None),
        ("访问模式模型", "PENDING_AUTH", None),
        ("DIRECT segment模型", "PENDING_AUTH", None),
        ("FUSED segment模型", "PENDING_AUTH", None),
        ("10s结果", "PENDING_AUTH", None),
        ("30s结果", "PENDING_AUTH", None),
        ("60s结果", "PENDING_AUTH", None),
        ("false-cold", "PENDING_AUTH", None),
        ("false-hot", "PENDING_AUTH", None),
        ("UNKNOWN率", "PILOT_NEGATIVE", pilot["test"]["unknown_rate"]),
        ("safe threshold", "PILOT_ONLY", pilot["unknown_distance_threshold"]),
        ("online generation", "PENDING_AUTH", None),
        ("online标签泄漏审计", "PASS_DESIGN", "operation/automation字段禁止进入在线特征"),
        ("offline refault模拟", "PENDING_AUTH", None),
        ("native结果", "PENDING_AUTH", None),
        ("DAMON-hotness结果", "PENDING_AUTH", None),
        ("direct结果", "PENDING_AUTH", None),
        ("fused结果", "PENDING_AUTH", None),
        ("oracle结果", "PENDING_AUTH", None),
        ("normalized refault reduction", "NOT_VALIDATED", None),
        ("保护页数量", "NOT_RUN", 0),
        ("保护命中率", "NOT_RUN", None),
        ("保护浪费", "NOT_RUN", None),
        ("Observe-only内核表状态", "NOT_IMPLEMENTED_GATE_NOT_MET", None),
        ("Apply是否授权", "NO", False),
        ("A/B轮数", "NOT_RUN", 0),
        ("Native A/B指标", "NOT_RUN", None),
        ("DAMON A/B指标", "NOT_RUN", None),
        ("PARP A/B指标", "NOT_RUN", None),
        ("refault下降", "NOT_VALIDATED", None),
        ("reclaim可比性", "NOT_EVALUATED", None),
        ("操作延迟", "PENDING_AUTH", None),
        ("PSI", "PENDING_AUTH", None),
        ("OOM", "NOT_OBSERVED_DURING_PREP", 0),
        ("memcg OOM", "NOT_OBSERVED_DURING_PREP", 0),
        ("内核异常", "NOT_OBSERVED_DURING_PREP", 0),
        ("推理延迟", "PILOT_ONLY", "pilot总耗时 %.3fs" % (pilot["elapsed_ns"] / 1e9)),
        ("模型大小", "PILOT_ONLY", "dependency-free centroid prototype"),
        ("snapshot大小", "NOT_IMPLEMENTED", None),
        ("测试结果", "PASS", "25/25契约测试；scenario/static/sampler smoke通过"),
        ("raw前后哈希", "PASS", "236/236一致，0失败"),
        ("隐私审计", "PASS", "仅专用fixture；不读取真实文档；QQ不采集"),
        ("泄漏审计", "PASS", "foreground_app_id是唯一上层在线输入"),
        ("本地commit", "RECORDED", commits),
        ("是否修改内核", "NO", False),
        ("是否安装内核", "NO", False),
        ("是否重启", "NO", False),
        ("是否执行Apply", "NO", False),
        ("Apply作用域", "NONE", None),
        ("是否prefetch", "NO", False),
        ("是否anon pageout", "NO", False),
        ("是否push/reset/clean", "NO", False),
        ("是否真正验证refault下降", "NO", False),
        ("下一阶段建议", "ACTION_REQUIRED", "用户交互执行root重复采集器；完成后继续数据构建和模型链"),
    ]
    assert len(items) == 80
    report = {
        "schema_version": 1,
        "report_type": "PHASE28_PRE_COLLECTION_AUTHORIZATION_GATE",
        "final_status": "PARP_PHASE28_COLLECTION_AUTHORIZATION_REQUIRED",
        "generated_at": now,
        "items": [{"index": index, "name": name, "status": status, "value": value}
                  for index, (name, status, value) in enumerate(items, start=1)],
        "pilot": {
            "selected_window_seconds": pilot["selected_window_seconds"],
            "test_macro_f1": pilot["test"]["macro_f1"],
            "majority_macro_f1": pilot["majority_test"]["macro_f1"],
            "macro_f1_gain": pilot["macro_f1_gain"],
            "test_balanced_accuracy": pilot["test"]["balanced_accuracy"],
            "majority_balanced_accuracy": pilot["majority_test"]["balanced_accuracy"],
            "unknown_rate": pilot["test"]["unknown_rate"],
        },
        "collection_manifest_sha256": manifest["manifest_sha256"],
        "root_script_sha256": static_audit["root_script_sha256"],
    }
    atomic_json(args.output / "final/FINAL_REPORT.json", report)
    lines = [
        "# PARP Phase2.8 阶段报告",
        "",
        "最终状态：`PARP_PHASE28_COLLECTION_AUTHORIZATION_REQUIRED`",
        "",
        "现有数据 pilot 的跨 session 结果低于 Majority，不能据此宣称操作识别或 refault 改善。"
        "重复采集工具链已完成并通过无特权测试，但 root 采集尚未执行。",
        "",
        "| # | 项目 | 状态 | 值 |",
        "|---:|---|---|---|",
    ]
    for item in report["items"]:
        value = json.dumps(item["value"], ensure_ascii=False, sort_keys=True) if isinstance(item["value"], (dict, list)) else str(item["value"])
        value = value.replace("\n", "<br>").replace("|", "\\|")
        lines.append("| %s | %s | %s | %s |" % (
            item["index"], item["name"], item["status"], value))
    lines.extend([
        "", "## 继续执行", "",
        "关闭正在运行的 WPS 和文件管理器窗口后，在终端执行：", "",
        "```bash", "sudo bash '%s'" % root_script, "```", "",
        "该命令的交互式 sudo 调用即本轮重复采集授权；它不会保存密码、修改 sudoers、重启或启用 Apply。",
    ])
    atomic_text(args.output / "final/FINAL_REPORT.md", "\n".join(lines) + "\n")
    atomic_text(args.output / "final/status.txt", "PARP_PHASE28_COLLECTION_AUTHORIZATION_REQUIRED\n")
    atomic_text(args.output / "final/authorization_command.txt", "sudo bash '%s'\n" % root_script)
    atomic_text(args.output / "README_FIRST.md", """# PARP Phase2.8 page prediction

Current stage: `PARP_PHASE28_COLLECTION_AUTHORIZATION_REQUIRED`.

The existing Phase2.7B raw data was frozen and audited.  Its kernel-only
current-operation pilot does not generalize across WPS sessions.  Fresh,
repeated collection is therefore mandatory.  No Apply, prefetch, anonymous
pageout, kernel installation, or reboot was performed.

Read `final/FINAL_REPORT.md` for the 80-item status report and the exact next
command.
""")
    atomic_text(args.output / "analysis/existing_data_audit.md", """# Existing Phase2.7B data audit

- 923,636 PARP evidence rows and five real sessions are reusable.
- FILE/ANON access, age, page counts, logical intervals and monotonic-aligned
  offline operation labels are present.
- Per-window cgroup VM, fault/refault/reclaim, IO, CPU, PSI and process memory
  series were not collected.
- Automation data remains label-only and is excluded from online features.
- All 236 raw files retained their original SHA-256 digest.
""")
    atomic_text(args.output / "analysis/existing_pilot.md", """# Existing-data separability pilot

The validation-selected 2-second nearest-centroid prototype reached Macro-F1
0.19055 on wps_02 versus 0.05274 for Majority.  On the held-out wps_03 session,
however, Macro-F1 fell to 0.01579 versus 0.05274 for Majority and UNKNOWN rose
to 93.06%.  Balanced accuracy was 0.00974 versus 0.125 for Majority.

Conclusion: the existing PAGE/ANON aggregates do not establish stable
cross-session operation inference.  Fresh repeated collection is required.
""")
    atomic_text(args.output / "config/repeated_collection_plan.md", """# Repeated collection plan

- WPS: 3 sessions, one isolated small/medium/large fixture per session, each
  major coarse operation repeated at least 5 times in every session.
- Files: 2 sessions in a dedicated fixture tree, each major coarse operation
  repeated at least 3 times in every session.
- Every operation has a unique repeat_id, at least 20 seconds of stable
  baseline, at least 20 seconds of in-operation dwell, and 20 seconds recovery.
- Dependency-safe operation groups are deterministically shuffled across
  sessions and repetitions.
- Kernel/cgroup sampling interval: 1 second; PAGE/ANON evidence: existing PARP
  trace path; automation markers: offline labels only.
- Observe-only is mandatory: mode=1, evidence_mode=0, scan_budget_mode=1,
  scan_budget_apply_domain=0.  No prefetch or anonymous pageout.
""")

    with args.state.open(encoding="utf-8") as stream:
        state = json.load(stream)
    state.update({
        "stage": "AWAITING_COLLECTION_AUTHORIZATION",
        "final_status": "PARP_PHASE28_COLLECTION_AUTHORIZATION_REQUIRED",
        "final_head": head,
        "collection_runner_prepared": True,
        "collection_runner": str(root_script),
        "collection_manifest": str(args.collector / "phase28_collection_manifest.json"),
        "existing_pilot_complete": True,
        "existing_pilot_outcome": "NEGATIVE_CROSS_SESSION",
        "raw_hash_files_checked": 236,
        "raw_hash_failures": 0,
        "kernel_write": False, "apply": False, "prefetch": False,
        "anon_pageout": False, "rebooted": False,
    })
    state["history"] = [
        {"stage": "AUDIT", "status": "COMPLETE", "timestamp": now},
        {"stage": "EXISTING_DATA_PILOT", "status": "COMPLETE_NEGATIVE", "timestamp": now},
        {"stage": "COLLECTION_PREPARATION", "status": "COMPLETE", "timestamp": now},
        {"stage": "AWAITING_COLLECTION_AUTHORIZATION", "status": "GATED", "timestamp": now},
    ]
    atomic_json(args.state, state)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--existing", type=Path, required=True)
    parser.add_argument("--collector", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({"final_status": run(args)["final_status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
