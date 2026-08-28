#!/usr/bin/env python3
"""Validate SHADOW rounds, label future page reuse, and train offline rankers."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


TEST_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = TEST_ROOT.parent
DEFAULT_TOOL_ROOT = (
    REPO_ROOT
    / "lzx/kernel/src/linux-6.17.13-parp-lzx/tools/parp/effective_tier"
)
MODE_NAMES = {
    0: "OFF", 1: "SHADOW_EFFECTIVE_TIER", 2: "APPLY_PROTECT_ONLY",
    3: "APPLY_BIDIRECTIONAL", 4: "APPLY_RANDOM_MATCHED",
    5: "APPLY_RECENCY_BASELINE",
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def stat(text: str, key: str) -> int:
    match = re.search(rf"(?:^|\s){re.escape(key)}[=: ]+(-?\d+)", text, re.MULTILINE)
    if not match:
        raise ValueError(f"missing effective-tier stat: {key}")
    return int(match.group(1))


def trace_loss(path: Path) -> int:
    text = path.read_text(encoding="utf-8", errors="replace")
    patterns = (
        r"^overrun:\s+(\d+)$", r"^commit overrun:\s+(\d+)$",
        r"^dropped events:\s+(\d+)$",
    )
    return sum(
        int(value) for pattern in patterns
        for value in re.findall(pattern, text, re.MULTILINE)
    )


def round_session(round_dir: Path) -> tuple[dict[str, Any], list[str]]:
    before = read_json(round_dir / "policy-state-before.json")
    after = read_json(round_dir / "policy-state-after.json")
    snapshot_before = read_json(round_dir / "snapshot-before.json")
    snapshot_after = read_json(round_dir / "snapshot-after.json")
    scenario = read_json(round_dir / "scenario.json")
    before_stats = str(before.get("effective_tier_stats", ""))
    after_stats = str(after.get("effective_tier_stats", ""))
    before_config = str(before.get("effective_tier_config", ""))
    mode = int(before.get("effective_tier_mode") or 0)
    experiment_id = stat(before_config, "experiment_id")
    session_id = stat(before_config, "session_id")
    candidates_before = stat(before_stats, "candidates")
    candidates_after = stat(after_stats, "candidates")
    sampled_before = stat(before_stats, "trace_decisions_sampled_out")
    sampled_after = stat(after_stats, "trace_decisions_sampled_out")
    loss_before = trace_loss(round_dir / "trace/stats-before.txt")
    loss_after = trace_loss(round_dir / "trace/stats-after.txt")
    plan = scenario.get("metadata", {}).get("scenario_plan", {})
    threshold = plan.get("oom_threshold", {}) if isinstance(plan, dict) else {}
    blockers: list[str] = []
    if str(before.get("variant")) != "shadow_train" or mode != 1:
        blockers.append("round is not shadow_train/effective-tier mode 1")
    if stat(before_config, "trace_all_candidates") != 1:
        blockers.append("effective_tier_trace_all_candidates was not enabled")
    if sampled_after - sampled_before:
        blockers.append("candidate decisions were sampled out")
    if loss_after - loss_before:
        blockers.append("trace loss increased during the round")
    if candidates_after <= candidates_before:
        blockers.append("no effective-tier candidates were observed")
    session = {
        "schema_version": 1,
        "experiment_id": str(experiment_id),
        "session_id": str(session_id),
        "app": "OTHER",
        "workload": str(plan.get("sequence_mode", "acceptance")),
        "mode": MODE_NAMES.get(mode, "OFF"),
        "pressure_level": "P3" if isinstance(threshold, dict) and threshold.get("enabled") else "P0",
        "start_ns": int(snapshot_before["monotonic_ns"]),
        "observation_end_ns": int(snapshot_after["monotonic_ns"]),
        "tier_gate_counter": {
            "measured": True, "source": "effective_tier_stats:candidates",
            "before": candidates_before, "after": candidates_after,
            "delta": candidates_after - candidates_before,
        },
        "trace_loss": {
            "measured": True, "source": "tracefs instance stats",
            "before": loss_before, "after": loss_after,
            "lost": loss_after - loss_before,
        },
        "source_round": str(round_dir.resolve()),
    }
    return session, blockers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round-dir", action="append", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tool-root", type=Path, default=DEFAULT_TOOL_ROOT)
    parser.add_argument("--allow-incomplete-diagnostic", action="store_true")
    args = parser.parse_args()
    sessions: list[dict[str, Any]] = []
    blockers: list[str] = []
    identities: set[tuple[str, str]] = set()
    for directory in args.round_dir:
        session, round_blockers = round_session(directory.resolve())
        identity = (str(session["experiment_id"]), str(session["session_id"]))
        if identity in identities:
            round_blockers.append("duplicate experiment/session trace identity")
        identities.add(identity)
        sessions.append(session)
        blockers.extend(f"{directory}: {reason}" for reason in round_blockers)

    output = args.output_dir.resolve()
    sessions_path = output / "session-metadata.json"
    write_json(sessions_path, {"schema_version": 1, "sessions": sessions})
    status = {
        "status": "READY" if not blockers else "INCOMPLETE",
        "round_count": len(sessions), "blockers": blockers,
        "trained_model_installed_in_kernel": False,
        "lzx_note": "offline training never rewrites kernel source automatically",
    }
    write_json(output / "pipeline-status.json", status)
    if blockers and not args.allow_incomplete_diagnostic:
        print(json.dumps(status, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    collector_dir = output / "dataset"
    analysis_dir = output / "analysis"
    command = [
        sys.executable, str(args.tool_root / "collector.py"),
        "--sessions", str(sessions_path), "--output-dir", str(collector_dir),
    ]
    for directory in args.round_dir:
        command.extend(["--trace-text", str(directory.resolve() / "trace/trace.txt")])
    subprocess.run(command, check=True)
    subprocess.run([
        sys.executable, str(args.tool_root / "analyze.py"),
        "--samples", str(collector_dir / "labeled_candidates.jsonl"),
        "--telemetry", str(collector_dir / "observability.jsonl"),
        "--output-dir", str(analysis_dir),
    ], check=True)
    status["status"] = "OFFLINE_ANALYSIS_COMPLETE"
    status["ranking_model"] = str(analysis_dir / "ranking_model.json")
    write_json(output / "pipeline-status.json", status)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
