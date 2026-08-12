#!/usr/bin/env python3
"""Build QQ candidates and evaluate Phase2.10 app-global reuse rankers.

This is an offline trace-proxy experiment.  It never writes a kernel, cgroup,
or PARP interface and never consumes future mixed-scenario A/B sessions.
"""

import argparse
from collections import Counter, defaultdict
import csv
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import time

from region_decode import decode_trace_line


FEATURES = (
    "delta_since_last_access", "delta_between_last_two",
    "delta_between_second_third", "file_last_delta", "file_previous_delta",
    "normalized_position", "file_size_log", "segment_size_log",
    "segment_ema", "file_ema", "segment_age", "file_age",
    "current_coverage", "weighted_coverage", "recent_access_count",
    "consecutive_inactive", "generation_proxy", "damon_hotness",
)
FORBIDDEN_FEATURE_FRAGMENTS = (
    "app_id", "operation", "session", "repeat", "path", "filename",
    "content", "contact", "message", "future",
)
TRAIN = {
    "WPS": ("wps_01",),
    "FILES": ("files_01",),
    "QQ": ("qq_train_01",),
}
CALIBRATION = {"WPS": ("wps_02",), "FILES": (), "QQ": ()}
EVALUATION = {
    "WPS": ("wps_03",),
    "FILES": ("files_02",),
    "QQ": ("qq_validation_01",),
}
WRONG = {"WPS": "FILES", "FILES": "QQ", "QQ": "WPS"}
MODEL_NAMES = ("GENERIC", "WPS", "FILES", "QQ")
WINDOW_NS = 10_000_000_000
POOL_SIZE = 128


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def stable_hash(value):
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def feature_value(name, value):
    value = float(value)
    if "delta" in name or name in (
        "segment_age", "file_age", "recent_access_count", "consecutive_inactive"
    ):
        return math.log1p(min(1_000_000, max(0, value)))
    return value


def candidate_vector(candidate):
    return [feature_value(name, candidate[name]) for name in FEATURES]


