#!/usr/bin/env python3
"""Train and evaluate dependency-free Phase2.7 page-state predictors."""

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
import time

from .baselines import GlobalFrequency, LastWindow, RecentFrequency
from .evaluate import multilabel_metrics
from .feature_encoder import FeatureEncoder
from .state_cluster import LightweightKMeans
from .state_markov import PageStateMarkov, duration_bin


def read_csv(path):
    with Path(path).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()
            if line.strip()]


def load_sessions(dataset, split):
    path = Path(dataset) / "splits" / f"{split}_sessions.txt"
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def assemble(dataset):
    dataset = Path(dataset)
    windows = {row["window_id"]: row for row in read_csv(dataset / "windows_10s.csv")}
    anon = {row["window_id"]: row for row in read_csv(dataset / "anon_windows_10s.csv")}
    active = {10: defaultdict(set), 100: defaultdict(set), 1000: defaultdict(set)}
    coverage10 = defaultdict(list)
    coverage100 = defaultdict(list)
    file_ids = defaultdict(set)
    for resolution in (10, 100):
        for row in read_csv(dataset / f"file_segments_l{resolution}.csv"):
            key = row["window_id"]
            segment = f'{row["file_id"]}:{resolution}:{row["segment_id"]}'
            coverage = float(row["coverage_ratio"])
            file_ids[key].add(row["file_id"])
            if coverage > 0 or float(row["weighted_coverage_ratio"]) > 0:
                active[resolution][key].add(segment)
            (coverage10 if resolution == 10 else coverage100)[key].append(coverage)
    sparse_top = defaultdict(list)
    for row in read_jsonl(dataset / "file_segments_l1000.jsonl"):
        key = row["window_id"]
        file_ids[key].add(row["file_id"])
        pairs = list(zip(row["active_segment_ids"], row["coverage_values"]))
        for segment, coverage in pairs:
            active[1000][key].add(f'{row["file_id"]}:1000:{segment}')
            sparse_top[key].append(float(coverage))
    rows = []
    for window in sorted(windows.values(), key=lambda row:
                         (row["session_id"], int(row["window_start_ns"]))):
        key = window["window_id"]
        l100 = coverage100[key]
        total = float(anon.get(key, {}).get("anon_total_bytes", 0) or 0)
        row = {
            "window_id": key, "session_id": window["session_id"],
            "window_start_ns": int(window["window_start_ns"]),
            "file_id": sorted(file_ids[key])[0] if file_ids[key] else "",
            "coverage_l10": sorted(coverage10[key], reverse=True)[:10],
            "coverage_l100_summary": [
                min(l100) if l100 else 0, max(l100) if l100 else 0,
                sum(l100) / len(l100) if l100 else 0,
                sum(value > 0 for value in l100) / len(l100) if l100 else 0],
            "l1000_topk": sorted(sparse_top[key], reverse=True)[:16],
            "file_count": int(window.get("file_count", 0) or 0),
            "active_file_count": int(window.get("active_file_count", 0) or 0),
            "anon_hot_ratio": float(anon.get(key, {}).get("anon_hot_bytes", 0) or 0) / total if total else 0,
            "anon_cooling_ratio": float(anon.get(key, {}).get("anon_cooling_ratio", 0) or 0),
            "foreground": window.get("foreground_state") == "FOREGROUND",
            "rss_bytes": float(window.get("rss_bytes", 0) or 0),
            "pss_bytes": float(window.get("pss_bytes", 0) or 0),
            "swap_bytes": float(window.get("swap_bytes", 0) or 0),
            "operation": window.get("dominant_operation", "UNKNOWN"),
            "active": {resolution: active[resolution][key]
                       for resolution in (10, 100, 1000)},
        }
        rows.append(row)
    return rows


def sequence_pairs(rows, steps):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["session_id"]].append(row)
    for session_rows in grouped.values():
        session_rows.sort(key=lambda row: row["window_start_ns"])
        for index in range(len(session_rows) - steps):
            yield session_rows[index], session_rows[index + steps]


def state_segment_distribution(rows, states, resolution):
    counts = defaultdict(Counter)
    totals = Counter()
    for row, state in zip(rows, states):
        totals[state] += 1
        counts[state].update(row["active"][resolution])
    return {state: {segment: count / totals[state]
                    for segment, count in values.items()}
            for state, values in counts.items()}


def advance_state(model, current, steps):
    probabilities = {current: 1.0}
    for _ in range(steps):
        following = defaultdict(float)
        for state, probability in probabilities.items():
            transitions = model.predict(state, duration_bin(10)) or {state: 1.0}
            for next_state, transition_probability in transitions.items():
                following[next_state] += probability * transition_probability
        probabilities = dict(following)
    return probabilities


def combine_state_segments(state_probabilities, distributions):
    result = defaultdict(float)
    for state, state_probability in state_probabilities.items():
        for segment, probability in distributions.get(state, {}).items():
            result[segment] += state_probability * probability
    return dict(result)


