from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def parse_events(
    path: str | Path,
    stderr_path: str | Path | None = None,
    window_start_ns: int | None = None,
    duration_s: float | None = None,
    window_stop_ns: int | None = None,
) -> dict[str, Any]:
    all_counts: Counter[str] = Counter()
    window_counts: Counter[str] = Counter()
    by_comm: dict[str, Counter[str]] = defaultdict(Counter)
    parse_errors = 0
    open_reclaims: dict[int, dict[str, Any]] = {}
    reclaim_pairs: list[dict[str, Any]] = []
    pairing_errors: list[str] = []
    window_end_ns = window_stop_ns
    if window_end_ns is None and window_start_ns is not None and duration_s is not None:
        window_end_ns = window_start_ns + int(duration_s * 1e9)

    def in_window(timestamp_ns: int) -> bool:
        if window_start_ns is None:
            return True
        if window_end_ns is None:
            return timestamp_ns >= window_start_ns
        return window_start_ns <= timestamp_ns < window_end_ns

    with Path(path).open(encoding="utf-8") as stream:
        for raw in stream:
            raw = raw.strip()
            if not raw.startswith("{"):
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                parse_errors += 1
                continue
            event_type = str(event.get("type", "unknown"))
            timestamp = int(event.get("ts_ns", 0))
            all_counts[event_type] += 1
            if event_type in {"collector_start", "collector_stop"} or in_window(timestamp):
                window_counts[event_type] += 1
                by_comm[str(event.get("comm", "unknown"))][event_type] += 1
            if event_type == "direct_reclaim_begin":
                tid = int(event.get("tid", -1))
                if tid in open_reclaims:
                    pairing_errors.append(f"nested direct reclaim begin for tid {tid}")
                else:
                    open_reclaims[tid] = event
            if event_type == "direct_reclaim_end":
                tid = int(event.get("tid", -1))
                begin = open_reclaims.pop(tid, None)
                if begin is None:
                    pairing_errors.append(f"direct reclaim end without begin for tid {tid}")
                else:
                    begin_ns = int(begin.get("ts_ns", 0))
                    end_ns = timestamp
                    duration_value = int(event.get("duration_ns", max(0, end_ns - begin_ns)))
                    if end_ns < begin_ns or duration_value < 0:
                        pairing_errors.append(f"negative direct reclaim duration for tid {tid}")
                    else:
                        reclaim_pairs.append(
                            {
                                "tid": tid,
                                "begin_ns": begin_ns,
                                "end_ns": end_ns,
                                "duration_ns": duration_value,
                                "comm": str(begin.get("comm", event.get("comm", "unknown"))),
                                "cgroup_id": begin.get("cgroup_id"),
                            }
                        )
    begin_count_all = all_counts["direct_reclaim_begin"]
    end_count_all = all_counts["direct_reclaim_end"]
    invalid_reasons: list[str] = []
    if all_counts["collector_start"] != 1:
        invalid_reasons.append(f"expected one collector_start event, observed {all_counts['collector_start']}")
    if all_counts["collector_stop"] != 1:
        invalid_reasons.append(f"expected one collector_stop event, observed {all_counts['collector_stop']}")
    if begin_count_all != end_count_all:
        invalid_reasons.append(f"direct reclaim begin/end mismatch: {begin_count_all}/{end_count_all}")
    for tid in open_reclaims:
        pairing_errors.append(f"direct reclaim begin event left open for tid {tid}")
    invalid_reasons.extend(pairing_errors)
    if parse_errors:
        invalid_reasons.append(f"JSON parse errors: {parse_errors}")
    lost_events_detected = False
    stderr_excerpt: list[str] = []
    if stderr_path is not None and Path(stderr_path).exists():
        stderr = Path(stderr_path).read_text(encoding="utf-8", errors="replace")
        lost_pattern = re.compile(r"(?:lost|dropped)\s+\d*\s*(?:event|record|sample)", re.IGNORECASE)
        lost_events_detected = bool(lost_pattern.search(stderr))
        if lost_events_detected:
            invalid_reasons.append("bpftrace reported lost or dropped events")
        stderr_excerpt = [line for line in stderr.splitlines() if line.strip()][-20:]
    if window_start_ns is None:
        selected_pairs = reclaim_pairs
        overlapping_pairs = reclaim_pairs
        boundary_spanning_count = 0
    else:
        selected_pairs = [pair for pair in reclaim_pairs if in_window(int(pair["begin_ns"]))]
        effective_end_ns = window_end_ns if window_end_ns is not None else 2**63 - 1
        overlapping_pairs = [
            pair
            for pair in reclaim_pairs
            if int(pair["end_ns"]) > window_start_ns and int(pair["begin_ns"]) < effective_end_ns
        ]
        boundary_spanning_count = sum(
            int(pair["begin_ns"]) < window_start_ns or int(pair["end_ns"]) > effective_end_ns
            for pair in overlapping_pairs
        )

    if window_start_ns is None:
        clipped_duration_ns = sum(int(pair["duration_ns"]) for pair in overlapping_pairs)
    else:
        clipped_duration_ns = 0
        for pair in overlapping_pairs:
            begin_ns = max(int(pair["begin_ns"]), window_start_ns)
            end_ns = int(pair["end_ns"])
            if window_end_ns is not None:
                end_ns = min(end_ns, window_end_ns)
            clipped_duration_ns += max(0, end_ns - begin_ns)
    started_duration_ns = sum(int(pair["duration_ns"]) for pair in selected_pairs)

    return {
        "valid": not invalid_reasons,
        "invalid_reasons": invalid_reasons,
        "event_counts": dict(window_counts),
        "global_event_counts": dict(all_counts),
        "direct_reclaim_count": len(selected_pairs) if not invalid_reasons else None,
        "direct_reclaim_total_duration_ms": clipped_duration_ns / 1e6,
        "direct_reclaim_started_full_duration_ms": started_duration_ns / 1e6,
        "direct_reclaim_boundary_spanning_count": boundary_spanning_count,
        "kswapd_wake_count": window_counts["kswapd_wake"],
        "oom_mark_victim_count": window_counts["oom_mark_victim"],
        "by_comm": {comm: dict(counter) for comm, counter in sorted(by_comm.items())},
        "parse_errors": parse_errors,
        "pairing_errors": pairing_errors,
        "lost_events_detected": lost_events_detected,
        "stderr_excerpt": stderr_excerpt,
        "window_start_ns": window_start_ns,
        "window_stop_ns": window_end_ns,
        "window_duration_s": duration_s,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--stderr")
    parser.add_argument("--start-file")
    parser.add_argument("--stop-file")
    parser.add_argument("--duration", type=float)
    args = parser.parse_args(argv)
    start_ns = None
    marker_errors: list[str] = []
    if args.start_file:
        try:
            marker = json.loads(Path(args.start_file).read_text(encoding="utf-8"))
            start_ns = int(marker["monotonic_ns"])
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            marker_errors.append(f"invalid start marker: {type(error).__name__}: {error}")
    stop_ns = None
    if args.stop_file:
        try:
            marker = json.loads(Path(args.stop_file).read_text(encoding="utf-8"))
            stop_ns = int(marker["monotonic_ns"])
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            marker_errors.append(f"invalid stop marker: {type(error).__name__}: {error}")
    result = parse_events(args.input, args.stderr, start_ns, args.duration, stop_ns)
    if marker_errors:
        result["valid"] = False
        result["invalid_reasons"].extend(marker_errors)
        result["direct_reclaim_count"] = None
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 4


if __name__ == "__main__":
    raise SystemExit(main())