def bit_range(start, end, pages):
    effective = max(1, min(100, pages))
    if pages <= 0 or end <= start:
        return 0, ()
    start = max(0, min(pages, start))
    end = max(start, min(pages, end))
    if end <= start:
        return 0, ()
    low = min(effective - 1, start * effective // pages)
    high = min(effective - 1, (end - 1) * effective // pages)
    return ((1 << (high - low + 1)) - 1) << low, range(low, high + 1)


class WindowFile:
    __slots__ = (
        "pages", "size", "observed", "active", "weights", "age_sum",
        "age_count", "max_age", "ratio_sum", "region_count",
    )

    def __init__(self, pages, size):
        self.pages = max(1, int(pages))
        self.size = max(0, int(size))
        self.observed = 0
        self.active = 0
        self.weights = {}
        self.age_sum = 0.0
        self.age_count = 0
        self.max_age = 0.0
        self.ratio_sum = 0.0
        self.region_count = 0

    def add(self, start, pages, accesses, maximum, age):
        mask, segments = bit_range(int(start), int(start) + int(pages), self.pages)
        ratio = min(1.0, max(0.0, float(accesses) / max(1, int(maximum))))
        self.observed |= mask
        if accesses > 0:
            self.active |= mask
            for segment in segments:
                self.weights[segment] = max(self.weights.get(segment, 0.0), ratio)
        self.age_sum += float(age)
        self.age_count += 1
        self.max_age = max(self.max_age, float(age))
        self.ratio_sum += ratio
        self.region_count += 1

    def finish(self):
        effective = max(1, min(100, self.pages))
        active_count = bin(self.active).count("1")
        weighted = sum(self.weights.values()) / effective
        return {
            "pages": self.pages,
            "size": self.size,
            "observed": self.observed,
            "active": self.active,
            "coverage": active_count / effective,
            "weighted": weighted,
            "intensity": self.ratio_sum / max(1, self.region_count),
            "age": self.age_sum / max(1, self.age_count),
            "max_age": self.max_age,
            "weighted_pages": weighted * self.pages,
        }


def qq_windows(trace_path, session):
    windows = defaultdict(dict)
    decoded = file_rows = 0
    for line in Path(trace_path).open(errors="replace"):
        if " type=0 " not in line:
            continue
        row = decode_trace_line(line)
        if not row or row.get("region_type") != "FILE" or row.get("nr_pages", 0) <= 0:
            continue
        required = (
            "sample_timestamp_ns", "dev_major", "dev_minor", "inode",
            "file_version", "file_page_count", "file_size_bytes",
            "file_page_start", "max_possible_accesses",
        )
        if not all(name in row for name in required):
            continue
        decoded += 1
        start = int(row["sample_timestamp_ns"]) // WINDOW_NS * WINDOW_NS
        key = (
            int(row["dev_major"]), int(row["dev_minor"]), int(row["inode"]),
            int(row["file_version"]),
        )
        item = windows[start].setdefault(
            key, WindowFile(row["file_page_count"], row["file_size_bytes"])
        )
        item.add(
            row["file_page_start"], row["nr_pages"], row["nr_accesses"],
            row["max_possible_accesses"], row["age"],
        )
        file_rows += 1
    output = []
    starts = sorted(windows)
    for start in starts[1:-1]:
        files = {}
        for raw_key, accumulator in windows[start].items():
            item = accumulator.finish()
            key = "%d:%d:%d:%d" % raw_key
            generation = "%d:%d" % (raw_key[3], item["pages"])
            files[(key, generation)] = item
        total = sum(item["weighted_pages"] for item in files.values()) or 1.0
        for item in files.values():
            item["share"] = item["weighted_pages"] / total
        output.append({
            "session_id": session,
            "app": "QQ",
            "app_id": 2,
            "start": start,
            "end": start + WINDOW_NS,
            "files": files,
        })
    return output, {"decoded_file_rows": decoded, "window_count": len(output)}


def candidate_universe(row, index, last_two, file_last, file_previous, ema):
    universe = []
    ordinal = 0
    for (key, generation), item in sorted(row["files"].items()):
        effective = min(100, item["pages"])
        for segment in range(effective):
            if not (item["observed"] >> segment) & 1:
                continue
            identity = (key, generation, segment)
            accesses = last_two.get(identity, [])
            current = bool((item["active"] >> segment) & 1)
            if current:
                accesses = (accesses + [index])[-3:]
                last_two[identity] = accesses
                file_previous[(key, generation)] = file_last.get((key, generation))
                file_last[(key, generation)] = index
            last = accesses[-1] if accesses else None
            previous = accesses[-2] if len(accesses) >= 2 else None
            third = accesses[-3] if len(accesses) >= 3 else None
            delta = (index - last) * 10 if last is not None else 1_000_000
            delta12 = (last - previous) * 10 if previous is not None else 1_000_000
            delta23 = (previous - third) * 10 if third is not None else 1_000_000
            ema_value = ema.get(identity, 0.0) * 0.8 + 0.2 * float(current)
            ema[identity] = ema_value
            recent = sum(1 for value in accesses if index - value <= 6)
            last_file = file_last.get((key, generation))
            previous_file = file_previous.get((key, generation))
            ordinal += 1
            universe.append({
                "identity": "%s|%s|%d" % (key, generation, segment),
                "file_key_metadata": key,
                "partition_generation": generation,
                "segment_id": segment,
                "ordinal": ordinal,
                "current_active": current,
                "delta_since_last_access": delta,
                "delta_between_last_two": delta12,
                "delta_between_second_third": delta23,
                "file_last_delta": (index - last_file) * 10 if last_file is not None else 1_000_000,
                "file_previous_delta": (index - previous_file) * 10 if previous_file is not None else 1_000_000,
                "normalized_position": segment / max(1, effective - 1),
                "file_size_log": math.log1p(item["pages"]),
                "segment_size_log": math.log1p(max(1, math.ceil(item["pages"] / effective))),
                "segment_ema": ema_value,
                "file_ema": item["share"],
                "segment_age": item["age"],
                "file_age": item["max_age"],
                "current_coverage": item["coverage"],
                "weighted_coverage": item["weighted"],
                "recent_access_count": recent,
                "consecutive_inactive": min(12, delta // 10) if delta < 1_000_000 else 12,
                "generation_proxy": min(1.0, item["age"] / max(1.0, item["max_age"])),
                "damon_hotness": float(current) + item["intensity"] + item["weighted"],
                "native_recency": -float(delta),
                "recent_frequency": recent + ema_value,
                "observed_state": "OBSERVED_ACTIVE" if current else "OBSERVED_INACTIVE",
            })
    return universe


def future_target(windows, index, candidate, horizon):
    key = (candidate["file_key_metadata"], candidate["partition_generation"])
    segment = candidate["segment_id"]
    for offset in range(1, horizon // 10 + 1):
        if index + offset >= len(windows):
            return None
        item = windows[index + offset]["files"].get(key)
        if item and (item["observed"] >> segment) & 1 and (item["active"] >> segment) & 1:
            return offset * 10
    return None


def build_qq_decisions(trace_path, session):
    windows, audit = qq_windows(trace_path, session)
    decisions = []
    last_two = {}
    file_last = {}
    file_previous = {}
    ema = {}
    for index, row in enumerate(windows):
        universe = candidate_universe(row, index, last_two, file_last, file_previous, ema)
        ordered = sorted(
            universe,
            key=lambda item: (
                item["current_active"], -item["generation_proxy"],
                -item["delta_since_last_access"], -item["segment_age"], item["ordinal"],
            ),
        )[:POOL_SIZE]
        if len(ordered) < 32:
            continue
        for candidate in ordered:
            candidate["future"] = {
                str(horizon): future_target(windows, index, candidate, horizon)
                for horizon in (10, 30, 60, 120)
            }
        candidate_hash = stable_hash("\n".join(item["identity"] for item in ordered))
        decision_id = stable_hash((session, row["start"], candidate_hash))[:24]
        decisions.append({
            "schema_version": 1,
            "decision_id": decision_id,
            "session_id": session,
            "app": "QQ",
            "app_id": 2,
            "domain_id": None,
            "window_start_ns": row["start"],
            "window_end_ns": row["end"],
            "candidate_schema": "MGLRU_ELIGIBLE_PROXY",
            "candidate_hash": candidate_hash,
            "candidate_count": len(ordered),
            "window_context": {"kernel_values": {}, "kernel_metrics_available": False},
            "candidates": ordered,
            "future_information_used_for_candidate_set": False,
        })
    audit["decision_count"] = len(decisions)
    audit["kernel_metrics_available"] = False
    audit["candidate_features_available"] = True
    return decisions, audit


def load_frozen_decisions(path, sessions):
    output = []
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["session_id"] in sessions:
                output.append(row)
    return output


def balanced_training(decisions):
    groups = defaultdict(list)
    for decision in decisions:
        groups[decision["app"]].append(decision)
    count = min(len(groups[app]) for app in ("WPS", "FILES", "QQ"))
    output = []
    for app in ("WPS", "FILES", "QQ"):
        output.extend(sorted(groups[app], key=lambda row: row["decision_id"])[:count])
    return output, {"decisions_per_app": count, "total_decisions": len(output)}


class Standardizer:
    def fit(self, rows):
        columns = list(zip(*rows))
        self.mean = [statistics.fmean(column) for column in columns]
        self.scale = [statistics.pstdev(column) or 1.0 for column in columns]
        return self

    def transform(self, row):
        return [(value - mean) / scale for value, mean, scale in zip(row, self.mean, self.scale)]

    def export(self):
        return {"mean": self.mean, "scale": self.scale, "fit_scope": "BALANCED_TRAIN_ONLY"}


class LinearRanker:
    def __init__(self, dimension):
        self.weights = [0.0] * dimension
        self.bias = 0.0
        self.pair_count = 0

    def fit(self, decisions, epochs=18):
        first_epoch_pairs = 0
        for epoch in range(epochs):
            rate = 0.08 / (1 + epoch * 0.12)
            for decision in decisions:
                positives = [item for item in decision["candidates"] if item["future"]["60"] is not None]
                negatives = [item for item in decision["candidates"] if item["future"]["60"] is None]
                if not positives or not negatives:
                    continue
                for index, positive in enumerate(positives):
                    for offset in (0, len(negatives) // 2, len(negatives) - 1):
                        negative = negatives[(index + offset) % len(negatives)]
                        difference = [a - b for a, b in zip(positive["scaled"], negative["scaled"])]
                        margin = sum(weight * value for weight, value in zip(self.weights, difference))
                        probability = 1 / (1 + math.exp(max(-40, min(40, -margin))))
                        error = 1 - probability
                        for column, value in enumerate(difference):
                            self.weights[column] += rate * (error * value - 1e-4 * self.weights[column])
                        if epoch == 0:
                            first_epoch_pairs += 1
        self.pair_count = first_epoch_pairs
        return self

    def score(self, candidate):
        return sum(weight * value for weight, value in zip(self.weights, candidate["scaled"])) + self.bias

    def export(self, name, sessions, scaler):
        return {
            "schema_version": 1,
            "name": name,
            "type": "LINEAR_PAIRWISE_LOGISTIC",
            "features": list(FEATURES),
            "weights": self.weights,
            "bias": self.bias,
            "pair_count": self.pair_count,
            "parameter_hash": stable_hash(self.weights),
            "training_sessions": sorted(sessions),
            "scaler": scaler.export(),
            "app_identity_is_router_only": True,
        }


def apply_scaler(decisions, scaler):
    for decision in decisions:
        for candidate in decision["candidates"]:
            candidate["scaled"] = scaler.transform(candidate_vector(candidate))


def order_metrics(decision, scores):
    candidates = decision["candidates"]
    count = len(candidates)
    protect = max(1, math.ceil(count * 0.10))
    reclaim = max(1, math.ceil(count * 0.50))
    order = sorted(range(count), key=lambda index: (-scores[index], candidates[index]["ordinal"]))
    relevant = [candidate["future"]["60"] is not None for candidate in candidates]
    positives = sum(relevant)
    protected = order[:protect]
    reclaimed = order[-reclaim:]
    after = sum(relevant[index] for index in reclaimed)
    hits = sum(relevant[index] for index in protected)
    positive_indices = [index for index, value in enumerate(relevant) if value]
    negative_indices = [index for index, value in enumerate(relevant) if not value]
    pairs = correct = ties = 0
    for positive in positive_indices:
        for negative in negative_indices:
            difference = scores[positive] - scores[negative]
            correct += 1 if difference > 1e-15 else 0.5 if abs(difference) <= 1e-15 else 0
            ties += abs(difference) <= 1e-15
            pairs += 1
    ideal = sum(1 / math.log2(rank + 2) for rank in range(min(protect, positives)))
    dcg = sum((1 if relevant[index] else 0) / math.log2(rank + 2) for rank, index in enumerate(protected))
    return {
        "decision_id": decision["decision_id"],
        "app": decision["app"],
        "candidate_hash": decision["candidate_hash"],
        "candidate_count": count,
        "reclaimed": reclaim,
        "positives": positives,
        "future_reuse_after_reclaim": after,
        "future_reuse_saved": positives - after,
        "normalized_refault_proxy_per_1000_reclaimed": after * 1000 / reclaim,
        "pairwise_auc": correct / pairs if pairs else 0.5,
        "ndcg_at_budget": dcg / ideal if ideal else 0.0,
        "recall_at_budget": hits / positives if positives else 0.0,
        "tie_rate": ties / pairs if pairs else 1.0,
        "ranking_hash": stable_hash([candidates[index]["identity"] for index in order]),
    }


def aggregate(rows):
    if not rows:
        return {"decision_count": 0}
    reclaimed = sum(row["reclaimed"] for row in rows)
    after = sum(row["future_reuse_after_reclaim"] for row in rows)
    return {
        "decision_count": len(rows),
        "candidate_count": sum(row["candidate_count"] for row in rows),
        "positive_candidates": sum(row["positives"] for row in rows),
        "positive_decisions": sum(row["positives"] > 0 for row in rows),
        "pairwise_evaluable_decisions": sum(0 < row["positives"] < row["candidate_count"] for row in rows),
        "reclaimed_candidates": reclaimed,
        "future_reuse_after_reclaim": after,
        "future_reuse_saved": sum(row["future_reuse_saved"] for row in rows),
        "normalized_refault_proxy_per_1000_reclaimed": after * 1000 / reclaimed if reclaimed else None,
        "pairwise_auc": statistics.fmean(row["pairwise_auc"] for row in rows),
        "ndcg_at_budget": statistics.fmean(row["ndcg_at_budget"] for row in rows),
        "recall_at_budget": statistics.fmean(row["recall_at_budget"] for row in rows),
        "tie_rate": statistics.fmean(row["tie_rate"] for row in rows),
        "ranking_hash": stable_hash([row["ranking_hash"] for row in rows]),
        "candidate_hash": stable_hash([row["candidate_hash"] for row in rows]),
    }


def evaluate(decisions, scorer):
    rows = []
    for decision in decisions:
        if scorer == "NATIVE":
            scores = [candidate["native_recency"] for candidate in decision["candidates"]]
        elif scorer == "RECENT":
            scores = [candidate["recent_frequency"] for candidate in decision["candidates"]]
        else:
            scores = [scorer.score(candidate) for candidate in decision["candidates"]]
        rows.append(order_metrics(decision, scores))
    return aggregate(rows), rows


def paired_block_ci(left_rows, right_rows, rounds=1000, seed=210):
    paired = []
    for left, right in zip(left_rows, right_rows):
        if left["decision_id"] != right["decision_id"]:
            raise ValueError("paired decision order mismatch")
        paired.append((left["app"], left["normalized_refault_proxy_per_1000_reclaimed"] - right["normalized_refault_proxy_per_1000_reclaimed"]))
    blocks = defaultdict(list)
    app_counts = Counter()
    for app, value in paired:
        block = "%s_%03d" % (app, app_counts[app] // 25)
        app_counts[app] += 1
        blocks[block].append(value)
    names = sorted(blocks)
    rng = random.Random(seed)
    samples = []
    for _ in range(rounds):
        values = [value for _ in names for value in blocks[rng.choice(names)]]
        samples.append(statistics.fmean(values) if values else 0.0)
    samples.sort()
    return {
        "paired_mean_improvement": statistics.fmean(value for _, value in paired) if paired else 0.0,
        "block_bootstrap_95_ci": [samples[int(0.025 * rounds)], samples[int(0.975 * rounds) - 1]],
        "paired_win_rate": sum(value > 0 for _, value in paired) / len(paired) if paired else 0.0,
        "decision_count": len(paired),
        "block_count": len(names),
        "seed": seed,
    }


def find_project(tree):
    cursor = Path(tree).resolve()
    for candidate in (cursor, *cursor.parents):
        if all((candidate / name).exists() for name in ("MGLRU-test/v4-parp/work", "automation", "outputs")):
            return candidate
    raise RuntimeError("PROJECT_ROOT not found")


def run(tree, output):
    started = time.time_ns()
    tree = Path(tree).resolve()
    project = find_project(tree)
    output = Path(output).resolve()
    phase29 = project / "outputs/parp_phase29a_workload_expert_20260803_102327"
    frozen = phase29 / "candidate_reconstruction/decisions_generation_tail_128.jsonl.gz"
    qq_root = output / "qq_collection/raw"
    input_paths = {
        "frozen_wps_files_decisions": frozen,
        "qq_train_trace": qq_root / "qq_train_01/trace/parp_region_evidence.filtered",
        "qq_validation_trace": qq_root / "qq_validation_01/trace/parp_region_evidence.filtered",
    }
    for path in input_paths.values():
        if not path.is_file():
            raise FileNotFoundError(path)

    selected_old = set(sum(TRAIN.values(), ()) + sum(CALIBRATION.values(), ()) + sum(EVALUATION.values(), ()))
    old = load_frozen_decisions(frozen, selected_old)
    qq_train, train_audit = build_qq_decisions(input_paths["qq_train_trace"], "qq_train_01")
    qq_validation, validation_audit = build_qq_decisions(input_paths["qq_validation_trace"], "qq_validation_01")
    decisions = old + qq_train + qq_validation
    by_session = Counter(row["session_id"] for row in decisions)
    by_app = Counter(row["app"] for row in decisions)
    roles = {}
    for app in ("WPS", "FILES", "QQ"):
        roles[app] = {
            "train": list(TRAIN[app]),
            "calibration_audit": list(CALIBRATION[app]),
            "evaluation": list(EVALUATION[app]),
        }
        sets = [set(value) for value in roles[app].values()]
        if any(sets[i] & sets[j] for i in range(len(sets)) for j in range(i + 1, len(sets))):
            raise ValueError("session leakage for " + app)

    train_sessions = set(sum(TRAIN.values(), ()))
    evaluation_sessions = set(sum(EVALUATION.values(), ()))
    train_decisions = [row for row in decisions if row["session_id"] in train_sessions]
    evaluation = [row for row in decisions if row["session_id"] in evaluation_sessions]
    balanced, balance_audit = balanced_training(train_decisions)
    scaler = Standardizer().fit([
        candidate_vector(candidate) for decision in balanced for candidate in decision["candidates"]
    ])
    apply_scaler(decisions, scaler)

    models = {"GENERIC": LinearRanker(len(FEATURES)).fit(balanced)}
    for app in ("WPS", "FILES", "QQ"):
        subset = [row for row in train_decisions if row["app"] == app]
        models[app] = LinearRanker(len(FEATURES)).fit(subset)
    model_dir = output / "models/phase210_app_rankers"
    for name, model in models.items():
        sessions = train_sessions if name == "GENERIC" else set(TRAIN[name])
        atomic_json(model_dir / ("%s_REUSE_RANKER.json" % name), model.export(name, sessions, scaler))

    matrix = {}
    details = {}
    per_decision = {}
    baseline_details = {}
    for app in ("WPS", "FILES", "QQ"):
        subset = [row for row in evaluation if row["app"] == app]
        matrix[app] = {}
        details[app] = {}
        per_decision[app] = {}
        baseline_details[app] = {}
        for baseline in ("NATIVE", "RECENT"):
            summary, rows = evaluate(subset, baseline)
            baseline_details[app][baseline] = summary
            per_decision[app][baseline] = rows
        for name in MODEL_NAMES:
            summary, rows = evaluate(subset, models[name])
            matrix[app][name] = summary["normalized_refault_proxy_per_1000_reclaimed"]
            details[app][name] = summary
            per_decision[app][name] = rows

    matched_better_generic = []
    matched_better_wrong = []
    g0_apps = []
    generic_rows = []
    matched_rows = []
    wrong_rows = []
    for app in ("WPS", "FILES", "QQ"):
        if matrix[app][app] < matrix[app]["GENERIC"]:
            matched_better_generic.append(app)
        if matrix[app][app] < matrix[app][WRONG[app]]:
            matched_better_wrong.append(app)
        baseline = min(
            baseline_details[app]["NATIVE"]["normalized_refault_proxy_per_1000_reclaimed"],
            baseline_details[app]["RECENT"]["normalized_refault_proxy_per_1000_reclaimed"],
        )
        if matrix[app][app] < baseline and matrix[app]["GENERIC"] < baseline:
            g0_apps.append(app)
        generic_rows.extend(per_decision[app]["GENERIC"])
        matched_rows.extend(per_decision[app][app])
        wrong_rows.extend(per_decision[app][WRONG[app]])
    generic_stats = paired_block_ci(generic_rows, matched_rows, seed=210)
    wrong_stats = paired_block_ci(wrong_rows, matched_rows, seed=211)
    g0 = len(g0_apps) == 3
    g1 = (
        len(matched_better_generic) >= 2
        and len(matched_better_wrong) >= 2
        and generic_stats["block_bootstrap_95_ci"][0] > 0
        and wrong_stats["block_bootstrap_95_ci"][0] > 0
    )
    data_adequacy = {}
    for app in ("WPS", "FILES", "QQ"):
        train_subset = [row for row in train_decisions if row["app"] == app]
        train_positive = sum(
            candidate["future"]["60"] is not None
            for decision in train_subset
            for candidate in decision["candidates"]
        )
        evaluation_summary = baseline_details[app]["NATIVE"]
        data_adequacy[app] = {
            "train_decisions": len(train_subset),
            "train_positive_candidates": train_positive,
            "evaluation_decisions": evaluation_summary["decision_count"],
            "evaluation_positive_candidates": evaluation_summary["positive_candidates"],
            "evaluation_positive_decisions": evaluation_summary["positive_decisions"],
            "pairwise_evaluable_decisions": evaluation_summary["pairwise_evaluable_decisions"],
            "minimum_train_positive_candidates": 20,
            "minimum_evaluation_positive_candidates": 20,
            "minimum_pairwise_evaluable_decisions": 10,
        }
        data_adequacy[app]["passed"] = (
            train_positive >= 20
            and evaluation_summary["positive_candidates"] >= 20
            and evaluation_summary["pairwise_evaluable_decisions"] >= 10
        )
    inadequate_apps = [app for app, row in data_adequacy.items() if not row["passed"]]
    if inadequate_apps:
        status = (
            "PARP_PHASE210_QQ_MODEL_DATA_INSUFFICIENT"
            if "QQ" in inadequate_apps
            else "PARP_PHASE210_DATA_INSUFFICIENT"
        )
    else:
        status = "PARP_PHASE210_OFFLINE_GATES_PASSED" if g0 and g1 else "PARP_PHASE210_APP_SPECIALIZATION_NOT_SUPPORTED"

    offline = output / "offline/app_specific"
    offline.mkdir(parents=True, exist_ok=True)
    with (offline / "app_model_cross_matrix.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["true_app", *MODEL_NAMES])
        for app in ("WPS", "FILES", "QQ"):
            writer.writerow([app, *[matrix[app][name] for name in MODEL_NAMES]])
    atomic_json(offline / "app_model_cross_matrix.json", {
        "schema_version": 1,
        "metric": "normalized_future_reuse_proxy_per_1000_reclaimed",
        "matrix": details,
        "baselines": baseline_details,
        "wrong_mapping": WRONG,
        "evaluation_sessions": {app: list(EVALUATION[app]) for app in EVALUATION},
        "real_refault": False,
    })
    gate = {
        "schema_version": 1,
        "status": status,
        "g0_passed": g0,
        "g1_passed": g1,
        "g0_apps": g0_apps,
        "matched_better_generic_apps": matched_better_generic,
        "matched_better_wrong_apps": matched_better_wrong,
        "matched_vs_generic_statistics": generic_stats,
        "matched_vs_wrong_statistics": wrong_stats,
        "data_adequacy": data_adequacy,
        "inadequate_apps": inadequate_apps,
        "real_refault": False,
        "claim_boundary": "held-out trace future-reuse proxy only",
    }
    atomic_json(offline / "offline_gate.json", gate)
    atomic_json(output / "models/model_training_gate.json", {
        "status": status,
        "trained": ["GENERIC_CROSS_APP_RANKER", "WPS_REUSE_RANKER", "FILES_REUSE_RANKER", "QQ_REUSE_RANKER"],
        "feature_schema": list(FEATURES),
        "app_identity_is_model_feature": False,
        "session_roles": roles,
        "balanced_generic_training": balance_audit,
        "ab_sessions_used": [],
    })
    atomic_json(output / "audit/phase210_offline_ingestion.json", {
        "schema_version": 1,
        "inputs": {name: {"path": str(path), "sha256": file_hash(path)} for name, path in input_paths.items()},
        "session_roles": roles,
        "decision_count_by_session": dict(sorted(by_session.items())),
        "decision_count_by_app": dict(sorted(by_app.items())),
        "qq_train_conversion": train_audit,
        "qq_validation_conversion": validation_audit,
        "feature_names": list(FEATURES),
        "forbidden_feature_hits": [name for name in FEATURES if any(fragment in name for fragment in FORBIDDEN_FEATURE_FRAGMENTS)],
        "future_used_for_candidate_set": False,
        "kernel_metrics_fabricated_for_qq": False,
        "operation_labels_used": False,
        "ab_sessions_used": [],
    })
    manifest = {
        "schema_version": 1,
        "candidate_schema": "MGLRU_ELIGIBLE_PROXY",
        "window_seconds": 10,
        "candidate_pool_size": POOL_SIZE,
        "horizon_seconds": 60,
        "protect_ratio": 0.10,
        "reclaim_ratio": 0.50,
        "same_candidates_for_all_models": True,
        "same_reclaim_count_for_all_models": True,
        "feature_schema": list(FEATURES),
        "session_roles": roles,
    }
    atomic_json(offline / "dataset_manifest.json", manifest)
    state = {
        "stage": "OFFLINE_APP_SPECIFIC_GATE_COMPLETE",
        "status": status,
        "timestamp_ns": time.time_ns(),
        "g0_passed": g0,
        "g1_passed": g1,
        "next_stage": "OBSERVE_DESIGN" if not inadequate_apps and g0 and g1 else "STOP_OFFLINE_GATE",
        "root_used": False,
        "kernel_write": False,
    }
    atomic_json(output / "state/state.json", state)
    with (output / "state/history.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(state, sort_keys=True) + "\n")
    atomic_json(output / "performance/offline_training.json", {
        "started_ns": started,
        "ended_ns": time.time_ns(),
        "models": {name: {"pair_count": model.pair_count, "parameter_hash": stable_hash(model.weights)} for name, model in models.items()},
    })
    print(json.dumps(gate, sort_keys=True))
    return gate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.tree, args.output)


if __name__ == "__main__":
    main()