def baseline_scores(model_name, train_sets, history):
    if model_name == "last_window":
        return LastWindow().predict(history)
    if model_name == "recent_frequency":
        return RecentFrequency(6).predict(history)
    return GlobalFrequency().fit(train_sets).predict(history)


def train_and_evaluate(dataset, models_dir, evaluation_dir, k_candidates=(8, 16, 32)):
    started = time.monotonic_ns()
    rows = assemble(dataset)
    split_rows = {}
    for split in ("train", "val", "test"):
        sessions = load_sessions(dataset, split)
        split_rows[split] = [row for row in rows if row["session_id"] in sessions]
    encoder = FeatureEncoder().fit(split_rows["train"])
    encoded = {split: encoder.transform(split_rows[split]) for split in split_rows}
    candidates = []
    for k in k_candidates:
        if int(k) > len(encoded["train"]):
            continue
        model = LightweightKMeans(int(k)).fit(encoded["train"])
        score = model.inertia(encoded["val"]) / max(1, len(encoded["val"]))
        candidates.append((score, int(k), model))
    if not candidates:
        raise ValueError("insufficient training rows for configured K candidates")
    validation_score, selected_k, cluster = min(candidates,
                                                 key=lambda item: (item[0], item[1]))
    states = {split: cluster.predict(encoded[split]) for split in split_rows}
    state_by_window = {row["window_id"]: state
                       for split in split_rows
                       for row, state in zip(split_rows[split], states[split])}
    transitions = []
    for current, following in sequence_pairs(split_rows["train"], 1):
        transitions.append((state_by_window[current["window_id"]],
                            duration_bin(10),
                            state_by_window[following["window_id"]]))
    markov = PageStateMarkov().fit(transitions)

    metrics = {10: {}, 30: {}, 60: {}}
    per_resolution = []
    for horizon in (10, 30, 60):
        steps = horizon // 10
        test_pairs = list(sequence_pairs(split_rows["test"], steps))
        for resolution in (10, 100, 1000):
            actual = [future["active"][resolution] for _, future in test_pairs]
            train_sets = [row["active"][resolution] for row in split_rows["train"]]
            distributions = state_segment_distribution(
                split_rows["train"], states["train"], resolution)
            model_scores = defaultdict(list)
            test_history = []
            for current, _ in test_pairs:
                test_history.append(current["active"][resolution])
                for name in ("last_window", "recent_frequency", "global_frequency"):
                    model_scores[name].append(baseline_scores(name, train_sets,
                                                              test_history))
                current_state = state_by_window[current["window_id"]]
                model_scores["page_state_markov"].append(combine_state_segments(
                    advance_state(markov, current_state, steps), distributions))
            for name, scores in model_scores.items():
                result = multilabel_metrics(actual, scores, top_k=10)
                metrics[horizon][f"level_{resolution}_{name}"] = result
                per_resolution.append({"horizon_s": horizon,
                                       "resolution": resolution,
                                       "model": name, **result})

    models_dir = Path(models_dir)
    evaluation_dir = Path(evaluation_dir)
    models_dir.mkdir(parents=True, exist_ok=True)
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "file_vocab.json").write_text(json.dumps(
        {"files": encoder.file_vocabulary, "fit_source": "train_only"},
        indent=2, sort_keys=True) + "\n")
    (models_dir / "feature_schema.json").write_text(
        json.dumps(encoder.schema(), indent=2, sort_keys=True) + "\n")
    (models_dir / "state_cluster.json").write_text(
        json.dumps(cluster.to_dict(), indent=2, sort_keys=True) + "\n")
    (models_dir / "state_markov.json").write_text(json.dumps(
        {"schema_version": 1, "semantics": "page_access_state_only",
         "transitions": markov.to_dict()}, indent=2, sort_keys=True) + "\n")
    metadata = {
        "schema_version": 1, "model_type": "page_state_markov",
        "selected_k": selected_k, "selection_source": "validation_only",
        "validation_inertia_per_row": validation_score,
        "train_rows": len(split_rows["train"]),
        "validation_rows": len(split_rows["val"]),
        "test_rows": len(split_rows["test"]),
        "future_features_used": False, "old_continue_reentry_used": False,
        "elapsed_ns": time.monotonic_ns() - started,
    }
    (models_dir / "model_metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    for horizon in (10, 30, 60):
        (evaluation_dir / f"metrics_{horizon}s.json").write_text(
            json.dumps(metrics[horizon], indent=2, sort_keys=True) + "\n")
    fields = list(per_resolution[0])
    with (evaluation_dir / "per_resolution.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(per_resolution)
    (evaluation_dir / "baseline_comparison.md").write_text(
        "# Baseline comparison\n\nGenerated for Last-window, Recent-frequency, "
        "Global-frequency, and Page-State Markov at L10/L100/L1000 and "
        "10s/30s/60s. See `per_resolution.csv`.\n")
    return metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--models", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--k", type=int, nargs="+", default=[8, 16, 32])
    args = parser.parse_args()
    print(json.dumps(train_and_evaluate(args.dataset, args.models,
                                        args.evaluation, args.k), sort_keys=True))


if __name__ == "__main__":
    main()
