"""Observe-only per-application file-page hot-set clustering and prediction.

The module consumes the existing eBPF ``page_access`` records.  It never
writes page-cache, cgroup, debugfs, reclaim, prefetch, or kernel policy state.
All clustering and transition learning is causal: a published model contains
only windows that were closed before the model was installed.
"""

from __future__ import annotations

import bisect
import concurrent.futures
import hashlib
import json
import math
import multiprocessing
import os
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence


BASE_ONLY = "BASE_ONLY"
UNKNOWN = "UNKNOWN"
MODEL_SCHEMA_VERSION = 1
MAX_EVENT_PAGES = 1_048_576


@dataclass(frozen=True, order=True)
class PageId:
    """Session-stable identity for one base page in a regular file."""

    device_major: int
    device_minor: int
    inode: int
    page_index: int

    def to_dict(self) -> dict[str, int]:
        return {
            "device_major": self.device_major,
            "device_minor": self.device_minor,
            "inode": self.inode,
            "page_index": self.page_index,
        }


@dataclass(frozen=True)
class PageSnapshot:
    session_id: str
    app_id: str
    foreground_epoch_id: str
    window_start_ns: int
    window_end_ns: int
    page_size: int
    pages: frozenset[PageId]
    page_access_events: int
    repeated_page_hits: int
    valid: bool
    invalid_reasons: tuple[str, ...] = ()

    @property
    def model_eligible(self) -> bool:
        return self.valid and bool(self.pages)

    def to_record(self) -> dict[str, Any]:
        return {
            "record_type": "SNAPSHOT",
            "schema_version": MODEL_SCHEMA_VERSION,
            "session_id": self.session_id,
            "app_id": self.app_id,
            "foreground_epoch_id": self.foreground_epoch_id,
            "window_start_ns": self.window_start_ns,
            "window_end_ns": self.window_end_ns,
            "page_size": self.page_size,
            "page_count": len(self.pages),
            "page_access_events": self.page_access_events,
            "repeated_page_hits": self.repeated_page_hits,
            "valid": self.valid,
            "model_eligible": self.model_eligible,
            "invalid_reasons": list(self.invalid_reasons),
            "page_ranges": pages_to_ranges(self.pages, self.page_size),
        }


@dataclass(frozen=True)
class PageBucket:
    bucket_id: str
    medoid_pages: frozenset[PageId]
    member_count: int
    hot_pages: frozenset[PageId]
    page_presence_counts: tuple[tuple[PageId, int], ...]
    rejection_threshold: float

    def to_dict(self, page_size: int) -> dict[str, Any]:
        return {
            "bucket_id": self.bucket_id,
            "member_count": self.member_count,
            "rejection_threshold": self.rejection_threshold,
            "medoid_page_ranges": pages_to_ranges(self.medoid_pages, page_size),
            "hot_page_ranges": pages_to_ranges(self.hot_pages, page_size),
            "page_probabilities": [
                {
                    **page.to_dict(),
                    "presence_count": count,
                    "member_count": self.member_count,
                    "probability": count / max(1, self.member_count),
                    "is_hot": page in self.hot_pages,
                }
                for page, count in self.page_presence_counts
            ],
        }


@dataclass(frozen=True)
class PageBucketModel:
    app_id: str
    model_version: str
    page_size: int
    trained_through_ns: int
    training_window_count: int
    base_hot_pages: frozenset[PageId]
    buckets: tuple[PageBucket, ...]
    selected_k: int
    silhouette: float
    first_order: dict[str, dict[str, int]] = field(default_factory=dict)
    second_order: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict)
    global_next: dict[str, int] = field(default_factory=dict)

    def classify(self, pages: frozenset[PageId]) -> tuple[str, float, str]:
        residual = pages - self.base_hot_pages
        if not residual:
            return BASE_ONLY, 1.0, "BASE_ONLY"
        if not self.buckets:
            return UNKNOWN, 0.0, "NO_BUCKETS"
        ranked = sorted(
            (
                (jaccard_similarity(residual, bucket.medoid_pages), bucket.bucket_id, bucket)
                for bucket in self.buckets
            ),
            key=lambda item: (-item[0], item[1]),
        )
        similarity, bucket_id, bucket = ranked[0]
        if similarity + 1e-12 < bucket.rejection_threshold:
            return UNKNOWN, similarity, "BELOW_REJECTION_THRESHOLD"
        return bucket_id, similarity, "ASSIGNED"

    def bucket_hot_pages(self, bucket_id: str) -> frozenset[PageId]:
        if bucket_id == BASE_ONLY:
            return frozenset()
        for bucket in self.buckets:
            if bucket.bucket_id == bucket_id:
                return bucket.hot_pages
        return frozenset()

    def transition_distribution(
        self, previous_bucket: str | None, current_bucket: str
    ) -> tuple[dict[str, float], str]:
        counts: dict[str, int] = {}
        source = "NONE"
        if previous_bucket is not None:
            counts = self.second_order.get((previous_bucket, current_bucket), {})
            if counts:
                source = "SECOND_ORDER"
        if not counts:
            counts = self.first_order.get(current_bucket, {})
            if counts:
                source = "FIRST_ORDER_BACKOFF"
        total = sum(max(0, int(value)) for value in counts.values())
        if total <= 0:
            return {}, "NONE"
        return {
            bucket_id: int(count) / total
            for bucket_id, count in sorted(counts.items())
            if int(count) > 0
        }, source

    def most_common_bucket(self) -> str:
        if not self.global_next:
            return ""
        return sorted(self.global_next, key=lambda key: (-self.global_next[key], key))[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MODEL_SCHEMA_VERSION,
            "app_id": self.app_id,
            "model_version": self.model_version,
            "page_size": self.page_size,
            "trained_through_ns": self.trained_through_ns,
            "training_window_count": self.training_window_count,
            "base_hot_page_ranges": pages_to_ranges(self.base_hot_pages, self.page_size),
            "selected_k": self.selected_k,
            "silhouette": self.silhouette,
            "buckets": [bucket.to_dict(self.page_size) for bucket in self.buckets],
            "first_order": [
                {"current_bucket": current, "next_counts": dict(sorted(next_counts.items()))}
                for current, next_counts in sorted(self.first_order.items())
            ],
            "second_order": [
                {
                    "previous_bucket": previous,
                    "current_bucket": current,
                    "next_counts": dict(sorted(next_counts.items())),
                }
                for (previous, current), next_counts in sorted(self.second_order.items())
            ],
            "global_next": dict(sorted(self.global_next.items())),
        }


