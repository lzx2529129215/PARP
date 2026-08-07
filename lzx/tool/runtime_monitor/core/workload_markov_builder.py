"""根据 workload 状态变化序列构建二阶 Markov 转移。"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MGLRU_MARKOV_TOPK = 4

REQUIRED_INPUT_FIELDS = [
    "session_id",
    "timestamp",
    "scope_name",
    "app_key",
    "app_id",
    "cgroup_id",
    "workload_id",
    "workload_name",
    "state_changed",
]

OUTPUT_FIELDS = [
    "session_id",
    "app_key",
    "app_id",
    "scope_name",
    "prev_workload_id",
    "current_workload_id",
    "next_workload_id",
    "count",
    "total_count",
    "confidence",
    "boost_level",
    "rank",
]


@dataclass(frozen=True)
class MarkovBuildResult:
    input_file: Path
    output_file: Path
    summary_file: Path
    total_state_rows: int
    total_state_changed_rows: int
    total_apps: int
    total_transition_keys: int
    total_transition_rows: int
    missing_fields: list[str]
    skipped_apps: list[str]
    final_result: str


def build_workload_markov(session_dir: str | Path) -> MarkovBuildResult:
    session_path = Path(session_dir).expanduser().resolve()
    input_file = session_path / "model" / "cgroup_workload_state_1s.csv"
    output_file = session_path / "model" / "workload_markov_transitions.csv"
    summary_file = session_path / "review" / "workload_markov_summary.md"
    output_file.parent.mkdir(parents=True, exist_ok=True)
    summary_file.parent.mkdir(parents=True, exist_ok=True)

    if not input_file.exists():
        raise FileNotFoundError(f"workload 状态 CSV 不存在: {input_file}")

    sequences: dict[tuple[str, str], list[int]] = defaultdict(list)
    app_metadata: dict[tuple[str, str], dict[str, str]] = {}
    total_state_rows = 0
    total_state_changed_rows = 0

    with input_file.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        missing_fields = [
            field for field in REQUIRED_INPUT_FIELDS if field not in fieldnames
        ]
        for row in reader:
            total_state_rows += 1
            if not _is_true(row.get("state_changed")):
                continue
            total_state_changed_rows += 1
            app_id = str(row.get("app_id", "")).strip()
            scope_name = str(row.get("scope_name", "")).strip()
            workload_id = _optional_int(row.get("workload_id"))
            if not app_id or workload_id is None:
                continue
            key = (app_id, scope_name)
            sequences[key].append(workload_id)
            app_metadata[key] = {
                "session_id": str(row.get("session_id", "")).strip()
                or session_path.name,
                "app_key": str(row.get("app_key", "")).strip(),
                "app_id": app_id,
                "scope_name": scope_name,
            }

    transition_counts: dict[
        tuple[str, str, int, int], Counter[int]
    ] = defaultdict(Counter)
    skipped_apps: list[str] = []
    eligible_apps: set[tuple[str, str]] = set()
    for key, sequence in sequences.items():
        if len(sequence) < 3:
            metadata = app_metadata[key]
            skipped_apps.append(
                f"{metadata['app_key'] or metadata['app_id']} "
                f"({metadata['scope_name']}): 状态变化序列长度={len(sequence)}"
            )
            continue
        eligible_apps.add(key)
        app_id, scope_name = key
        for index in range(len(sequence) - 2):
            prev_workload = sequence[index]
            current_workload = sequence[index + 1]
            next_workload = sequence[index + 2]
            transition_counts[
                (app_id, scope_name, prev_workload, current_workload)
            ][next_workload] += 1

    per_app_key_count: Counter[tuple[str, str]] = Counter()
    per_app_row_count: Counter[tuple[str, str]] = Counter()
    total_transition_rows = 0
    with output_file.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for transition_key in sorted(
            transition_counts,
            key=lambda item: (_sort_int(item[0]), item[1], item[2], item[3]),
        ):
            app_id, scope_name, prev_workload, current_workload = transition_key
            app_key = (app_id, scope_name)
            metadata = app_metadata[app_key]
            next_counts = transition_counts[transition_key]
            total_count = sum(next_counts.values())
            ranked = sorted(
                next_counts.items(),
                key=lambda item: (-item[1], item[0]),
            )[:MGLRU_MARKOV_TOPK]
            per_app_key_count[app_key] += 1
            for rank, (next_workload, count) in enumerate(ranked, start=1):
                confidence = max(
                    0,
                    min(10000, int(count * 10000 / total_count + 0.5)),
                )
                writer.writerow(
                    {
                        **metadata,
                        "prev_workload_id": prev_workload,
                        "current_workload_id": current_workload,
                        "next_workload_id": next_workload,
                        "count": count,
                        "total_count": total_count,
                        "confidence": confidence,
                        "boost_level": _boost_level(confidence),
                        "rank": rank,
                    }
                )
                per_app_row_count[app_key] += 1
                total_transition_rows += 1

    total_transition_keys = len(transition_counts)
    final_result = (
        "PASS"
        if eligible_apps and total_transition_rows > 0 and output_file.exists()
        else "FAIL"
    )
    _write_summary(
        summary_file=summary_file,
        input_file=input_file,
        output_file=output_file,
        total_state_rows=total_state_rows,
        total_state_changed_rows=total_state_changed_rows,
        total_apps=len(sequences),
        total_transition_keys=total_transition_keys,
        total_transition_rows=total_transition_rows,
        app_metadata=app_metadata,
        per_app_key_count=per_app_key_count,
        per_app_row_count=per_app_row_count,
        skipped_apps=skipped_apps,
        missing_fields=missing_fields,
        final_result=final_result,
    )
    return MarkovBuildResult(
        input_file=input_file,
        output_file=output_file,
        summary_file=summary_file,
        total_state_rows=total_state_rows,
        total_state_changed_rows=total_state_changed_rows,
        total_apps=len(sequences),
        total_transition_keys=total_transition_keys,
        total_transition_rows=total_transition_rows,
        missing_fields=missing_fields,
        skipped_apps=skipped_apps,
        final_result=final_result,
    )


def _boost_level(confidence: int) -> int:
    if confidence >= 8000:
        return 3
    if confidence >= 5000:
        return 2
    if confidence > 0:
        return 1
    return 0


def _is_true(value: Any) -> bool:
    return str(value or "").strip().lower() == "true"


def _optional_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _sort_int(value: str) -> tuple[int, str]:
    try:
        return int(value), value
    except ValueError:
        return sys.maxsize, value


def _write_summary(
    *,
    summary_file: Path,
    input_file: Path,
    output_file: Path,
    total_state_rows: int,
    total_state_changed_rows: int,
    total_apps: int,
    total_transition_keys: int,
    total_transition_rows: int,
    app_metadata: dict[tuple[str, str], dict[str, str]],
    per_app_key_count: Counter[tuple[str, str]],
    per_app_row_count: Counter[tuple[str, str]],
    skipped_apps: list[str],
    missing_fields: list[str],
    final_result: str,
) -> None:
    lines = [
        "# Workload Markov 转移汇总",
        "",
        f"- input_file: `{input_file}`",
        f"- output_file: `{output_file}`",
        f"- total_state_rows: {total_state_rows}",
        f"- total_state_changed_rows: {total_state_changed_rows}",
        f"- total_apps: {total_apps}",
        f"- total_transition_keys: {total_transition_keys}",
        f"- total_transition_rows: {total_transition_rows}",
        f"- missing_fields: {', '.join(missing_fields) if missing_fields else 'none'}",
        "",
        "## 各应用转移统计",
        "",
    ]
    for key in sorted(
        app_metadata,
        key=lambda item: (_sort_int(item[0]), item[1]),
    ):
        metadata = app_metadata[key]
        lines.extend(
            [
                f"### {metadata['app_key'] or metadata['app_id']}",
                f"- app_id: {metadata['app_id']}",
                f"- scope_name: {metadata['scope_name']}",
                f"- transition_key_count: {per_app_key_count.get(key, 0)}",
                f"- transition_row_count: {per_app_row_count.get(key, 0)}",
                "",
            ]
        )
    lines.append("## 跳过的应用")
    lines.append("")
    if skipped_apps:
        lines.extend(f"- {item}" for item in skipped_apps)
    else:
        lines.append("- none")
    lines.extend(["", f"- final_result: {final_result}"])
    summary_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 workload 二阶 Markov 转移。")
    parser.add_argument("--session-dir", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = build_workload_markov(args.session_dir)
    except Exception as exc:
        print(f"workload Markov builder 失败: {exc}", file=sys.stderr)
        return 1
    for field in result.missing_fields:
        print(f"warning: 输入字段缺失: {field}", file=sys.stderr)
    print(f"output_file={result.output_file}")
    print(f"summary_file={result.summary_file}")
    print(f"final_result={result.final_result}")
    return 0 if result.final_result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
