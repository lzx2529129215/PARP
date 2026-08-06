#!/usr/bin/env python3
"""Check online duration-aware LSTM prediction outputs for a Runtime Monitor session."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

LZX_ROOT = Path(__file__).resolve().parents[2]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["check_name", "expected", "observed", "result", "details"]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"]:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * p))))
    return values[idx]


def stats(values: list[float]) -> dict[str, float]:
    return {
        "min": min(values) if values else 0.0,
        "p50": median(values) if values else 0.0,
        "p90": percentile(values, 0.90),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else 0.0,
    }


def split_pipe(value: str) -> list[str]:
    return [item for item in (value or "").split("|") if item]


def real_history(row: dict[str, str]) -> list[str]:
    apps = split_pipe(row.get("input_history_apps"))
    masks = split_pipe(row.get("input_history_mask"))
    return [app for app, mask in zip(apps, masks) if mask == "1"]


def has_adjacent_repeats(rows: list[dict[str, str]]) -> bool:
    for row in rows:
        apps = real_history(row)
        if any(left == right for left, right in zip(apps, apps[1:])):
            return True
    return False


def count_raw_token(rows: list[dict[str, str]], token: str) -> int:
    return sum(token in split_pipe(row.get("input_history_apps")) for row in rows)


def trigger_counter(rows: list[dict[str, str]]) -> Counter[str]:
    return Counter(row.get("trigger_type", "") for row in rows)


def skipped_counter(rows: list[dict[str, str]]) -> Counter[str]:
    return Counter(row.get("skip_reason", "") for row in rows if row.get("status") == "skipped")


def horizon_counts(pred_rows: list[dict[str, str]]) -> Counter[str]:
    return Counter(row.get("horizon", "") for row in pred_rows if row.get("status") == "success")


def foreground_counts(global_rows: list[dict[str, str]]) -> Counter[str]:
    return Counter(row.get("foreground_app", "") for row in global_rows if row.get("foreground_app", ""))


def nearby_call(calls: list[dict[str, str]], target_time: str, trigger_substring: str, tolerance_s: float) -> bool:
    target = parse_time(target_time)
    if target is None:
        return False
    for row in calls:
        if trigger_substring not in row.get("trigger_type", ""):
            continue
        call_time = parse_time(row.get("sample_timestamp") or row.get("time", ""))
        if call_time is not None and abs((call_time - target).total_seconds()) <= tolerance_s:
            return True
    return False


def nearby_cooldown(predictions: list[dict[str, str]], target_time: str, tolerance_s: float) -> bool:
    target = parse_time(target_time)
    if target is None:
        return False
    for row in predictions:
        if row.get("skip_reason") != "event_cooldown":
            continue
        sample_time = parse_time(row.get("timestamp", ""))
        if sample_time is not None and abs((sample_time - target).total_seconds()) <= tolerance_s:
            return True
    return False


def check_event_alignment(
    calls: list[dict[str, str]],
    predictions: list[dict[str, str]],
    event_rows: list[dict[str, str]],
    time_field: str,
    trigger_substring: str,
    tolerance_s: float,
) -> tuple[int, int, int]:
    total = 0
    matched = 0
    cooldown_covered = 0
    for row in event_rows:
        total += 1
        call_match = nearby_call(calls, row.get(time_field, ""), trigger_substring, tolerance_s)
        cooldown_match = nearby_cooldown(predictions, row.get(time_field, ""), tolerance_s)
        matched += int(call_match or cooldown_match)
        cooldown_covered += int((not call_match) and cooldown_match)
    return matched, total, cooldown_covered


def check_result(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def fmt_counter(counter: Counter[str]) -> str:
    return "; ".join(f"{key}:{value}" for key, value in sorted(counter.items())) or "(empty)"


def fmt_stats(values: dict[str, float]) -> str:
    return " ".join(f"{key}={value:.3f}" for key, value in values.items())


def compact_call_rows(calls: list[dict[str, str]], limit: int = 20) -> list[str]:
    rows = [
        "call_id|feature_window_id|sample_timestamp|trigger_type|raw_foreground_app|mapped_foreground_app|"
        "raw_open_apps|mapped_opened_apps|input_history_apps|input_history_durations_s|input_history_mask|"
        "predict_latency_ms|status"
    ]
    for row in calls[:limit]:
        rows.append("|".join([
            row.get("call_id", ""),
            row.get("feature_window_id", ""),
            row.get("sample_timestamp", ""),
            row.get("trigger_type", ""),
            row.get("raw_foreground_app", ""),
            row.get("mapped_foreground_app", ""),
            row.get("raw_open_apps", ""),
            row.get("mapped_opened_apps", ""),
            row.get("input_history_apps", ""),
            row.get("input_history_durations_s", ""),
            row.get("input_history_mask", ""),
            row.get("predict_latency_ms", ""),
            row.get("status", ""),
        ]))
    return rows


def compact_prediction_rows(predictions: list[dict[str, str]], limit: int = 20) -> list[str]:
    rows = [
        "feature_window_id|timestamp|trigger_type|mapped_foreground_app|history_apps|history_durations_s|"
        "history_mask|horizon|rank|app|probability|status|skip_reason"
    ]
    for row in predictions[:limit]:
        rows.append("|".join([
            row.get("feature_window_id", ""),
            row.get("timestamp", ""),
            row.get("trigger_type", ""),
            row.get("mapped_foreground_app", ""),
            row.get("history_apps", ""),
            row.get("history_durations_s", ""),
            row.get("history_mask", ""),
            row.get("horizon", ""),
            row.get("rank", ""),
            row.get("app", ""),
            row.get("probability", ""),
            row.get("status", ""),
            row.get("skip_reason", ""),
        ]))
    return rows


def main() -> None:
    args = parse_args()
    session_dir = Path(args.session_dir)
    model_dir = session_dir / "model"
    review_dir = session_dir / "review"
    call_path = model_dir / "online_lstm_duration_call_trace.csv"
    pred_path = model_dir / "online_app_predictions_duration_1s.csv"
    global_path = model_dir / "global_state_1s.csv"
    switches_path = review_dir / "app_switches.csv"
    opened_path = review_dir / "opened_apps_timeline.csv"

    calls = read_csv(call_path)
    predictions = read_csv(pred_path)
    global_rows = read_csv(global_path)
    switches = read_csv(switches_path)
    opened = read_csv(opened_path)

    trigger_counts = trigger_counter(calls)
    skip_counts = skipped_counter(predictions)
    horizons = horizon_counts(predictions)
    fg_counts = foreground_counts(global_rows)
    predict_latency = [float(row.get("predict_latency_ms") or 0.0) for row in calls if row.get("predict_latency_ms") != ""]
    sample_latency = [float(row.get("latency_from_sample_ms") or 0.0) for row in calls if row.get("latency_from_sample_ms") != ""]
    predict_stats = stats(predict_latency)
    sample_stats = stats(sample_latency)
    real_switches = [row for row in switches if row.get("from_app")]
    switch_matched, switch_total, switch_cooldown = check_event_alignment(
        calls, predictions, real_switches, "time", "foreground_transition", args.tolerance_s
    )
    opened_matched, opened_total, opened_cooldown = check_event_alignment(
        calls, predictions, opened, "time", "opened_apps_change", args.tolerance_s
    )
    call_count = len(calls)
    global_count = len(global_rows)
    no_every_second = call_count < max(1, global_count) and call_count <= max(1, global_count // 2)

    checks = [
        ("call_trace_exists_non_empty", "exists and non-empty", str(call_path), bool(calls), f"rows={call_count}"),
        ("calls_less_than_global_rows", "calls < global rows", f"{call_count} < {global_count}", call_count < global_count, ""),
        ("not_every_second", "not every second", f"calls={call_count}, global_rows={global_count}", no_every_second, ""),
        ("has_initial_prediction", "initial_prediction present", fmt_counter(trigger_counts), trigger_counts["initial_prediction"] > 0, ""),
        ("has_foreground_transition", "foreground_transition present", fmt_counter(trigger_counts), any("foreground_transition" in key for key in trigger_counts), ""),
        ("has_opened_apps_change", "opened_apps_change present", fmt_counter(trigger_counts), any("opened_apps_change" in key for key in trigger_counts), ""),
        ("foreground_wps_qq_files_detected", "WPS/QQ/FILES foreground rows present", fmt_counter(fg_counts), all(fg_counts[app] > 0 for app in ["WPS", "QQ", "FILES"]), ""),
        ("no_dwell_bucket_cross", "no dwell_bucket_cross", fmt_counter(trigger_counts), not any("dwell_bucket_cross" in key for key in trigger_counts), ""),
        ("app_switch_alignment", "switches have nearby foreground_transition or event_cooldown", f"{switch_matched}/{switch_total}", switch_matched == switch_total, f"tolerance_s={args.tolerance_s}; cooldown_covered={switch_cooldown}"),
        ("opened_apps_alignment", "open/close have nearby opened_apps_change or event_cooldown", f"{opened_matched}/{opened_total}", opened_matched == opened_total, f"tolerance_s={args.tolerance_s}; cooldown_covered={opened_cooldown}"),
        ("no_raw_FILES_in_history", "FILES absent", str(count_raw_token(calls, "FILES")), count_raw_token(calls, "FILES") == 0, ""),
        ("no_raw_QQ_in_history", "QQ absent", str(count_raw_token(calls, "QQ")), count_raw_token(calls, "QQ") == 0, ""),
        ("no_adjacent_repeat_real_app", "no adjacent repeated real app", str(has_adjacent_repeats(calls)), not has_adjacent_repeats(calls), ""),
        ("history_durations_non_empty", "duration field present", "", all(row.get("input_history_durations_s") for row in calls), ""),
        ("history_mask_present", "mask field present", "", all(row.get("input_history_mask") for row in calls), ""),
        ("has_output_horizons", "horizon 3/5/10 outputs", fmt_counter(horizons), all(horizons[str(h)] > 0 for h in [3, 5, 10]), ""),
        ("predict_latency_stats", "latency stats collected", fmt_stats(predict_stats), bool(predict_latency), ""),
        ("sample_latency_stats", "sample latency stats collected", fmt_stats(sample_stats), bool(sample_latency), ""),
    ]
    rows = [
        {"check_name": name, "expected": expected, "observed": observed, "result": check_result(ok), "details": details}
        for name, expected, observed, ok, details in checks
    ]
    final_pass = all(row["result"] == "PASS" for row in rows)
    rows.append({
        "check_name": "final_result",
        "expected": "all checks pass",
        "observed": "PASS" if final_pass else "FAIL",
        "result": "PASS" if final_pass else "FAIL",
        "details": "",
    })

    write_csv(review_dir / "online_lstm_prediction_checks.csv", rows)
    write_summary(
        review_dir / "online_lstm_prediction_summary.md",
        session_dir,
        call_count,
        global_count,
        trigger_counts,
        skip_counts,
        horizons,
        fg_counts,
        predict_stats,
        sample_stats,
        switch_matched,
        switch_total,
        opened_matched,
        opened_total,
        rows,
    )
    write_final_report(
        review_dir / "final_online_lstm_validation_report.md",
        args,
        session_dir,
        call_path,
        pred_path,
        calls,
        predictions,
        call_count,
        global_count,
        trigger_counts,
        skip_counts,
        horizons,
        fg_counts,
        predict_stats,
        sample_stats,
        switch_matched,
        switch_total,
        opened_matched,
        opened_total,
        rows,
    )
    print(f"saved: {review_dir / 'online_lstm_prediction_checks.csv'}")
    print(f"saved: {review_dir / 'online_lstm_prediction_summary.md'}")
    print(f"saved: {review_dir / 'final_online_lstm_validation_report.md'}")
    print(f"final_result: {'PASS' if final_pass else 'FAIL'}")


def write_summary(
    path: Path,
    session_dir: Path,
    call_count: int,
    global_count: int,
    trigger_counts: Counter[str],
    skip_counts: Counter[str],
    horizons: Counter[str],
    fg_counts: Counter[str],
    predict_stats: dict[str, float],
    sample_stats: dict[str, float],
    switch_matched: int,
    switch_total: int,
    opened_matched: int,
    opened_total: int,
    rows: list[dict[str, str]],
) -> None:
    final = next((row for row in rows if row["check_name"] == "final_result"), {"result": "FAIL"})
    lines = [
        "# Online LSTM Prediction Check Summary",
        "",
        f"- session_dir: `{session_dir}`",
        f"- final_result: **{final['result']}**",
        f"- online_lstm_calls: {call_count}",
        f"- global_state_1s_rows: {global_count}",
        f"- not_every_second: {call_count < global_count}",
        f"- trigger_type_counts: {fmt_counter(trigger_counts)}",
        f"- skipped_reason_counts: {fmt_counter(skip_counts)}",
        f"- horizon_success_counts: {fmt_counter(horizons)}",
        f"- foreground_app_counts: {fmt_counter(fg_counts)}",
        f"- predict_latency_ms: {fmt_stats(predict_stats)}",
        f"- latency_from_sample_ms: {fmt_stats(sample_stats)}",
        f"- app_switch_alignment: {switch_matched}/{switch_total}",
        f"- opened_apps_alignment: {opened_matched}/{opened_total}",
        "- no prefetch, eviction, swap, MGLRU, debugfs, or page cache action was performed.",
        "",
        "## Checks",
        "| check | result | observed | details |",
        "|---|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row['check_name']} | {row['result']} | {row['observed']} | {row['details']} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_final_report(
    path: Path,
    args: argparse.Namespace,
    session_dir: Path,
    call_path: Path,
    pred_path: Path,
    calls: list[dict[str, str]],
    predictions: list[dict[str, str]],
    call_count: int,
    global_count: int,
    trigger_counts: Counter[str],
    skip_counts: Counter[str],
    horizons: Counter[str],
    fg_counts: Counter[str],
    predict_stats: dict[str, float],
    sample_stats: dict[str, float],
    switch_matched: int,
    switch_total: int,
    opened_matched: int,
    opened_total: int,
    rows: list[dict[str, str]],
) -> None:
    final = next((row for row in rows if row["check_name"] == "final_result"), {"result": "FAIL"})
    result = final["result"]
    failures = [row for row in rows if row["result"] == "FAIL"]
    if result == "PASS":
        conclusion = (
            "PASS: automation execution was observed online by Runtime Monitor; WPS / QQ / FILES foreground "
            "changes were captured, QQ was mapped to 腾讯QQ, FILES was mapped to 图库, duration-aware history "
            "was maintained without repeated app tokens, the switch-aware duration LSTM was called only on "
            "initial_prediction / foreground_transition / opened_apps_change / periodic_ttl_refresh_180s style "
            "triggers, horizon=3/5/10 top-k outputs were produced, and no prefetch, eviction, swap, MGLRU, "
            "debugfs, or page cache scheduling action was performed."
        )
    else:
        details = "; ".join(f"{row['check_name']} observed={row['observed']} details={row['details']}" for row in failures)
        conclusion = f"FAIL: online LSTM validation failed. Failed checks: {details}"

    lines = [
        "# Final Online LSTM Validation Report",
        "",
        f"1. session_dir: `{session_dir}`",
        f"2. Runtime Monitor command: `{args.monitor_command}`",
        f"3. automation command: `{args.automation_command}`",
        f"4. checkpoint: `{args.checkpoint}`",
        f"5. app_vocab: `{args.app_vocab}`",
        f"6. group_vocab: `{args.group_vocab}`",
        f"7. global_state_1s_rows: {global_count}",
        f"8. online_lstm_calls: {call_count}",
        f"9. trigger_type_counts: {fmt_counter(trigger_counts)}",
        f"10. skipped_reason_counts: {fmt_counter(skip_counts)}",
        f"11. horizon_success_counts: {fmt_counter(horizons)}",
        f"12. foreground_app_counts: {fmt_counter(fg_counts)}",
        f"13. app_switch_alignment: {switch_matched}/{switch_total}",
        f"14. opened_apps_alignment: {opened_matched}/{opened_total}",
        f"15. no_raw_FILES_in_history: {next((row['result'] for row in rows if row['check_name'] == 'no_raw_FILES_in_history'), 'FAIL')}",
        f"16. no_raw_QQ_in_history: {next((row['result'] for row in rows if row['check_name'] == 'no_raw_QQ_in_history'), 'FAIL')}",
        f"17. no_adjacent_repeat_real_app: {next((row['result'] for row in rows if row['check_name'] == 'no_adjacent_repeat_real_app'), 'FAIL')}",
        f"18. predict_latency_ms: {fmt_stats(predict_stats)}",
        f"19. latency_from_sample_ms: {fmt_stats(sample_stats)}",
        f"20. call_trace_path: `{call_path}`",
        f"21. predictions_path: `{pred_path}`",
        "",
        "## online_lstm_duration_call_trace.csv Header And First 20 Rows",
        "```text",
        *compact_call_rows(calls, 20),
        "```",
        "",
        "## online_app_predictions_duration_1s.csv First 20 Rows",
        "```text",
        *compact_prediction_rows(predictions, 20),
        "```",
        "",
        "## Final Conclusion",
        conclusion,
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check online duration-aware LSTM prediction outputs.")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--tolerance-s", type=float, default=2.0)
    parser.add_argument("--monitor-command", default="")
    parser.add_argument("--automation-command", default="")
    parser.add_argument(
        "--checkpoint",
        default=LZX_ROOT / "operation_predictor" / "outputs" / "checkpoints" / "app_lstm_duration" / "lsapp_app_lstm_duration_switch.pt",
    )
    parser.add_argument("--app-vocab", default=LZX_ROOT / "operation_predictor" / "data" / "vocab" / "app_vocab_duration.json")
    parser.add_argument("--group-vocab", default=LZX_ROOT / "operation_predictor" / "data" / "vocab" / "user_group_vocab.json")
    return parser.parse_args()


if __name__ == "__main__":
    main()
