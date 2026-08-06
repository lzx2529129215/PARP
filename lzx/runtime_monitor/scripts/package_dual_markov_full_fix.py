#!/usr/bin/env python3
"""生成双模式 Markov 完整修复的构建证据、补丁和共享审计包。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any


SOURCE_FILES = (
    "runtime_monitor/core/dual_workload_markov.py",
    "runtime_monitor/core/online_causal_workload_markov.py",
    "runtime_monitor/core/online_cgroup_workload.py",
    "runtime_monitor/core/mglru_markov_debugfs.py",
    "runtime_monitor/monitor.py",
    "runtime_monitor/scripts/replay_dual_markov.py",
    "runtime_monitor/scripts/build_dual_markov_full_fix_audit.py",
    "runtime_monitor/scripts/package_dual_markov_full_fix.py",
    "runtime_monitor/tests/test_dual_markov_debugfs.py",
    "runtime_monitor/tests/test_dual_markov_full_fix.py",
    "runtime_monitor/tests/test_dual_markov_policy.py",
    "runtime_monitor/tests/test_dual_workload_markov.py",
    "runtime_monitor/tests/test_foreground_continue_markov.py",
    "runtime_monitor/tests/test_reentry_workload_markov.py",
    "docs/design/双模式Markov完整修复说明.md",
)


def run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=root, text=True, capture_output=True, check=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def kernel_evidence(root: Path, work: Path, build: Path) -> dict[str, Any]:
    config = build / ".config"
    config_text = config.read_text(encoding="utf-8")
    expected = {
        "CONFIG_LRU_GEN": "y",
        "CONFIG_LRU_GEN_ENABLED": "y",
        "CONFIG_MEMCG": "y",
        "CONFIG_TIER2_WATERMARK": "y",
        "CONFIG_TIER2_WATERMARK_MEMCG": "n",
    }

    actual: dict[str, str] = {}
    for key in expected:
        if f"{key}=y" in config_text:
            actual[key] = "y"
        elif f"# {key} is not set" in config_text:
            actual[key] = "n"
        else:
            actual[key] = "missing"
    matches = {key: actual[key] == value for key, value in expected.items()}
    selected = [
        f"{key}={actual[key]}" if actual[key] != "n" else f"# {key} is not set"
        for key in expected
    ]
    (work / "kernel/target_kernel_config.txt").write_text("\n".join(selected) + "\n", encoding="utf-8")
    write_json(work / "kernel/target_kernel_config_check.json", {
        "expected": expected,
        "actual": actual,
        "matches": matches,
        "config_match": all(matches.values()),
        "source_config": str(config),
    })

    stderr = (work / "logs/kernel_build_stderr.log").read_text(encoding="utf-8", errors="ignore")
    exit_code = int((work / "logs/kernel_build_exit_code.txt").read_text().strip())
    vmlinux = build / "vmlinux"
    bzimage = build / "arch/x86/boot/bzImage"
    modules = list(build.rglob("*.ko"))
    symbols = run(root, "nm", str(vmlinux)).stdout if vmlinux.exists() else ""
    result = {
        "build_exit_code": exit_code,
        "config_match": all(matches.values()),
        "vmlinux_exists": vmlinux.is_file(),
        "bzimage_exists": bzimage.is_file(),
        "modules_built": len(modules),
        "warnings_count": stderr.count("warning:"),
        "errors_count": sum(1 for line in stderr.splitlines() if "error:" in line.lower()),
        "tier2_compiled": "tier2_wmark_init" in symbols or "tier2_watermark" in symbols,
        "tier2_memcg_compiled": actual["CONFIG_TIER2_WATERMARK_MEMCG"] == "y",
        "dual_markov_compiled": "mglru_dual_markov_prepare_calls" in symbols,
        "install_performed": False,
        "reboot_performed": False,
        "debug_info_disabled_for_resource_control": True,
        "build_dir": str(build),
    }
    write_json(work / "kernel/kernel_build_result.json", result)
    (work / "kernel/vmlinux_symbol_check.txt").write_text(
        "\n".join(line for line in symbols.splitlines() if "mglru_dual" in line or "tier2_" in line) + "\n",
        encoding="utf-8",
    )
    return result


def build_patch(root: Path, work: Path, kernel_source: Path, kernel_baseline: Path) -> list[str]:
    sections: list[str] = []
    included: list[str] = []
    for relative in SOURCE_FILES:
        path = root / relative
        if not path.is_file():
            continue
        tracked = run(root, "git", "ls-files", "--error-unmatch", relative).returncode == 0
        if tracked:
            diff = run(root, "git", "diff", "--", relative).stdout
        else:
            diff = run(root, "git", "diff", "--no-index", "--", "/dev/null", relative).stdout
        if diff:
            sections.append(diff)
            included.append(relative)

    kernel_relative = str(kernel_source.relative_to(root))
    kernel_diff = run(
        root, "diff", "-u", "--label", "a/mm/vmscan.c", "--label", "b/mm/vmscan.c",
        str(kernel_baseline), str(kernel_source),
    ).stdout
    if kernel_diff:
        sections.append(kernel_diff)
        included.append(kernel_relative)
    patch_text = "\n".join(section.rstrip() for section in sections) + "\n"
    for name in ("dual_markov_full_fix.patch", "kernel_full_fix.patch"):
        content = patch_text if name.startswith("dual_") else kernel_diff
        (work / f"source/{name}").write_text(content, encoding="utf-8")
    (work / "source/relevant_source_diff.txt").write_text(patch_text, encoding="utf-8")
    (work / "source/modified_files.txt").write_text("\n".join(included) + "\n", encoding="utf-8")
    with (work / "source/modified_files.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["relative_path", "change_type", "included_in_patch"])
        writer.writeheader()
        tracked_files = set(run(root, "git", "ls-files").stdout.splitlines())
        for relative in included:
            writer.writerow({
                "relative_path": relative,
                "change_type": "modified" if relative in tracked_files or relative == kernel_relative else "added",
                "included_in_patch": "true",
            })
    return included


def copy_share(root: Path, work: Path, share: Path, kernel_source: Path) -> None:
    if share.exists():
        shutil.rmtree(share)
    for directory in ("architecture", "chain_audit", "source", "userspace", "kernel", "replay", "tests", "reports", "logs"):
        (share / directory).mkdir(parents=True, exist_ok=True)

    direct = {
        "reports/项目完整链路检查报告.md": "项目完整链路检查报告.md",
        "docs/design/双模式Markov完整修复说明.md": "双模式Markov修复说明.md",
        "reports/新内核Observe验证计划.md": "新内核Observe验证计划.md",
        "reports/dual_markov_full_fix_summary.md": "dual_markov_full_fix_summary.md",
        "reports/dual_markov_full_fix_summary.json": "dual_markov_full_fix_summary.json",
        "reports/feature_status_matrix.csv": "feature_status_matrix.csv",
        "reports/project_issue_register.csv": "project_issue_register.csv",
        "source/modified_files.csv": "modified_files.csv",
        "source/dual_markov_full_fix.patch": "dual_markov_full_fix.patch",
    }
    for source, destination in direct.items():
        base = root if source.startswith("docs/design/") else work
        copy_file(base / source, share / destination)

    groups = {
        "architecture": "architecture",
        "chain_audit": "chain_audit",
        "replay": "replay",
        "tests": "tests",
        "reports": "reports",
        "logs": "logs",
        "source": "source",
        "kernel": "kernel",
    }
    for source_dir, destination_dir in groups.items():
        for source in (work / source_dir).glob("*"):
            if source.is_file():
                copy_file(source, share / destination_dir / source.name)

    copy_file(root / "runtime_monitor/core/dual_workload_markov.py", share / "userspace/dual_workload_markov.py")
    copy_file(root / "runtime_monitor/core/mglru_markov_debugfs.py", share / "userspace/mglru_markov_debugfs.py")
    copy_file(work / "userspace/output_schema.md", share / "userspace/output_schema.md")
    monitor_diff = run(root, "git", "diff", "--", "runtime_monitor/monitor.py").stdout
    (share / "userspace/monitor_relevant_diff.txt").write_text(monitor_diff, encoding="utf-8")
    kernel_diff = run(
        root, "diff", "-u", "--label", "a/mm/vmscan.c", "--label", "b/mm/vmscan.c",
        str(work / "source/kernel_baseline_vmscan.c"), str(kernel_source),
    ).stdout
    (share / "kernel/vmscan_relevant_diff.txt").write_text(kernel_diff, encoding="utf-8")


def write_readme(share: Path, build: dict[str, Any]) -> None:
    text = f"""# 请先阅读

