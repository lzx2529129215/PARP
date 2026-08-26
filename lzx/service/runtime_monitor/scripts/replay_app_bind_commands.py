#!/usr/bin/env python3
"""按 Bindfix upsert 语义离线回放历史 ``app bind`` 写入。"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime_monitor.core.app_bind_table import AppBindTable, binding_expired


COMMAND = re.compile(r"^app\s+bind\s+(\d+)\s+(\d+)\s+(\d+)$")
ROWS = [
    "sequence", "timestamp_ns", "app_id", "cgroup_id", "ttl_ms", "status",
    "source_row", "action", "active_entries", "expired_entries", "error",
]
FINAL = ["slot", "app_id", "cgroup_id", "ttl_ms", "updated_at_ms", "expired"]


def integer(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def collect(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result = []
    for source_row, row in enumerate(rows, start=2):
        command = str(row.get("command", "")).strip()
        match = COMMAND.fullmatch(command)
        if not match:
            continue
        result.append({
            "source_row": str(source_row),
            "timestamp_ns": str(row.get("timestamp_ns", "0")),
            "app_id": match.group(1), "cgroup_id": match.group(2),
            "ttl_ms": match.group(3), "status": str(row.get("status", "")),
        })
    return sorted(result, key=lambda row: (integer(row["timestamp_ns"]), integer(row["source_row"])))


def main() -> int:
    parser = argparse.ArgumentParser(description="离线回放 app bind 日志")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--capacity", type=int, default=32)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    table = AppBindTable(args.capacity)
    commands = collect(args.input)
    replay_rows: list[dict[str, object]] = []
    pairs: set[tuple[int, int]] = set()
    for sequence, command in enumerate(commands, start=1):
        now_ms = integer(command["timestamp_ns"]) // 1_000_000
        app_id, cgroup_id, ttl_ms = (integer(command[key]) for key in ("app_id", "cgroup_id", "ttl_ms"))
        action = table.upsert(app_id, cgroup_id, ttl_ms, now_ms)
        pairs.add((app_id, cgroup_id))
        replay_rows.append({
            "sequence": sequence, "timestamp_ns": command["timestamp_ns"],
            "app_id": app_id, "cgroup_id": cgroup_id, "ttl_ms": ttl_ms,
            "status": command["status"], "source_row": command["source_row"],
            "action": action, "active_entries": table.active_entries(now_ms),
            "expired_entries": table.expired_entries(now_ms),
            "error": "" if action != "enospc" else "table_full_live_unique_bindings",
        })

    with (args.output_dir / "app_bind_replay.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ROWS)
        writer.writeheader()
        writer.writerows(replay_rows)
    final_now = integer(commands[-1]["timestamp_ns"]) // 1_000_000 if commands else 0
    final_rows = []
    for slot, binding in enumerate(table.slots):
        if binding is None:
            continue
        final_rows.append({"slot": slot, "app_id": binding.app_id, "cgroup_id": binding.cgroup_id,
                           "ttl_ms": binding.ttl_ms, "updated_at_ms": binding.updated_at_ms,
                           "expired": str(binding_expired(binding, final_now)).lower()})
    with (args.output_dir / "app_bind_table_final.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FINAL)
        writer.writeheader()
        writer.writerows(final_rows)

    summary = {
        "input_file": str(args.input), "raw_bind_commands": len(commands),
        "unique_app_cgroup_pairs": len(pairs), "insert_count": table.stats.insert,
        "refresh_count": table.stats.refresh, "replace_cgroup_count": table.stats.replace_cgroup,
        "replace_app_count": table.stats.replace_app, "expired_reuse_count": table.stats.expired_reuse,
        "enospc_count": table.stats.enospc, "invalid_count": table.stats.invalid,
        "final_active_entries": table.active_entries(final_now),
        "final_expired_entries": table.expired_entries(final_now), "capacity": table.capacity,
        "high_watermark": table.stats.high_watermark,
        "final_result": "PASS" if table.stats.enospc == 0 else "FAIL",
    }
    (args.output_dir / "app_bind_replay_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if table.stats.enospc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
