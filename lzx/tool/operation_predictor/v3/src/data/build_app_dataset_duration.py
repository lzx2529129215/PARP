#!/usr/bin/env python3
"""Build duration-aware LSApp datasets with switch-aware labels."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, TextIO


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SOURCE = ROOT / "data" / "raw" / "lsapp" / "app_events.csv"
DEFAULT_TSV = ROOT / "data" / "raw" / "datasets" / "LSApp" / "after_mapping" / "add_opened_apps" / "lsapp_mapped_with_opened.clean.tsv"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "processed" / "app_lstm_duration_gap3600_periodic180"
DEFAULT_GROUP = "通用用户"
HORIZONS = [3, 5, 10]
NAME_FIXES = {"大宗点评": "大众点评"}
APP_NAME_MAP = {"WPS": "WPS", "QQ": "腾讯QQ", "FILES": "图库"}
UNKNOWN_VALUES = {"", "UNKNOWN", "None", "none", "null", "NULL"}


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def format_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def format_duration(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.3f}".rstrip("0").rstrip(".")


def open_text(path: Path) -> TextIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="")
    return path.open("r", encoding="utf-8", errors="replace", newline="")


def load_vocab(path: str | Path) -> dict[str, int]:
    return {app: int(app_id) for app, app_id in json.loads(Path(path).read_text(encoding="utf-8")).items()}


def split_apps(value: str, sep: str = ";") -> list[str]:
    return [item.strip() for item in value.split(sep) if item.strip()] if value else []


def dedupe_keep_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def normalize_name(name: str | None) -> str:
    raw = "" if name is None else str(name).strip()
    return NAME_FIXES.get(raw, raw)


def map_app(raw_app: str | None, app_vocab: dict[str, int]) -> str:
    raw = normalize_name(raw_app)
    if raw in UNKNOWN_VALUES:
        return "<UNKNOWN>"
    mapped = APP_NAME_MAP.get(raw, raw)
    return mapped if mapped in app_vocab else "<UNKNOWN>"


def multihot(apps: Iterable[str], app_vocab: dict[str, int], include_unknown: bool = False) -> str:
    vec = ["0"] * len(app_vocab)
    for app in apps:
        if app == "<PAD>" or (app == "<UNKNOWN>" and not include_unknown):
            continue
        app_id = app_vocab.get(app)
        if app_id is not None:
            vec[app_id] = "1"
    return "|".join(vec)


def read_app_events(path: Path, app_vocab: dict[str, int]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"user_id", "timestamp", "foreground_app", "opened_apps", "user_group"}
        if not reader.fieldnames or required - set(reader.fieldnames):
            raise ValueError(f"missing app_events fields in {path}")
        for row in reader:
            mapped_fg = map_app(row.get("foreground_app"), app_vocab)
            opened = [map_app(app, app_vocab) for app in split_apps(row.get("opened_apps", ""), ";")]
            rows.append({
                "user_id": row.get("user_id", ""),
                "timestamp": row.get("timestamp", ""),
                "foreground_app": mapped_fg,
                "raw_foreground_app": row.get("foreground_app", ""),
                "opened_apps": ";".join(dedupe_keep_order(app for app in opened if app != "<UNKNOWN>")),
                "user_group": row.get("user_group") or DEFAULT_GROUP,
            })
    return rows


def find_column(fieldnames: Iterable[str], target: str, fallback: int) -> str:
    fields = list(fieldnames)
    lowered = [field.strip().lower() for field in fields]
    if target in lowered:
        return fields[lowered.index(target)]
    for field, lowered_field in zip(fields, lowered):
        if target == "user_id" and lowered_field.endswith("user_id"):
            return field
    if fallback < len(fields):
        return fields[fallback]
    raise ValueError(f"cannot find column {target!r}")


def read_lsapp_tsv(path: Path, app_vocab: dict[str, int], user_group: str, limit: int = 0) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    opened_by_session: dict[tuple[str, str], list[str]] = {}
    with open_text(path) as fin:
        reader = csv.DictReader(fin, delimiter="\t")
        if not reader.fieldnames:
            raise ValueError(f"empty LSApp input: {path}")
        user_col = find_column(reader.fieldnames, "user_id", 0)
        session_col = find_column(reader.fieldnames, "session_id", 1)
        ts_col = find_column(reader.fieldnames, "timestamp", 2)
        app_col = find_column(reader.fieldnames, "app_name", 3)
        event_col = find_column(reader.fieldnames, "event_type", 4)
        for read_count, row in enumerate(reader, start=1):
            event_type = row.get(event_col, "").strip()
            if event_type not in {"Opened", "Closed", "User Interaction"}:
                continue
            raw_app = normalize_name(row.get(app_col, ""))
            app = map_app(raw_app, app_vocab)
            key = (row.get(user_col, "").strip(), row.get(session_col, "").strip())
            opened = opened_by_session.setdefault(key, [])
            if app != "<UNKNOWN>":
                if event_type == "Closed":
                    opened[:] = [item for item in opened if item != app]
                elif app not in opened:
                    opened.append(app)
            if event_type not in {"Opened", "User Interaction"}:
                continue
            rows.append({
                "user_id": key[0],
                "timestamp": row.get(ts_col, "").strip(),
                "foreground_app": app,
                "raw_foreground_app": raw_app,
                "opened_apps": ";".join(dedupe_keep_order(opened)),
                "user_group": user_group,
            })
            if limit and read_count >= limit:
                break
    return rows


def resolve_rows(args: argparse.Namespace, app_vocab: dict[str, int]) -> tuple[str, Path, list[dict[str, str]]]:
    source = Path(args.source_file or args.input or DEFAULT_SOURCE)
    if source.exists():
        if source.suffix == ".csv":
            return "app_events_csv", source, read_app_events(source, app_vocab)
        return "lsapp_tsv", source, read_lsapp_tsv(source, app_vocab, args.user_group, args.limit)
    if DEFAULT_TSV.exists():
        return "lsapp_tsv", DEFAULT_TSV, read_lsapp_tsv(DEFAULT_TSV, app_vocab, args.user_group, args.limit)
    raise FileNotFoundError(f"Cannot find LSApp source. Checked {source} and {DEFAULT_TSV}")


def note_for(raw_app: str, gap_cut: bool, last_fallback: bool) -> str:
    notes: list[str] = []
    if raw_app == "FILES":
        notes.append("mapped FILES to 图库")
    if raw_app == "QQ":
        notes.append("mapped QQ to 腾讯QQ")
    if raw_app in UNKNOWN_VALUES:
        notes.append("mapped unknown to <UNKNOWN>")
    if gap_cut:
        notes.append("session gap cut")
    if last_fallback:
        notes.append("last segment dwell fallback 1s")
    return "; ".join(notes)


def build_segments(rows: list[dict[str, str]], max_gap_s: float) -> tuple[list[dict[str, str]], dict[str, int]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    bad_ts = 0
    for row in rows:
        try:
            row["_timestamp"] = parse_time(row["timestamp"])  # type: ignore[index]
        except Exception:
            bad_ts += 1
            continue
        grouped[row["user_id"]].append(row)

    segments: list[dict[str, str]] = []
    gap_cut_count = 0
    session_count = 0
    last_fallback = 0
    for user_id, events in grouped.items():
        events.sort(key=lambda item: item["_timestamp"])  # type: ignore[index]
        current: dict[str, str] | None = None
        last_ts: datetime | None = None
        session_idx = -1
        per_session_segment: Counter[int] = Counter()

        def close_current(end_ts: datetime, fallback: bool = False) -> None:
            nonlocal current, last_fallback
            if current is None:
                return
            dwell = 1.0 if fallback else max(1.0, (end_ts - parse_time(current["start_time"])).total_seconds())
            current["end_time"] = format_time(end_ts if not fallback else parse_time(current["start_time"]))
            current["dwell_s"] = format_duration(dwell)
            current["source_note"] = note_for(current.get("raw_app", ""), current.get("gap_cut_before") == "1", fallback)
            if fallback:
                last_fallback += 1
            segments.append(current)
            current = None

        for event in events:
            app = event["foreground_app"]
            ts = event["_timestamp"]  # type: ignore[index]
            gap_cut = bool(last_ts is not None and (ts - last_ts).total_seconds() > max_gap_s)
            if current is None or gap_cut:
                if current is not None:
                    gap_cut_count += int(gap_cut)
                    close_current(last_ts or ts)
                session_idx += 1
                session_count += 1
                current = {
                    "user_id": user_id,
                    "session_id": f"{user_id}:{session_idx}",
                    "segment_id": str(per_session_segment[session_idx]),
                    "start_time": format_time(ts),
                    "end_time": "",
                    "app": app,
                    "raw_app": event.get("raw_foreground_app", ""),
                    "dwell_s": "",
                    "opened_apps_start": event.get("opened_apps", ""),
                    "user_group": event.get("user_group") or DEFAULT_GROUP,
                    "gap_cut_before": "1" if gap_cut else "0",
                    "source_note": "",
                }
                per_session_segment[session_idx] += 1
            elif current["app"] != app:
                close_current(ts)
                current = {
                    "user_id": user_id,
                    "session_id": f"{user_id}:{session_idx}",
                    "segment_id": str(per_session_segment[session_idx]),
                    "start_time": format_time(ts),
                    "end_time": "",
                    "app": app,
                    "raw_app": event.get("raw_foreground_app", ""),
                    "dwell_s": "",
                    "opened_apps_start": event.get("opened_apps", ""),
                    "user_group": event.get("user_group") or DEFAULT_GROUP,
                    "gap_cut_before": "0",
                    "source_note": "",
                }
                per_session_segment[session_idx] += 1
            last_ts = ts
        if current is not None:
            close_current(parse_time(current["start_time"]), fallback=True)

    segments.sort(key=lambda item: (item["user_id"], item["session_id"], int(item["segment_id"])))
    return segments, {
        "users": len(grouped),
        "bad_timestamps": bad_ts,
        "last_segment_fallback_1s": last_fallback,
        "session_gap_cut_count": gap_cut_count,
        "session_count": session_count,
    }


def pad_history(apps: list[str], durations: list[float], history_len: int) -> tuple[list[str], list[str], list[str]]:
    apps = apps[-history_len:]
    durations = durations[-history_len:]
    pad = max(0, history_len - len(apps))
    return ["<PAD>"] * pad + apps, ["0"] * pad + [format_duration(d) for d in durations], ["0"] * pad + ["1"] * len(apps)


def future_labels(user_segments: list[dict[str, str]], idx: int, anchor_time: datetime) -> dict[int, list[str]]:
    labels: dict[int, set[str]] = {h: set() for h in HORIZONS}
    max_until = anchor_time + timedelta(minutes=max(HORIZONS))
    for future in user_segments[idx:]:
        start = parse_time(future["start_time"])
        end = parse_time(future["end_time"])
        if start > max_until:
            break
        if end < anchor_time:
            continue
        app = future["app"]
        if app in {"<PAD>", "<UNKNOWN>"}:
            continue
        for horizon in HORIZONS:
            if start <= anchor_time + timedelta(minutes=horizon) and end >= anchor_time:
                labels[horizon].add(app)
    return {h: sorted(v) for h, v in labels.items()}


def build_anchors(segment: dict[str, str], args: argparse.Namespace) -> list[tuple[str, datetime, float]]:
    start = parse_time(segment["start_time"])
    dwell = max(1.0, float(segment["dwell_s"]))
    anchors = [("foreground_transition", start, 1.0)]
    if args.anchor_mode == "event_plus_periodic":
        step = float(args.periodic_anchor_s)
        elapsed = step
        while elapsed < dwell:
            anchors.append((f"periodic_refresh_{int(step)}s", start + timedelta(seconds=elapsed), elapsed))
            elapsed += step
    if args.enable_debug_dwell_buckets:
        for bucket in [float(x) for x in args.dwell_buckets.split(",") if x.strip()]:
            if dwell >= bucket:
                anchors.append((f"dwell_bucket_cross:{format_duration(bucket)}s", start + timedelta(seconds=bucket), bucket))
    anchors.sort(key=lambda item: (item[1], item[0]))
    return anchors


def build_samples(segments: list[dict[str, str]], app_vocab: dict[str, int], group_vocab: dict[str, int], args: argparse.Namespace) -> tuple[list[dict[str, str]], dict[str, float], list[float]]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for segment in segments:
        grouped[segment["session_id"]].append(segment)

    samples: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    durations_all: list[float] = []
    current_in_labels: Counter[int] = Counter()
    non_empty_next: Counter[int] = Counter()

    for _session_id, user_segments in grouped.items():
        user_segments.sort(key=lambda item: parse_time(item["start_time"]))
        for idx, segment in enumerate(user_segments):
            next_segment = user_segments[idx + 1] if idx + 1 < len(user_segments) else None
            next_app = str(next_segment.get("app", "")) if next_segment else ""
            has_next_switch = bool(
                next_app
                and next_app not in {"<PAD>", "<UNKNOWN>"}
                and next_app != segment["app"]
            )
            for trigger_type, anchor_time, elapsed_s in build_anchors(segment, args):
                previous = user_segments[max(0, idx - args.history_len + 1): idx]
                history_segments = previous + [{**segment, "dwell_s": format_duration(elapsed_s)}]
                history_apps, history_durations, history_mask = pad_history(
                    [item["app"] for item in history_segments],
                    [float(item["dwell_s"]) for item in history_segments],
                    args.history_len,
                )
                labels = future_labels(user_segments, idx, anchor_time)
                if not any(labels.values()) and not has_next_switch:
                    continue
                current_app = segment["app"]
                labels_next = {h: [app for app in labels[h] if app != current_app] for h in HORIZONS}
                opened = split_apps(segment.get("opened_apps_start", ""), ";")
                group_id = group_vocab.get(segment.get("user_group", DEFAULT_GROUP), group_vocab.get(DEFAULT_GROUP, 0))
                row: dict[str, str] = {
                    "user_id": segment["user_id"],
                    "session_id": segment["session_id"],
                    "timestamp": format_time(anchor_time),
                    "trigger_type": trigger_type,
                    "anchor_type": trigger_type.split(":")[0],
                    "current_app": current_app,
                    "current_app_id": str(app_vocab.get(current_app, app_vocab["<UNKNOWN>"])),
                    "next_app": next_app if has_next_switch else "",
                    "next_app_id": str(app_vocab.get(next_app, -1)) if has_next_switch else "",
                    "has_next_switch": "1" if has_next_switch else "0",
                    "history_apps": "|".join(history_apps),
                    "history_durations_s": "|".join(history_durations),
                    "history_mask": "|".join(history_mask),
                    "opened_apps": "|".join(opened),
                    "opened_apps_multihot": multihot(opened, app_vocab),
                    "user_group": str(int(group_id)),
                    "user_group_name": segment.get("user_group", DEFAULT_GROUP),
                }
                for horizon in HORIZONS:
                    row[f"labels_{horizon}"] = "|".join(labels[horizon])
                    row[f"labels_{horizon}_multihot"] = multihot(labels[horizon], app_vocab)
                    row[f"labels_next_{horizon}"] = "|".join(labels_next[horizon])
                    row[f"labels_next_{horizon}_multihot"] = multihot(labels_next[horizon], app_vocab)
                    row[f"has_next_{horizon}"] = "1" if labels_next[horizon] else "0"
                    current_in_labels[horizon] += int(current_app in labels[horizon])
                    non_empty_next[horizon] += int(bool(labels_next[horizon]))
                durations_all.extend(float(v) for v, m in zip(history_durations, history_mask) if m == "1")
                samples.append(row)
                counts[trigger_type.split(":")[0]] += 1

    total = max(1, len(samples))
    stats: dict[str, float] = {
        "foreground_transition_anchor_count": counts["foreground_transition"],
        "periodic_refresh_anchor_count": sum(v for k, v in counts.items() if k.startswith("periodic_refresh_")),
        "dwell_bucket_cross_anchor_count": counts["dwell_bucket_cross"],
    }
    for horizon in HORIZONS:
        stats[f"current_app_in_labels_{horizon}_ratio"] = current_in_labels[horizon] / total
        stats[f"non_empty_labels_next_{horizon}_ratio"] = non_empty_next[horizon] / total
    return samples, stats, durations_all


def split_samples(samples: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    samples = sorted(samples, key=lambda item: item["timestamp"])
    n = len(samples)
    train_end = int(n * 0.70)
    val_end = train_end + int(n * 0.15)
    return samples[:train_end], samples[train_end:val_end], samples[val_end:]


def write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in fields} for row in rows])


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, int(round((len(values) - 1) * p))))
    return values[idx]


def duration_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "p50": 0.0, "p90": 0.0, "p99": 0.0, "max": 0.0}
    return {"min": min(values), "p50": percentile(values, 0.5), "p90": percentile(values, 0.9), "p99": percentile(values, 0.99), "max": max(values)}


def has_adjacent_repeats(samples: list[dict[str, str]]) -> bool:
    for sample in samples:
        apps = [a for a, m in zip(sample["history_apps"].split("|"), sample["history_mask"].split("|")) if m == "1"]
        if any(a == b for a, b in zip(apps, apps[1:])):
            return True
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build duration-aware LSApp app LSTM dataset.")
    parser.add_argument("--source-file", help="LSApp app_events.csv or mapped TSV.")
    parser.add_argument("--input", help="Backward-compatible alias for --source-file.")
    parser.add_argument("--app-vocab", default=str(ROOT / "data" / "vocab" / "app_vocab_duration.json"))
    parser.add_argument("--group-vocab", default=str(ROOT / "data" / "vocab" / "user_group_vocab.json"))
    parser.add_argument("--history-len", type=int, default=5)
    parser.add_argument("--duration-cap-s", type=float, default=600.0)
    parser.add_argument("--max-session-gap-s", type=float, default=3600.0)
    parser.add_argument("--anchor-mode", choices=["event_plus_periodic"], default="event_plus_periodic")
    parser.add_argument("--periodic-anchor-s", type=float, default=180.0)
    parser.add_argument("--enable-debug-dwell-buckets", action="store_true")
    parser.add_argument("--dwell-buckets", default="5,15,30,60")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--user-group", default=DEFAULT_GROUP)
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app_vocab = load_vocab(args.app_vocab)
    group_vocab = load_vocab(args.group_vocab)
    source_kind, source_path, rows = resolve_rows(args, app_vocab)
    segments, segment_stats = build_segments(rows, args.max_session_gap_s)
    samples, sample_stats, durations = build_samples(segments, app_vocab, group_vocab, args)
    if not samples:
        raise ValueError("no duration-aware samples generated")
    train, val, test = split_samples(samples)

    output_dir = Path(args.output_dir)
    fields = [
        "user_id", "session_id", "timestamp", "trigger_type", "anchor_type", "current_app", "current_app_id",
        "next_app", "next_app_id", "has_next_switch",
        "history_apps", "history_durations_s", "history_mask", "opened_apps", "opened_apps_multihot",
        "user_group", "user_group_name",
        "labels_3", "labels_5", "labels_10",
        "labels_3_multihot", "labels_5_multihot", "labels_10_multihot",
        "labels_next_3", "labels_next_5", "labels_next_10",
        "labels_next_3_multihot", "labels_next_5_multihot", "labels_next_10_multihot",
        "has_next_3", "has_next_5", "has_next_10",
    ]
    segment_fields = ["user_id", "session_id", "segment_id", "start_time", "end_time", "app", "dwell_s", "gap_cut_before", "source_note"]
    write_csv(output_dir / "lsapp_app_state_segments.csv", segment_fields, segments)
    write_csv(output_dir / "train.csv", fields, train)
    write_csv(output_dir / "val.csv", fields, val)
    write_csv(output_dir / "test.csv", fields, test)
    meta = {
        "history_len": args.history_len,
        "duration_cap_s": args.duration_cap_s,
        "max_session_gap_s": args.max_session_gap_s,
        "anchor_mode": args.anchor_mode,
        "periodic_anchor_s": args.periodic_anchor_s,
        "debug_dwell_buckets_enabled": bool(args.enable_debug_dwell_buckets),
        "num_apps": len(app_vocab),
        "pad_id": app_vocab["<PAD>"],
        "unknown_id": app_vocab["<UNKNOWN>"],
        "horizons": HORIZONS,
        "split": "time_ordered_70_15_15",
        "source": "modified LSApp",
        "source_kind": source_kind,
        "source_path": str(source_path),
        "label_mode": "both_persistence_and_switch",
        "labels_persistence": ["labels_3", "labels_5", "labels_10"],
        "labels_switch": ["labels_next_3", "labels_next_5", "labels_next_10"],
        "no_repeated_token_for_duration": not has_adjacent_repeats(samples),
        "rows_loaded": len(rows),
        "segments": len(segments),
        "samples": len(samples),
        "num_samples": len(samples),
        "train_samples": len(train),
        "val_samples": len(val),
        "test_samples": len(test),
        "duration_stats": duration_stats(durations),
        **segment_stats,
        **sample_stats,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "dataset_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"source: {source_path}")
    print(f"segments: {len(segments)}")
    print(f"samples: {len(samples)}")
    print(f"train/val/test: {len(train)}/{len(val)}/{len(test)}")
    print(f"session_gap_cut_count: {segment_stats['session_gap_cut_count']}")
    print(f"session_count: {segment_stats['session_count']}")
    print(f"foreground_transition anchors: {sample_stats['foreground_transition_anchor_count']}")
    print(f"periodic_refresh anchors: {sample_stats['periodic_refresh_anchor_count']}")
    print(f"duration stats: {meta['duration_stats']}")
    print(f"saved to: {output_dir}")


if __name__ == "__main__":
    main()
