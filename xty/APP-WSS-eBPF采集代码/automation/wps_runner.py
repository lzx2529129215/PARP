"""Plan validator, dry-run, replay scheduler, and aligned single-op runner."""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

try:
    from .wps_90_plan import OPERATIONS, get_operation, validate_plan
    from .wps_actions import active_title, active_window_snapshot, dispatch
except ImportError:  # direct invocation with PYTHONPATH=automation
    from wps_90_plan import OPERATIONS, get_operation, validate_plan
    from wps_actions import active_title, active_window_snapshot, dispatch


def append_trace(path: Path, row: dict):
    fields = ["run_id", "op_id", "source_event", "event_type", "planned_start_ns",
              "actual_start_ns", "actual_end_ns", "jitter_ms", "window_title_before",
              "window_title_after", "active_window_id", "window_x", "window_y",
              "window_width", "window_height", "pointer_x", "pointer_y",
              "pointer_window_id", "status", "error"]
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        if not exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fields})


def wait_until(deadline_ns: int):
    while True:
        remaining = deadline_ns - time.monotonic_ns()
        if remaining <= 0:
            return
        time.sleep(min(remaining / 1e9, 0.2))


def execute_one(args, op_id: int) -> int:
    spec = get_operation(op_id)
    trace = Path(args.trace) if args.trace else None
    planned = int(args.planned_start_ns or time.monotonic_ns())
    if args.wait_until_planned:
        wait_until(planned)
    actual_start = time.monotonic_ns()
    before = active_title()
    status, error, after = "PASS", "", before
    try:
        after = dispatch(spec.handler, spec.args, args.testdata)
    except Exception as exc:  # trace the failure before returning it
        status = "FAIL"
        error = f"{type(exc).__name__}: {exc}"
        after = active_title()
    actual_end = time.monotonic_ns()
    snapshot = active_window_snapshot()
    row = {
        "run_id": args.run_id, "op_id": spec.op_id,
        "source_event": spec.source_event, "event_type": spec.event_type,
        "planned_start_ns": planned, "actual_start_ns": actual_start,
        "actual_end_ns": actual_end,
        "jitter_ms": f"{(actual_start - planned) / 1e6:.3f}",
        "window_title_before": before, "window_title_after": after,
        "status": status, "error": error,
        **snapshot,
    }
    if trace:
        append_trace(trace, row)
    print(json.dumps(row, ensure_ascii=False), flush=True)
    return 0 if status == "PASS" else 1


def load_testdata(project: Path) -> dict:
    samples = Path(os.environ.get("WPS_SAMPLES_DIR", project / "samples")).expanduser()
    return {
        "word": os.environ.get(
            "WPS90_WORD",
            str(samples / "word_200m.docx")),
        "ppt": os.environ.get(
            "WPS90_PPT",
            str(samples / "ppt_200m.pptx")),
        "excel": os.environ.get(
            "WPS90_EXCEL",
            str(samples / "excel_200m.xlsx")),
        "save_path": os.environ.get(
            "WPS90_SAVE_PATH", str(project / "runs" / "wps90_automation_output.docx")),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate-plan", action="store_true")
    parser.add_argument("--from-op", type=int, default=1)
    parser.add_argument("--to-op", type=int, default=90)
    parser.add_argument("--resume-from", type=int)
    parser.add_argument("--timing-mode", choices=("source",), default="source")
    parser.add_argument("--time-scale", type=float, default=1.0)
    parser.add_argument("--single-op", type=int)
    parser.add_argument("--execute-only", action="store_true")
    parser.add_argument("--wait-until-planned", action="store_true")
    parser.add_argument("--planned-start-ns", type=int)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--run-id", default="wps90-run")
    args = parser.parse_args()
    args.project = args.project.resolve()
    if args.resume_from is not None:
        args.from_op = args.resume_from
    if args.time_scale <= 0:
        parser.error("--time-scale must be positive")
    args.testdata = load_testdata(args.project)
    result = validate_plan()
    if args.validate_plan or args.dry_run:
        result["selected_range"] = [args.from_op, args.to_op]
        result["selected_handlers"] = [row.handler for row in OPERATIONS[args.from_op - 1:args.to_op]]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["missing_handlers"] or not result["contiguous_ids"]:
            return 1
        if args.dry_run:
            return 0
    if args.single_op is not None:
        return execute_one(args, args.single_op)
    if not args.execute_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    selected = OPERATIONS[args.from_op - 1:args.to_op]
    sequence_t0 = time.monotonic_ns()
    base_offset_ms = selected[0].planned_offset_ms if selected else 0
    for spec in selected:
        child = argparse.Namespace(**vars(args))
        child.planned_start_ns = sequence_t0 + int(
            (spec.planned_offset_ms - base_offset_ms) * 1_000_000 * args.time_scale)
        child.wait_until_planned = True
        rc = execute_one(child, spec.op_id)
        if rc:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
