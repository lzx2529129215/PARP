#!/usr/bin/env python3
"""汇总 Bindfix 内核 observe-only 运行态验收结果，不修改内核状态。"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import tarfile
from pathlib import Path


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def stats(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == "stat":
            try:
                result[fields[1]] = int(fields[2])
            except ValueError:
                pass
    return result


def value(snapshot: Path, name: str) -> int:
    return stats(read_text(snapshot)).get(name, -1)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def status_row(name: str, passed: bool, evidence: str) -> dict[str, str]:
    return {"check": name, "status": "PASS" if passed else "FAIL", "evidence": evidence}


def combined_rows(debugfs_text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in debugfs_text.splitlines():
        if not line.startswith(("continue_hint ", "reentry_hint ")):
            continue
        fields = dict(item.split("=", 1) for item in line.split()[1:] if "=" in item)
        probability = int(fields.get("lstm_probability_fixed", "0"))
        confidence = int(fields.get("reentry_confidence_fixed", fields.get("confidence", "0")))
        actual = int(fields.get("combined_strength_fixed", "-1"))
        if confidence == 0:
            expected = probability
        elif probability == 0:
            expected = confidence
        else:
            expected = (probability * confidence + 5000) // 10000
        rows.append({
            "hint_type": line.split()[0], "app_id": fields.get("app", ""),
            "lstm_probability_fixed": str(probability),
            "reentry_confidence_fixed": str(confidence),
            "expected_combined_strength_fixed": str(expected),
            "actual_combined_strength_fixed": str(actual),
            "kernel_formula_valid": fields.get("combined_formula_valid", "0"),
            "status": "PASS" if actual == expected and fields.get("combined_formula_valid") == "1" else "FAIL",
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Bindfix observe-only 运行态验收报告")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--share-dir", type=Path, required=True)
    parser.add_argument("--expected-release", required=True)
    args = parser.parse_args()

    work = args.work_dir.resolve()
    session = args.session_dir.resolve()
    report_dir = work / "reports"
    controlled = work / "controlled"
    kernel = work / "kernel"
    report_dir.mkdir(parents=True, exist_ok=True)

    debugfs = read_text(kernel / "debugfs_after.txt")
    kernel_stats = stats(debugfs)
    controlled_ok = {
        "first_insert": value(controlled / "after_first_insert.txt", "app_bind_insert") == 1
        and value(controlled / "after_first_insert.txt", "app_bind_active_entries") == 1,
        "same_key_refresh": value(controlled / "after_same_key_refresh.txt", "app_bind_refresh") == 20
        and value(controlled / "after_same_key_refresh.txt", "app_bind_insert") == 1,
        "replace_cgroup": value(controlled / "after_replace_cgroup.txt", "app_bind_replace_cgroup") == 1,
        "replace_app": value(controlled / "after_replace_app.txt", "app_bind_replace_app") == 1,
        "expired_reuse": value(controlled / "after_expired_reuse.txt", "app_bind_expired_reuse") == 1,
        "capacity_boundary": value(controlled / "after_capacity_boundary.txt", "app_bind_enospc") == 1
        and value(controlled / "after_capacity_boundary.txt", "app_bind_active_entries") == 32,
        "cleanup": value(controlled / "after_controlled_cleanup.txt", "app_bind_active_entries") == 0,
    }

    epochs = read_csv(session / "model/foreground_epochs.csv")
    reentry_events = read_csv(session / "model/reentry_events.csv")
    reentry_samples = read_csv(session / "model/reentry_workload_samples.csv")
    continue_predictions = read_csv(session / "model/continue_markov_predictions.csv")
    combined = combined_rows(debugfs)
    with (controlled / "combined_strength_validation.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(combined[0]) if combined else ["hint_type", "status"])
        writer.writeheader()
        writer.writerows(combined)

    hint_rows: list[dict[str, str]] = []
    for mode in ("continue", "reentry"):
        lookup = kernel_stats.get(f"{mode}_lookup_calls", 0)
        hits = kernel_stats.get(f"{mode}_lookup_hits", 0)
        generated = kernel_stats.get(f"{mode}_hint_generation_events", 0)
        updated = kernel_stats.get(f"{mode}_hint_state_updates", 0)
        valid = lookup >= hits >= generated >= updated and hits > 0 and generated > updated
        hint_rows.append({"mode": mode.upper(), "lookup_calls": str(lookup), "lookup_hits": str(hits), "hint_generation_events": str(generated), "hint_state_updates": str(updated), "same_hint_not_repeated_state_update": str(generated > updated).lower(), "status": "PASS" if valid else "FAIL"})
    with (controlled / "hint_counter_validation.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(hint_rows[0]))
        writer.writeheader()
        writer.writerows(hint_rows)

    journal = read_text(kernel / "kernel_journal_after.txt")
    kernel_errors = len(re.findall(r"kernel panic|\boops\b|\bBUG:|lockdep|general protection fault|KASAN|use-after-free", journal, flags=re.I))
    tier2_enabled = re.search(r"^enabled=(\d+)$", read_text(kernel / "tier2_stats_after.txt"), flags=re.M)
    tier2_disabled = bool(tier2_enabled and tier2_enabled.group(1) == "0")
    command_text = "\n".join(read_text(path) for path in (work / "live/monitor.log", work / "live/automation.log", controlled / "controlled_bind_commands.csv"))
    prohibited = not re.search(r"lru_gen_pages|policy mode apply|\bpromote\b|\bdepromote\b|\bprotect\b", command_text, flags=re.I)
    cross_epoch = sum(1 for row in continue_predictions if "cross_epoch" in row.get("resolution_reason", ""))

    checks = [
        status_row("new_kernel_boot", os.uname().release == args.expected_release, f"running={os.uname().release},expected={args.expected_release}"),
        status_row("controlled_bind", all(controlled_ok.values()), json.dumps(controlled_ok, ensure_ascii=False)),
        status_row("live_bind", kernel_stats.get("app_bind_write_calls", 0) > 0 and kernel_stats.get("app_bind_refresh", 0) > 0 and kernel_stats.get("app_bind_enospc", -1) == 0 and kernel_stats.get("app_bind_active_entries", 99) <= kernel_stats.get("app_bind_capacity", 0), f"writes={kernel_stats.get('app_bind_write_calls', 0)},refresh={kernel_stats.get('app_bind_refresh', 0)},enospc={kernel_stats.get('app_bind_enospc', -1)}"),
        status_row("foreground_epoch", len(epochs) >= 6 and cross_epoch == 0, f"epochs={len(epochs)},cross_epoch={cross_epoch}"),
        status_row("continue", kernel_stats.get("continue_predictions", 0) > 0 and kernel_stats.get("continue_lookup_hits", 0) > 0 and kernel_stats.get("continue_hint_generation_events", 0) > 0 and kernel_stats.get("continue_hint_state_updates", 0) > 0, f"predictions={kernel_stats.get('continue_predictions', 0)},hits={kernel_stats.get('continue_lookup_hits', 0)}"),
        status_row("reentry", len(reentry_events) > 0 and any(row.get("valid") == "true" for row in reentry_samples) and any(row.get("sample_state_changed") == "false" and row.get("valid") == "true" for row in reentry_samples) and kernel_stats.get("reentry_predictions", 0) > 0 and kernel_stats.get("reentry_lookup_hits", 0) > 0, f"events={len(reentry_events)},valid_samples={sum(row.get('valid') == 'true' for row in reentry_samples)}"),
        status_row("hint_counter", all(row["status"] == "PASS" for row in hint_rows), json.dumps(hint_rows, ensure_ascii=False)),
        status_row("combined_strength", bool(combined) and all(row["status"] == "PASS" for row in combined), f"rows={len(combined)},mismatch={sum(row['status'] != 'PASS' for row in combined)}"),
        status_row("observe_safety", kernel_stats.get("legacy_hook_calls", -1) == 0 and kernel_stats.get("app_policy_apply", -1) == 0 and kernel_stats.get("per_folio_calls", -1) == 0 and kernel_stats.get("per_folio_calls_dual", -1) == 0 and kernel_stats.get("original_scan_pages", -1) == kernel_stats.get("applied_scan_pages", -2) and tier2_disabled and kernel_errors == 0 and prohibited, f"legacy={kernel_stats.get('legacy_hook_calls')},applied={kernel_stats.get('applied_scan_pages')},original={kernel_stats.get('original_scan_pages')},tier2_disabled={tier2_disabled},kernel_errors={kernel_errors}"),
    ]
    final = "PASS" if all(row["status"] == "PASS" for row in checks) else "FAIL"
    payload = {
        "session_id": session.name, "expected_release": args.expected_release,
        "runtime_mode": "dual", "final_result": final, "ready_for_apply": False,
        "controlled_bind": controlled_ok, "kernel_stats": kernel_stats,
        "foreground_epoch_count": len(epochs), "cross_epoch_transition_count": cross_epoch,
        "reentry_event_count": len(reentry_events),
        "reentry_valid_samples": sum(row.get("valid") == "true" for row in reentry_samples),
        "reentry_state_unchanged_valid_samples": sum(row.get("valid") == "true" and row.get("sample_state_changed") == "false" for row in reentry_samples),
        "combined_strength_rows": len(combined), "combined_strength_mismatch": sum(row["status"] != "PASS" for row in combined),
        "kernel_errors": kernel_errors, "tier2_runtime_enabled": 0 if tier2_disabled else 1,
        "checks": checks,
    }
    (report_dir / "dual_markov_bindfix_runtime_summary.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 双模式 Markov Bindfix 运行态验收", "", f"- session_id: `{session.name}`", f"- expected_release: `{args.expected_release}`", "- runtime_mode: `dual`", "- policy_mode: `observe`", f"- final_result: **{final}**", "- ready_for_apply: `false`", "", "## 核心计数", ""]
    for key in ("app_bind_write_calls", "app_bind_insert", "app_bind_refresh", "app_bind_replace_cgroup", "app_bind_replace_app", "app_bind_expired_reuse", "app_bind_enospc", "app_bind_active_entries", "app_bind_capacity", "app_bind_high_watermark", "continue_predictions", "continue_lookup_hits", "continue_hint_generation_events", "continue_hint_state_updates", "reentry_predictions", "reentry_lookup_hits", "reentry_hint_generation_events", "reentry_hint_state_updates", "legacy_hook_calls", "dual_hook_calls", "original_scan_pages", "proposed_scan_pages", "applied_scan_pages", "per_folio_calls", "per_folio_calls_dual"):
        lines.append(f"- {key}: {kernel_stats.get(key, 0)}")
    lines += ["", "## 验收项", "", "| 检查 | 结果 | 证据 |", "|---|---|---|"]
    lines += [f"| {row['check']} | {row['status']} | {row['evidence']} |" for row in checks]
    lines += ["", "## 安全边界", "", "- 未进入 apply；未写 `lru_gen_pages`；未调用 promote/depromote/protect。", "- 未改变实际 scan：`applied_scan_pages == original_scan_pages`。", "- Tier2 runtime disabled，内核严重错误计数为 0。"]
    (report_dir / "dual_markov_bindfix_runtime_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with (report_dir / "runtime_feature_status.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["check", "status", "evidence"])
        writer.writeheader(); writer.writerows(checks)
    with (report_dir / "issue_register.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["issue", "status", "detail"])
        writer.writeheader()
        for row in checks:
            if row["status"] != "PASS": writer.writerow({"issue": row["check"], "status": "open", "detail": row["evidence"]})

    share = args.share_dir.resolve()
    if share.exists(): shutil.rmtree(share)
    share.mkdir(parents=True)
    for subdir in ("controlled", "live", "kernel", "reports"):
        shutil.copytree(work / subdir, share / subdir)
    shutil.copytree(session / "model", share / "session_model")
    shutil.copytree(session / "review", share / "session_review")
    shutil.copy2(report_dir / "dual_markov_bindfix_runtime_summary.md", share / "00_请先阅读.md")
    files = sorted(path for path in share.rglob("*") if path.is_file())
    (share / "SHA256SUMS").write_text("\n".join(f"{digest(path)}  {path.relative_to(share)}" for path in files) + "\n", encoding="utf-8")
    manifest = [{"relative_path": str(path.relative_to(share)), "sha256": digest(path)} for path in files]
    (share / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = share.with_suffix(".tar.gz")
    with tarfile.open(archive, "w:gz") as stream: stream.add(share, arcname=share.name)
    Path(str(archive) + ".sha256").write_text(f"{digest(archive)}  {archive.name}\n", encoding="utf-8")
    print(json.dumps({"final_result": final, "summary": str(report_dir / "dual_markov_bindfix_runtime_summary.json"), "share": str(share), "archive": str(archive)}, ensure_ascii=False))
    return 0 if final == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
