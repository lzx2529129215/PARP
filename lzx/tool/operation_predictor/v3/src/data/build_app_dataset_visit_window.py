#!/usr/bin/env python3
"""Keep the LSApp preprocessing/anchors, relabel entry windows without leakage."""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from v3.src.data.build_app_dataset_duration import (read_app_events, build_segments,
    build_anchors, pad_history, parse_time, format_time, load_vocab, write_csv)

HORIZONS = (30, 180)
VISIT_DEFINITION = "foreground_entry_strictly_after_anchor_inclusive_deadline"


def window_labels(entries, anchor, observed_until, app_vocab):
    """Unknown negatives are masked per app/window; positives remain usable."""
    labels, masks = {}, {}
    real_apps = [a for a in app_vocab if not a.startswith("<")]
    for horizon in HORIZONS:
        deadline = anchor + timedelta(seconds=horizon)
        positive = {app for when, app in entries if anchor < when <= min(deadline, observed_until)
                    and app in real_apps}
        labels[horizon] = sorted(positive)
        masks[horizon] = [int(a in positive or (a in real_apps and deadline <= observed_until))
                          for a, _ in sorted(app_vocab.items(), key=lambda item: item[1])]
    return labels, masks


def build_dataset(rows, app_vocab, group_vocab, args):
    segments, segment_stats = build_segments(rows, args.max_session_gap_s)
    sessions = defaultdict(list)
    events_by_user = defaultdict(list)
    for row in rows:
        if "_timestamp" in row:
            events_by_user[row["user_id"]].append(row)
    for events in events_by_user.values():
        events.sort(key=lambda r: r["_timestamp"])
    times_by_user = {u: [r["_timestamp"] for r in events] for u, events in events_by_user.items()}
    for segment in segments:
        sessions[segment["session_id"]].append(segment)
    # Preserve exact last observed timestamps, including repeated events in the
    # final segment. The legacy 1s fallback is not evidence of future observation.
    observed = {}
    user_sessions = defaultdict(list)
    for sid, items in sessions.items():
        user_sessions[items[0]["user_id"]].append(sid)
    for user, ids in user_sessions.items():
        ids.sort(key=lambda sid: parse_time(sessions[sid][0]["start_time"]))
        times = times_by_user[user]
        for pos, sid in enumerate(ids):
            stop = parse_time(sessions[ids[pos + 1]][0]["start_time"]) if pos + 1 < len(ids) else None
            end = bisect.bisect_left(times, stop) - 1 if stop else len(times) - 1
            observed[sid] = times[end]
    anchors = []
    for sid, items in sessions.items():
        items.sort(key=lambda s: parse_time(s["start_time"]))
        for idx, segment in enumerate(items):
            for trigger, anchor, elapsed in build_anchors(segment, args):
                anchors.append((anchor, sid, idx, trigger, elapsed))
    anchors.sort(key=lambda x: x[0])
    n = len(anchors)
    cut1, cut2 = int(n * .70), int(n * .70) + int(n * .15)
    # Keep equal timestamps together: a timestamp cannot belong to two splits.
    times = [a[0] for a in anchors]
    cut1 = bisect.bisect_left(times, times[cut1]) if cut1 < n else n
    cut2 = bisect.bisect_left(times, times[cut2]) if cut2 < n else n
    bounds = [0, cut1, cut2, n]
    output = {}
    for split, begin, end in zip(("train", "val", "test"), bounds, bounds[1:]):
        partition_end = times[end] - timedelta(microseconds=1) if end < n else None
        split_rows = []
        for anchor, sid, idx, trigger, elapsed in anchors[begin:end]:
            items = sessions[sid]
            segment = items[idx]
            history = items[max(0, idx - args.history_len + 1):idx]
            apps, durations, mask = pad_history([s["app"] for s in history] + [segment["app"]],
                [float(s["dwell_s"]) for s in history] + [elapsed], args.history_len)
            until = min(observed[sid], partition_end) if partition_end else observed[sid]
            future = []
            for item in items[idx + 1:]:
                start = parse_time(item["start_time"])
                if start > min(anchor + timedelta(seconds=180), until):
                    break
                future.append((start, item["app"]))
            labels, masks = window_labels(future, anchor, until, app_vocab)
            # Same opened-app feature as the prior duration dataset.
            opened = segment.get("opened_apps_start", "").replace(";", "|")
            row = {
                "user_id": segment["user_id"], "session_id": sid, "timestamp": format_time(anchor),
                "trigger_type": trigger, "current_app": segment["app"],
                "history_apps": "|".join(apps), "history_durations_s": "|".join(durations),
                "history_mask": "|".join(mask), "opened_apps": opened,
                "user_group": str(group_vocab[segment["user_group"]]),
                "observed_until": until.isoformat(sep=" "),
            }
            for h in HORIZONS:
                row[f"labels_visit_{h}s"] = "|".join(labels[h])
                row[f"valid_visit_{h}s"] = "|".join(map(str, masks[h]))
            split_rows.append(row)
        output[split] = split_rows
    segment_export = [{**s, "observed_until": observed[s["session_id"]].isoformat(sep=" ")}
                      for s in segments]
    return output, segment_export, segment_stats


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-file", default=str(ROOT / "data/lsapp_expanded/raw/app_events.csv"))
    p.add_argument("--app-vocab", default=str(ROOT / "data/vocab/lsapp_expanded/app_vocab_duration.json"))
    p.add_argument("--group-vocab", default=str(ROOT / "data/vocab/lsapp_expanded/user_group_vocab.json"))
    p.add_argument("--output-dir", required=True)
    p.add_argument("--history-len", type=int, default=5)
    p.add_argument("--duration-cap-s", type=float, default=600.)
    p.add_argument("--max-session-gap-s", type=float, default=3600.)
    p.add_argument("--periodic-anchor-s", type=float, default=180.)
    args = p.parse_args()
    args.anchor_mode = "event_plus_periodic"
    args.enable_debug_dwell_buckets = False
    if min(args.history_len, args.duration_cap_s, args.max_session_gap_s, args.periodic_anchor_s) <= 0:
        p.error("history and time parameters must be positive")
    out = Path(args.output_dir)
    if out.exists():
        raise FileExistsError(f"output must be new: {out}")
    app_vocab, group_vocab = load_vocab(args.app_vocab), load_vocab(args.group_vocab)
    rows = read_app_events(Path(args.source_file), app_vocab)
    data, segments, stats = build_dataset(rows, app_vocab, group_vocab, args)
    if any(not values for values in data.values()):
        raise ValueError("all three time partitions must be nonempty")
    out.mkdir(parents=True)
    for split, values in data.items():
        write_csv(out / f"{split}.csv", list(values[0]), values)
    write_csv(out / "segments.csv", list(segments[0]), segments)
    meta = {"schema_version": 1, "horizons_s": HORIZONS, "visit_definition": VISIT_DEFINITION,
            "app_vocab": app_vocab, "group_vocab": group_vocab, "args": vars(args),
            "source_sha256": hashlib.sha256(Path(args.source_file).read_bytes()).hexdigest(),
            "split": "time_ordered_70_15_15_equal_timestamps_kept_together",
            "segment_stats": stats, "splits": {}}
    for split, values in data.items():
        meta["splits"][split] = {"samples": len(values), "start": values[0]["timestamp"],
            "end": values[-1]["timestamp"], **{f"valid_app_labels_{h}s": sum(
                sum(map(int, row[f"valid_visit_{h}s"].split("|"))) for row in values) for h in HORIZONS}}
    (out / "dataset_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
