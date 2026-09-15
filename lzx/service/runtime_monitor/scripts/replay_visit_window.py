#!/usr/bin/env python3
"""Replay held-out LSApp sessions with original timestamps and virtual 30s ticks.

No timestamp compression, wall-clock sleeps, GUI changes, kernel writes or
training occur. Both sampled and direct-event runners process identical state.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch

RUNTIME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME))
from layout import OPERATION_PREDICTOR_ROOT as ROOT
sys.path.insert(0, str(ROOT))
from online_visit_window import OnlineVisitWindowRunner
from v3.src.data.build_app_dataset_visit_window import window_labels
from v3.train.train_app_lstm_visit_window import metrics


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset-dir", default=str(ROOT / "data/lsapp_expanded/processed/app_visit_window_v1"))
    p.add_argument("--checkpoint", default=str(ROOT / "outputs/lsapp_expanded/visit_window_v1/app_lstm_visit_window.pt"))
    p.add_argument("--output-dir", required=True)
    p.add_argument("--sessions", type=int, default=20)
    args = p.parse_args()
    if args.sessions < 1:
        p.error("sessions must be positive")
    torch.set_num_threads(1)
    dataset = Path(args.dataset_dir)
    meta = json.loads((dataset / "dataset_meta.json").read_text())
    vocab = meta["app_vocab"]
    source = Path(meta["args"]["source_file"])
    if hashlib.sha256(source.read_bytes()).hexdigest() != meta["source_sha256"]:
        raise ValueError("source differs from the training dataset provenance")
    by_session = defaultdict(list)
    with (dataset / "segments.csv").open() as stream:
        for row in csv.DictReader(stream):
            by_session[row["session_id"]].append(row)
    for items in by_session.values():
        items.sort(key=lambda r: r["start_time"])
    candidates = sorted((sid for sid, items in by_session.items()
                         if items[0]["start_time"] >= meta["splits"]["test"]["start"]),
                        key=lambda sid: by_session[sid][0]["start_time"])
    selected = candidates[:args.sessions]
    if not selected:
        raise ValueError("no complete held-out sessions")
    users = {by_session[sid][0]["user_id"] for sid in selected}
    user_events = defaultdict(list)
    with source.open() as stream:
        for row in csv.DictReader(stream):
            if row["user_id"] in users:
                user_events[row["user_id"]].append(row)
    for events in user_events.values():
        events.sort(key=lambda r: r["timestamp"])
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=False)
    runner_args = argparse.Namespace(session_id="heldout-visit-replay", lstm_checkpoint=args.checkpoint,
        app_vocab=meta["args"]["app_vocab"], group_vocab=meta["args"]["group_vocab"],
        user_group="通用用户", device="cpu", history_len=meta["args"]["history_len"],
        duration_cap_s=meta["args"]["duration_cap_s"], app_key_to_vocab_name={a: a for a in vocab})
    sample = OnlineVisitWindowRunner(runner_args, out / "sample/model", out / "sample/review")
    direct = OnlineVisitWindowRunner(runner_args, out / "direct/model", out / "direct/review")
    if sample.predictor is None or direct.predictor is None:
        raise ValueError(sample.predictor_error or direct.predictor_error)
    predictions, labels, masks, eligible, evidence = [], [], [], [], []
    refreshes = 0
    max_difference = 0.
    summary = []
    try:
        for sid in selected:
            segments = by_session[sid]
            start, end = segments[0]["start_time"], segments[0]["observed_until"]
            raw = user_events[segments[0]["user_id"]]
            raw_times = [r["timestamp"] for r in raw]
            events = raw[bisect.bisect_left(raw_times, start):bisect.bisect_right(raw_times, end)]
            entry_times = [(dt.datetime.fromisoformat(s["start_time"]), s["app"]) for s in segments]
            observed = dt.datetime.fromisoformat(end)
            sample.reset_history()
            direct.reset_history()
            next_tick = None
            current = None
            calls_before = len(predictions)

            def consume(timestamp, state, is_event):
                nonlocal refreshes, max_difference
                feature = {"session_id": sid, "feature_window_id": f"{sid}:{timestamp}",
                           "timestamp": timestamp, "foreground_app": state["foreground_app"],
                           "open_apps": state["opened_apps"].replace(";", "|")}
                prev = direct.previous_row or {}
                changed = (direct.map_app(feature["foreground_app"]) != direct.map_app(prev.get("foreground_app", "")) or
                    set(direct.map_open_apps(feature["open_apps"])) != set(direct.map_open_apps(prev.get("open_apps", ""))))
                a = sample.process_sample(feature)
                b = direct.process_event(feature, "APP_SWITCH") if is_event and changed else direct.process_sample(feature)
                if a["inference_executed"] != b["inference_executed"]:
                    raise AssertionError("sample/direct trigger discrepancy")
                if not a["inference_executed"]:
                    return
                if a["status"] != "success" or b["status"] != "success":
                    raise AssertionError(f"inference failed: {a.get('skip_reason')} {b.get('skip_reason')}")
                for key in ("history_apps", "history_durations_s", "history_mask", "hot_apps", "cold_apps", "neutral_apps"):
                    if a[key] != b[key]:
                        raise AssertionError(f"sample/direct mismatch: {key}")
                pa = np.zeros((len(vocab), 2), np.float32)
                pb = np.zeros_like(pa)
                for r in a["all_probabilities"]:
                    pa[r["app_id"]] = [r["p_visit_30s"], r["p_visit_180s"]]
                for r in b["all_probabilities"]:
                    pb[r["app_id"]] = [r["p_visit_30s"], r["p_visit_180s"]]
                difference = float(np.abs(pa - pb).max())
                max_difference = max(max_difference, difference)
                if difference > 1e-6:
                    raise AssertionError("sample/direct prediction mismatch")
                when = dt.datetime.fromisoformat(timestamp)
                yy, mm = window_labels(entry_times, when, observed, vocab)
                y, m = np.zeros_like(pa), np.zeros_like(pa)
                for i, h in enumerate((30, 180)):
                    for app in yy[h]:
                        y[vocab[app], i] = 1
                    m[:, i] = mm[h]
                running = np.zeros(len(vocab), bool)
                for app in sample.map_open_apps(feature["open_apps"]):
                    running[vocab[app]] = True
                fg = sample.map_app(feature["foreground_app"])
                if fg in vocab:
                    running[vocab[fg]] = False
                predictions.append(pa)
                labels.append(y)
                masks.append(m)
                eligible.append(running)
                refreshes += int(a["trigger_type"] == "periodic_refresh_30s")
                evidence.append({"session_id": sid, "timestamp": timestamp, "trigger": a["trigger_type"],
                    "history_apps": a["history_apps"], "history_durations_s": a["history_durations_s"]})

            for event in events:
                when = dt.datetime.fromisoformat(event["timestamp"])
                while current is not None and next_tick < when:
                    consume(next_tick.isoformat(sep=" "), current, False)
                    next_tick += dt.timedelta(seconds=30)
                before = sample.last_prediction_time
                consume(event["timestamp"], event, True)
                current = event
                if sample.last_prediction_time != before or next_tick is None:
                    next_tick = when + dt.timedelta(seconds=30)
                elif next_tick <= when:
                    next_tick += dt.timedelta(seconds=30)
            summary.append({"session_id": sid, "start": start, "observed_until": end,
                            "source_events": len(events), "prediction_calls": len(predictions) - calls_before})
    finally:
        sample.close()
        direct.close()
    report = metrics(np.array(predictions), np.array(labels), np.array(masks), np.array(eligible), vocab)
    report.update(session_selection="first_complete_test_sessions_by_start_time", sessions=summary,
        sample_direct_max_probability_difference=max_difference, periodic_refresh_calls=refreshes,
        timestamp_policy="original timestamps with virtual 30s ticks; no time compression",
        checkpoint_sha256=hashlib.sha256(Path(args.checkpoint).read_bytes()).hexdigest())
    (out / "replay_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    np.savez_compressed(out / "replay_predictions.npz", probabilities=predictions, labels=labels, valid=masks, eligible=eligible)
    with (out / "replay_rows.jsonl").open("w") as stream:
        for row in evidence:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"sessions": len(selected), "predictions": len(predictions), "periodic_refresh_calls": refreshes,
        "sample_direct_max_probability_difference": max_difference, "thermal": report["thermal"]}, indent=2))


if __name__ == "__main__":
    main()
