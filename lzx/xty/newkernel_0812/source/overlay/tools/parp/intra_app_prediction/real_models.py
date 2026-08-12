#!/usr/bin/env python3
"""Dependency-free training, evaluation, and online replay for real Phase2.7B."""

import argparse
from collections import Counter, defaultdict
import csv
import json
import math
import os
from pathlib import Path
import resource
import sqlite3
import statistics
import time

from .real_pipeline import (HORIZONS, RESOLUTIONS, SESSION_ORDER, SOURCE,
                            atomic_json, atomic_text,
                            enforce_probability_monotonicity, percentile,
                            probability_to_q15)
from .state_cluster import LightweightKMeans
from .state_markov import PageStateMarkov, duration_bin


MODELS = ("last_window", "recent_frequency", "global_frequency",
          "page_state_markov")
TOP_KS = (1, 5, 10, 20)
THRESHOLDS = tuple(index / 10 for index in range(1, 10))


def validate_prediction_contract(payload):
    """Validate the strict dependency-free subset of prediction_schema.json."""
    required = {
        "schema_version", "run_id", "app_id", "domain_id", "model_type",
        "model_version", "prediction_generation", "generated_ns", "ttl_ns",
        "file_segments", "anon_prediction", "kernel_write",
    }
    optional = {"session_id", "unknown_reason", "future_features_used"}
    missing = required - set(payload)
    unexpected = set(payload) - required - optional
    if missing or unexpected:
        raise ValueError("prediction keys missing=%s unexpected=%s" %
                         (sorted(missing), sorted(unexpected)))
    if payload["schema_version"] != 1 or not payload["run_id"]:
        raise ValueError("invalid schema_version or run_id")
    if payload["model_type"] != "page_state_markov":
        raise ValueError("invalid model_type")
    if payload["kernel_write"] is not False:
        raise ValueError("kernel_write must be false")
    if payload.get("future_features_used", False) is not False:
        raise ValueError("future features are forbidden")
    for name in ("domain_id", "generated_ns"):
        if not isinstance(payload[name], int) or payload[name] < 0:
            raise ValueError("invalid %s" % name)
    for name in ("model_version", "prediction_generation", "ttl_ns"):
        if not isinstance(payload[name], int) or payload[name] < 1:
            raise ValueError("invalid %s" % name)
    segment_required = {
        "dev_major", "dev_minor", "inode", "file_version",
        "partition_generation", "file_page_count", "requested_bins",
        "effective_bins", "segment_id", "probability_10s_q15",
        "probability_30s_q15", "probability_60s_q15", "confidence_q15",
    }
    for segment in payload["file_segments"]:
        if segment_required - set(segment):
            raise ValueError("file segment contract is incomplete")
        if segment["requested_bins"] not in RESOLUTIONS:
            raise ValueError("invalid requested_bins")
        if not 1 <= segment["effective_bins"] <= segment["file_page_count"]:
            raise ValueError("invalid effective_bins")
        probabilities = tuple(segment["probability_%ds_q15" % horizon]
                              for horizon in HORIZONS)
        if not all(isinstance(value, int) and 0 <= value <= 32767
                   for value in probabilities + (segment["confidence_q15"],)):
            raise ValueError("invalid Q15 value")
        if not probabilities[0] <= probabilities[1] <= probabilities[2]:
            raise ValueError("horizon probabilities are not monotonic")
    anon_required = {"hot_bytes_10s", "hot_bytes_30s", "hot_bytes_60s",
                     "cooling_probability_q15"}
    anon = payload["anon_prediction"]
    if anon_required - set(anon):
        raise ValueError("anonymous prediction contract is incomplete")
    if not all(isinstance(anon[name], int) and anon[name] >= 0
               for name in ("hot_bytes_10s", "hot_bytes_30s", "hot_bytes_60s")):
        raise ValueError("invalid anonymous hot bytes")
    if not isinstance(anon["cooling_probability_q15"], int) or not (
            0 <= anon["cooling_probability_q15"] <= 32767):
        raise ValueError("invalid anonymous cooling Q15")
    return True


def _safe(numerator, denominator):
    return numerator / denominator if denominator else 0.0


def _distance(left, right):
    return math.sqrt(sum((float(a) - float(b)) ** 2
                         for a, b in zip(left, right)))


class Standardizer:
    def __init__(self):
        self.means = []
        self.scales = []

    def fit(self, rows):
        columns = list(zip(*rows))
        self.means = [sum(column) / len(column) for column in columns]
        self.scales = []
        for column, mean in zip(columns, self.means):
            variance = sum((value - mean) ** 2 for value in column) / len(column)
            self.scales.append(math.sqrt(variance) or 1.0)
        return self

    def transform(self, rows):
        return [[(value - mean) / scale
                 for value, mean, scale in zip(row, self.means, self.scales)]
                for row in rows]

    def payload(self):
        return {"schema_version": 1, "means": self.means,
                "scales": self.scales, "fit_source": "wps_01_only"}


def _average_precision(actual, scores):
    if not actual:
        return 1.0 if not scores else 0.0
    hits = 0
    total = 0.0
    for rank, (key, _) in enumerate(sorted(scores.items(),
                                           key=lambda item: (-item[1], item[0])), 1):
        if key in actual:
            hits += 1
            total += hits / rank
    return total / len(actual)


