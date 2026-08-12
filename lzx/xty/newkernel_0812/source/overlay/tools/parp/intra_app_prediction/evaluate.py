"""Dependency-free multi-label metrics for page segment prediction."""


def _safe(numerator, denominator):
    return numerator / denominator if denominator else 0.0


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


def multilabel_metrics(actual_rows, score_rows, top_k=10,
                       cold_threshold=.1, hot_threshold=.9):
    if len(actual_rows) != len(score_rows):
        raise ValueError("actual and score row counts differ")
    tp = fp = fn = hit = false_cold = false_hot = 0
    cold_candidates = hot_candidates = 0
    precision_rows = []
    recall_rows = []
    f1_rows = []
    jaccard_rows = []
    average_precision_rows = []
    universe = set()
    predicted_union = set()
    for actual, scores in zip(actual_rows, score_rows):
        actual = set(actual)
        ranked = sorted(scores, key=lambda key: (-scores[key], key))[:top_k]
        predicted = set(ranked)
        common = len(actual & predicted)
        row_precision = _safe(common, len(predicted))
        row_recall = _safe(common, len(actual))
        precision_rows.append(row_precision)
        recall_rows.append(row_recall)
        f1_rows.append(_safe(2 * row_precision * row_recall,
                             row_precision + row_recall))
        jaccard_rows.append(_safe(common, len(actual | predicted)))
        average_precision_rows.append(_average_precision(actual, scores))
        tp += common
        fp += len(predicted - actual)
        fn += len(actual - predicted)
        hit += bool(common)
        false_cold += sum(scores.get(key, 0.0) <= cold_threshold
                          for key in actual)
        cold_candidates += len(actual)
        false_hot += sum(value >= hot_threshold and key not in actual
                         for key, value in scores.items())
        hot_candidates += sum(value >= hot_threshold for value in scores.values())
        universe.update(actual)
        universe.update(scores)
        predicted_union.update(predicted)
    count = len(actual_rows)
    micro_precision = _safe(tp, tp + fp)
    micro_recall = _safe(tp, tp + fn)
    return {
        "precision_at_k": _safe(sum(precision_rows), count),
        "recall_at_k": _safe(sum(recall_rows), count),
        "f1_at_k": _safe(sum(f1_rows), count),
        "jaccard": _safe(sum(jaccard_rows), count),
        "micro_precision": micro_precision,
        "micro_recall": micro_recall,
        "micro_f1": _safe(2 * micro_precision * micro_recall,
                           micro_precision + micro_recall),
        "macro_f1": _safe(sum(f1_rows), count),
        "average_precision": _safe(sum(average_precision_rows), count),
        "hit_rate": _safe(hit, count),
        "coverage": _safe(len(predicted_union), len(universe)),
        "false_protection_rate": _safe(fp, tp + fp),
        "false_reclaim_risk": _safe(fn, tp + fn),
        "false_cold": _safe(false_cold, cold_candidates),
        "false_hot": _safe(false_hot, hot_candidates),
    }
