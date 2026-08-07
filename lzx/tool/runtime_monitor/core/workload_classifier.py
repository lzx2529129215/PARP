"""Rule-based cgroup workload classification from collector delta CSV files."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FAULT_THRESHOLD = 10
MEMORY_ACTIVITY_THRESHOLD_BYTES = 4 * 1024 * 1024

WORKLOAD_NAMES = {
    0: "LOW_ACTIVITY",
    1: "ANON_FAULT_HEAVY",
    2: "FILE_FAULT_HEAVY",
    3: "FILE_REFAULT_HEAVY",
    4: "MAJOR_FAULT_HEAVY",
    5: "MEMORY_GROWTH_HEAVY",
    6: "MIXED_ACTIVE",
}

INPUT_METRIC_FIELDS = [
    "memory_current_delta",
    "anon_delta",
    "file_delta",
    "pgfault_delta",
    "pgmajfault_delta",
    "workingset_refault_file_delta",
    "workingset_refault_anon_delta",
]

REQUIRED_INPUT_FIELDS = ["timestamp", "scope_name", *INPUT_METRIC_FIELDS]

OUTPUT_FIELDS = [
    "session_id",
    "timestamp",
    "scope_name",
    "app_key",
    "app_id",
    "cgroup_id",
    "workload_id",
    "workload_name",
    "reason",
    *INPUT_METRIC_FIELDS,
    "state_changed",
]


@dataclass(frozen=True)
class AppIdentity:
    app_key: str
    app_id: str


@dataclass(frozen=True)
class ClassificationResult:
    input_file: Path
    output_file: Path
    summary_file: Path
    total_rows: int
    scopes: list[str]
    missing_fields: list[str]
    final_result: str


def classify_metrics(values: dict[str, int]) -> tuple[int, str]:
    pgmajfault = values["pgmajfault_delta"]
    file_refault = values["workingset_refault_file_delta"]
    pgfault = values["pgfault_delta"]
    file_delta = values["file_delta"]
    anon_delta = values["anon_delta"]
    memory_delta = values["memory_current_delta"]

    if pgmajfault > 0:
        return 4, f"major_fault: pgmajfault_delta={pgmajfault}"
    if file_refault > 0:
        return 3, f"file_refault: workingset_refault_file_delta={file_refault}"
    if pgfault >= FAULT_THRESHOLD and file_delta > 0:
        return 2, f"file_fault: pgfault_delta={pgfault},file_delta={file_delta}"
    if pgfault >= FAULT_THRESHOLD and anon_delta > 0:
        return 1, f"anon_fault: pgfault_delta={pgfault},anon_delta={anon_delta}"
    if memory_delta >= MEMORY_ACTIVITY_THRESHOLD_BYTES:
        return 5, f"memory_growth: memory_current_delta={memory_delta}"
    if (
        pgfault >= FAULT_THRESHOLD
        or abs(memory_delta) >= MEMORY_ACTIVITY_THRESHOLD_BYTES
        or abs(file_delta) >= MEMORY_ACTIVITY_THRESHOLD_BYTES
        or abs(anon_delta) >= MEMORY_ACTIVITY_THRESHOLD_BYTES
    ):
        return 6, (
            "mixed_active: "
            f"pgfault_delta={pgfault},memory_current_delta={memory_delta},"
            f"file_delta={file_delta},anon_delta={anon_delta}"
        )
    return 0, "low_activity"


def classify_session(
    session_dir: str | Path,
    app_scope_config: str | Path,
) -> ClassificationResult:
    session_path = Path(session_dir).expanduser().resolve()
    input_file = session_path / "model" / "cgroup_memory_workload_delta_1s.csv"
    output_file = session_path / "model" / "cgroup_workload_state_1s.csv"
    summary_file = session_path / "review" / "cgroup_workload_state_summary.md"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.parent.mkdir(parents=True, exist_ok=True)

    if not input_file.exists():
        raise FileNotFoundError(f"cgroup workload delta CSV does not exist: {input_file}")

    scope_map = _load_scope_map(app_scope_config)
    distributions: dict[str, Counter[int]] = defaultdict(Counter)
    change_counts: Counter[str] = Counter()
    previous_states: dict[str, int] = {}
    scopes: list[str] = []
    seen_scopes: set[str] = set()
    missing_fields: list[str] = []
    total_rows = 0

    with input_file.open("r", encoding="utf-8", newline="") as src:
        reader = csv.DictReader(src)
        fieldnames = reader.fieldnames or []
        missing_fields = [field for field in REQUIRED_INPUT_FIELDS if field not in fieldnames]
        with output_file.open("w", encoding="utf-8", newline="") as dst:
            writer = csv.DictWriter(dst, fieldnames=OUTPUT_FIELDS)
            writer.writeheader()
            for row in reader:
                scope_name = str(row.get("scope_name", "")).strip()
                if scope_name not in seen_scopes:
                    seen_scopes.add(scope_name)
                    scopes.append(scope_name)
                identity = scope_map.get(scope_name, AppIdentity("", ""))
                values = {field: _parse_int(row.get(field, 0)) for field in INPUT_METRIC_FIELDS}
                workload_id, reason = classify_metrics(values)
                state_changed = (
                    scope_name not in previous_states
                    or previous_states[scope_name] != workload_id
                )
                previous_states[scope_name] = workload_id
                distributions[scope_name][workload_id] += 1
                if state_changed:
                    change_counts[scope_name] += 1
                writer.writerow(
                    {
                        "session_id": row.get("session_id") or session_path.name,
                        "timestamp": row.get("timestamp", ""),
                        "scope_name": scope_name,
                        "app_key": row.get("app_key") or identity.app_key,
                        "app_id": row.get("app_id") or identity.app_id,
                        "cgroup_id": row.get("cgroup_id", ""),
                        "workload_id": workload_id,
                        "workload_name": WORKLOAD_NAMES[workload_id],
                        "reason": reason,
                        **values,
                        "state_changed": str(state_changed).lower(),
                    }
                )
                total_rows += 1

    final_result = "PASS" if total_rows > 0 and any(scope for scope in scopes) else "FAIL"
    _write_summary(
        summary_file=summary_file,
        input_file=input_file,
        output_file=output_file,
        total_rows=total_rows,
        scopes=scopes,
        distributions=distributions,
        change_counts=change_counts,
        missing_fields=missing_fields,
        final_result=final_result,
    )
    return ClassificationResult(
        input_file=input_file,
        output_file=output_file,
        summary_file=summary_file,
        total_rows=total_rows,
        scopes=scopes,
        missing_fields=missing_fields,
        final_result=final_result,
    )


def _load_scope_map(path: str | Path) -> dict[str, AppIdentity]:
    config_path = Path(path).expanduser().resolve()
    data = json.loads(config_path.read_text(encoding="utf-8"))
    result: dict[str, AppIdentity] = {}
    for raw in data.get("apps", []):
        if not isinstance(raw, dict):
            continue
        scope_name = str(raw.get("scope_name", "")).strip()
        if scope_name:
            result[scope_name] = AppIdentity(
                app_key=str(raw.get("app_key", "")).strip(),
                app_id=str(raw.get("app_id", "")).strip(),
            )
    return result


def _parse_int(value: Any) -> int:
    try:
        return int(float(str(value or 0).strip()))
    except (TypeError, ValueError):
        return 0


def _write_summary(
    *,
    summary_file: Path,
    input_file: Path,
    output_file: Path,
    total_rows: int,
    scopes: list[str],
    distributions: dict[str, Counter[int]],
    change_counts: Counter[str],
    missing_fields: list[str],
    final_result: str,
) -> None:
    lines = [
        "# Cgroup Workload 状态汇总",
        "",
        f"- input_file: `{input_file}`",
        f"- output_file: `{output_file}`",
        f"- total_rows: {total_rows}",
        f"- scopes: {', '.join(scopes) if scopes else 'none'}",
        f"- missing_fields: {', '.join(missing_fields) if missing_fields else 'none'}",
        "",
        "## 各 Scope Workload 分布",
        "",
    ]
    for scope in scopes:
        lines.append(f"### {scope or '(empty scope)'}")
        counts = distributions[scope]
        for workload_id, workload_name in WORKLOAD_NAMES.items():
            lines.append(
                f"- {workload_id} {workload_name}: {counts.get(workload_id, 0)}"
            )
        lines.append(f"- state_changed_count: {change_counts.get(scope, 0)}")
        lines.append("")
    lines.append(f"- final_result: {final_result}")
    summary_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify cgroup memory workload delta rows into workload states."
    )
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--app-scope-config", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = classify_session(args.session_dir, args.app_scope_config)
    except Exception as exc:
        print(f"workload classifier failed: {exc}", file=sys.stderr)
        return 1
    for field in result.missing_fields:
        print(f"warning: input field missing, using 0: {field}", file=sys.stderr)
    print(f"output_file={result.output_file}")
    print(f"summary_file={result.summary_file}")
    print(f"final_result={result.final_result}")
    return 0 if result.final_result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