@dataclass(frozen=True)
class PageBucketPrediction:
    prediction_id: str
    session_id: str
    app_id: str
    foreground_epoch_id: str
    model_version: str
    model_trained_through_ns: int
    prediction_time_ns: int
    source_window_start_ns: int
    source_window_end_ns: int
    previous_bucket: str
    current_bucket: str
    current_similarity: float
    transition_source: str
    bucket_probabilities: dict[str, float]
    predicted_bucket: str
    confidence: float
    base_hot_pages: frozenset[PageId]
    current_hot_pages: frozenset[PageId]
    predicted_hot_pages: frozenset[PageId]
    global_baseline_pages: frozenset[PageId]
    page_size: int

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": MODEL_SCHEMA_VERSION,
            "prediction_id": self.prediction_id,
            "session_id": self.session_id,
            "app_id": self.app_id,
            "foreground_epoch_id": self.foreground_epoch_id,
            "model_version": self.model_version,
            "model_trained_through_ns": self.model_trained_through_ns,
            "prediction_time_ns": self.prediction_time_ns,
            "source_window_start_ns": self.source_window_start_ns,
            "source_window_end_ns": self.source_window_end_ns,
            "previous_bucket": self.previous_bucket,
            "current_bucket": self.current_bucket,
            "current_similarity": self.current_similarity,
            "transition_source": self.transition_source,
            "bucket_probabilities": dict(sorted(self.bucket_probabilities.items())),
            "predicted_bucket": self.predicted_bucket,
            "confidence": self.confidence,
            "base_hot_page_count": len(self.base_hot_pages),
            "current_hot_page_count": len(self.current_hot_pages),
            "predicted_hot_page_count": len(self.predicted_hot_pages),
            "base_hot_page_ranges": pages_to_ranges(self.base_hot_pages, self.page_size),
            "current_hot_page_ranges": pages_to_ranges(self.current_hot_pages, self.page_size),
            "predicted_hot_page_ranges": pages_to_ranges(self.predicted_hot_pages, self.page_size),
        }


@dataclass(frozen=True)
class _ClusterCandidate:
    k: int
    medoids: tuple[frozenset[PageId], ...]
    assignments: tuple[int, ...]
    silhouette: float


@dataclass
class _RuntimeState:
    model_version: str = ""
    foreground_epoch_id: str = ""
    previous_bucket: str | None = None
    current_bucket: str | None = None


