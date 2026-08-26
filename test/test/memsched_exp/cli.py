from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .frames import analyze_frame_times
from .metrics import summarize
from .protocol import ProtocolError, read_marker, wait_for_markers, write_marker
from .schema import validate_manifest
from .snapshot import host_metadata, take_snapshot


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect(args: argparse.Namespace) -> int:
    if args.duration <= 0 or args.interval <= 0:
        raise ValueError("duration and interval must be positive")
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    cgroup = Path(args.cgroup) if args.cgroup else None
    extra_metadata = {}
    if args.metadata_file:
        extra_metadata = json.loads(Path(args.metadata_file).read_text(encoding="utf-8"))
    manifest = None
    if args.manifest_file:
        manifest = json.loads(Path(args.manifest_file).read_text(encoding="utf-8"))
        manifest_reasons = validate_manifest(manifest)
        if manifest_reasons:
            raise ValueError("invalid run manifest: " + "; ".join(manifest_reasons))
    metadata = {
        **host_metadata(output),
        "name": args.name,
        "requested_duration_s": args.duration,
        "sample_interval_s": args.interval,
        "cache_state": args.cache_state,
        "scenario": args.scenario,
        "experiment": extra_metadata,
        "manifest": manifest,
    }
    _write_json(output / "metadata.json", metadata)

    ready_file = Path(args.ready_file) if args.ready_file else None
    start_file = Path(args.start_file) if args.start_file else None
    stop_file = Path(args.stop_file) if args.stop_file else None
    done_file = Path(args.done_file) if args.done_file else None
    for marker in (ready_file, done_file):
        if marker is not None:
            marker.unlink(missing_ok=True)
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)

    before = take_snapshot(Path(args.proc_root), cgroup).to_dict()
    _write_json(output / "before.json", before)
    ready_value = (
        write_marker(
            ready_file,
            "collector_ready",
            collector_name=args.name,
            before_monotonic_ns=before["monotonic_ns"],
            cgroup=str(cgroup) if cgroup else None,
        )
        if ready_file is not None
        else {
            "event": "collector_ready_internal",
            "monotonic_ns": time.monotonic_ns(),
            "realtime_ns": time.time_ns(),
        }
    )

    try:
        if start_file is not None:
            start_value = wait_for_markers(
                [start_file],
                args.start_timeout,
                minimum_monotonic_ns=before["monotonic_ns"],
            )[0]
        else:
            start_value = {
                "event": "collection_start_internal",
                "monotonic_ns": int(ready_value["monotonic_ns"]),
                "realtime_ns": int(ready_value["realtime_ns"]),
            }
    except ProtocolError as error:
        metadata["valid"] = False
        metadata["invalid_reason"] = str(error)
        _write_json(output / "metadata.json", metadata)
        if done_file is not None:
            write_marker(done_file, "collector_done", collector_name=args.name, valid=False, error=str(error))
        return 6

    window_start_ns = int(start_value["monotonic_ns"])
    deadline_ns = window_start_ns + int(args.duration * 1e9)
    metadata["valid"] = True
    metadata["protocol"] = {
        "version": 1,
        "ready_file": str(ready_file) if ready_file else None,
        "start_file": str(start_file) if start_file else None,
        "stop_file": str(stop_file) if stop_file else None,
        "done_file": str(done_file) if done_file else None,
        "before_monotonic_ns": before["monotonic_ns"],
        "ready_monotonic_ns": int(ready_value["monotonic_ns"]),
        "start_monotonic_ns": window_start_ns,
        "pre_start_boundary_ms": (window_start_ns - before["monotonic_ns"]) / 1e6,
    }
    _write_json(output / "metadata.json", metadata)

    stop_value: dict[str, object] | None = None
    with (output / "samples.jsonl").open("w", encoding="utf-8") as samples:
        while True:
            snap = take_snapshot(Path(args.proc_root), cgroup).to_dict()
            samples.write(json.dumps(snap, ensure_ascii=False) + "\n")
            samples.flush()
            now_ns = time.monotonic_ns()
            if stop_file is not None and stop_file.exists():
                try:
                    stop_value = read_marker(stop_file)
                except ProtocolError as error:
                    stop_value = {
                        "event": "invalid_stop_file_observed",
                        "monotonic_ns": now_ns,
                        "realtime_ns": time.time_ns(),
                    }
                    metadata["valid"] = False
                    metadata["invalid_reason"] = str(error)
                break
            remaining_s = (deadline_ns - now_ns) / 1e9
            if remaining_s <= 0:
                stop_value = {
                    "event": "duration_deadline",
                    "monotonic_ns": deadline_ns,
                    "realtime_ns": time.time_ns(),
                }
                break
            time.sleep(min(args.interval, remaining_s))
    after = take_snapshot(Path(args.proc_root), cgroup).to_dict()
    _write_json(output / "after.json", after)
    window_end_ns = min(int(stop_value["monotonic_ns"]), after["monotonic_ns"])
    if window_end_ns <= window_start_ns:
        metadata["valid"] = False
        metadata["invalid_reason"] = "collection stop boundary does not follow start boundary"
        window_end_ns = after["monotonic_ns"]
    metadata["protocol"].update(
        {
            "stop_event": stop_value["event"],
            "stop_monotonic_ns": int(stop_value["monotonic_ns"]),
            "after_monotonic_ns": after["monotonic_ns"],
            "post_stop_boundary_ms": (after["monotonic_ns"] - int(stop_value["monotonic_ns"])) / 1e6,
        }
    )
    _write_json(output / "metadata.json", metadata)
    elapsed_s = (window_end_ns - window_start_ns) / 1e9
    summary = summarize(before, after, metadata["cpu_count"], elapsed_s_override=elapsed_s)
    summary["measurement_window"] = {
        "start_monotonic_ns": window_start_ns,
        "end_monotonic_ns": window_end_ns,
        "pre_start_boundary_ms": metadata["protocol"]["pre_start_boundary_ms"],
        "post_stop_boundary_ms": metadata["protocol"]["post_stop_boundary_ms"],
    }
    _write_json(output / "summary.json", summary)
    if done_file is not None:
        write_marker(
            done_file,
            "collector_done",
            collector_name=args.name,
            valid=metadata["valid"],
            after_monotonic_ns=after["monotonic_ns"],
        )
    print(output / "summary.json")
    return 0 if metadata["valid"] else 6