def metrics_for(samples, top_k):
    """Metrics on explicitly future-observed candidate segments only."""
    tp = fp = fn = hit = 0
    precision = []
    recall = []
    f1 = []
    jaccard = []
    ap = []
    actual_total = candidate_actual = candidates = 0
    used_k = []
    unknown_windows = 0
    for sample in samples:
        actual = set(sample["actual"])
        valid = set(sample["candidate"]) & set(sample["future_known"])
        actual_total += len(actual)
        candidate_actual += len(actual & valid)
        candidates += len(valid)
        scores = {key: value for key, value in sample["scores"].items()
                  if key in valid}
        if sample.get("unknown"):
            unknown_windows += 1
        ranked = sorted(scores, key=lambda key: (-scores[key], key))[:min(top_k, len(valid))]
        predicted = set(ranked)
        used_k.append(len(ranked))
        common = len(actual & predicted)
        row_p = _safe(common, len(predicted))
        row_r = _safe(common, len(actual))
        precision.append(row_p); recall.append(row_r)
        f1.append(_safe(2 * row_p * row_r, row_p + row_r))
        jaccard.append(_safe(common, len(actual | predicted)))
        ap.append(_average_precision(actual, scores))
        tp += common; fp += len(predicted - actual); fn += len(actual - predicted)
        hit += bool(common)
    count = len(samples)
    micro_p = _safe(tp, tp + fp)
    micro_r = _safe(tp, tp + fn)
    return {"precision_at_%d" % top_k: _safe(sum(precision), count),
            "recall_at_%d" % top_k: _safe(sum(recall), count),
            "f1_at_%d" % top_k: _safe(sum(f1), count),
            "jaccard": _safe(sum(jaccard), count),
            "micro_precision": micro_p, "micro_recall": micro_r,
            "micro_f1": _safe(2 * micro_p * micro_r, micro_p + micro_r),
            "macro_f1": _safe(sum(f1), count),
            "average_precision": _safe(sum(ap), count),
            "hit_rate": _safe(hit, count),
            "candidate_coverage": _safe(candidate_actual, actual_total),
            "candidate_count": candidates,
            "actual_count": actual_total,
            "effective_k_mean": _safe(sum(used_k), len(used_k)),
            "unknown_rate": _safe(unknown_windows, count)}


def threshold_metrics(samples, threshold):
    tp = fp = fn = tn = 0
    false_cold_by_op = Counter()
    false_hot_by_op = Counter()
    actual_by_op = Counter()
    inactive_by_op = Counter()
    for sample in samples:
        actual = set(sample["actual"])
        valid = set(sample["candidate"]) & set(sample["future_known"])
        scores = sample["scores"]
        predicted = {key for key in valid if scores.get(key, 0.0) >= threshold}
        actual_valid = actual & valid
        tp += len(predicted & actual_valid)
        fp += len(predicted - actual_valid)
        fn += len(actual_valid - predicted)
        tn += len(valid - actual_valid - predicted)
        operation = sample.get("operation", "UNKNOWN")
        false_cold_by_op[operation] += len(actual_valid - predicted)
        false_hot_by_op[operation] += len(predicted - actual_valid)
        actual_by_op[operation] += len(actual_valid)
        inactive_by_op[operation] += len(valid - actual_valid)
    precision = _safe(tp, tp + fp)
    recall = _safe(tp, tp + fn)
    return {"threshold": threshold, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall,
            "f1": _safe(2 * precision * recall, precision + recall),
            "false_cold_count": fn, "false_cold_denominator": tp + fn,
            "false_cold_rate": _safe(fn, tp + fn),
            "false_hot_count": fp, "false_hot_denominator": fp + tn,
            "false_hot_rate": _safe(fp, fp + tn),
            "false_protection_rate": _safe(fp, tp + fp),
            "false_reclaim_risk": _safe(fn, tp + fn),
            "reclaim_candidate_ratio": _safe(tn + fn, tp + fp + fn + tn),
            "by_operation": {op: {"false_cold": false_cold_by_op[op],
                                   "actual": actual_by_op[op],
                                   "false_hot": false_hot_by_op[op],
                                   "inactive": inactive_by_op[op]}
                             for op in sorted(set(actual_by_op) | set(inactive_by_op))}}