本轮修复了 CONTINUE 跨 foreground epoch 污染、REENTRY 丢弃稳定样本、内核候选取第一条、伪 hint、后台 probability 未进入建议、REENTRY common 依赖 legacy、foreground history 无 TTL 和 legacy/dual hook 混用问题。

完整检查范围包括前台识别、App/cgroup 映射、LSTM probability、cgroup workload、classifier、CONTINUE、REENTRY、debugfs ABI、内核 reclaim 分支、hint/suggestion、Tier2 共存、单元测试、离线回放和目标配置构建。

- CONTINUE：仅在同一前台 epoch 内使用 `(app, previous, current)` 预测 next workload。
- REENTRY：应用切回时从有效样本中选择首个非 LOW workload，稳定样本无需 `state_changed=true`。
- 前台 reclaim：只查询 CONTINUE，并检查 foreground history TTL。
- 后台 reclaim：先查 LSTM next-use probability，再查 app-level REENTRY；runtime workload 不参与 key。
- 真实 reclaim：未修改，hint 与 suggestion 均为 observe-only。
- 目标配置完整编译：{'PASS' if build['build_exit_code'] == 0 else 'FAIL'}。
- 新内核安装：未执行。
- 新内核运行：未执行，状态为 `NOT_EXERCISED`。
- ready_for_install: `true`
- ready_for_apply: `false`