def analyze(args: argparse.Namespace) -> int:
    run = Path(args.run)
    before = json.loads((run / "before.json").read_text(encoding="utf-8"))
    after = json.loads((run / "after.json").read_text(encoding="utf-8"))
    metadata = json.loads((run / "metadata.json").read_text(encoding="utf-8"))
    protocol = metadata.get("protocol", {})
    elapsed_override = None
    if protocol.get("start_monotonic_ns") is not None and protocol.get("stop_monotonic_ns") is not None:
        elapsed_override = (int(protocol["stop_monotonic_ns"]) - int(protocol["start_monotonic_ns"])) / 1e9
    summary = summarize(before, after, int(metadata.get("cpu_count", 1)), elapsed_override)
    if elapsed_override is not None:
        summary["measurement_window"] = {
            "start_monotonic_ns": int(protocol["start_monotonic_ns"]),
            "end_monotonic_ns": int(protocol["stop_monotonic_ns"]),
            "pre_start_boundary_ms": protocol.get("pre_start_boundary_ms"),
            "post_stop_boundary_ms": protocol.get("post_stop_boundary_ms"),
        }
    _write_json(run / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def frames(args: argparse.Namespace) -> int:
    result = analyze_frame_times(args.csv, args.budget_ms, args.window_start_ns, args.window_end_ns)
    if args.output:
        _write_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memsched-exp")
    sub = parser.add_subparsers(dest="command", required=True)

    collect_p = sub.add_parser("collect", help="collect system and optional cgroup metrics")
    collect_p.add_argument("--name", required=True)
    collect_p.add_argument("--duration", type=float, required=True)
    collect_p.add_argument("--interval", type=float, default=1.0)
    collect_p.add_argument("--output", required=True)
    collect_p.add_argument("--cgroup", help="absolute cgroup v2 path")
    collect_p.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    collect_p.add_argument("--cache-state", default="unspecified", choices=("unspecified", "process-cold", "strict-cold", "warm"))
    collect_p.add_argument("--scenario")
    collect_p.add_argument("--metadata-file")
    collect_p.add_argument("--manifest-file", help="validated schema-v4 run manifest")
    collect_p.add_argument("--ready-file", help="publish this marker after the before snapshot is durable")
    collect_p.add_argument("--start-file", help="after arming, wait for this workload-start marker")
    collect_p.add_argument("--start-timeout", type=float, default=60.0)
    collect_p.add_argument("--stop-file", help="finish early and take the after snapshot when this marker appears")
    collect_p.add_argument("--done-file", help="publish this marker after the after snapshot and summary are durable")
    collect_p.set_defaults(func=collect)

    analyze_p = sub.add_parser("analyze", help="recompute a run summary")
    analyze_p.add_argument("--run", required=True)
    analyze_p.set_defaults(func=analyze)

    frames_p = sub.add_parser("frames", help="analyze frame timestamp/duration CSV")
    frames_p.add_argument("--csv", required=True)
    frames_p.add_argument("--budget-ms", type=float, default=16.7)
    frames_p.add_argument("--output")
    frames_p.add_argument("--window-start-ns", type=int)
    frames_p.add_argument("--window-end-ns", type=int)
    frames_p.set_defaults(func=frames)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