class RealModelPipeline:
    def __init__(self, project):
        self.project = Path(project)
        self.real = self.project / "outputs/parp_phase27b_real_dataset_20260802_194342"
        self.dataset = self.real / "dataset"
        self.models = self.real / "models"
        self.evaluation = self.real / "evaluation"
        self.online = self.real / "online"
        self.analysis = self.real / "analysis"
        self.validation = self.real / "validation"
        with (self.real / "state/run.json").open(encoding="utf-8") as stream:
            self.run = json.load(stream)
        self.db_path = self.real / "work/dataset_build/dataset.sqlite"
        self.connection = sqlite3.connect(str(self.db_path))
        self.connection.row_factory = sqlite3.Row
        self.models.mkdir(exist_ok=True)
        self.evaluation.mkdir(exist_ok=True)
        self.online.mkdir(exist_ok=True)
        self.windows = self._load_windows()
        self.window_by_id = {row["window_id"]: row for row in self.windows}
        self.session_rows = defaultdict(list)
        for row in self.windows:
            self.session_rows[row["session_id"]].append(row)
        for rows in self.session_rows.values():
            rows.sort(key=lambda item: item["window_index"])
        self.operations = sorted({row["dominant_operation"] for row in self.windows})
        self.features, self.feature_names = self._features()
        self.train_ids = [row["window_id"] for row in self.session_rows["wps_01"]]
        self.val_ids = [row["window_id"] for row in self.session_rows["wps_02"]][6:]
        self.test_ids = [row["window_id"] for row in self.session_rows["wps_03"]][6:]
        self.files_train_ids = [row["window_id"] for row in self.session_rows["files_01"]]
        self.files_test_ids = [row["window_id"] for row in self.session_rows["files_02"]][6:]
        self.scaler = Standardizer().fit([self.features[key] for key in self.train_ids])
        transformed = self.scaler.transform([self.features[row["window_id"]]
                                             for row in self.windows])
        self.scaled = {row["window_id"]: value
                       for row, value in zip(self.windows, transformed)}
        self.cluster, self.selected_k, self.k_audit = self._select_k()
        self.states = {key: self.cluster.predict_one(value)
                       for key, value in self.scaled.items()}
        self.markov = self._fit_markov(self.states)
        self.unknown = self._unknown_thresholds()
        self.file_vocab_audit = self._file_vocab()
        self.eval_rows = []
        self.threshold_rows = []
        self.false_rows = []
        self.operation_rows = []
        self.file_class_rows = []
        self.monotonic = Counter()
        self.selected_recent = {}
        self.selected_thresholds = {}
        self.learning_stats = {}
        self.training_started = time.monotonic_ns()

    def close(self):
        self.connection.close()

    def _load_windows(self):
        rows = []
        with (self.dataset / "windows_10s.csv").open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                if row["is_complete_window"] != "True":
                    continue
                rows.append({**row, "window_index": int(self.connection.execute(
                    "SELECT window_index FROM windows WHERE window_id=?", (row["window_id"],)).fetchone()[0]),
                    "window_start_ns": int(row["window_start_ns"]),
                    "file_count": int(row["file_count"]),
                    "active_file_count": int(row["active_file_count"]),
                    "active_l10": int(row["active_segment_count_l10"]),
                    "active_l100": int(row["active_segment_count_l100"]),
                    "active_l1000": int(row["active_segment_count_l1000"]),
                    "anon_total": int(row["anon_total_bytes"]),
                    "anon_hot": int(row["anon_hot_bytes"]),
                    "anon_cold": int(row["anon_cold_bytes"]),
                    "unresolved": int(row["unresolved_anon_bytes"])})
        return sorted(rows, key=lambda item: (SESSION_ORDER.index(item["session_id"]),
                                               item["window_index"]))

    def _features(self):
        names = ["file_count", "active_file_count", "active_l10",
                 "active_l100", "active_l1000", "anon_log_bytes",
                 "anon_hot_ratio", "anon_cold_ratio", "anon_unresolved_ratio"]
        names += ["operation=" + operation for operation in self.operations]
        output = {}
        for row in self.windows:
            total = row["anon_total"]
            values = [float(row["file_count"]), float(row["active_file_count"]),
                      math.log1p(row["active_l10"]), math.log1p(row["active_l100"]),
                      math.log1p(row["active_l1000"]), math.log1p(total),
                      _safe(row["anon_hot"], total), _safe(row["anon_cold"], total),
                      _safe(row["unresolved"], total + row["unresolved"])]
            values += [float(row["dominant_operation"] == operation)
                       for operation in self.operations]
            output[row["window_id"]] = values
        return output, names

    def _select_k(self):
        train = [self.scaled[key] for key in self.train_ids]
        val = [self.scaled[key] for key in self.val_ids]
        candidates = []
        for k in (8, 16, 32):
            model = LightweightKMeans(k, max_iter=100).fit(train)
            inertia = model.inertia(val) / len(val)
            candidates.append({"k": k, "validation_inertia_per_row": inertia,
                               "model": model})
        selected = min(candidates, key=lambda item: (item["validation_inertia_per_row"],
                                                     item["k"]))
        return selected["model"], selected["k"], [
            {key: value for key, value in item.items() if key != "model"}
            for item in candidates]

    def _fit_markov(self, states):
        transitions = []
        rows = self.session_rows["wps_01"]
        for current, following in zip(rows, rows[1:]):
            transitions.append((states[current["window_id"]], duration_bin(10),
                                states[following["window_id"]]))
        return PageStateMarkov().fit(transitions)

    def _unknown_thresholds(self):
        train_distances = [_distance(self.scaled[key],
                                     self.cluster.centroids[self.states[key]])
                           for key in self.train_ids]
        p95 = percentile(train_distances, .95)
        p99 = percentile(train_distances, .99)
        val_distances = [_distance(self.scaled[key],
                                   self.cluster.centroids[self.states[key]])
                         for key in self.val_ids]
        candidates = [(p95, _safe(sum(value > p95 for value in val_distances), len(val_distances))),
                      (p99, _safe(sum(value > p99 for value in val_distances), len(val_distances)))]
        selected = min(candidates, key=lambda item: (abs(item[1] - .05), -item[0]))
        test_distances = [_distance(self.scaled[key],
                                    self.cluster.centroids[self.states[key]])
                          for key in self.test_ids]
        return {"p95": p95, "p99": p99, "selected": selected[0],
                "selection_source": "wps_02_validation_only",
                "validation_unknown_rate": selected[1],
                "test_unknown_rate": _safe(sum(value > selected[0] for value in test_distances),
                                             len(test_distances))}

    def _file_vocab(self):
        vocab = {row[0] for row in self.connection.execute("""
          SELECT DISTINCT s.file_id FROM segments s JOIN windows w USING(window_id)
          WHERE w.session='wps_01' AND w.is_complete=1
        """)}
        result = {"schema_version": 1, "files": sorted(vocab),
                  "fit_source": "wps_01_only", "unknown_bucket": "UNK_FILE"}
        for session, name in (("wps_02", "validation"), ("wps_03", "test")):
            values = {row[0] for row in self.connection.execute(
                "SELECT DISTINCT file_id FROM segments WHERE session=?", (session,))}
            result[name + "_unk_file_ratio"] = _safe(len(values - vocab), len(values))
        return result

    def _load_resolution(self, resolution):
        active = defaultdict(set)
        observed = defaultdict(set)
        key_to_id = {}
        id_to_key = []
        query = """SELECT s.window_id,s.file_id,s.partition_generation,s.segment_id,
                          s.observation_state,s.coverage,s.weighted_coverage
                   FROM segments s JOIN windows w USING(window_id)
                   WHERE s.resolution=? AND w.is_complete=1
                   ORDER BY s.session,s.window_index"""
        for row in self.connection.execute(query, (resolution,)):
            key = "%s|%s|%s" % (row["file_id"], row["partition_generation"],
                                  row["segment_id"])
            identifier = key_to_id.get(key)
            if identifier is None:
                identifier = len(id_to_key)
                key_to_id[key] = identifier
                id_to_key.append(key)
            if row["observation_state"] != "NOT_OBSERVED":
                observed[row["window_id"]].add(identifier)
            if row["coverage"] > 0 or row["weighted_coverage"] > 0:
                active[row["window_id"]].add(identifier)
        return active, observed, id_to_key

    @staticmethod
    def _global_scores(train_ids, active):
        counts = Counter(segment for key in train_ids for segment in active[key])
        return {segment: count / len(train_ids) for segment, count in counts.items()}

    def _state_distribution(self, train_ids, active, states):
        counts = defaultdict(Counter)
        totals = Counter()
        for key in train_ids:
            state = states[key]
            totals[state] += 1
            counts[state].update(active[key])
        return {state: {segment: count / totals[state]
                        for segment, count in values.items()}
                for state, values in counts.items()}

    def _advance(self, state, steps):
        probabilities = {state: 1.0}
        for _ in range(steps):
            following = defaultdict(float)
            for current, probability in probabilities.items():
                transitions = self.markov.predict(current, duration_bin(10)) or {current: 1.0}
                for target, transition in transitions.items():
                    following[target] += probability * transition
            probabilities = dict(following)
        return probabilities

    @staticmethod
    def _combine_states(state_probabilities, distribution):
        scores = defaultdict(float)
        for state, state_probability in state_probabilities.items():
            for segment, probability in distribution.get(state, {}).items():
                scores[segment] += state_probability * probability
        return dict(scores)

    def _scores(self, model, current_id, history_ids, active, global_scores,
                state_distribution, horizon, recent_history):
        if model == "last_window":
            return {segment: 1.0 for segment in active[current_id]}, False
        if model == "recent_frequency":
            selected = history_ids[-recent_history:]
            counts = Counter(segment for key in selected for segment in active[key])
            return ({segment: count / len(selected) for segment, count in counts.items()}
                    if selected else {}), False
        if model == "global_frequency":
            return dict(global_scores), False
        distance = _distance(self.scaled[current_id],
                             self.cluster.centroids[self.states[current_id]])
        if distance > self.unknown["selected"]:
            return {}, True
        probabilities = self._advance(self.states[current_id], horizon // 10)
        return self._combine_states(probabilities, state_distribution), False

    def _samples(self, session, horizon, model, active, observed,
                 global_scores, state_distribution, recent_history):
        rows = self.session_rows[session]
        start_index = 6 if session in ("wps_02", "wps_03", "files_02") else 0
        output = []
        steps = horizon // 10
        for position in range(start_index, len(rows) - steps):
            current = rows[position]
            current_id = current["window_id"]
            history = [row["window_id"] for row in rows[:position + 1]]
            future = rows[position + 1:position + steps + 1]
            actual = set().union(*(active[row["window_id"]] for row in future))
            future_known = set().union(*(observed[row["window_id"]] for row in future))
            candidate = set(observed[current_id])
            for prior in rows[max(0, position - 12):position]:
                candidate.update(observed[prior["window_id"]])
            scores, unknown = self._scores(model, current_id, history, active,
                                           global_scores, state_distribution,
                                           horizon, recent_history)
            output.append({"window_id": current_id, "scores": scores,
                           "actual": actual, "future_known": future_known,
                           "candidate": candidate, "unknown": unknown,
                           "operation": current["dominant_operation"]})
        return output

    def _select_recent_and_threshold(self, resolution, active, observed,
                                     global_scores, state_distribution):
        for horizon in HORIZONS:
            candidates = []
            for history in (3, 6, 12):
                samples = self._samples("wps_02", horizon, "recent_frequency",
                                        active, observed, global_scores,
                                        state_distribution, history)
                score = metrics_for(samples, 10)["f1_at_10"]
                candidates.append((score, history))
            self.selected_recent[(resolution, horizon)] = max(
                candidates, key=lambda item: (item[0], -item[1]))[1]
            for model in MODELS:
                history = self.selected_recent[(resolution, horizon)]
                samples = self._samples("wps_02", horizon, model, active,
                                        observed, global_scores,
                                        state_distribution, history)
                rows = [threshold_metrics(samples, threshold)
                        for threshold in THRESHOLDS]
                selected = max(rows, key=lambda item: (item["f1"],
                                                       -item["false_cold_rate"],
                                                       item["threshold"]))
                self.selected_thresholds[(model, resolution, horizon)] = selected["threshold"]
                for row in rows:
                    self.threshold_rows.append({"split": "validation", "app": "WPS",
                                                "model": model, "resolution": resolution,
                                                "horizon_s": horizon, **{k: v for k, v in row.items()
                                                if k != "by_operation"}})

    def _evaluate_app(self, app, train_session, test_session, resolution,
                      active, observed, id_to_key, global_scores,
                      state_distribution):
        file_classes = self._file_classes()
        for model in MODELS:
            triples = defaultdict(dict)
            for horizon in HORIZONS:
                history = self.selected_recent[(resolution, horizon)]
                samples = self._samples(test_session, horizon, model, active,
                                        observed, global_scores,
                                        state_distribution, history)
                block = {"model": model, "app": app, "resolution": resolution,
                         "horizon_s": horizon, "sample_windows": len(samples),
                         "recent_history": history if model == "recent_frequency" else None}
                for top_k in TOP_KS:
                    block.update(metrics_for(samples, top_k))
                threshold = self.selected_thresholds[(model, resolution, horizon)]
                threshold_result = threshold_metrics(samples, threshold)
                block.update({key: value for key, value in threshold_result.items()
                              if key not in ("by_operation", "threshold")})
                block["selected_threshold"] = threshold
                self.eval_rows.append(block)
                for operation, values in threshold_result["by_operation"].items():
                    self.operation_rows.append({"app": app, "model": model,
                        "resolution": resolution, "horizon_s": horizon,
                        "operation": operation, **values})
                self.false_rows.append({"app": app, "model": model,
                    "resolution": resolution, "horizon_s": horizon,
                    "false_cold_count": threshold_result["false_cold_count"],
                    "false_cold_denominator": threshold_result["false_cold_denominator"],
                    "false_cold_rate": threshold_result["false_cold_rate"],
                    "false_hot_count": threshold_result["false_hot_count"],
                    "false_hot_denominator": threshold_result["false_hot_denominator"],
                    "false_hot_rate": threshold_result["false_hot_rate"]})
                class_counts = defaultdict(Counter)
                for sample in samples:
                    actual = set(sample["actual"])
                    valid = set(sample["candidate"]) & set(sample["future_known"])
                    predicted = {key for key in valid
                                 if sample["scores"].get(key, 0.0) >= threshold}
                    for segment in valid:
                        file_id = id_to_key[segment].split("|", 1)[0]
                        file_class = file_classes.get(file_id, "UNKNOWN")
                        is_actual = segment in actual
                        is_predicted = segment in predicted
                        class_counts[file_class]["tp" if is_actual and is_predicted
                                                  else "fn" if is_actual
                                                  else "fp" if is_predicted
                                                  else "tn"] += 1
                    triples[sample["window_id"]][horizon] = sample["scores"]
                for file_class, counts in sorted(class_counts.items()):
                    self.file_class_rows.append({
                        "app": app, "model": model, "resolution": resolution,
                        "horizon_s": horizon, "file_class": file_class,
                        "selected_threshold": threshold,
                        "tp": counts["tp"], "fp": counts["fp"],
                        "fn": counts["fn"], "tn": counts["tn"],
                        "false_cold_rate": _safe(
                            counts["fn"], counts["tp"] + counts["fn"]),
                        "false_hot_rate": _safe(
                            counts["fp"], counts["fp"] + counts["tn"]),
                    })
            for window_id, values in triples.items():
                if set(values) != set(HORIZONS):
                    continue
                segments = set().union(*(set(values[h]) for h in HORIZONS))
                for segment in segments:
                    raw = tuple(values[h].get(segment, 0.0) for h in HORIZONS)
                    self.monotonic["comparisons"] += 1
                    if not raw[0] <= raw[1] <= raw[2]:
                        self.monotonic["violations"] += 1
                    _, changed = enforce_probability_monotonicity(*raw)
                    self.monotonic["adjustments"] += int(changed)

    def _file_classes(self):
        return {row[0]: row[1] for row in self.connection.execute(
            "SELECT file_id,file_class FROM files")}

    def _resolution_learning_stats(self, resolution, active, observed,
                                   global_scores):
        payload = {"resolution": resolution, "applications": {},
                   "future_reuse": {}, "operation_feature_gain": {}}
        for app, sessions in (("WPS", ("wps_01", "wps_02", "wps_03")),
                              ("FILES", ("files_01", "files_02"))):
            same, different, active_counts = [], [], []
            for session in sessions:
                rows = self.session_rows[session]
                active_counts.extend(len(active[row["window_id"]]) for row in rows)
                for left, right in zip(rows, rows[1:]):
                    left_set = active[left["window_id"]]
                    right_set = active[right["window_id"]]
                    value = _safe(len(left_set & right_set), len(left_set | right_set))
                    target = same if left["dominant_operation"] == right["dominant_operation"] else different
                    target.append(value)
            payload["applications"][app] = {
                "complete_windows": len(active_counts),
                "active_segments_mean": _safe(sum(active_counts), len(active_counts)),
                "active_segments_p50": percentile(active_counts, .5),
                "active_segments_p95": percentile(active_counts, .95),
                "active_segments_p99": percentile(active_counts, .99),
                "same_operation_adjacent_jaccard_mean": _safe(sum(same), len(same)),
                "same_operation_adjacent_pairs": len(same),
                "different_operation_adjacent_jaccard_mean": _safe(
                    sum(different), len(different)),
                "different_operation_adjacent_pairs": len(different),
            }
        for horizon in HORIZONS:
            ratios = []
            steps = horizon // 10
            for session in SESSION_ORDER:
                rows = self.session_rows[session]
                for position in range(len(rows) - steps):
                    current = active[rows[position]["window_id"]]
                    future = set().union(*(active[row["window_id"]]
                                           for row in rows[position + 1:position + steps + 1]))
                    ratios.append(_safe(len(current & future), len(future)))
            payload["future_reuse"][str(horizon)] = {
                "current_covers_future_active_mean": _safe(sum(ratios), len(ratios)),
                "samples": len(ratios),
            }
        operation_counts = defaultdict(Counter)
        operation_totals = Counter()
        for window_id in self.train_ids:
            operation = self.window_by_id[window_id]["dominant_operation"]
            operation_totals[operation] += 1
            operation_counts[operation].update(active[window_id])
        operation_scores = {
            operation: {segment: count / operation_totals[operation]
                        for segment, count in counts.items()}
            for operation, counts in operation_counts.items()
        }
        for horizon in HORIZONS:
            samples_global, samples_operation = [], []
            steps = horizon // 10
            rows = self.session_rows["wps_03"]
            for position in range(6, len(rows) - steps):
                current = rows[position]
                future = rows[position + 1:position + steps + 1]
                actual = set().union(*(active[row["window_id"]] for row in future))
                known = set().union(*(observed[row["window_id"]] for row in future))
                candidate = observed[current["window_id"]]
                common = {"window_id": current["window_id"], "actual": actual,
                          "future_known": known, "candidate": candidate,
                          "unknown": False}
                samples_global.append({**common, "scores": global_scores})
                samples_operation.append({**common, "scores": operation_scores.get(
                    current["dominant_operation"], {})})
            global_result = metrics_for(samples_global, 10)
            operation_result = metrics_for(samples_operation, 10)
            payload["operation_feature_gain"][str(horizon)] = {
                "global_frequency_average_precision": global_result["average_precision"],
                "operation_conditioned_average_precision": operation_result["average_precision"],
                "average_precision_delta": operation_result["average_precision"] -
                                           global_result["average_precision"],
                "global_frequency_micro_f1_at_10": global_result["micro_f1"],
                "operation_conditioned_micro_f1_at_10": operation_result["micro_f1"],
                "evaluation_only": True,
            }
        return payload

    def train_evaluate(self):
        started = time.monotonic_ns()
        resolution_runtime = {}
        reuse = {}
        for resolution in RESOLUTIONS:
            stage_start = time.monotonic_ns()
            active, observed, id_to_key = self._load_resolution(resolution)
            wps_global = self._global_scores(self.train_ids, active)
            distribution = self._state_distribution(self.train_ids, active,
                                                     self.states)
            self.learning_stats[str(resolution)] = self._resolution_learning_stats(
                resolution, active, observed, wps_global)
            self._select_recent_and_threshold(resolution, active, observed,
                                              wps_global, distribution)
            self._evaluate_app("WPS", "wps_01", "wps_03", resolution,
                               active, observed, id_to_key, wps_global,
                               distribution)
            files_global = self._global_scores(self.files_train_ids, active)
            self._evaluate_app("FILES", "files_01", "files_02", resolution,
                               active, observed, id_to_key, files_global,
                               distribution)
            jaccards = []
            for session in SESSION_ORDER:
                rows = self.session_rows[session]
                for left, right in zip(rows, rows[1:]):
                    a, b = active[left["window_id"]], active[right["window_id"]]
                    jaccards.append(_safe(len(a & b), len(a | b)))
            reuse[str(resolution)] = {"adjacent_jaccard_mean": _safe(sum(jaccards), len(jaccards)),
                                      "adjacent_jaccard_p50": percentile(jaccards, .5),
                                      "adjacent_jaccard_p95": percentile(jaccards, .95),
                                      "unique_segment_keys": len(id_to_key)}
            resolution_runtime[str(resolution)] = time.monotonic_ns() - stage_start
            if resolution == 100:
                self._online_replay(active, observed, id_to_key, distribution,
                                    wps_global, files_global)
        self._write_outputs(resolution_runtime, reuse,
                            time.monotonic_ns() - started)
        return {"selected_k": self.selected_k, "evaluation_rows": len(self.eval_rows),
                "online_generations": self.online_generations,
                "elapsed_ns": time.monotonic_ns() - started}

    def _online_replay(self, active, observed, id_to_key, distribution,
                       wps_global, files_global):
        predictions = []
        actual_rows = []
        latencies = []
        adjustments = 0
        evaluation_samples = []
        generation = defaultdict(int)
        for session, global_scores in (("wps_03", wps_global),
                                       ("files_02", files_global)):
            rows = self.session_rows[session]
            start = 6
            for position in range(start, len(rows) - 6):
                begin = time.perf_counter_ns()
                current = rows[position]
                current_id = current["window_id"]
                history = [row["window_id"] for row in rows[:position + 1]]
                horizon_scores = {}
                unknown = False
                for horizon in HORIZONS:
                    scores, is_unknown = self._scores(
                        "page_state_markov", current_id, history, active,
                        global_scores, distribution, horizon,
                        self.selected_recent[(100, horizon)])
                    horizon_scores[horizon] = scores
                    unknown = unknown or is_unknown
                candidate_segments = set().union(*(set(value) for value in horizon_scores.values()))
                ranked = sorted(candidate_segments,
                                key=lambda key: (-max(horizon_scores[h].get(key, 0)
                                                     for h in HORIZONS), key))[:128]
                file_segments = []
                for segment in ranked:
                    raw = tuple(horizon_scores[h].get(segment, 0.0) for h in HORIZONS)
                    calibrated, changed = enforce_probability_monotonicity(*raw)
                    adjustments += int(changed)
                    file_id, partition, segment_id = id_to_key[segment].split("|")
                    identity = self.connection.execute(
                        "SELECT dev_major,dev_minor,inode,file_version,file_pages FROM files WHERE file_id=?",
                        (file_id,)).fetchone()
                    file_segments.append({"file_id": file_id,
                        "dev_major": identity[0], "dev_minor": identity[1],
                        "inode": identity[2], "file_version": identity[3],
                        "partition_generation": int(partition),
                        "file_page_count": identity[4], "requested_bins": 100,
                        "effective_bins": min(100, identity[4]),
                        "resolution": 100, "segment_id": int(segment_id),
                        "raw_probability_10s": raw[0], "raw_probability_30s": raw[1],
                        "raw_probability_60s": raw[2],
                        "probability_10s": calibrated[0],
                        "probability_30s": calibrated[1],
                        "probability_60s": calibrated[2],
                        "probability_10s_q15": probability_to_q15(calibrated[0]),
                        "probability_30s_q15": probability_to_q15(calibrated[1]),
                        "probability_60s_q15": probability_to_q15(calibrated[2]),
                        "confidence_q15": 32767 if not unknown else 0,
                        "monotonic_adjusted": changed})
                generation[session] += 1
                metadata = self.window_by_id[current_id]
                total = metadata["anon_total"]
                prediction = {"schema_version": 1, "run_id": self.run["run_id"],
                    "app_id": int(metadata["app_id"]),
                    "domain_id": int(metadata["domain_id"]),
                    "session_id": session, "model_type": "page_state_markov",
                    "model_version": 1,
                    "prediction_generation": generation[session],
                    "generated_ns": int(metadata["window_start_ns"]) + 10_000_000_000,
                    "ttl_ns": 60_000_000_000, "kernel_write": False,
                    "file_segments": file_segments,
                    "anon_prediction": {
                        "hot_bytes_10s": metadata["anon_hot"],
                        "hot_bytes_30s": metadata["anon_hot"],
                        "hot_bytes_60s": metadata["anon_hot"],
                        "cooling_probability_q15": probability_to_q15(
                            _safe(metadata["anon_cold"], total)),
                        "hot_ratio": _safe(metadata["anon_hot"], total),
                        "cooling_ratio": _safe(metadata["anon_cold"], total)},
                    "unknown_reason": "DISTANCE_REJECTED" if unknown else None,
                    "future_features_used": False}
                validate_prediction_contract(prediction)
                predictions.append(prediction)
                future_payload = {"session_id": session,
                                  "prediction_generation": generation[session]}
                for horizon in HORIZONS:
                    steps = horizon // 10
                    future = rows[position + 1:position + steps + 1]
                    actual = set().union(*(active[row["window_id"]] for row in future))
                    known = set().union(*(observed[row["window_id"]] for row in future))
                    future_payload["actual_%ds" % horizon] = [id_to_key[value] for value in sorted(actual)]
                    if horizon == 10:
                        scores = {key: horizon_scores[horizon].get(key, 0.0)
                                  for key in candidate_segments}
                        evaluation_samples.append({"actual": actual,
                            "future_known": known, "candidate": observed[current_id],
                            "scores": scores, "unknown": unknown})
                actual_rows.append(future_payload)
                latencies.append(time.perf_counter_ns() - begin)
        self.online_generations = len(predictions)
        self._jsonl(self.online / "predictions.jsonl", predictions)
        self._jsonl(self.online / "actual_future_segments.jsonl", actual_rows)
        atomic_json(self.online / "online_evaluation.json", {
            "schema_version": 1, "model": "page_state_markov",
            "resolution": 100, "horizon_s": 10,
            **metrics_for(evaluation_samples, 10),
            "kernel_write": False, "future_features_used": False})
        atomic_json(self.online / "inference_latency.json", {
            "schema_version": 1, "samples": len(latencies),
            "p50_ns": percentile(latencies, .5), "p95_ns": percentile(latencies, .95),
            "p99_ns": percentile(latencies, .99),
            "windows_per_second": 1e9 / statistics.mean(latencies) if latencies else None,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
        atomic_json(self.online / "monotonicity_adjustments.json", {
            "schema_version": 1, "adjustments": adjustments,
            "predictions": len(predictions), "calibration": "p30=max(raw30,raw10); p60=max(raw60,p30)"})

    @staticmethod
    def _jsonl(path, rows):
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True) + "\n")
        os.replace(str(temporary), str(path))

    @staticmethod
    def _csv(path, rows):
        rows = list(rows)
        temporary = path.with_name(path.name + ".tmp")
        fields = sorted({key for row in rows for key in row})
        with temporary.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields,
                                    extrasaction="ignore")
            writer.writeheader(); writer.writerows(rows)
        os.replace(str(temporary), str(path))

    def _write_outputs(self, resolution_runtime, reuse, elapsed_ns):
        atomic_json(self.models / "file_vocab.json", self.file_vocab_audit)
        atomic_json(self.models / "feature_schema.json", {
            "schema_version": 1, "features": self.feature_names,
            "feature_count": len(self.feature_names), "fit_source": "wps_01_only",
            "future_features_used": False})
        atomic_json(self.models / "scaler.json", self.scaler.payload())
        atomic_json(self.models / "page_state_cluster.json", self.cluster.to_dict())
        atomic_json(self.models / "state_markov.json", {
            "schema_version": 1, "semantics": "PageAccessState_t_to_PageAccessState_t_plus_1",
            "old_continue_reentry_used": False, "transitions": self.markov.to_dict()})
        thresholds = {"schema_version": 1,
            "balanced": {"%s:l%d:h%d" % key: value
                         for key, value in self.selected_thresholds.items()},
            "recent_history": {"l%d:h%d" % key: value
                               for key, value in self.selected_recent.items()},
            "unknown": self.unknown, "selection_source": "wps_02_validation_only"}
        safe = {}
        for limit in (.01, .05, .10):
            by_configuration = {}
            for model in MODELS:
                for resolution in RESOLUTIONS:
                    for horizon in HORIZONS:
                        eligible = [row for row in self.threshold_rows
                                    if row["model"] == model
                                    and row["resolution"] == resolution
                                    and row["horizon_s"] == horizon
                                    and row["false_cold_rate"] <= limit]
                        selected = max(
                            eligible,
                            key=lambda item: item["reclaim_candidate_ratio"],
                            default=None)
                        key = "%s:l%d:h%d" % (model, resolution, horizon)
                        by_configuration[key] = selected
            safe[str(limit)] = by_configuration
        thresholds["safe_thresholds"] = safe
        atomic_json(self.models / "thresholds.json", thresholds)
        model_metadata = {"schema_version": 1, "models": list(MODELS),
            "resolutions": list(RESOLUTIONS), "horizons_s": list(HORIZONS),
            "selected_k": self.selected_k, "k_candidates": self.k_audit,
            "unknown": self.unknown, "train_session": "wps_01",
            "validation_session": "wps_02", "test_session": "wps_03",
            "files_secondary_split": {"train": "files_01", "test": "files_02"},
            "future_features_used": False, "old_continue_reentry_used": False,
            "kernel_write": False, "elapsed_ns": elapsed_ns,
            "resolution_runtime_ns": resolution_runtime}
        atomic_json(self.models / "model_metadata.json", model_metadata)
        for horizon in HORIZONS:
            payload = {"schema_version": 1, "horizon_s": horizon,
                       "results": [row for row in self.eval_rows
                                   if row["horizon_s"] == horizon]}
            atomic_json(self.evaluation / ("metrics_%ds.json" % horizon), payload)
        self._csv(self.evaluation / "per_model_resolution.csv", self.eval_rows)
        self._csv(self.evaluation / "threshold_pareto.csv", self.threshold_rows)
        self._csv(self.evaluation / "false_cold_analysis.csv", self.false_rows)
        self._csv(self.evaluation / "false_hot_analysis.csv", self.false_rows)
        self._csv(self.evaluation / "per_operation.csv", self.operation_rows)
        file_class_summary = self._file_class_summary()
        self._csv(self.evaluation / "per_file_class.csv", self.file_class_rows)
        violations = self.monotonic["violations"]
        comparisons = self.monotonic["comparisons"]
        atomic_json(self.evaluation / "raw_probability_monotonicity.json", {
            "schema_version": 1, "comparisons": comparisons,
            "violations": violations, "violation_rate": _safe(violations, comparisons),
            "calibration_adjustments": self.monotonic["adjustments"],
            "online_calibration_required": True})
        best = sorted(self.eval_rows, key=lambda row: (-row.get("micro_f1", 0),
                                                       row["app"], row["model"]))[:12]
        lines = ["# Baseline comparison", "", "All hyperparameters and thresholds were selected on WPS validation only.", "",
                 "| App | Model | Level | Horizon | Micro F1 | AP | False-cold | UNKNOWN |",
                 "|---|---|---:|---:|---:|---:|---:|---:|"]
        for row in best:
            lines.append("| {app} | {model} | {resolution} | {horizon_s}s | {micro_f1:.4f} | {average_precision:.4f} | {false_cold_rate:.4f} | {unknown_rate:.4f} |".format(**row))
        atomic_text(self.evaluation / "baseline_comparison.md", "\n".join(lines) + "\n")
        atomic_json(self.analysis / "temporal_reuse_analysis.json", reuse)
        atomic_text(self.analysis / "temporal_reuse_analysis.md",
                    "# Temporal reuse analysis\n\n" + "\n".join(
                        "- Level %s adjacent-window Jaccard mean: %.4f" %
                        (resolution, values["adjacent_jaccard_mean"])
                        for resolution, values in reuse.items()) + "\n")
        atomic_json(self.analysis / "learnability_analysis.json", {
            "schema_version": 1, "resolutions": self.learning_stats,
            "same_vs_different_operation_scope": "adjacent complete windows",
            "operation_conditioned_scores_fit_source": "wps_01_only",
            "operation_gain_evaluation_source": "wps_03_only",
        })
        self._write_learning_analyses(reuse, file_class_summary)
        atomic_json(self.analysis / "performance_audit.json", {
            "schema_version": 1, "training_evaluation_elapsed_ns": elapsed_ns,
            "per_resolution_elapsed_ns": resolution_runtime,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "model_size_bytes": sum(path.stat().st_size for path in self.models.rglob("*") if path.is_file()),
            "dataset_size_bytes": sum(path.stat().st_size for path in self.dataset.rglob("*") if path.is_file()),
            "user_space_async_only": True, "kernel_path_execution": False})

    def _file_class_summary(self):
        counts = Counter(row[0] for row in self.connection.execute(
            "SELECT file_class FROM files"))
        return [{"file_class": name, "file_count": count,
                 "note": "classification uses public size/stability only"}
                for name, count in sorted(counts.items())]

    def _write_learning_analyses(self, reuse, file_classes):
        active_counts = {resolution: [row["active_l%d" % resolution]
                                      for row in self.windows]
                         for resolution in RESOLUTIONS}
        resolution_payload = {}
        for resolution, values in active_counts.items():
            resolution_payload[str(resolution)] = {
                "mean_active_segments": statistics.mean(values),
                "p50": percentile(values, .5), "p95": percentile(values, .95),
                "p99": percentile(values, .99), **reuse[str(resolution)]}
        atomic_json(self.analysis / "resolution_comparison.json", resolution_payload)
        atomic_text(self.analysis / "resolution_comparison.md",
                    "# Resolution comparison\n\n" + "\n".join(
                        "- Level %s: mean active %.2f, P95 %.2f, adjacent Jaccard %.4f" %
                        (key, value["mean_active_segments"], value["p95"],
                         value["adjacent_jaccard_mean"])
                        for key, value in resolution_payload.items()) + "\n")
        atomic_json(self.analysis / "file_role_analysis.json", {
            "schema_version": 1, "classes": file_classes,
            "absolute_path_used": False})
        atomic_text(self.analysis / "file_role_analysis.md",
                    "# File role analysis\n\nNo private absolute path is used. Roles are size/stability classes; see JSON.\n")
        operations = Counter(row["dominant_operation"] for row in self.windows)
        atomic_json(self.analysis / "operation_segment_patterns.json", {
            "schema_version": 1, "window_counts": operations,
            "per_operation_evaluation_rows": len(self.operation_rows),
            "per_resolution_learnability": {
                key: value["operation_feature_gain"]
                for key, value in self.learning_stats.items()}})
        atomic_text(self.analysis / "operation_segment_patterns.md",
                    "# Operation/segment patterns\n\n" + "\n".join(
                        "- %s: %d complete windows" % item
                        for item in sorted(operations.items())) + "\n")
        atomic_json(self.analysis / "data_diversity.json", {
            "schema_version": 1, "sessions": list(SESSION_ORDER),
            "applications": ["WPS", "FILES"], "operation_types": self.operations,
            "same_document_temporal_generalization": True,
            "unseen_document_evaluation": "NOT_AVAILABLE",
            "statistical_stability": "PILOT: three WPS and two Files sessions"})
        atomic_text(self.analysis / "data_diversity.md",
                    "# Data diversity\n\nThree WPS temporal sessions and two secondary Files sessions are available. WPS evaluates same-document temporal generalization, not unseen-document generalization.\n")
        ablation = self._ablation()
        atomic_json(self.analysis / "feature_ablation.json", ablation)

    def _ablation(self):
        groups = {
            "page_only": list(range(0, 6)),
            "page_plus_anon": list(range(0, 9)),
            "page_plus_operation": list(range(0, 6)) + list(range(9, len(self.feature_names))),
            "page_plus_anon_plus_operation": list(range(len(self.feature_names))),
        }
        rows = []
        for name, indexes in groups.items():
            train = [[self.scaled[key][index] for index in indexes]
                     for key in self.train_ids]
            val = [[self.scaled[key][index] for index in indexes]
                   for key in self.val_ids]
            model = LightweightKMeans(self.selected_k).fit(train)
            states = {key: model.predict_one(
                [self.scaled[key][index] for index in indexes])
                for key in self.scaled}
            markov = self._fit_markov(states)
            def transition_accuracy(session_ids):
                correct = total = 0
                for session in session_ids:
                    sequence = self.session_rows[session]
                    for current, following in zip(sequence, sequence[1:]):
                        predicted = markov.predict(
                            states[current["window_id"]], duration_bin(10))
                        if not predicted:
                            continue
                        selected = max(predicted, key=lambda key: (predicted[key], -key))
                        correct += selected == states[following["window_id"]]
                        total += 1
                return _safe(correct, total), total
            val_accuracy, val_transitions = transition_accuracy(("wps_02",))
            test_accuracy, test_transitions = transition_accuracy(("wps_03",))
            rows.append({"features": name, "feature_count": len(indexes),
                         "validation_inertia_per_row": model.inertia(val) / len(val),
                         "validation_markov_top1_state_accuracy": val_accuracy,
                         "validation_transitions": val_transitions,
                         "test_markov_top1_state_accuracy": test_accuracy,
                         "test_transitions": test_transitions})
        return {"schema_version": 1, "selection_data": "wps_02_validation",
                "rows": rows, "markov_semantics": "state_t_to_state_t_plus_1",
                "note": "small pilot; ablation stability is limited"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args()
    pipeline = RealModelPipeline(args.project)
    try:
        print(json.dumps(pipeline.train_evaluate(), sort_keys=True))
    finally:
        pipeline.close()


if __name__ == "__main__":
    main()