def jaccard_similarity(left: frozenset[PageId], right: frozenset[PageId]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def pages_to_ranges(pages: Iterable[PageId], page_size: int) -> list[dict[str, int]]:
    """Compress sorted page identities into deterministic contiguous ranges."""

    grouped: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    for page in pages:
        grouped[(page.device_major, page.device_minor, page.inode)].append(page.page_index)
    ranges: list[dict[str, int]] = []
    for (major, minor, inode), indices in sorted(grouped.items()):
        ordered = sorted(set(indices))
        if not ordered:
            continue
        start = previous = ordered[0]
        for index in ordered[1:] + [ordered[-1] + 2]:
            if index == previous + 1:
                previous = index
                continue
            count = previous - start + 1
            ranges.append({
                "device_major": major,
                "device_minor": minor,
                "inode": inode,
                "start_page_index": start,
                "page_count": count,
                "start_offset_bytes": start * page_size,
                "length_bytes": count * page_size,
            })
            start = previous = index
    return ranges


def train_page_hotset_model(
    *,
    app_id: str,
    model_version: str,
    page_size: int,
    snapshots: Sequence[PageSnapshot],
    base_hot_coverage: float,
    bucket_hot_coverage: float,
    min_cluster_members: int = 10,
) -> PageBucketModel | None:
    """Train one deterministic K=2--5 page-set model from closed windows."""

    ordered = sorted(snapshots, key=lambda row: (row.window_start_ns, row.window_end_ns))
    eligible = [snapshot for snapshot in ordered if snapshot.model_eligible]
    if not eligible:
        return None
    base_required = max(1, math.ceil(len(eligible) * base_hot_coverage))
    page_coverage: Counter[PageId] = Counter()
    for snapshot in eligible:
        page_coverage.update(snapshot.pages)
    base_hot = frozenset(page for page, count in page_coverage.items() if count >= base_required)

    residual_rows = [
        (snapshot, frozenset(snapshot.pages - base_hot))
        for snapshot in eligible
        if snapshot.pages - base_hot
    ]
    candidates: dict[int, _ClusterCandidate] = {}
    for k in (2, 3, 4, 5):
        candidate = _fit_kmedoids(
            [pages for _snapshot, pages in residual_rows],
            k=k,
            min_cluster_members=max(
                int(min_cluster_members), math.ceil(len(residual_rows) * 0.05)
            ),
        )
        if candidate is not None:
            candidates[k] = candidate
    if not candidates:
        return None
    # Prefer a meaningfully better silhouette; within the 0.02 tolerance use
    # the smaller, more stable state space.  This preserves the original
    # This retains the former K=4/K=5 tolerance rule while allowing a data set
    # with only two or three durable work states to train instead of being
    # rejected outright.
    selected = candidates[min(candidates)]
    for k in sorted(candidates):
        candidate = candidates[k]
        if candidate.silhouette > selected.silhouette + 0.02:
            selected = candidate

    clusters: dict[int, list[frozenset[PageId]]] = defaultdict(list)
    for pages, assignment in zip((row[1] for row in residual_rows), selected.assignments):
        clusters[assignment].append(pages)
    buckets: list[PageBucket] = []
    for index, medoid in enumerate(selected.medoids):
        members = clusters[index]
        presence: Counter[PageId] = Counter()
        similarities: list[float] = []
        for pages in members:
            presence.update(pages)
            similarities.append(jaccard_similarity(pages, medoid))
        hot_required = max(1, math.ceil(len(members) * bucket_hot_coverage))
        hot_pages = frozenset(page for page, count in presence.items() if count >= hot_required)
        similarities.sort()
        p05_index = int(math.floor(0.05 * max(0, len(similarities) - 1)))
        threshold = max(0.1, similarities[p05_index] if similarities else 1.0)
        buckets.append(PageBucket(
            bucket_id=f"B{index + 1}",
            medoid_pages=medoid,
            member_count=len(members),
            hot_pages=hot_pages,
            page_presence_counts=tuple(sorted(presence.items())),
            rejection_threshold=threshold,
        ))

    partial = PageBucketModel(
        app_id=app_id,
        model_version=model_version,
        page_size=page_size,
        trained_through_ns=max(snapshot.window_end_ns for snapshot in eligible),
        training_window_count=len(eligible),
        base_hot_pages=base_hot,
        buckets=tuple(buckets),
        selected_k=selected.k,
        silhouette=selected.silhouette,
    )
    first, second, global_next = _build_transition_tables(partial, ordered)
    return replace(
        partial,
        first_order=first,
        second_order=second,
        global_next=global_next,
    )


def _fit_kmedoids(
    page_sets: Sequence[frozenset[PageId]], *, k: int, min_cluster_members: int
) -> _ClusterCandidate | None:
    if len(page_sets) < k:
        return None
    unique_sets = _unique_page_sets(page_sets)
    if len(unique_sets) < k:
        return None
    # Page sets can each contain tens of thousands of IDs.  These deterministic
    # bounds keep medoid fitting practical while retaining a representative
    # spread of a 300--3600 window history.
    reference = _bounded_sample(unique_sets, 128)
    first_candidates = _bounded_sample(unique_sets, 32)
    first = min(
        first_candidates,
        key=lambda candidate: (
            sum(1.0 - jaccard_similarity(candidate, other) for other in reference),
            _page_set_sort_key(candidate),
        ),
    )
    medoids = [first]
    while len(medoids) < k:
        remaining = [pages for pages in unique_sets if pages not in medoids]
        choice = min(
            remaining,
            key=lambda pages: (
                -min(1.0 - jaccard_similarity(pages, medoid) for medoid in medoids),
                _page_set_sort_key(pages),
            ),
        )
        medoids.append(choice)

    for _iteration in range(10):
        assignments = _assign_pages(page_sets, medoids)
        updated: list[frozenset[PageId]] = []
        for cluster_index, current in enumerate(medoids):
            members = [pages for pages, assigned in zip(page_sets, assignments) if assigned == cluster_index]
            if not members:
                return None
            unique_members = _unique_page_sets(members)
            candidates = _bounded_sample(unique_members, 16)
            if current not in candidates:
                candidates = [current, *candidates]
            comparison = _bounded_sample(members, 32)
            updated.append(min(
                candidates,
                key=lambda candidate: (
                    sum(1.0 - jaccard_similarity(candidate, other) for other in comparison),
                    _page_set_sort_key(candidate),
                ),
            ))
        if updated == medoids:
            break
        medoids = updated

    medoids = sorted(medoids, key=_page_set_sort_key)
    assignments = _assign_pages(page_sets, medoids)
    sizes = Counter(assignments)
    if any(sizes[index] < min_cluster_members for index in range(k)):
        return None
    silhouette = _silhouette(page_sets, assignments, k)
    return _ClusterCandidate(k, tuple(medoids), tuple(assignments), silhouette)


def _assign_pages(
    page_sets: Sequence[frozenset[PageId]], medoids: Sequence[frozenset[PageId]]
) -> list[int]:
    result: list[int] = []
    for pages in page_sets:
        result.append(min(
            range(len(medoids)),
            key=lambda index: (-jaccard_similarity(pages, medoids[index]), index),
        ))
    return result


def _silhouette(
    page_sets: Sequence[frozenset[PageId]], assignments: Sequence[int], k: int
) -> float:
    sampled_indices = _bounded_sample(list(range(len(page_sets))), 128)
    cluster_indices = {
        cluster: _bounded_sample(
            [index for index, assigned in enumerate(assignments) if assigned == cluster],
            32,
        )
        for cluster in range(k)
    }
    values: list[float] = []
    for index in sampled_indices:
        own = assignments[index]
        own_peers = [peer for peer in cluster_indices[own] if peer != index]
        a = (
            sum(1.0 - jaccard_similarity(page_sets[index], page_sets[peer]) for peer in own_peers)
            / len(own_peers)
            if own_peers else 0.0
        )
        other_distances: list[float] = []
        for cluster in range(k):
            if cluster == own or not cluster_indices[cluster]:
                continue
            peers = cluster_indices[cluster]
            other_distances.append(
                sum(1.0 - jaccard_similarity(page_sets[index], page_sets[peer]) for peer in peers)
                / len(peers)
            )
        b = min(other_distances) if other_distances else 0.0
        denominator = max(a, b)
        values.append((b - a) / denominator if denominator > 0 else 0.0)
    return sum(values) / len(values) if values else 0.0


def _build_transition_tables(
    model: PageBucketModel, snapshots: Sequence[PageSnapshot]
) -> tuple[dict[str, dict[str, int]], dict[tuple[str, str], dict[str, int]], dict[str, int]]:
    first: dict[str, Counter[str]] = defaultdict(Counter)
    second: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    global_next: Counter[str] = Counter()
    epoch = ""
    sequence: list[str] = []
    for snapshot in sorted(snapshots, key=lambda row: row.window_start_ns):
        if not snapshot.model_eligible:
            epoch = ""
            sequence = []
            continue
        bucket, _similarity, _reason = model.classify(snapshot.pages)
        if bucket == UNKNOWN:
            epoch = ""
            sequence = []
            continue
        if snapshot.foreground_epoch_id != epoch:
            epoch = snapshot.foreground_epoch_id
            sequence = []
        if sequence and sequence[-1] == bucket:
            continue
        if sequence:
            first[sequence[-1]][bucket] += 1
            global_next[bucket] += 1
        if len(sequence) >= 2:
            second[(sequence[-2], sequence[-1])][bucket] += 1
        sequence.append(bucket)
    return (
        {key: dict(value) for key, value in first.items()},
        {key: dict(value) for key, value in second.items()},
        dict(global_next),
    )


def _unique_page_sets(
    page_sets: Sequence[frozenset[PageId]],
) -> list[frozenset[PageId]]:
    result: list[frozenset[PageId]] = []
    seen: set[frozenset[PageId]] = set()
    for pages in page_sets:
        if pages not in seen:
            seen.add(pages)
            result.append(pages)
    return result


def _bounded_sample(values: Sequence[Any], limit: int) -> list[Any]:
    if len(values) <= limit:
        return list(values)
    return [values[min(len(values) - 1, int(index * len(values) / limit))] for index in range(limit)]


@lru_cache(maxsize=4096)
def _page_set_sort_key(pages: frozenset[PageId]) -> tuple[int, tuple[PageId, ...]]:
    """Stable medoid tie-break key, computed once for each immutable set.

    WPS windows can contain tens of thousands of pages.  K-medoids revisits
    the same immutable candidates repeatedly, so recomputing their sorted
    representation on every comparison turned a bounded pass into minutes.
    """
    return len(pages), tuple(sorted(pages))


class PageHotsetShadow:
    """Window, train, classify, predict, and audit file-page hot sets."""

    def __init__(
        self,
        *,
        session_id: str,
        model_dir: Path,
        prediction_dir: Path,
        review_dir: Path,
        window_ms: int = 1000,
        lateness_ms: int = 500,
        warmup_windows: int = 300,
        retrain_windows: int = 60,
        history_windows: int = 3600,
        base_hot_coverage: float = 0.8,
        bucket_hot_coverage: float = 0.5,
        background_training: bool = True,
        page_size: int | None = None,
        minimum_resolved_predictions: int = 30,
    ) -> None:
        if window_ms <= 0 or lateness_ms < 0:
            raise ValueError("page hotset window must be positive and lateness non-negative")
        if warmup_windows <= 0 or retrain_windows <= 0 or history_windows < warmup_windows:
            raise ValueError("invalid page hotset training window configuration")
        if not 0 < base_hot_coverage <= 1 or not 0 < bucket_hot_coverage <= 1:
            raise ValueError("page hotset coverage thresholds must be in (0, 1]")
        self.session_id = session_id
        self.model_dir = Path(model_dir)
        self.prediction_dir = Path(prediction_dir)
        self.review_dir = Path(review_dir)
        self.window_ns = int(window_ms) * 1_000_000
        self.lateness_ns = int(lateness_ms) * 1_000_000
        self.warmup_windows = int(warmup_windows)
        self.retrain_windows = int(retrain_windows)
        self.history_windows = int(history_windows)
        self.base_hot_coverage = float(base_hot_coverage)
        self.bucket_hot_coverage = float(bucket_hot_coverage)
        self.page_size = int(page_size or os.sysconf("SC_PAGE_SIZE"))
        self.minimum_resolved_predictions = int(minimum_resolved_predictions)
        self.model_versions_dir = self.model_dir / "page_hotset_models"
        self.model_versions_dir.mkdir(parents=True, exist_ok=True)
        self.prediction_dir.mkdir(parents=True, exist_ok=True)
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_file = (self.model_dir / "page_snapshots.jsonl").open("w", encoding="utf-8")
        self._prediction_file = (self.prediction_dir / "page_hotset_predictions.jsonl").open("w", encoding="utf-8")
        self._outcome_file = (self.prediction_dir / "page_hotset_outcomes.jsonl").open("w", encoding="utf-8")

        self._foreground_changes: list[tuple[int, str]] = []
        self._source_changes: list[tuple[int, bool, str]] = []
        self._window_events: dict[tuple[int, str], Counter[PageId]] = defaultdict(Counter)
        self._window_event_counts: Counter[tuple[int, str]] = Counter()
        self._window_aggregated_repeats: Counter[tuple[int, str]] = Counter()
        self._invalid_windows: dict[int, set[str]] = defaultdict(set)
        self._next_window_start_ns: int | None = None
        self._last_finalized_start_ns = -1
        self._histories: dict[str, deque[PageSnapshot]] = defaultdict(deque)
        self._history_eligible: Counter[str] = Counter()
        self._eligible_seen: Counter[str] = Counter()
        self._last_training_count: Counter[str] = Counter()
        self._training_submitted_count: Counter[str] = Counter()
        self._models: dict[str, PageBucketModel] = {}
        self._model_serial: Counter[str] = Counter()
        self._runtime: dict[str, _RuntimeState] = defaultdict(_RuntimeState)
        self._pending: dict[str, PageBucketPrediction] = {}
        self._prediction_serial = 0
        self._futures: dict[str, concurrent.futures.Future[PageBucketModel | None]] = {}
        self._background_training = bool(background_training)
        self._executor: concurrent.futures.ProcessPoolExecutor | None = (
            concurrent.futures.ProcessPoolExecutor(
                max_workers=1,
                mp_context=multiprocessing.get_context("spawn"),
            )
            if background_training else None
        )
        self._outcomes: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._stats: Counter[str] = Counter()
        self._stats_by_app: dict[str, Counter[str]] = defaultdict(Counter)
        self._closed = False

    @property
    def models(self) -> dict[str, PageBucketModel]:
        return dict(self._models)

    def observe_foreground(self, timestamp_ns: int, app_id: str) -> None:
        timestamp_ns = int(timestamp_ns)
        if timestamp_ns <= 0:
            return
        app = str(app_id or UNKNOWN).strip().upper() or UNKNOWN
        self._insert_change(self._foreground_changes, (timestamp_ns, app), value_index=1)
        self._seed_window_start(timestamp_ns)

    def set_source_available(
        self, timestamp_ns: int, available: bool, reason: str = ""
    ) -> None:
        timestamp_ns = int(timestamp_ns)
        if timestamp_ns <= 0:
            return
        self._insert_change(
            self._source_changes,
            (timestamp_ns, bool(available), str(reason)),
            value_index=1,
        )
        self._seed_window_start(timestamp_ns)

    def mark_source_gap(self, timestamp_ns: int, reason: str) -> None:
        timestamp_ns = int(timestamp_ns)
        if timestamp_ns <= 0:
            return
        start = timestamp_ns - (timestamp_ns % self.window_ns)
        self._invalid_windows[start].add(str(reason or "FILE_EVENT_SOURCE_GAP"))
        self._seed_window_start(timestamp_ns)

    def observe_page_access(self, event: dict[str, Any]) -> bool:
        if str(event.get("event", "")) != "page_access":
            return False
        timestamp_ns = int(event.get("ts_ns", 0) or event.get("timestamp_ns", 0) or 0)
        app = str(event.get("app", "")).strip().upper()
        major = int(event.get("device_major", 0) or 0)
        minor = int(event.get("device_minor", 0) or 0)
        inode = int(event.get("inode", 0) or 0)
        offset = int(event.get("offset", 0) or 0)
        size = int(event.get("size", 0) or 0)
        if timestamp_ns <= 0 or not app or inode <= 0 or offset < 0 or size <= 0:
            self._stats["rejected_page_events"] += 1
            return False
        window_start = timestamp_ns - (timestamp_ns % self.window_ns)
        if window_start <= self._last_finalized_start_ns:
            self._stats["late_page_events"] += 1
            self._invalid_windows[window_start].add("LATE_EVENT_AFTER_CLOSE")
            self._write_json(self._snapshot_file, {
                "record_type": "INVALIDATION",
                "session_id": self.session_id,
                "window_start_ns": window_start,
                "reason": "LATE_EVENT_AFTER_CLOSE",
                "event_timestamp_ns": timestamp_ns,
            })
            return False
        first_page = offset // self.page_size
        last_page = (offset + size - 1) // self.page_size
        if last_page - first_page + 1 > MAX_EVENT_PAGES:
            self._invalid_windows[window_start].add("PAGE_RANGE_TOO_LARGE")
            self._stats["rejected_page_events"] += 1
            return False
        key = (window_start, app)
        for page_index in range(first_page, last_page + 1):
            self._window_events[key][PageId(major, minor, inode, page_index)] += 1
        self._window_event_counts[key] += 1
        self._stats["accepted_page_events"] += 1
        self._seed_window_start(timestamp_ns)
        return True

    def observe_page_window(self, event: dict[str, Any]) -> bool:
        """Merge one helper-compressed page range chunk into its fixed window."""
        if str(event.get("event", "")) != "page_access_window":
            return False
        window_start = int(event.get("window_start_ns", 0) or 0)
        app = str(event.get("app", "")).strip().upper()
        page_size = int(event.get("page_size", self.page_size) or self.page_size)
        ranges = event.get("page_ranges", [])
        if (
            window_start <= 0
            or window_start % self.window_ns
            or not app
            or page_size != self.page_size
            or not isinstance(ranges, list)
        ):
            self._stats["rejected_page_windows"] += 1
            return False
        if window_start <= self._last_finalized_start_ns:
            self._stats["late_page_events"] += 1
            self._invalid_windows[window_start].add("LATE_EVENT_AFTER_CLOSE")
            return False
        key = (window_start, app)
        accepted_pages = 0
        for raw in ranges:
            if not isinstance(raw, dict):
                continue
            major = int(raw.get("device_major", 0) or 0)
            minor = int(raw.get("device_minor", 0) or 0)
            inode = int(raw.get("inode", 0) or 0)
            first_page = int(raw.get("start_page_index", -1))
            page_count = int(raw.get("page_count", 0) or 0)
            if (
                inode <= 0
                or first_page < 0
                or page_count <= 0
                or page_count > MAX_EVENT_PAGES
            ):
                self._invalid_windows[window_start].add("PAGE_RANGE_INVALID")
                continue
            for page_index in range(first_page, first_page + page_count):
                self._window_events[key][
                    PageId(major, minor, inode, page_index)
                ] += 1
                accepted_pages += 1
        self._window_event_counts[key] += int(
            event.get("page_access_events", 0) or 0
        )
        self._window_aggregated_repeats[key] += int(
            event.get("repeated_page_hits", 0) or 0
        )
        if accepted_pages:
            self._stats["accepted_page_window_chunks"] += 1
        self._seed_window_start(window_start)
        return bool(accepted_pages)

    def advance(self, now_ns: int | None = None) -> None:
        if self._closed:
            return
        now_ns = int(now_ns if now_ns is not None else time.time_ns())
        self._poll_training()
        if self._next_window_start_ns is None:
            return
        cutoff = now_ns - self.lateness_ns
        ready: list[int] = []
        cursor = self._next_window_start_ns
        while cursor + self.window_ns <= cutoff:
            ready.append(cursor)
            cursor += self.window_ns
        if not ready:
            return
        for index, window_start in enumerate(ready):
            self._finalize_window(
                window_start,
                processing_time_ns=now_ns,
                allow_prediction=index == len(ready) - 1,
            )
        self._next_window_start_ns = cursor
        self._poll_training()

    def close(self, now_ns: int | None = None) -> None:
        if self._closed:
            return
        self.advance(now_ns)
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        self._poll_training(force=True)
        for app in list(self._pending):
            self._cancel_pending(app, "SESSION_END", int(now_ns or time.time_ns()))
        self._write_summary()
        for handle in (self._snapshot_file, self._prediction_file, self._outcome_file):
            handle.flush()
            handle.close()
        self._closed = True

    def _seed_window_start(self, timestamp_ns: int) -> None:
        start = timestamp_ns - (timestamp_ns % self.window_ns)
        if self._next_window_start_ns is None:
            self._next_window_start_ns = start
        elif self._last_finalized_start_ns < 0:
            self._next_window_start_ns = min(self._next_window_start_ns, start)

    @staticmethod
    def _insert_change(changes: list[tuple[Any, ...]], row: tuple[Any, ...], *, value_index: int) -> None:
        timestamps = [int(item[0]) for item in changes]
        position = bisect.bisect_left(timestamps, int(row[0]))
        if position < len(changes) and int(changes[position][0]) == int(row[0]):
            changes[position] = row
        else:
            changes.insert(position, row)
        compacted: list[tuple[Any, ...]] = []
        for item in changes:
            if compacted and compacted[-1][value_index] == item[value_index]:
                continue
            compacted.append(item)
        changes[:] = compacted

    def _timeline_value(
        self, changes: Sequence[tuple[Any, ...]], start_ns: int, end_ns: int
    ) -> tuple[tuple[Any, ...] | None, bool]:
        timestamps = [int(item[0]) for item in changes]
        position = bisect.bisect_right(timestamps, start_ns) - 1
        if position < 0:
            return None, True
        current = changes[position]
        next_position = position + 1
        changed = bool(
            next_position < len(changes)
            and start_ns < int(changes[next_position][0]) < end_ns
        )
        return current, changed

    def _finalize_window(
        self, window_start_ns: int, *, processing_time_ns: int, allow_prediction: bool
    ) -> None:
        window_end_ns = window_start_ns + self.window_ns
        reasons = set(self._invalid_windows.pop(window_start_ns, set()))
        foreground_row, foreground_changed = self._timeline_value(
            self._foreground_changes, window_start_ns, window_end_ns
        )
        if foreground_row is None:
            app = UNKNOWN
            epoch_id = ""
            reasons.add("FOREGROUND_UNKNOWN")
        else:
            app = str(foreground_row[1])
            epoch_id = f"{app}:{int(foreground_row[0])}"
            if app == UNKNOWN:
                reasons.add("FOREGROUND_UNKNOWN")
        if foreground_changed:
            reasons.add("FOREGROUND_CHANGED")

        source_row, source_changed = self._timeline_value(
            self._source_changes, window_start_ns, window_end_ns
        )
        if source_row is None or not bool(source_row[1]):
            reasons.add(
                str(source_row[2]) if source_row is not None and source_row[2]
                else "FILE_EVENT_SOURCE_UNAVAILABLE"
            )
        if source_changed:
            reasons.add("FILE_EVENT_SOURCE_CHANGED")

        key = (window_start_ns, app)
        page_counts = self._window_events.pop(key, Counter())
        event_count = int(self._window_event_counts.pop(key, 0))
        # Events from background Apps remain counted by source diagnostics but
        # never enter a foreground page snapshot or training history.
        for stale_key in [item for item in self._window_events if item[0] == window_start_ns]:
            self._stats["background_page_events"] += self._window_event_counts.pop(stale_key, 0)
            self._window_aggregated_repeats.pop(stale_key, 0)
            self._window_events.pop(stale_key, None)
        pages = frozenset(page_counts)
        repeated = (
            sum(max(0, count - 1) for count in page_counts.values())
            + int(self._window_aggregated_repeats.pop(key, 0))
        )
        structural_reasons = {reason for reason in reasons if reason != "NO_PAGE_ACCESS"}
        valid = not structural_reasons
        if not pages:
            reasons.add("NO_PAGE_ACCESS")
        snapshot = PageSnapshot(
            session_id=self.session_id,
            app_id=app,
            foreground_epoch_id=epoch_id,
            window_start_ns=window_start_ns,
            window_end_ns=window_end_ns,
            page_size=self.page_size,
            pages=pages,
            page_access_events=event_count,
            repeated_page_hits=repeated,
            valid=valid,
            invalid_reasons=tuple(sorted(reasons)),
        )
        self._last_finalized_start_ns = window_start_ns
        self._stats["snapshots"] += 1
        self._stats_by_app[app]["snapshots"] += 1
        if not valid:
            self._stats["invalid_snapshots"] += 1
            self._stats_by_app[app]["invalid_snapshots"] += 1
        elif not pages:
            self._stats["empty_snapshots"] += 1
            self._stats_by_app[app]["empty_snapshots"] += 1

        classification = self._process_snapshot(
            snapshot,
            processing_time_ns=processing_time_ns,
            allow_prediction=allow_prediction,
        )
        self._write_json(self._snapshot_file, {**snapshot.to_record(), **classification})
        if app != UNKNOWN:
            self._append_history(snapshot)
            if snapshot.model_eligible:
                self._eligible_seen[app] += 1
                self._stats["eligible_snapshots"] += 1
                self._stats_by_app[app]["eligible_snapshots"] += 1
            self._maybe_schedule_training(app)

    def _append_history(self, snapshot: PageSnapshot) -> None:
        app = snapshot.app_id
        history = self._histories[app]
        history.append(snapshot)
        if snapshot.model_eligible:
            self._history_eligible[app] += 1
        while self._history_eligible[app] > self.history_windows and history:
            removed = history.popleft()
            if removed.model_eligible:
                self._history_eligible[app] -= 1

    def _maybe_schedule_training(self, app: str) -> None:
        if app in self._futures:
            return
        seen = int(self._eligible_seen[app])
        has_model = app in self._models
        if not has_model and seen < self.warmup_windows:
            return
        if has_model and seen - int(self._last_training_count[app]) < self.retrain_windows:
            return
        self._model_serial[app] += 1
        version = f"{_safe_name(app)}-v{self._model_serial[app]:04d}"
        snapshots = tuple(self._histories[app])
        self._training_submitted_count[app] = seen
        kwargs = {
            "app_id": app,
            "model_version": version,
            "page_size": self.page_size,
            "snapshots": snapshots,
            "base_hot_coverage": self.base_hot_coverage,
            "bucket_hot_coverage": self.bucket_hot_coverage,
        }
        self._stats["training_jobs"] += 1
        if self._background_training:
            assert self._executor is not None
            self._futures[app] = self._executor.submit(train_page_hotset_model, **kwargs)
        else:
            model = train_page_hotset_model(**kwargs)
            self._last_training_count[app] = seen
            if model is None:
                self._stats["training_not_ready"] += 1
            else:
                self._install_model(model)

    def _poll_training(self, *, force: bool = False) -> None:
        for app, future in list(self._futures.items()):
            if not force and not future.done():
                continue
            try:
                model = future.result()
            except Exception as exc:  # pragma: no cover - process failures are environment-specific
                self._stats["training_errors"] += 1
                self._write_json(self._snapshot_file, {
                    "record_type": "TRAINING_ERROR",
                    "session_id": self.session_id,
                    "app_id": app,
                    "error": str(exc),
                })
                model = None
            self._last_training_count[app] = self._training_submitted_count[app]
            self._futures.pop(app, None)
            if model is None:
                self._stats["training_not_ready"] += 1
            else:
                self._install_model(model)

    def _install_model(self, model: PageBucketModel) -> None:
        app = model.app_id
        if app in self._pending:
            self._cancel_pending(app, "MODEL_REPLACED", time.time_ns())
        self._models[app] = model
        self._runtime[app] = _RuntimeState(model_version=model.model_version)
        app_dir = self.model_versions_dir / _safe_name(app)
        app_dir.mkdir(parents=True, exist_ok=True)
        destination = app_dir / f"{model.model_version}.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(model.to_dict(), ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
        self._stats["models_installed"] += 1
        self._stats_by_app[app]["models_installed"] += 1

    def _process_snapshot(
        self,
        snapshot: PageSnapshot,
        *,
        processing_time_ns: int,
        allow_prediction: bool,
    ) -> dict[str, Any]:
        app = snapshot.app_id
        if app == UNKNOWN:
            return {"classification_status": "FOREGROUND_UNKNOWN", "bucket_id": UNKNOWN}
        if not snapshot.model_eligible:
            self._break_runtime(app, "INVALID_OR_EMPTY_WINDOW", processing_time_ns)
            return {"classification_status": "NOT_ELIGIBLE", "bucket_id": UNKNOWN}
        model = self._models.get(app)
        if model is None:
            self._break_runtime(app, "MODEL_NOT_READY", processing_time_ns)
            return {"classification_status": "MODEL_NOT_READY", "bucket_id": UNKNOWN}
        bucket, similarity, reason = model.classify(snapshot.pages)
        if bucket == UNKNOWN:
            self._stats["unknown_classifications"] += 1
            self._stats_by_app[app]["unknown_classifications"] += 1
            self._break_runtime(app, "UNKNOWN_ACTUAL", processing_time_ns)
            return {
                "classification_status": reason,
                "model_version": model.model_version,
                "bucket_id": UNKNOWN,
                "bucket_similarity": similarity,
            }

        runtime = self._runtime[app]
        if runtime.model_version != model.model_version:
            self._break_runtime(app, "MODEL_REPLACED", processing_time_ns)
            runtime = _RuntimeState(model_version=model.model_version)
            self._runtime[app] = runtime
        if runtime.foreground_epoch_id and runtime.foreground_epoch_id != snapshot.foreground_epoch_id:
            self._cancel_pending(app, "FOREGROUND_EPOCH_END", processing_time_ns)
            runtime.previous_bucket = None
            runtime.current_bucket = None
        runtime.foreground_epoch_id = snapshot.foreground_epoch_id
        if runtime.current_bucket == bucket:
            return {
                "classification_status": "SAME_BUCKET",
                "model_version": model.model_version,
                "bucket_id": bucket,
                "bucket_similarity": similarity,
                "prediction_id": "",
            }

        if runtime.current_bucket is not None:
            self._resolve_pending(app, snapshot, bucket, processing_time_ns)
            runtime.previous_bucket = runtime.current_bucket
        runtime.current_bucket = bucket
        if not allow_prediction:
            return {
                "classification_status": "BACKLOG_REPLAY",
                "model_version": model.model_version,
                "bucket_id": bucket,
                "bucket_similarity": similarity,
                "prediction_id": "",
            }
        distribution, transition_source = model.transition_distribution(
            runtime.previous_bucket, bucket
        )
        if not distribution:
            return {
                "classification_status": "NO_TRANSITION",
                "model_version": model.model_version,
                "bucket_id": bucket,
                "bucket_similarity": similarity,
                "prediction_id": "",
            }
        ranked = sorted(distribution.items(), key=lambda item: (-item[1], item[0]))
        predicted_bucket, confidence = ranked[0]
        self._prediction_serial += 1
        prediction_id = f"{self.session_id}-page-p{self._prediction_serial:06d}"
        current_hot = model.base_hot_pages | model.bucket_hot_pages(bucket)
        predicted_hot = model.base_hot_pages | model.bucket_hot_pages(predicted_bucket)
        global_bucket = model.most_common_bucket()
        global_pages = model.base_hot_pages | model.bucket_hot_pages(global_bucket)
        prediction = PageBucketPrediction(
            prediction_id=prediction_id,
            session_id=self.session_id,
            app_id=app,
            foreground_epoch_id=snapshot.foreground_epoch_id,
            model_version=model.model_version,
            model_trained_through_ns=model.trained_through_ns,
            prediction_time_ns=processing_time_ns,
            source_window_start_ns=snapshot.window_start_ns,
            source_window_end_ns=snapshot.window_end_ns,
            previous_bucket=runtime.previous_bucket or "",
            current_bucket=bucket,
            current_similarity=similarity,
            transition_source=transition_source,
            bucket_probabilities=distribution,
            predicted_bucket=predicted_bucket,
            confidence=confidence,
            base_hot_pages=model.base_hot_pages,
            current_hot_pages=current_hot,
            predicted_hot_pages=predicted_hot,
            global_baseline_pages=global_pages,
            page_size=self.page_size,
        )
        self._pending[app] = prediction
        self._write_json(self._prediction_file, prediction.to_record())
        self._stats["predictions"] += 1
        self._stats_by_app[app]["predictions"] += 1
        return {
            "classification_status": "PREDICTED",
            "model_version": model.model_version,
            "bucket_id": bucket,
            "bucket_similarity": similarity,
            "prediction_id": prediction_id,
            "predicted_bucket_id": predicted_bucket,
            "prediction_confidence": confidence,
        }

    def _break_runtime(self, app: str, reason: str, timestamp_ns: int) -> None:
        self._cancel_pending(app, reason, timestamp_ns)
        runtime = self._runtime[app]
        runtime.foreground_epoch_id = ""
        runtime.previous_bucket = None
        runtime.current_bucket = None

    def _cancel_pending(self, app: str, reason: str, timestamp_ns: int) -> None:
        prediction = self._pending.pop(app, None)
        if prediction is None:
            return
        self._write_json(self._outcome_file, {
            "schema_version": MODEL_SCHEMA_VERSION,
            "prediction_id": prediction.prediction_id,
            "session_id": self.session_id,
            "app_id": app,
            "model_version": prediction.model_version,
            "resolution_status": reason,
            "resolution_time_ns": int(timestamp_ns),
            "causal_valid": False,
        })
        self._stats["censored_predictions"] += 1
        self._stats_by_app[app]["censored_predictions"] += 1

    def _resolve_pending(
        self,
        app: str,
        actual: PageSnapshot,
        actual_bucket: str,
        resolution_time_ns: int,
    ) -> None:
        prediction = self._pending.pop(app, None)
        if prediction is None:
            return
        causal = bool(
            prediction.model_trained_through_ns <= prediction.prediction_time_ns
            and prediction.prediction_time_ns < actual.window_end_ns
            and prediction.model_version == self._models[app].model_version
        )
        metrics = _page_metrics(prediction.predicted_hot_pages, actual.pages)
        base_metrics = _page_metrics(prediction.base_hot_pages, actual.pages)
        current_metrics = _page_metrics(prediction.current_hot_pages, actual.pages)
        global_metrics = _page_metrics(prediction.global_baseline_pages, actual.pages)
        row: dict[str, Any] = {
            "schema_version": MODEL_SCHEMA_VERSION,
            "prediction_id": prediction.prediction_id,
            "session_id": self.session_id,
            "app_id": app,
            "foreground_epoch_id": actual.foreground_epoch_id,
            "model_version": prediction.model_version,
            "prediction_time_ns": prediction.prediction_time_ns,
            "actual_window_start_ns": actual.window_start_ns,
            "actual_window_end_ns": actual.window_end_ns,
            "resolution_time_ns": resolution_time_ns,
            "resolution_status": "RESOLVED" if causal else "NON_CAUSAL",
            "causal_valid": causal,
            "predicted_bucket": prediction.predicted_bucket,
            "actual_bucket": actual_bucket,
            "bucket_hit": prediction.predicted_bucket == actual_bucket,
            "lead_time_ms": (actual.window_end_ns - prediction.prediction_time_ns) / 1_000_000,
            **{f"page_{key}": value for key, value in metrics.items()},
            **{f"base_{key}": value for key, value in base_metrics.items()},
            **{f"current_{key}": value for key, value in current_metrics.items()},
            **{f"global_{key}": value for key, value in global_metrics.items()},
        }
        self._write_json(self._outcome_file, row)
        if causal:
            self._outcomes[app].append(row)
            self._stats["resolved_causal_predictions"] += 1
            self._stats_by_app[app]["resolved_causal_predictions"] += 1
            if row["bucket_hit"]:
                self._stats["bucket_hits"] += 1
                self._stats_by_app[app]["bucket_hits"] += 1
        else:
            self._stats["non_causal_predictions"] += 1

    def _write_summary(self) -> None:
        lines = [
            "# Page Hotset Shadow Summary",
            "",
            f"- session_id: `{self.session_id}`",
            "- mode: observe-only",
            f"- page_size: {self.page_size}",
            f"- window_ms: {self.window_ns // 1_000_000}",
            f"- lateness_ms: {self.lateness_ns // 1_000_000}",
            f"- warmup/retrain/history windows: {self.warmup_windows}/{self.retrain_windows}/{self.history_windows}",
            f"- base/bucket hot coverage: {self.base_hot_coverage:.3f}/{self.bucket_hot_coverage:.3f}",
            f"- snapshots: {self._stats['snapshots']}",
            f"- eligible_snapshots: {self._stats['eligible_snapshots']}",
            f"- invalid_snapshots: {self._stats['invalid_snapshots']}",
            f"- empty_snapshots: {self._stats['empty_snapshots']}",
            f"- late_page_events: {self._stats['late_page_events']}",
            f"- models_installed: {self._stats['models_installed']}",
            f"- predictions: {self._stats['predictions']}",
            f"- resolved_causal_predictions: {self._stats['resolved_causal_predictions']}",
            "",
            "## Per-App Acceptance",
            "",
            "| App | Model | K | Resolved | Bucket Top-1 | Page Recall | Page Precision | Amplification | Result |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        apps = sorted(set(self._stats_by_app) | set(self._models) | set(self._outcomes))
        for app in apps:
            rows = self._outcomes.get(app, [])
            count = len(rows)
            bucket_accuracy = _mean(rows, "bucket_hit")
            recall = _mean(rows, "page_recall")
            precision = _mean(rows, "page_precision")
            amplification = _mean(rows, "page_amplification")
            if count < self.minimum_resolved_predictions:
                result = "INSUFFICIENT_DATA"
            elif recall >= 0.8 and amplification <= 2.0:
                result = "PASS"
            else:
                result = "FAIL"
            model = self._models.get(app)
            lines.append(
                f"| {app} | {model.model_version if model else ''} | "
                f"{model.selected_k if model else ''} | {count} | {bucket_accuracy:.4f} | "
                f"{recall:.4f} | {precision:.4f} | {amplification:.4f} | {result} |"
            )
            if rows:
                lines.extend([
                    "",
                    f"### {app} baselines",
                    "- base-only recall/amplification: "
                    f"{_mean(rows, 'base_recall'):.4f}/"
                    f"{_mean(rows, 'base_amplification'):.4f}",
                    "- current-bucket recall/amplification: "
                    f"{_mean(rows, 'current_recall'):.4f}/"
                    f"{_mean(rows, 'current_amplification'):.4f}",
                    "- global-popular recall/amplification: "
                    f"{_mean(rows, 'global_recall'):.4f}/"
                    f"{_mean(rows, 'global_amplification'):.4f}",
                ])
        lines.extend([
            "",
            "## Boundaries",
            "",
            "- Only eBPF page-cache read-path page_access events are modeled.",
            "- UNKNOWN and invalid/empty windows break transition context.",
            "- Unpredicted pages are not asserted to be cold.",
            "- No kernel or memory-policy write is performed by this component.",
        ])
        (self.review_dir / "page_hotset_summary.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    @staticmethod
    def _write_json(handle: Any, row: dict[str, Any]) -> None:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def _page_metrics(predicted: frozenset[PageId], actual: frozenset[PageId]) -> dict[str, float | int]:
    intersection = len(predicted & actual)
    union = len(predicted | actual)
    return {
        "predicted_pages": len(predicted),
        "actual_pages": len(actual),
        "intersection_pages": intersection,
        "recall": intersection / len(actual) if actual else 0.0,
        "precision": intersection / len(predicted) if predicted else 0.0,
        "jaccard": intersection / union if union else 1.0,
        "amplification": len(predicted) / max(1, len(actual)),
    }


def _mean(rows: Sequence[dict[str, Any]], key: str) -> float:
    if not rows:
        return 0.0
    return sum(float(row.get(key, 0.0) or 0.0) for row in rows) / len(rows)


def _safe_name(value: str) -> str:
    cleaned = "".join(character.lower() if character.isalnum() else "-" for character in value)
    cleaned = cleaned.strip("-") or "app"
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{cleaned}-{digest}"
