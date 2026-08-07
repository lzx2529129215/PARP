"""Evaluation metrics for application and operation prediction."""

from __future__ import annotations

from collections.abc import Iterable


def _ignore_set(ignore_labels: Iterable[int] | None = None) -> set[int]:
    """Normalize ignored label ids."""
    return {int(label) for label in ignore_labels} if ignore_labels is not None else set()


def _label_set(labels: Iterable[int] | set[int], ignore_labels: Iterable[int] | None = None) -> set[int]:
    """Normalize label formats to a set of integer ids.

    Supported inputs:
    - a set of label ids
    - an iterable of label ids
    - a multi-hot vector, for example [0, 1, 0, 1] -> {1, 3}
    """
    ignored = _ignore_set(ignore_labels)

    if isinstance(labels, set):
        return {int(label) for label in labels if int(label) not in ignored}

    values = list(labels)
    if not values:
        return set()

    is_multihot = all(value in (0, 1, False, True) for value in values) and len(values) > 2
    if is_multihot:
        return {idx for idx, value in enumerate(values) if value and idx not in ignored}

    return {int(value) for value in values if int(value) not in ignored}


def _pred_list(preds: Iterable[int], ignore_labels: Iterable[int] | None = None) -> list[int]:
    ignored = _ignore_set(ignore_labels)
    return [int(pred) for pred in preds if int(pred) not in ignored]


def hit_at_k(
    pred_topk: Iterable[int],
    labels: Iterable[int] | set[int],
    ignore_labels: Iterable[int] | None = None,
) -> int:
    """Hit@K: return 1 if any non-ignored true label appears in top-k."""
    label_ids = _label_set(labels, ignore_labels)
    if not label_ids:
        return 0
    return int(bool(set(_pred_list(pred_topk, ignore_labels)) & label_ids))


def recall_at_k(
    pred_topk: Iterable[int],
    labels: Iterable[int] | set[int],
    ignore_labels: Iterable[int] | None = None,
) -> float:
    """Recall@K: covered true labels divided by true labels."""
    label_ids = _label_set(labels, ignore_labels)
    if not label_ids:
        return 0.0
    return len(set(_pred_list(pred_topk, ignore_labels)) & label_ids) / len(label_ids)


def precision_at_k(
    pred_topk: Iterable[int],
    labels: Iterable[int] | set[int],
    ignore_labels: Iterable[int] | None = None,
) -> float:
    """Precision@K: matched true labels divided by non-ignored predictions."""
    preds = _pred_list(pred_topk, ignore_labels)
    if not preds:
        return 0.0
    return len(set(preds) & _label_set(labels, ignore_labels)) / len(preds)


def mrr(
    pred_ranked_list: Iterable[int],
    labels: Iterable[int] | set[int],
    ignore_labels: Iterable[int] | None = None,
) -> float:
    """Reciprocal rank of the first non-ignored prediction that hits a true label."""
    label_ids = _label_set(labels, ignore_labels)
    if not label_ids:
        return 0.0
    for rank, pred in enumerate(_pred_list(pred_ranked_list, ignore_labels), start=1):
        if pred in label_ids:
            return 1.0 / rank
    return 0.0


def topk_accuracy(
    pred_topk: Iterable[int],
    label: int,
    ignore_labels: Iterable[int] | None = None,
) -> int:
    """Top-K accuracy for a single-label case."""
    label = int(label)
    if label in _ignore_set(ignore_labels):
        return 0
    return int(label in set(_pred_list(pred_topk, ignore_labels)))


def _self_test() -> None:
    assert hit_at_k([1, 3], [0, 1, 0, 0]) == 1
    assert hit_at_k([2, 3], {1}) == 0
    assert recall_at_k([1, 2], {1, 3}) == 0.5
    assert precision_at_k([1, 2], {1, 3}) == 0.5
    assert mrr([4, 2, 1], {1}) == 1 / 3
    assert topk_accuracy([4, 2, 1], 2) == 1

    ignored = {0, 1}
    assert _label_set([1, 1, 0, 1], ignore_labels=ignored) == {3}
    assert hit_at_k([0, 1, 3], {1}, ignore_labels=ignored) == 0
    assert recall_at_k([0, 2, 3], {0, 2, 4}, ignore_labels={0}) == 0.5
    assert precision_at_k([0, 1, 2], {2}, ignore_labels=ignored) == 1.0
    assert mrr([0, 1, 2], {2}, ignore_labels=ignored) == 1.0
    assert topk_accuracy([0, 1, 2], 1, ignore_labels=ignored) == 0
    print("metrics self-test passed")


if __name__ == "__main__":
    _self_test()
