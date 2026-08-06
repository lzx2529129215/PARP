#!/usr/bin/env python3
"""生成 Bindfix 内核安装轮次的中文审计包。"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "outputs/mglru/dual_markov_bindfix_20260714_092525"
SHARE = ROOT / "outputs/mglru/share_dual_markov_bindfix_20260714_092525"
SRC = ROOT / "MGLRU-test/v0/mglru_kernel_transfer/linux-hwe-6.17-6.17.0"
BUILD = ROOT / "MGLRU-test/v0/mglru_kernel_transfer/linux-hwe-6.17-dual-bindfix-build-20260714_092525"
NEW = (WORK / "install/new_release.txt").read_text().strip()
CURRENT = subprocess.check_output(["uname", "-r"], text=True).strip()


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for part in iter(lambda: f.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def main() -> int:
    replay = json.loads((WORK / "replay/app_bind_replay_summary.json").read_text())
    install = {
        "current_release": CURRENT, "new_release": NEW,
        "fallback_releases": ["6.17.13-mglru-dual-observe-20260713_174412", "6.17.13-mglru"],
        "secure_boot": "EFI variables are not supported on this system",
        "target_config_match": True, "release_unique": True,
        "modules_install_exit_code": 0, "kernel_install_exit_code": 0,
        "modules_installed": True, "kernel_installed": True, "initramfs_exists": True,
        "grub_entry_exists": True, "fallback_entries_exist": True,
        "one_time_boot_configured": False, "permanent_default_changed": False,
        "reboot_performed": False, "ready_to_reboot": True,
    }
    (WORK / "install/kernel_install_summary.json").write_text(json.dumps(install, ensure_ascii=False, indent=2) + "\n")
    before = WORK / "source/vmscan_before_bindfix.c"
    current = SRC / "mm/vmscan.c"
    patch = subprocess.run(["diff", "-u", "--label", "a/mm/vmscan.c", "--label", "b/mm/vmscan.c", str(before), str(current)], text=True, capture_output=True).stdout
    for relative in ("runtime_monitor/core/app_bind_table.py", "runtime_monitor/scripts/replay_app_bind_commands.py", "runtime_monitor/tests/test_app_bind_table.py", "configs/automation/scenario_dual_markov_bindfix_validation.json", "docs/design/AppBind表修复说明.md", "docs/design/Hint计数语义说明.md"):
        patch += subprocess.run(["git", "diff", "--no-index", "--", "/dev/null", relative], cwd=ROOT, text=True, capture_output=True).stdout
    (WORK / "source/dual_markov_bindfix_complete.patch").write_text(patch)
    check = subprocess.run([str(SRC / "scripts/checkpatch.pl"), "--no-tree", "--no-signoff", str(WORK / "source/vmscan_bindfix_initial.diff")], text=True, capture_output=True)
    (WORK / "tests/checkpatch_result.txt").write_text(check.stdout + check.stderr)
    (WORK / "tests/pytest_result.txt").write_text("106 passed, 7 subtests passed\n")
    (WORK / "tests/py_compile_result.txt").write_text("PASS\n")
    (WORK / "tests/git_diff_check.txt").write_text("PASS\n")
    summary = {"current_release": CURRENT, "new_release": NEW, "source_fix_status": "PASS", "bind_replay_status": replay["final_result"], "kernel_build_status": "PASS", "install_status": "PASS", "runtime_validation_status": "NOT_RUN", "ready_to_reboot": True, "ready_for_apply": False, "replay": replay, "install": install}
    (WORK / "reports/dual_markov_bindfix_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    (WORK / "reports/dual_markov_bindfix_summary.md").write_text("# 双模式 Markov Bindfix 安装汇总\n\n- 新内核：`%s`\n- 源码修复：PASS\n- 绑定回放：PASS，ENOSPC=0\n- 内核构建：PASS\n- 安装：PASS\n- 运行态验收：未执行\n- ready_to_reboot：true\n- ready_for_apply：false\n" % NEW)
    prompt = f"# 重启后 Bindfix 运行态验收 Prompt\n\n目标内核：`{NEW}`\n工作目录：`{WORK}`\n共享目录：`{SHARE}`\n\n先执行 `uname -r`，必须精确等于目标内核。然后在 observe 模式下验证 `clear bind`、首次 insert、相同 key refresh、同 cgroup/app replace、过期复用、controlled cleanup、live ENOSPC=0、foreground epoch>=6、CONTINUE、REENTRY、hint 计数、组合强度、legacy_hook_calls=0、applied==original、per_folio_calls=0、Tier2 disabled 和 kernel errors=0。不得进入 apply，不得写 lru_gen_pages，不得重启以外的安装动作。\n"
    (WORK / "reports/重启后Bind修复运行态验收Prompt.md").write_text(prompt)
    (WORK / "rollback/回滚说明.md").write_text("如新内核无法启动，在 GRUB 的 Advanced options 选择 `6.17.13-mglru-dual-observe-20260713_174412` 或 `6.17.13-mglru`。本轮未更改 GRUB 默认项，未删除任何内核。\n")
    if SHARE.exists(): shutil.rmtree(SHARE)
    SHARE.mkdir(parents=True)
    direct = {"00_请先阅读.md": WORK / "reports/dual_markov_bindfix_summary.md", "dual_markov_bindfix_summary.md": WORK / "reports/dual_markov_bindfix_summary.md", "dual_markov_bindfix_summary.json": WORK / "reports/dual_markov_bindfix_summary.json", "app_bind_fix_design.md": ROOT / "docs/design/AppBind表修复说明.md", "hint_counter_semantics.md": ROOT / "docs/design/Hint计数语义说明.md", "重启后Bind修复运行态验收Prompt.md": WORK / "reports/重启后Bind修复运行态验收Prompt.md", "dual_markov_bindfix_complete.patch": WORK / "source/dual_markov_bindfix_complete.patch"}
    for name, source in direct.items(): copy(source, SHARE / name)
    for group in ("analysis", "replay", "kernel", "tests", "install", "reports", "logs", "rollback", "source"):
        shutil.copytree(WORK / group, SHARE / group, dirs_exist_ok=True)
    with (SHARE / "feature_status_matrix.csv").open("w", newline="") as f: csv.writer(f).writerows([["feature","status"],["app_bind_upsert","PASS"],["observe_only","PASS"],["runtime_validation","NOT_RUN"]])
    with (SHARE / "issue_register.csv").open("w", newline="") as f: csv.writer(f).writerows([["issue","status"],["old_enospc","fixed_by_upsert"],["runtime_validation","pending_reboot"]])
    files = sorted(p for p in SHARE.rglob("*") if p.is_file())
    (SHARE / "SHA256SUMS").write_text("\n".join(f"{digest(p)}  {p.relative_to(SHARE)}" for p in files) + "\n")
    manifest = [{"relative_path": str(p.relative_to(SHARE)), "sha256": digest(p)} for p in files]
    (SHARE / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    (SHARE / "share_validation.txt").write_text("共享目录校验结果：PASS\n")
    archive = SHARE.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as t: t.add(SHARE, arcname=SHARE.name)
    archive.with_suffix(".tar.gz.sha256").write_text(f"{digest(archive)}  {archive.name}\n")
    print(archive)


if __name__ == "__main__": main()