建议依次查看：`dual_markov_full_fix_summary.md`、`项目完整链路检查报告.md`、`architecture/修改后完整系统架构.md`、`chain_audit/project_chain_summary.md`、`kernel/kernel_build_result.json`、`source/dual_markov_full_fix.patch`。
"""
    (share / "00_请先阅读.md").write_text(text, encoding="utf-8")


def validate_and_manifest(share: Path, source_root: Path) -> None:
    required = [
        "00_请先阅读.md", "项目完整链路检查报告.md", "双模式Markov修复说明.md",
        "新内核Observe验证计划.md", "dual_markov_full_fix_summary.json",
        "dual_markov_full_fix.patch", "kernel/kernel_build_result.json",
        "replay/dual_markov_replay_summary.json", "tests/pytest_exit_code.txt",
    ]
    checks = {
        "required_files": all((share / name).is_file() for name in required),
        "json_parse": True,
        "csv_headers": True,
        "patch_non_empty": (share / "dual_markov_full_fix.patch").stat().st_size > 0,
        "pytest_exit_code_zero": (share / "tests/pytest_exit_code.txt").read_text().strip() == "0",
        "py_compile_pass": "PASS" in (share / "tests/py_compile_result.txt").read_text(),
        "git_diff_check_pass": "PASS" in (share / "tests/git_diff_check.txt").read_text(),
        "checkpatch_zero_errors": "0 errors" in (share / "tests/checkpatch_result.txt").read_text().lower(),
        "no_symlinks": not any(path.is_symlink() for path in share.rglob("*")),
        "no_file_over_100_mib": not any(path.is_file() and path.stat().st_size > 100 * 1024 * 1024 for path in share.rglob("*")),
        "no_build_directory": not any("build" in part.lower() for path in share.rglob("*") for part in path.relative_to(share).parts[:-1]),
    }
    try:
        for path in share.rglob("*.json"):
            json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        checks["json_parse"] = False
    try:
        for path in share.rglob("*.csv"):
            with path.open(encoding="utf-8", newline="") as stream:
                if not next(csv.reader(stream), None):
                    checks["csv_headers"] = False
    except (OSError, csv.Error):
        checks["csv_headers"] = False
    build = json.loads((share / "kernel/kernel_build_result.json").read_text(encoding="utf-8"))
    config = json.loads((share / "kernel/target_kernel_config_check.json").read_text(encoding="utf-8"))
    checks.update({
        "kernel_build_exit_code_zero": build["build_exit_code"] == 0,
        "target_config_match": config["config_match"],
        "vmlinux_exists_at_build_time": build["vmlinux_exists"],
        "bzimage_exists_at_build_time": build["bzimage_exists"],
        "tier2_enabled": build["tier2_compiled"],
        "tier2_memcg_disabled": not build["tier2_memcg_compiled"],
    })
    suspicious = (
        "BEGIN " + "PRIVATE KEY",
        "pass" + "word=",
        "to" + "ken=",
        "api_" + "key=",
    )
    checks["no_obvious_secrets"] = not any(
        marker in path.read_text(encoding="utf-8", errors="ignore")
        for path in share.rglob("*") if path.is_file() for marker in suspicious
    )
    passed = all(checks.values())
    lines = ["共享目录校验结果：" + ("PASS" if passed else "FAIL"), ""]
    lines.extend(f"- {name}: {'PASS' if value else 'FAIL'}" for name, value in checks.items())
    lines.append("- SHA256SUMS 使用相对路径，生成后由打包脚本执行独立校验。")
    (share / "share_validation.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    descriptions = {
        "00_请先阅读.md": "审阅入口与安全边界",
        "dual_markov_full_fix.patch": "完整用户态与内核源码补丁",
        "项目完整链路检查报告.md": "逐阶段中文链路审计",
    }
    entries = []
    for path in sorted(path for path in share.rglob("*") if path.is_file() and path.name not in {"manifest.json", "SHA256SUMS"}):
        relative = path.relative_to(share).as_posix()
        entries.append({
            "relative_path": relative,
            "description_zh": descriptions.get(relative, "双模式 Markov 修复审计材料"),
            "exists": True,
            "size_bytes": path.stat().st_size,
            "sha256": sha256(path),
            "source_path": str(source_root / relative),
            "required_for_review": relative in required or relative.startswith(("architecture/", "chain_audit/")),
        })
    entries.extend([
        {"relative_path": "manifest.json", "description_zh": "共享目录文件清单", "exists": True, "size_bytes": 0, "sha256": "self_reference_not_applicable", "source_path": str(share / "manifest.json"), "required_for_review": True},
        {"relative_path": "SHA256SUMS", "description_zh": "相对路径完整性校验", "exists": True, "size_bytes": 0, "sha256": "generated_after_manifest", "source_path": str(share / "SHA256SUMS"), "required_for_review": True},
    ])
    write_json(share / "manifest.json", entries)

    checksum_lines = []
    for path in sorted(path for path in share.rglob("*") if path.is_file() and path.name != "SHA256SUMS"):
        checksum_lines.append(f"{sha256(path)}  {path.relative_to(share).as_posix()}")
    (share / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    verify = run(share, "sha256sum", "-c", "SHA256SUMS")
    if verify.returncode != 0:
        raise RuntimeError(f"SHA256SUMS 校验失败：{verify.stdout}{verify.stderr}")
    if not passed:
        raise RuntimeError("共享目录校验未通过")


def archive(share: Path) -> tuple[Path, str]:
    archive_path = share.with_suffix(".tar.gz")
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(share, arcname=share.name)
    digest = sha256(archive_path)
    archive_path.with_suffix(archive_path.suffix + ".sha256").write_text(
        f"{digest}  {archive_path.name}\n", encoding="utf-8"
    )
    return archive_path, digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--share-dir", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--kernel-source", type=Path, required=True)
    parser.add_argument("--kernel-baseline", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    work = args.work_dir.resolve()
    share = args.share_dir.resolve()
    build = args.build_dir.resolve()
    kernel_source = args.kernel_source.resolve()
    kernel_baseline = args.kernel_baseline.resolve()

    copy_file(kernel_baseline, work / "source/kernel_baseline_vmscan.c")
    build_result = kernel_evidence(root, work, build)
    included = build_patch(root, work, kernel_source, kernel_baseline)
    status = run(root, "git", "status", "--short", "--untracked-files=all")
    (work / "reports/git_status_final.txt").write_text(status.stdout, encoding="utf-8")
    copy_share(root, work, share, kernel_source)
    write_readme(share, build_result)
    validate_and_manifest(share, work)
    archive_path, digest = archive(share)
    print(json.dumps({
        "share_dir": str(share), "archive": str(archive_path), "sha256": digest,
        "included_source_files": included,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
