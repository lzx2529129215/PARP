#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Offline pairwise next-real-access ranking for PARP effective tier.

This module deliberately has no live-kernel interfaces.  Pairwise comparison
and the Bradley--Terry sigmoid exist only in the offline dataset/training
path.  The exported runtime contract is an independent, fixed-cost per-folio
additive lookup score; it performs no candidate sorting, pair comparison, or
sigmoid evaluation.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import (DefaultDict, Dict, Iterable, Iterator, List, Mapping,
                    MutableMapping, Optional, Sequence, Tuple)

try:
    from .contracts import (
        BASE_FEATURES,
        DEFAULT_RANKING_TIE_MARGIN_NS,
        FEATURE_EDGES,
        NATIVE_TIER_FEATURE,
        RANKING_HORIZON_NS,
        RANKING_TIE_MARGINS_NS,
        TIER_IDX_FEATURE,
    )
except ImportError:  # Direct execution from this directory.
    from contracts import (  # type: ignore
        BASE_FEATURES,
        DEFAULT_RANKING_TIE_MARGIN_NS,
        FEATURE_EDGES,
        NATIVE_TIER_FEATURE,
        RANKING_HORIZON_NS,
        RANKING_TIE_MARGINS_NS,
        TIER_IDX_FEATURE,
    )


S16_MIN = -(1 << 15)
S16_MAX = (1 << 15) - 1
S32_MIN = -(1 << 31)
S32_MAX = (1 << 31) - 1
TIER_SCALE = 256

DEFAULT_MAX_PAIRS_PER_GROUP = 64
SUPPORTED_MAX_PAIRS_PER_GROUP = (32, 64, 128)
DEFAULT_APP_PAIR_CAP = 4096
DEFAULT_FALLBACK_WINDOW_NS = 100_000_000
DEFAULT_HARD_PAIR_GAP_NS = 250_000_000

SCORE_SEMANTICS = "higher_score_means_earlier_next_real_access"
MODEL_TYPE = "pairwise_linear_ranker"

RANK_ABLATIONS: Mapping[str, Tuple[str, ...]] = {
    "rank_base": tuple(BASE_FEATURES),
    "rank_plus_native_tier": tuple(BASE_FEATURES) +
    (NATIVE_TIER_FEATURE,),
    "rank_plus_native_tier_and_tier_idx": tuple(BASE_FEATURES) +
    (NATIVE_TIER_FEATURE, TIER_IDX_FEATURE),
    "recency_only_rank": ("time_since_last_real_access_ms",),
    "recent_frequency_rank": (
        "time_since_last_real_access_ms",
        "consecutive_reclaim_candidate_count",
        "access_ema_q8",
    ),
}

CandidateKey = Tuple[str, str, str]
SessionKey = Tuple[str, str]
LifetimeKey = Tuple[str, str, int]


class RankingError(ValueError):
    """An input cannot safely participate in offline ranking."""


@dataclass(frozen=True)
class RankingCandidate:
    """One folio at the native MGLRU tier gate and its offline outcome."""

    experiment_id: str
    session_id: str
    candidate_id: str
    folio_cookie: str
    folio_lifetime_epoch: int
    split: str
    app: str
    page_type: str
    candidate_time_ns: int
    batch_id: Optional[str]
    reclaim_epoch: Optional[str]
    features: Mapping[str, int]
    native_tier: int
    native_tier_idx: int
    special_native_protect: bool
    next_reuse_delay_ns: Optional[int]
    observed_within_horizon: bool
    censored_by_session_end: bool
    folio_nr_pages: int = 1
    horizon_ns: int = RANKING_HORIZON_NS
    recorded_tie_margin_ns: int = DEFAULT_RANKING_TIE_MARGIN_NS

    @property
    def key(self) -> CandidateKey:
        return (self.experiment_id, self.session_id, self.candidate_id)

    @property
    def session_key(self) -> SessionKey:
        return (self.experiment_id, self.session_id)

    @property
    def lifetime_key(self) -> LifetimeKey:
        # Folio cookies are boot/experiment scoped, not session scoped.
        return (self.experiment_id, self.folio_cookie,
                self.folio_lifetime_epoch)

    @property
    def is_horizon_censored(self) -> bool:
        return (not self.observed_within_horizon and
                not self.censored_by_session_end)


@dataclass(frozen=True)
class PairSample:
    """One sampled comparison; ``label`` applies to left minus right."""

    left_key: CandidateKey
    right_key: CandidateKey
    label: int
    earlier_key: CandidateKey
    split: str
    app: str
    page_type: str
    group_level: str
    group_key: str
    stratum: str
    tie_margin_ns: int
    horizon_ns: int

    def swapped(self) -> "PairSample":
        return PairSample(
            left_key=self.right_key,
            right_key=self.left_key,
            label=-self.label,
            earlier_key=self.earlier_key,
            split=self.split,
            app=self.app,
            page_type=self.page_type,
            group_level=self.group_level,
            group_key=self.group_key,
            stratum=self.stratum,
            tie_margin_ns=self.tie_margin_ns,
            horizon_ns=self.horizon_ns,
        )


@dataclass(frozen=True)
class RankingConfig:
    horizon_ns: int = RANKING_HORIZON_NS
    tie_margin_ns: int = DEFAULT_RANKING_TIE_MARGIN_NS
    max_pairs_per_group: int = DEFAULT_MAX_PAIRS_PER_GROUP
    app_pair_cap: int = DEFAULT_APP_PAIR_CAP
    fallback_window_ns: int = DEFAULT_FALLBACK_WINDOW_NS
    hard_pair_gap_ns: int = DEFAULT_HARD_PAIR_GAP_NS
    seed: str = "parp-rank-pairs-v1"

    def validate(self) -> None:
        if self.horizon_ns != RANKING_HORIZON_NS:
            raise RankingError("ranking mainline requires a 5s horizon")
        if self.tie_margin_ns not in RANKING_TIE_MARGINS_NS:
            raise RankingError("tie margin must be one of 0ms, 10ms, 50ms")
        if self.max_pairs_per_group not in SUPPORTED_MAX_PAIRS_PER_GROUP:
            raise RankingError(
                "max_pairs_per_group must be one of 32, 64, 128")
        if self.app_pair_cap < 1:
            raise RankingError("app_pair_cap must be positive")
        if self.fallback_window_ns < 1:
            raise RankingError("fallback_window_ns must be positive")
        if self.hard_pair_gap_ns <= self.tie_margin_ns:
            raise RankingError("hard-pair gap must exceed the tie margin")
        if not self.seed:
            raise RankingError("pair sampling seed cannot be empty")


@dataclass(frozen=True)
class FloatRankModel:
    """Offline floating-point additive scorer learned from pair differences."""

    feature_names: Tuple[str, ...]
    bin_boundaries: Tuple[Tuple[int, ...], ...]
    weights: Tuple[Tuple[float, ...], ...]
    bias: float
    horizon_ns: int
    tie_margin_ns: int
    epochs: int
    learning_rate: float
    l2: float

    @property
    def model_type(self) -> str:
        return MODEL_TYPE

    @property
    def score_semantics(self) -> str:
        return SCORE_SEMANTICS

    def score(self, candidate: RankingCandidate) -> float:
        """Independently score one folio; no pair or sigmoid is involved."""

        total = self.bias
        for name, edges, weights in zip(self.feature_names,
                                        self.bin_boundaries,
                                        self.weights):
            total += weights[bisect.bisect_left(edges,
                                                _feature(candidate, name))]
        return total


@dataclass(frozen=True)
class QuantizedRankModel:
    """Kernel-shaped s16 lookup tables with a checked s32 accumulator."""

    feature_names: Tuple[str, ...]
    bin_boundaries: Tuple[Tuple[int, ...], ...]
    weights: Tuple[Tuple[int, ...], ...]
    bias: int
    weight_scale: int
    horizon_ns: int
    tie_margin_ns: int

    @property
    def model_type(self) -> str:
        return MODEL_TYPE

    @property
    def score_semantics(self) -> str:
        return SCORE_SEMANTICS

    def score(self, candidate: RankingCandidate) -> int:
        total = checked_s32_add(0, self.bias)
        for name, edges, weights in zip(self.feature_names,
                                        self.bin_boundaries,
                                        self.weights):
            selected = weights[bisect.bisect_left(
                edges, _feature(candidate, name))]
            total = checked_s32_add(total, selected)
        return total


def candidate_from_labeled(row: Mapping[str, object]) -> RankingCandidate:
    """Convert one collector-produced labeled row without changing semantics."""

    required = (
        "experiment_id", "session_id", "folio_cookie",
        "folio_lifetime_epoch", "timestamp_ns", "source_seq", "split",
        "app", "page_type", "features", "native_tier",
        "native_tier_idx", "special_native_protect", "batch_id",
        "reclaim_epoch", "next_reuse_delay_ns", "observed_within_horizon",
        "censored_by_session_end", "folio_nr_pages", "horizon_ns",
        "tie_margin_ns",
    )
    missing = [name for name in required if name not in row]
    if missing:
        raise RankingError("labeled candidate missing: %s" %
                           ", ".join(sorted(missing)))
    raw_features = row["features"]
    if not isinstance(raw_features, Mapping):
        raise RankingError("candidate features must be an object")
    features: Dict[str, int] = {}
    for name, value in raw_features.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise RankingError("feature %s must be an integer" % name)
        features[str(name)] = value
    candidate_id = "%s:%s:%s:%s" % (
        row["folio_cookie"], row["folio_lifetime_epoch"],
        row["timestamp_ns"], row["source_seq"])
    delay = row["next_reuse_delay_ns"]
    if delay is not None and (isinstance(delay, bool) or
                              not isinstance(delay, int)):
        raise RankingError("next_reuse_delay_ns must be integer or null")
    return RankingCandidate(
        experiment_id=str(row["experiment_id"]),
        session_id=str(row["session_id"]),
        candidate_id=candidate_id,
        folio_cookie=str(row["folio_cookie"]),
        folio_lifetime_epoch=int(row["folio_lifetime_epoch"]),
        split=str(row["split"]),
        app=str(row["app"]),
        page_type=str(row["page_type"]),
        candidate_time_ns=int(row["timestamp_ns"]),
        batch_id=str(row["batch_id"]) if row["batch_id"] is not None else None,
        reclaim_epoch=(str(row["reclaim_epoch"])
                       if row["reclaim_epoch"] is not None else None),
        features=features,
        native_tier=int(row["native_tier"]),
        native_tier_idx=int(row["native_tier_idx"]),
        special_native_protect=bool(row["special_native_protect"]),
        next_reuse_delay_ns=delay,
        observed_within_horizon=bool(row["observed_within_horizon"]),
        censored_by_session_end=bool(row["censored_by_session_end"]),
        folio_nr_pages=int(row["folio_nr_pages"]),
        horizon_ns=int(row["horizon_ns"]),
        recorded_tie_margin_ns=int(row["tie_margin_ns"]),
    )


def validate_session_splits(candidates: Sequence[RankingCandidate]) -> None:
    """Reject session or folio-lifetime leakage before constructing pairs."""

    split_by_session: Dict[SessionKey, str] = {}
    split_by_lifetime: Dict[LifetimeKey, str] = {}
    app_by_session: Dict[SessionKey, str] = {}
    keys = set()
    for candidate in candidates:
        _validate_candidate(candidate)
        if candidate.key in keys:
            raise RankingError("duplicate ranking candidate key")
        keys.add(candidate.key)
        old = split_by_session.setdefault(candidate.session_key,
                                          candidate.split)
        if old != candidate.split:
            raise RankingError("session split leakage")
        old_app = app_by_session.setdefault(candidate.session_key,
                                            candidate.app)
        if old_app != candidate.app:
            raise RankingError("one session cannot have multiple Apps")
        old = split_by_lifetime.setdefault(candidate.lifetime_key,
                                           candidate.split)
        if old != candidate.split:
            raise RankingError("folio lifetime crosses session splits")


def _validate_candidate(candidate: RankingCandidate) -> None:
    if candidate.split not in ("train", "validation", "test"):
        raise RankingError("invalid session split")
    if candidate.page_type not in ("anon", "file"):
        raise RankingError("page_type must be anon or file")
    if candidate.candidate_time_ns < 0:
        raise RankingError("candidate_time_ns cannot be negative")
    if (isinstance(candidate.folio_nr_pages, bool) or
            not isinstance(candidate.folio_nr_pages, int) or
            candidate.folio_nr_pages < 1):
        raise RankingError("folio_nr_pages must be a positive integer")
    if not 0 <= candidate.native_tier <= 3 or not 0 <= candidate.native_tier_idx <= 3:
        raise RankingError("native tier fields must be in [0, 3]")
    if candidate.horizon_ns != RANKING_HORIZON_NS:
        raise RankingError("candidate horizon must be 5s")
    if candidate.recorded_tie_margin_ns not in RANKING_TIE_MARGINS_NS:
        raise RankingError("candidate records an unsupported tie margin")
    if candidate.observed_within_horizon:
        delay = candidate.next_reuse_delay_ns
        if delay is None or not 0 < delay <= candidate.horizon_ns:
            raise RankingError("observed reuse requires an in-horizon delay")
        if candidate.censored_by_session_end:
            raise RankingError("observed reuse cannot be session-end censored")
    elif candidate.next_reuse_delay_ns is not None:
        raise RankingError("unobserved reuse delay must be null")


def _feature(candidate: RankingCandidate, name: str) -> int:
    if name == NATIVE_TIER_FEATURE:
        return candidate.native_tier
    if name == TIER_IDX_FEATURE:
        return candidate.native_tier_idx
    try:
        value = candidate.features[name]
    except KeyError as exc:
        raise RankingError("missing feature %s" % name) from exc
    if isinstance(value, bool) or not isinstance(value, int):
        raise RankingError("feature %s must be an integer" % name)
    return value


def _choose_two(value: int) -> int:
    return value * (value - 1) // 2


def _stable_digest(*parts: object) -> bytes:
    payload = "\x00".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(payload).digest()


def _stable_group_text(parts: Sequence[object]) -> str:
    return "/".join(str(part) for part in parts)


def _unique_lifetimes(candidates: Iterable[RankingCandidate]) -> Tuple[
        List[RankingCandidate], int]:
    result = []
    seen = set()
    skipped = 0
    for candidate in sorted(candidates,
                            key=lambda item: (item.candidate_time_ns,
                                              item.key)):
        if candidate.lifetime_key in seen:
            skipped += 1
            continue
        seen.add(candidate.lifetime_key)
        result.append(candidate)
    return result, skipped


class _PairSpace:
    """O(N log N) index over valid pairs, with O(log N) unranking."""

    def __init__(self, candidates: Sequence[RankingCandidate],
                 tie_margin_ns: int, hard_pair_gap_ns: int):
        self.observed = sorted(
            (candidate for candidate in candidates
             if candidate.observed_within_horizon),
            key=lambda item: (int(item.next_reuse_delay_ns), item.key))
        self.horizon_censored = sorted(
            (candidate for candidate in candidates
             if candidate.is_horizon_censored), key=lambda item: item.key)
        self.session_censored = sorted(
            (candidate for candidate in candidates
             if candidate.censored_by_session_end), key=lambda item: item.key)
        self.tie_margin_ns = tie_margin_ns
        self.hard_pair_gap_ns = hard_pair_gap_ns
        delays = [int(item.next_reuse_delay_ns) for item in self.observed]

        near_counts: List[int] = []
        far_counts: List[int] = []
        self._near_start: List[int] = []
        self._far_start: List[int] = []
        self.tie_pairs = 0
        for index, delay in enumerate(delays):
            tie_end = bisect.bisect_right(delays, delay + tie_margin_ns,
                                          index + 1)
            near_end = bisect.bisect_right(delays,
                                           delay + hard_pair_gap_ns,
                                           tie_end)
            self.tie_pairs += tie_end - index - 1
            self._near_start.append(tie_end)
            self._far_start.append(near_end)
            near_counts.append(near_end - tie_end)
            far_counts.append(len(delays) - near_end)
        self._near_prefix = _prefix(near_counts)
        self._far_prefix = _prefix(far_counts)
        self.strata_counts = {
            "hard_observed": sum(near_counts),
            "wide_observed": sum(far_counts),
            "observed_vs_horizon_censored": (
                len(self.observed) * len(self.horizon_censored)),
        }
        self.double_censored_pairs = _choose_two(len(self.horizon_censored))
        session_count = len(self.session_censored)
        other_count = len(self.observed) + len(self.horizon_censored)
        self.session_end_censored_pairs = (
            _choose_two(session_count) + session_count * other_count)

    @property
    def available_pairs(self) -> int:
        return sum(self.strata_counts.values())

    def pair_at(self, stratum: str, rank: int) -> Tuple[
            RankingCandidate, RankingCandidate]:
        count = self.strata_counts[stratum]
        if not 0 <= rank < count:
            raise IndexError("pair rank outside stratum")
        if stratum == "observed_vs_horizon_censored":
            censored_count = len(self.horizon_censored)
            earlier = self.observed[rank // censored_count]
            later = self.horizon_censored[rank % censored_count]
            return earlier, later
        if stratum == "hard_observed":
            row, offset = _unrank_prefix(self._near_prefix, rank)
            return self.observed[row], self.observed[
                self._near_start[row] + offset]
        if stratum == "wide_observed":
            row, offset = _unrank_prefix(self._far_prefix, rank)
            return self.observed[row], self.observed[
                self._far_start[row] + offset]
        raise KeyError("unknown pair stratum")


def _prefix(counts: Sequence[int]) -> List[int]:
    total = 0
    result = []
    for count in counts:
        total += count
        result.append(total)
    return result


def _unrank_prefix(prefix: Sequence[int], rank: int) -> Tuple[int, int]:
    row = bisect.bisect_right(prefix, rank)
    previous = prefix[row - 1] if row else 0
    return row, rank - previous


@dataclass(frozen=True)
class _GroupSpace:
    level: str
    key_parts: Tuple[object, ...]
    split: str
    app: str
    page_type: str
    candidates: Tuple[RankingCandidate, ...]
    space: _PairSpace
    duplicate_lifetimes_skipped: int

    @property
    def key_text(self) -> str:
        return _stable_group_text(self.key_parts)


def _fallback_clusters(candidates: Sequence[RankingCandidate],
                       window_ns: int) -> Iterator[List[RankingCandidate]]:
    ordered = sorted(candidates,
                     key=lambda item: (item.candidate_time_ns, item.key))
    start = 0
    while start < len(ordered):
        anchor = ordered[start].candidate_time_ns
        end = start + 1
        while (end < len(ordered) and
               ordered[end].candidate_time_ns - anchor < window_ns):
            end += 1
        yield ordered[start:end]
        start = end


def _candidate_groups(candidates: Sequence[RankingCandidate],
                      config: RankingConfig) -> List[_GroupSpace]:
    primary: DefaultDict[Tuple[object, ...], List[RankingCandidate]] = (
        defaultdict(list))
    for candidate in candidates:
        if candidate.batch_id is None:
            continue
        key = (candidate.split, candidate.experiment_id,
               candidate.session_id, candidate.batch_id,
               candidate.page_type)
        primary[key].append(candidate)

    groups: List[_GroupSpace] = []
    covered_lifetimes = set()
    for key, raw in sorted(primary.items(), key=lambda item: str(item[0])):
        unique, duplicates = _unique_lifetimes(raw)
        if len(unique) < 2:
            continue
        space = _PairSpace(unique, config.tie_margin_ns,
                           config.hard_pair_gap_ns)
        # The fallback is used only when this primary context has no usable
        # ordering pair.  It may then combine insufficient batch contexts.
        if not space.available_pairs:
            continue
        groups.append(_GroupSpace(
            level="primary_batch",
            key_parts=key,
            split=unique[0].split,
            app=unique[0].app,
            page_type=unique[0].page_type,
            candidates=tuple(unique),
            space=space,
            duplicate_lifetimes_skipped=duplicates,
        ))
        covered_lifetimes.update(item.lifetime_key for item in unique)

    fallback: DefaultDict[Tuple[object, ...], List[RankingCandidate]] = (
        defaultdict(list))
    for candidate in candidates:
        if candidate.lifetime_key in covered_lifetimes:
            continue
        if candidate.reclaim_epoch is None:
            continue
        key = (candidate.split, candidate.experiment_id,
               candidate.session_id, candidate.reclaim_epoch,
               candidate.page_type)
        fallback[key].append(candidate)

    for key, raw in sorted(fallback.items(), key=lambda item: str(item[0])):
        for cluster_number, cluster in enumerate(
                _fallback_clusters(raw, config.fallback_window_ns)):
            unique, duplicates = _unique_lifetimes(cluster)
            if len(unique) < 2:
                continue
            space = _PairSpace(unique, config.tie_margin_ns,
                               config.hard_pair_gap_ns)
            groups.append(_GroupSpace(
                level="fallback_epoch_window",
                key_parts=key + (cluster_number,),
                split=unique[0].split,
                app=unique[0].app,
                page_type=unique[0].page_type,
                candidates=tuple(unique),
                space=space,
                duplicate_lifetimes_skipped=duplicates,
            ))
    return groups


def _coprime_step(total: int, digest: bytes) -> int:
    if total <= 1:
        return 1
    step = int.from_bytes(digest[8:16], "big") % total
    if step == 0:
        step = 1
    while math.gcd(step, total) != 1:
        step += 1
        if step == total:
            step = 1
    return step


def _permuted_ranks(total: int, seed_parts: Sequence[object]) -> Iterator[int]:
    """Yield a deterministic permutation without allocating ``range(total)``."""

    digest = _stable_digest(*seed_parts)
    start = int.from_bytes(digest[:8], "big") % total
    step = _coprime_step(total, digest)
    for index in range(total):
        yield (start + index * step) % total


def _sample_group(group: _GroupSpace, count: int,
                  config: RankingConfig) -> List[PairSample]:
    nonempty = [name for name in (
        "hard_observed", "wide_observed",
        "observed_vs_horizon_censored")
                if group.space.strata_counts[name]]
    iterators = {
        name: _permuted_ranks(
            group.space.strata_counts[name],
            (config.seed, group.key_text, name))
        for name in nonempty
    }
    sampled: List[PairSample] = []
    active = list(nonempty)
    while active and len(sampled) < count:
        remaining = []
        for stratum in active:
            if len(sampled) >= count:
                break
            try:
                rank = next(iterators[stratum])
            except StopIteration:
                continue
            remaining.append(stratum)
            earlier, later = group.space.pair_at(stratum, rank)
            # Alternate orientation within a group (with a seeded starting
            # side) so manifests cover both +/- labels whenever possible.
            orientation = ((_stable_digest(config.seed, group.key_text)[0] +
                            len(sampled)) & 1)
            if orientation:
                left, right, label = later, earlier, -1
            else:
                left, right, label = earlier, later, 1
            sampled.append(PairSample(
                left_key=left.key,
                right_key=right.key,
                label=label,
                earlier_key=earlier.key,
                split=group.split,
                app=group.app,
                page_type=group.page_type,
                group_level=group.level,
                group_key=group.key_text,
                stratum=stratum,
                tie_margin_ns=config.tie_margin_ns,
                horizon_ns=config.horizon_ns,
            ))
        active = remaining
    return sampled


def build_pair_dataset(
        candidates: Sequence[RankingCandidate],
        config: RankingConfig = RankingConfig(),
) -> Tuple[List[PairSample], Dict[str, object]]:
    """Build bounded pairs after session splitting, never O(N squared) rows.

    The exact number of usable pairs is computed combinatorially.  At most the
    configured cap (32, 64, or 128; default 64) ranks per group are unranked,
    and the global per-App cap is applied within each train/validation/test
    split.  Candidate-record tie margins are audit metadata only;
    ``config.tie_margin_ns`` is authoritative for this build.
    """

    config.validate()
    validate_session_splits(candidates)
    groups = _candidate_groups(candidates, config)
    groups.sort(key=lambda group: (
        0 if group.level == "primary_batch" else 1,
        _stable_digest(config.seed, group.key_text)))

    app_counts: Counter[Tuple[str, str]] = Counter()
    sampled: List[PairSample] = []
    group_details = []
    available_pairs = 0
    tie_pairs = 0
    double_censored = 0
    session_censored_pairs = 0
    duplicate_lifetimes = 0
    not_sampled_group_cap = 0
    not_sampled_app_cap = 0
    eligible_keys = set()

    for group in groups:
        space = group.space
        available_pairs += space.available_pairs
        tie_pairs += space.tie_pairs
        double_censored += space.double_censored_pairs
        session_censored_pairs += space.session_end_censored_pairs
        duplicate_lifetimes += group.duplicate_lifetimes_skipped
        eligible_keys.update(item.key for item in group.candidates)
        requested = min(config.max_pairs_per_group, space.available_pairs)
        not_sampled_group_cap += max(0, space.available_pairs - requested)
        app_key = (group.split, group.app)
        remaining = max(0, config.app_pair_cap - app_counts[app_key])
        take = min(requested, remaining)
        not_sampled_app_cap += requested - take
        selected = _sample_group(group, take, config)
        sampled.extend(selected)
        app_counts[app_key] += len(selected)
        group_details.append({
            "group_level": group.level,
            "group_key": group.key_text,
            "split": group.split,
            "app": group.app,
            "page_type": group.page_type,
            "candidate_count": len(group.candidates),
            "available_pairs": space.available_pairs,
            "sampled_pairs": len(selected),
            "tie_pairs_skipped": space.tie_pairs,
            "double_censored_pairs_skipped": space.double_censored_pairs,
            "session_end_censored_pairs_skipped":
                space.session_end_censored_pairs,
            "strata_available": dict(space.strata_counts),
            "strata_sampled": dict(Counter(pair.stratum
                                             for pair in selected)),
        })

    pair_app = Counter(pair.app for pair in sampled)
    pair_type = Counter(pair.page_type for pair in sampled)
    pair_split = Counter(pair.split for pair in sampled)
    pair_level = Counter(pair.group_level for pair in sampled)
    pair_strata = Counter(pair.stratum for pair in sampled)
    labels = Counter("positive" if pair.label == 1 else "negative"
                     for pair in sampled)
    manifest: Dict[str, object] = {
        "schema_version": 1,
        "task": "pairwise_next-reuse_ranking",
        "split_unit": "session_before_pair_construction",
        "session_split_leakage": False,
        "folio_lifetime_split_leakage": False,
        "raw_candidate_count": len(candidates),
        "eligible_candidate_count": len(eligible_keys),
        "available_pair_count": available_pairs,
        "sampled_pair_count": len(sampled),
        "tie_pairs_skipped": tie_pairs,
        "double_censored_pairs_skipped": double_censored,
        "session_end_censored_pairs_skipped": session_censored_pairs,
        "duplicate_lifetime_candidates_skipped": duplicate_lifetimes,
        "pairs_not_sampled_by_group_cap": not_sampled_group_cap,
        "pairs_not_sampled_by_app_cap": not_sampled_app_cap,
        "ranking_horizon_ns": config.horizon_ns,
        "tie_margin_ns": config.tie_margin_ns,
        "supported_tie_margins_ns": list(RANKING_TIE_MARGINS_NS),
        "tie_margin_authority": "pair_builder_config",
        "candidate_recorded_tie_margins_ns": dict(Counter(
            str(candidate.recorded_tie_margin_ns)
            for candidate in candidates)),
        "fallback_window_ns": config.fallback_window_ns,
        "max_pairs_per_group": config.max_pairs_per_group,
        "default_max_pairs_per_group": DEFAULT_MAX_PAIRS_PER_GROUP,
        "supported_pair_cap_ablations": list(
            SUPPORTED_MAX_PAIRS_PER_GROUP),
        "app_pair_cap_per_split": config.app_pair_cap,
        "seed": config.seed,
        "pairs_by_app": dict(pair_app),
        "pairs_by_page_type": dict(pair_type),
        "pairs_by_split": dict(pair_split),
        "pairs_by_group_level": dict(pair_level),
        "pairs_by_stratum": dict(pair_strata),
        "label_direction_counts": {
            "positive": labels["positive"],
            "negative": labels["negative"],
        },
        "group_count": len(groups),
        "groups": group_details,
        "sampling_complexity": "O(candidates_log_candidates + sampled_pairs)",
        "all_pairs_materialized": False,
    }
    return sampled, manifest


def candidate_index(candidates: Sequence[RankingCandidate]) -> Dict[
        CandidateKey, RankingCandidate]:
    result: Dict[CandidateKey, RankingCandidate] = {}
    for candidate in candidates:
        if candidate.key in result:
            raise RankingError("duplicate candidate key")
        result[candidate.key] = candidate
    return result


def _model_edges(feature_names: Sequence[str],
                 bin_boundaries: Optional[Mapping[str, Sequence[int]]]
                 ) -> Tuple[Tuple[int, ...], ...]:
    source = bin_boundaries or FEATURE_EDGES
    rows = []
    for name in feature_names:
        if name not in source:
            raise RankingError("no bin boundaries for feature %s" % name)
        edges = tuple(int(value) for value in source[name])
        if any(left >= right for left, right in zip(edges, edges[1:])):
            raise RankingError("feature bin boundaries must increase")
        rows.append(edges)
    return tuple(rows)


def _training_derivative(label_times_margin: float) -> float:
    """Stable ``1 / (1 + exp(y * margin))`` for offline loss only."""

    if label_times_margin >= 0.0:
        value = math.exp(-label_times_margin)
        return value / (1.0 + value)
    value = math.exp(label_times_margin)
    return 1.0 / (1.0 + value)


def fit_pairwise_ranker(
        candidates: Sequence[RankingCandidate],
        pairs: Sequence[PairSample],
        feature_names: Sequence[str] = RANK_ABLATIONS["rank_base"],
        bin_boundaries: Optional[Mapping[str, Sequence[int]]] = None,
        *, epochs: int = 400, learning_rate: float = 0.5,
        l2: float = 1e-4, required_split: str = "train",
) -> FloatRankModel:
    """Fit a deterministic Bradley--Terry scorer over one-hot bin diffs.

    Pair differencing makes an intercept unidentifiable, so the single-folio
    bias is deliberately fixed to zero.  Per-feature lookup rows are centered
    after every full-batch gradient step to remove their additive ambiguity.
    """

    if not pairs:
        raise RankingError("pairwise training requires at least one pair")
    if epochs < 1 or learning_rate <= 0.0 or l2 < 0.0:
        raise RankingError("invalid optimizer configuration")
    names = tuple(str(name) for name in feature_names)
    if not names or len(set(names)) != len(names):
        raise RankingError("feature names must be nonempty and unique")
    allowed_feature_sets = frozenset(RANK_ABLATIONS.values())
    if names not in allowed_feature_sets:
        raise RankingError(
            "feature set must exactly match one declared ranking ablation")
    edges = _model_edges(names, bin_boundaries)
    lookup = candidate_index(candidates)
    weights = [[0.0] * (len(row) + 1) for row in edges]

    encoded = []
    for pair in pairs:
        if pair.split != required_split:
            raise RankingError("trainer may consume only %s pairs" %
                               required_split)
        try:
            left = lookup[pair.left_key]
            right = lookup[pair.right_key]
        except KeyError as exc:
            raise RankingError("pair references an unknown candidate") from exc
        if pair.label not in (-1, 1):
            raise RankingError("pair label must be -1 or +1")
        if left.key == right.key or left.lifetime_key == right.lifetime_key:
            raise RankingError("a folio lifetime cannot rank against itself")
        if (left.split != required_split or right.split != required_split or
                left.split != pair.split or right.split != pair.split):
            raise RankingError("pair endpoint split disagrees with pair")
        if (left.session_key != right.session_key or
                left.page_type != right.page_type):
            raise RankingError("pair endpoints do not share decision context")
        left_bins = tuple(bisect.bisect_left(row, _feature(left, name))
                          for name, row in zip(names, edges))
        right_bins = tuple(bisect.bisect_left(row, _feature(right, name))
                           for name, row in zip(names, edges))
        encoded.append((pair.label, left_bins, right_bins))

    for _epoch in range(epochs):
        gradient = [[0.0] * len(row) for row in weights]
        for label, left_bins, right_bins in encoded:
            margin = sum(weights[index][left] - weights[index][right]
                         for index, (left, right) in enumerate(
                             zip(left_bins, right_bins)))
            factor = -label * _training_derivative(label * margin)
            for index, (left, right) in enumerate(zip(left_bins, right_bins)):
                gradient[index][left] += factor
                gradient[index][right] -= factor
        normalizer = float(len(encoded))
        for index, row in enumerate(weights):
            for bin_index in range(len(row)):
                row[bin_index] -= learning_rate * (
                    gradient[index][bin_index] / normalizer +
                    l2 * row[bin_index])
            centre = sum(row) / len(row)
            for bin_index in range(len(row)):
                row[bin_index] -= centre

    tie_margins = {pair.tie_margin_ns for pair in pairs}
    horizons = {pair.horizon_ns for pair in pairs}
    if len(tie_margins) != 1 or len(horizons) != 1:
        raise RankingError("training pairs disagree on horizon/tie margin")
    return FloatRankModel(
        feature_names=names,
        bin_boundaries=edges,
        weights=tuple(tuple(value for value in row) for row in weights),
        bias=0.0,
        horizon_ns=next(iter(horizons)),
        tie_margin_ns=next(iter(tie_margins)),
        epochs=epochs,
        learning_rate=learning_rate,
        l2=l2,
    )


def _round_away_from_zero(value: float) -> int:
    if value >= 0.0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def quantize_ranker(model: FloatRankModel,
                    preferred_scale: int = 4096) -> QuantizedRankModel:
    """Quantize lookup weights and bias to s16 without silent clipping."""

    if preferred_scale < 1:
        raise RankingError("preferred quantization scale must be positive")
    maximum = max([abs(model.bias)] +
                  [abs(value) for row in model.weights for value in row])
    if maximum == 0.0:
        scale = 1
    else:
        scale = min(preferred_scale, int(S16_MAX // maximum))
        if scale < 1:
            raise RankingError("float weights cannot be represented as s16")
    weights = tuple(tuple(_round_away_from_zero(value * scale)
                          for value in row) for row in model.weights)
    bias = _round_away_from_zero(model.bias * scale)
    scalars = [bias] + [value for row in weights for value in row]
    if any(value < S16_MIN or value > S16_MAX for value in scalars):
        raise RankingError("quantized lookup scalar outside s16")
    minimum = bias + sum(min(row) for row in weights)
    maximum_score = bias + sum(max(row) for row in weights)
    if minimum < S32_MIN or maximum_score > S32_MAX:
        raise RankingError("quantized score range cannot fit s32")
    return QuantizedRankModel(
        feature_names=model.feature_names,
        bin_boundaries=model.bin_boundaries,
        weights=weights,
        bias=bias,
        weight_scale=scale,
        horizon_ns=model.horizon_ns,
        tie_margin_ns=model.tie_margin_ns,
    )


def checked_s32_add(left: int, right: int) -> int:
    result = int(left) + int(right)
    if result < S32_MIN or result > S32_MAX:
        raise OverflowError("rank score accumulator overflow")
    return result


def rank_score_to_delta_q8(score: int, cold_threshold: int,
                           hot_threshold_1: int, hot_threshold_2: int,
                           *, hot_threshold_3: Optional[int] = None,
                           experimental_plus3: bool = False) -> int:
    """Map a non-probabilistic integer rank score to virtual Q8 tiers."""

    if not cold_threshold < hot_threshold_1 < hot_threshold_2:
        raise RankingError("rank score thresholds must strictly increase")
    if hot_threshold_3 is not None and hot_threshold_3 <= hot_threshold_2:
        raise RankingError("experimental +3 threshold must be above +2")
    if score <= cold_threshold:
        return -TIER_SCALE
    if (experimental_plus3 and hot_threshold_3 is not None and
            score >= hot_threshold_3):
        return 3 * TIER_SCALE
    if score >= hot_threshold_2:
        return 2 * TIER_SCALE
    if score >= hot_threshold_1:
        return TIER_SCALE
    return 0


def _score_value(scores: Mapping[CandidateKey, float],
                 key: CandidateKey) -> float:
    try:
        return float(scores[key])
    except KeyError as exc:
        raise RankingError("missing candidate score") from exc


def pairwise_accuracy(pairs: Sequence[PairSample],
                      scores: Mapping[CandidateKey, float]) -> Dict[str, object]:
    correct = 0
    tied = 0
    for pair in pairs:
        margin = (_score_value(scores, pair.left_key) -
                  _score_value(scores, pair.right_key))
        if margin == 0.0:
            tied += 1
        elif (margin > 0.0) == (pair.label > 0):
            correct += 1
    count = len(pairs)
    return {
        "pairs": count,
        "correct": correct,
        "score_ties": tied,
        "pairwise_accuracy": ((correct + 0.5 * tied) / count
                              if count else None),
    }


def concordance_index(pairs: Sequence[PairSample],
                      scores: Mapping[CandidateKey, float]) -> Optional[float]:
    return pairwise_accuracy(pairs, scores)["pairwise_accuracy"]  # type: ignore[return-value]


def _relevance(candidate: RankingCandidate) -> Optional[float]:
    if candidate.censored_by_session_end:
        return None
    if candidate.is_horizon_censored:
        return 0.0
    assert candidate.next_reuse_delay_ns is not None
    return max(0.0, (candidate.horizon_ns - candidate.next_reuse_delay_ns) /
               candidate.horizon_ns)


def ndcg_at_k(candidates: Sequence[RankingCandidate],
              scores: Mapping[CandidateKey, float], k: int) -> Optional[float]:
    if k < 1:
        raise RankingError("NDCG K must be positive")
    usable = [(candidate, _relevance(candidate)) for candidate in candidates]
    usable = [(candidate, relevance) for candidate, relevance in usable
              if relevance is not None]
    if not usable:
        return None
    predicted = sorted(usable,
                       key=lambda item: (-_score_value(scores, item[0].key),
                                         item[0].key))[:k]
    ideal = sorted(usable, key=lambda item: (-float(item[1]),
                                             item[0].key))[:k]

    def dcg(rows: Sequence[Tuple[RankingCandidate, Optional[float]]]) -> float:
        return sum(float(relevance) / math.log2(index + 2.0)
                   for index, (_candidate, relevance) in enumerate(rows))

    denominator = dcg(ideal)
    return dcg(predicted) / denominator if denominator else None


def grouped_ndcg_at_k(candidates: Sequence[RankingCandidate],
                      pairs: Sequence[PairSample],
                      scores: Mapping[CandidateKey, float],
                      k: int) -> Optional[float]:
    """Average NDCG inside sampled reclaim contexts, never across sessions."""

    lookup = candidate_index(candidates)
    group_keys: DefaultDict[Tuple[str, str], set] = defaultdict(set)
    for pair in pairs:
        key = (pair.group_level, pair.group_key)
        group_keys[key].add(pair.left_key)
        group_keys[key].add(pair.right_key)
    weighted = 0.0
    weight_total = 0
    for keys in group_keys.values():
        rows = [lookup[key] for key in sorted(keys)]
        value = ndcg_at_k(rows, scores, k)
        if value is None:
            continue
        weight = min(k, len(rows))
        weighted += value * weight
        weight_total += weight
    return weighted / weight_total if weight_total else None


def _average_ranks(values: Sequence[float]) -> List[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + end - 1) / 2.0
        for index in order[start:end]:
            ranks[index] = rank
        start = end
    return ranks


def spearman_rank_correlation(candidates: Sequence[RankingCandidate],
                              scores: Mapping[CandidateKey, float]
                              ) -> Optional[float]:
    observed = [candidate for candidate in candidates
                if candidate.observed_within_horizon]
    if len(observed) < 2:
        return None
    score_ranks = _average_ranks([_score_value(scores, item.key)
                                  for item in observed])
    # Higher target means earlier reuse, matching the score direction.
    target_ranks = _average_ranks([-float(item.next_reuse_delay_ns)
                                   for item in observed])
    score_mean = statistics.fmean(score_ranks)
    target_mean = statistics.fmean(target_ranks)
    covariance = sum((left - score_mean) * (right - target_mean)
                     for left, right in zip(score_ranks, target_ranks))
    score_var = sum((value - score_mean) ** 2 for value in score_ranks)
    target_var = sum((value - target_mean) ** 2 for value in target_ranks)
    denominator = math.sqrt(score_var * target_var)
    return covariance / denominator if denominator else None


def _base_page_count(candidates: Sequence[RankingCandidate]) -> int:
    return sum(candidate.folio_nr_pages for candidate in candidates)


def _weighted_reuse_rate(
        candidates: Sequence[RankingCandidate], *,
        window_ns: Optional[int] = None) -> Optional[float]:
    usable = [candidate for candidate in candidates
              if not candidate.censored_by_session_end]
    total_pages = _base_page_count(usable)
    if not total_pages:
        return None
    reused_pages = sum(
        candidate.folio_nr_pages for candidate in usable
        if candidate.observed_within_horizon and
        (window_ns is None or
         int(candidate.next_reuse_delay_ns) <= window_ns))
    return reused_pages / total_pages


def _weighted_observed_median(
        candidates: Sequence[RankingCandidate]) -> Optional[int]:
    observed = sorted(
        (int(candidate.next_reuse_delay_ns), candidate.folio_nr_pages)
        for candidate in candidates if candidate.observed_within_horizon)
    total_pages = sum(pages for _delay, pages in observed)
    if not total_pages:
        return None
    # Match statistics.median for integer weights without expanding a large
    # folio into one Python object per base page.
    lower_rank = (total_pages - 1) // 2
    upper_rank = total_pages // 2
    cumulative = 0
    lower_value: Optional[int] = None
    upper_value: Optional[int] = None
    for delay, pages in observed:
        next_cumulative = cumulative + pages
        if lower_value is None and lower_rank < next_cumulative:
            lower_value = delay
        if upper_rank < next_cumulative:
            upper_value = delay
            break
        cumulative = next_cumulative
    assert lower_value is not None and upper_value is not None
    return int((lower_value + upper_value) / 2)


def _kaplan_meier_median(candidates: Sequence[RankingCandidate]) -> Optional[int]:
    usable = [candidate for candidate in candidates
              if not candidate.censored_by_session_end]
    if not usable:
        return None
    timeline: DefaultDict[int, List[int]] = defaultdict(lambda: [0, 0])
    for candidate in usable:
        if candidate.observed_within_horizon:
            assert candidate.next_reuse_delay_ns is not None
            timeline[candidate.next_reuse_delay_ns][0] += \
                candidate.folio_nr_pages
        else:
            timeline[candidate.horizon_ns][1] += candidate.folio_nr_pages
    at_risk = _base_page_count(usable)
    survival = 1.0
    for timestamp in sorted(timeline):
        events, censored = timeline[timestamp]
        if events:
            survival *= 1.0 - events / at_risk
            if survival <= 0.5:
                return timestamp
        at_risk -= events + censored
    return None


def score_bucket_monotonicity(
        candidates: Sequence[RankingCandidate],
        scores: Mapping[CandidateKey, float], num_buckets: int = 5,
) -> Dict[str, object]:
    if num_buckets < 2:
        raise RankingError("at least two score buckets are required")
    usable = [candidate for candidate in candidates
              if not candidate.censored_by_session_end]
    usable.sort(key=lambda item: (_score_value(scores, item.key), item.key))
    if not usable:
        return {
            "status": "INSUFFICIENT_SCORE_BUCKETS",
            "score_direction": SCORE_SEMANTICS,
            "distinct_score_count": 0,
            "bucket_count": 0,
            "required_adjacent_comparisons": 1,
            "known_adjacent_median_comparisons": 0,
            "known_adjacent_rate_comparisons": 0,
            "evidence_sufficient": False,
            "buckets_low_to_high_score": [],
            "median_delay_nonincreasing": False,
            "reuse_rates_nondecreasing": False,
            "monotonicity_pass": False,
        }
    # Never split identical scores across buckets: the model cannot order
    # candidates that have exactly the same score.
    score_groups: List[List[RankingCandidate]] = []
    for row in usable:
        if (not score_groups or
                _score_value(scores, score_groups[-1][0].key) !=
                _score_value(scores, row.key)):
            score_groups.append([])
        score_groups[-1].append(row)
    bucket_count = min(num_buckets, len(score_groups))
    buckets = []
    for bucket in range(bucket_count):
        start = bucket * len(score_groups) // bucket_count
        end = (bucket + 1) * len(score_groups) // bucket_count
        rows = [row for group in score_groups[start:end] for row in group]
        rates = {}
        for label, window in (("100ms", 100_000_000),
                              ("500ms", 500_000_000),
                              ("1s", 1_000_000_000),
                              ("5s", 5_000_000_000)):
            rates[label] = _weighted_reuse_rate(rows, window_ns=window)
        buckets.append({
            "score_min": _score_value(scores, rows[0].key),
            "score_max": _score_value(scores, rows[-1].key),
            "count": len(rows),
            "base_pages": _base_page_count(rows),
            "median_next_reuse_delay_ns": _kaplan_meier_median(rows),
            "reuse_rate": rates,
        })
    adjacent = list(zip(buckets, buckets[1:]))
    known_median_comparisons = sum(
        left["median_next_reuse_delay_ns"] is not None and
        right["median_next_reuse_delay_ns"] is not None
        for left, right in adjacent)
    known_rate_comparisons = sum(
        all(left["reuse_rate"][window] is not None and
            right["reuse_rate"][window] is not None
            for window in ("100ms", "500ms", "1s", "5s"))
        for left, right in adjacent)
    evidence_sufficient = bool(
        len(score_groups) >= 2 and len(adjacent) >= 1 and
        known_median_comparisons == len(adjacent) and
        known_rate_comparisons == len(adjacent))
    median_direction = bool(
        evidence_sufficient and
        all(int(left["median_next_reuse_delay_ns"]) >=
            int(right["median_next_reuse_delay_ns"])
            for left, right in adjacent))
    rate_direction = bool(
        evidence_sufficient and all(
            all(float(left["reuse_rate"][window]) <=
                float(right["reuse_rate"][window])
                for left, right in adjacent)
            for window in ("100ms", "500ms", "1s", "5s")))
    return {
        "status": "COMPLETE" if evidence_sufficient else
                  "INSUFFICIENT_MONOTONICITY_COMPARISONS",
        "score_direction": SCORE_SEMANTICS,
        "distinct_score_count": len(score_groups),
        "bucket_count": len(buckets),
        "required_adjacent_comparisons": max(1, len(buckets) - 1),
        "known_adjacent_median_comparisons": known_median_comparisons,
        "known_adjacent_rate_comparisons": known_rate_comparisons,
        "evidence_sufficient": evidence_sufficient,
        "buckets_low_to_high_score": buckets,
        "median_delay_nonincreasing": median_direction,
        "reuse_rates_nondecreasing": rate_direction,
        "monotonicity_pass": median_direction and rate_direction,
    }


def fixed_native_tier_stratification(
        candidates: Sequence[RankingCandidate], pairs: Sequence[PairSample],
        scores: Mapping[CandidateKey, float]) -> Dict[str, object]:
    lookup = candidate_index(candidates)
    result = {}
    for tier in range(4):
        rows = [candidate for candidate in candidates
                if candidate.native_tier == tier]
        tier_pairs = [pair for pair in pairs
                      if lookup[pair.left_key].native_tier == tier and
                      lookup[pair.right_key].native_tier == tier]
        result[str(tier)] = {
            "candidate_count": len(rows),
            "pairwise": pairwise_accuracy(tier_pairs, scores),
            "ndcg_at_10": ndcg_at_k(rows, scores, 10),
            "spearman": spearman_rank_correlation(rows, scores),
        }
    boundary = [candidate for candidate in candidates
                if candidate.native_tier == candidate.native_tier_idx + 1]
    boundary_observed = [candidate for candidate in boundary
                         if candidate.observed_within_horizon]
    return {
        "native_tier": result,
        "boundary_native_tier_eq_tier_idx_plus_1": {
            "candidate_count": len(boundary),
            "base_pages": _base_page_count(boundary),
            # Spearman only consumes observed delays, so its effective sample
            # size must travel with the coefficient used by deployment gates.
            "spearman_observed_candidate_count": len(boundary_observed),
            "spearman_observed_base_pages":
                _base_page_count(boundary_observed),
            "ndcg_at_10": ndcg_at_k(boundary, scores, 10),
            "spearman": spearman_rank_correlation(boundary, scores),
        },
    }


def quantized_ordering_consistency(
        pairs: Sequence[PairSample], float_scores: Mapping[CandidateKey, float],
        quantized_scores: Mapping[CandidateKey, float]) -> Dict[str, object]:
    consistent = 0
    quantized_ties = 0
    compared = 0
    for pair in pairs:
        float_margin = (_score_value(float_scores, pair.left_key) -
                        _score_value(float_scores, pair.right_key))
        quant_margin = (_score_value(quantized_scores, pair.left_key) -
                        _score_value(quantized_scores, pair.right_key))
        if float_margin == 0.0:
            continue
        compared += 1
        if quant_margin == 0.0:
            quantized_ties += 1
        elif (float_margin > 0.0) == (quant_margin > 0.0):
            consistent += 1
    return {
        "compared_non_tied_float_pairs": compared,
        "consistent_orderings": consistent,
        "quantized_ties": quantized_ties,
        "ordering_consistency": consistent / compared if compared else None,
    }


def evaluate_ranker(candidates: Sequence[RankingCandidate],
                    pairs: Sequence[PairSample],
                    scores: Mapping[CandidateKey, float],
                    *, ndcg_k: int = 10) -> Dict[str, object]:
    """Return the primary rank metrics and required stratifications."""

    by_app = {}
    for app in sorted({candidate.app for candidate in candidates}):
        rows = [candidate for candidate in candidates if candidate.app == app]
        app_pairs = [pair for pair in pairs if pair.app == app]
        by_app[app] = {
            "pairwise": pairwise_accuracy(app_pairs, scores),
            "ndcg": grouped_ndcg_at_k(rows, app_pairs, scores, ndcg_k),
            "c_index": concordance_index(app_pairs, scores),
            "spearman": spearman_rank_correlation(rows, scores),
        }
    by_type = {}
    for page_type in ("anon", "file"):
        rows = [candidate for candidate in candidates
                if candidate.page_type == page_type]
        type_pairs = [pair for pair in pairs if pair.page_type == page_type]
        by_type[page_type] = {
            "pairwise": pairwise_accuracy(type_pairs, scores),
            "ndcg": grouped_ndcg_at_k(rows, type_pairs, scores, ndcg_k),
            "c_index": concordance_index(type_pairs, scores),
            "spearman": spearman_rank_correlation(rows, scores),
        }
    by_session = {}
    for session in sorted({candidate.session_key for candidate in candidates}):
        rows = [candidate for candidate in candidates
                if candidate.session_key == session]
        session_pairs = [pair for pair in pairs
                         if pair.left_key[:2] == session and
                         pair.right_key[:2] == session]
        key = "%s/%s" % session
        by_session[key] = {
            "pairwise": pairwise_accuracy(session_pairs, scores),
            "ndcg": grouped_ndcg_at_k(rows, session_pairs, scores, ndcg_k),
            "c_index": concordance_index(session_pairs, scores),
            "spearman": spearman_rank_correlation(rows, scores),
        }
    return {
        "score_semantics": SCORE_SEMANTICS,
        "score_is_probability": False,
        "pairwise": pairwise_accuracy(pairs, scores),
        "ndcg_at_%d" % ndcg_k: grouped_ndcg_at_k(
            candidates, pairs, scores, ndcg_k),
        "c_index": concordance_index(pairs, scores),
        "spearman": spearman_rank_correlation(candidates, scores),
        "score_bucket_monotonicity": score_bucket_monotonicity(
            candidates, scores),
        "fixed_native_tier": fixed_native_tier_stratification(
            candidates, pairs, scores),
        "by_app": by_app,
        "by_page_type": by_type,
        "by_session": by_session,
    }


def _reuse_rate(rows: Sequence[RankingCandidate]) -> Optional[float]:
    return _weighted_reuse_rate(rows)


def _wilson_upper(positive: int, total: int) -> Optional[float]:
    if not total:
        return None
    z = 1.959963984540054
    p = positive / total
    denominator = 1.0 + z * z / total
    centre = (p + z * z / (2.0 * total)) / denominator
    radius = z * math.sqrt((p * (1.0 - p) + z * z /
                            (4.0 * total)) / total) / denominator
    return min(1.0, centre + radius)


def _bootstrap_percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    position = percentile * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _bootstrap_rng(tag: str, sessions: Sequence[SessionKey]) -> random.Random:
    payload = "%s\0%s" % (tag, "\0".join(
        "%s/%s" % key for key in sorted(sessions)))
    seed = int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8],
                          "big")
    return random.Random(seed)


MIN_BOOTSTRAP_VALID_RESAMPLES = 200
MIN_BOOTSTRAP_VALID_FRACTION = 0.80


def _minimum_valid_resamples(resamples: int) -> int:
    if isinstance(resamples, bool) or not isinstance(resamples, int) or \
            resamples < 1:
        raise RankingError("bootstrap resamples must be a positive integer")
    return max(MIN_BOOTSTRAP_VALID_RESAMPLES,
               int(math.ceil(resamples * MIN_BOOTSTRAP_VALID_FRACTION)))


def _session_cluster_rate_ci(
        rows: Sequence[RankingCandidate], *, tag: str,
        resamples: int = 1000) -> Dict[str, object]:
    minimum_valid = _minimum_valid_resamples(resamples)
    valid = [row for row in rows if not row.censored_by_session_end]
    by_session: DefaultDict[SessionKey, List[RankingCandidate]] = defaultdict(
        list)
    for row in valid:
        by_session[row.session_key].append(row)
    sessions = sorted(by_session)
    estimate = _reuse_rate(valid)
    if estimate is None or len(sessions) < 2:
        return {
            "status": "INSUFFICIENT_SESSIONS",
            "method": "session_cluster_bootstrap",
            "session_count": len(sessions),
            "requested_resamples": resamples,
            "minimum_valid_resamples": minimum_valid,
            "valid_resamples": 0,
            "estimate": estimate,
            "ci95": None,
            "gate_eligible": False,
        }
    rng = _bootstrap_rng(tag, sessions)
    values = []
    for _index in range(resamples):
        sampled = [sessions[rng.randrange(len(sessions))]
                   for _session in sessions]
        selected = [row for key in sampled for row in by_session[key]]
        value = _reuse_rate(selected)
        if value is not None:
            values.append(value)
    if len(values) < minimum_valid:
        return {
            "status": "INSUFFICIENT_RESAMPLES",
            "method": "session_cluster_bootstrap",
            "session_count": len(sessions),
            "requested_resamples": resamples,
            "minimum_valid_resamples": minimum_valid,
            "valid_resamples": len(values),
            "estimate": estimate,
            "ci95": None,
            "gate_eligible": False,
        }
    return {
        "status": "COMPLETE",
        "method": "session_cluster_bootstrap",
        "session_count": len(sessions),
        "requested_resamples": resamples,
        "minimum_valid_resamples": minimum_valid,
        "valid_resamples": len(values),
        "estimate": estimate,
        "ci95": [
            _bootstrap_percentile(values, 0.025),
            _bootstrap_percentile(values, 0.975),
        ],
        "gate_eligible": True,
    }


def _session_cluster_rate_difference_ci(
        selected: Sequence[RankingCandidate],
        baseline: Sequence[RankingCandidate], *, tag: str,
        expected_direction: str, resamples: int = 1000,
) -> Dict[str, object]:
    if expected_direction not in ("positive", "negative"):
        raise RankingError("invalid bootstrap direction")
    minimum_valid = _minimum_valid_resamples(resamples)
    selected_valid = [row for row in selected
                      if not row.censored_by_session_end]
    baseline_valid = [row for row in baseline
                      if not row.censored_by_session_end]
    selected_by_session: DefaultDict[
        SessionKey, List[RankingCandidate]] = defaultdict(list)
    baseline_by_session: DefaultDict[
        SessionKey, List[RankingCandidate]] = defaultdict(list)
    for row in selected_valid:
        selected_by_session[row.session_key].append(row)
    for row in baseline_valid:
        baseline_by_session[row.session_key].append(row)
    selected_sessions = set(selected_by_session)
    baseline_sessions = set(baseline_by_session)
    sessions = sorted(selected_sessions & baseline_sessions)
    paired_selected = [row for key in sessions
                       for row in selected_by_session[key]]
    paired_baseline = [row for key in sessions
                       for row in baseline_by_session[key]]
    selected_rate = _reuse_rate(paired_selected)
    baseline_rate = _reuse_rate(paired_baseline)
    estimate = (selected_rate - baseline_rate
                if selected_rate is not None and baseline_rate is not None
                else None)
    if estimate is None or len(sessions) < 2:
        return {
            "status": "INSUFFICIENT_PAIRED_SESSIONS",
            "method": "session_cluster_bootstrap",
            "expected_direction": expected_direction,
            "session_count": len(sessions),
            "paired_session_count": len(sessions),
            "selected_only_session_count": len(
                selected_sessions - baseline_sessions),
            "baseline_only_session_count": len(
                baseline_sessions - selected_sessions),
            "requested_resamples": resamples,
            "minimum_valid_resamples": minimum_valid,
            "valid_resamples": 0,
            "rate_difference": estimate,
            "ci95": None,
            "direction_gate_pass": False,
            "gate_eligible": False,
        }
    rng = _bootstrap_rng(tag, sessions)
    differences = []
    for _index in range(resamples):
        sampled = [sessions[rng.randrange(len(sessions))]
                   for _session in sessions]
        selected_rows = [row for key in sampled
                         for row in selected_by_session[key]]
        baseline_rows = [row for key in sampled
                         for row in baseline_by_session[key]]
        left = _reuse_rate(selected_rows)
        right = _reuse_rate(baseline_rows)
        if left is not None and right is not None:
            differences.append(left - right)
    if len(differences) < minimum_valid:
        return {
            "status": "INSUFFICIENT_RESAMPLES",
            "method": "session_cluster_bootstrap",
            "expected_direction": expected_direction,
            "session_count": len(sessions),
            "paired_session_count": len(sessions),
            "selected_only_session_count": len(
                selected_sessions - baseline_sessions),
            "baseline_only_session_count": len(
                baseline_sessions - selected_sessions),
            "requested_resamples": resamples,
            "minimum_valid_resamples": minimum_valid,
            "valid_resamples": len(differences),
            "rate_difference": estimate,
            "ci95": None,
            "direction_gate_pass": False,
            "gate_eligible": False,
        }
    interval = [
        _bootstrap_percentile(differences, 0.025),
        _bootstrap_percentile(differences, 0.975),
    ]
    direction_pass = (interval[0] > 0.0 if expected_direction == "positive"
                      else interval[1] < 0.0)
    return {
        "status": "COMPLETE",
        "method": "session_cluster_bootstrap",
        "expected_direction": expected_direction,
        "session_count": len(sessions),
        "paired_session_count": len(sessions),
        "selected_only_session_count": len(
            selected_sessions - baseline_sessions),
        "baseline_only_session_count": len(
            baseline_sessions - selected_sessions),
        "requested_resamples": resamples,
        "minimum_valid_resamples": minimum_valid,
        "valid_resamples": len(differences),
        "rate_difference": estimate,
        "ci95": interval,
        "direction_gate_pass": direction_pass,
        "gate_eligible": True,
    }


def _threshold_group_stats(rows: Sequence[RankingCandidate],
                           all_rows: Sequence[RankingCandidate], *,
                           tag: str) -> Dict[str, object]:
    valid = [row for row in rows if not row.censored_by_session_end]
    total_valid = [row for row in all_rows
                   if not row.censored_by_session_end]
    selected_base_pages = _base_page_count(valid)
    total_base_pages = _base_page_count(total_valid)
    by_app = {app: {"count": len(selected),
                    "candidate_count": len(selected),
                    "base_pages": _base_page_count(selected),
                    "reuse_rate": _reuse_rate(selected)}
              for app in sorted({row.app for row in valid})
              for selected in [[row for row in valid if row.app == app]]}
    by_type = {page_type: {"count": len(selected),
                           "candidate_count": len(selected),
                           "base_pages": _base_page_count(selected),
                           "reuse_rate": _reuse_rate(selected)}
               for page_type in ("anon", "file")
               for selected in [[row for row in valid
                                  if row.page_type == page_type]]}
    return {
        "count": len(valid),
        "candidate_count": len(valid),
        "base_pages": selected_base_pages,
        # Coverage is intentionally base-page weighted.  Candidate coverage is
        # retained under an explicit name for diagnostics only.
        "coverage": (selected_base_pages / total_base_pages
                     if total_base_pages else None),
        "candidate_coverage": (len(valid) / len(total_valid)
                               if total_valid else None),
        "base_page_coverage": (selected_base_pages / total_base_pages
                               if total_base_pages else None),
        "future_reuse_rate_5s": _reuse_rate(valid),
        "future_reuse_rate_5s_ci95": _session_cluster_rate_ci(
            valid, tag=tag),
        "median_observed_next_reuse_delay_ns": (
            _weighted_observed_median(valid)),
        "per_app": by_app,
        "per_page_type": by_type,
    }


def select_validation_thresholds(
        candidates: Sequence[RankingCandidate],
        scores: Mapping[CandidateKey, int], *,
        max_upgrade_coverage: float = 0.5,
        max_downgrade_mistake_ci95_upper: float = 0.10,
) -> Dict[str, object]:
    """Select integer score thresholds using validation sessions only."""

    if not candidates or any(row.split != "validation" for row in candidates):
        raise RankingError("score thresholds may use validation sessions only")
    if not 0.0 < max_upgrade_coverage <= 1.0:
        raise RankingError("max_upgrade_coverage must be in (0, 1]")
    if not 0.0 <= max_downgrade_mistake_ci95_upper <= 1.0:
        raise RankingError(
            "downgrade mistake CI upper limit must be in [0, 1]")
    unique_scores = sorted({int(_score_value(scores, row.key))
                            for row in candidates})
    if len(unique_scores) < 3:
        raise RankingError("threshold selection needs at least three scores")

    native_reclaim = [row for row in candidates
                      if not row.special_native_protect and
                      row.native_tier <= row.native_tier_idx and
                      not row.censored_by_session_end]
    native_reclaim_base_pages = _base_page_count(native_reclaim)
    hot_options = []
    for threshold in unique_scores[1:]:
        selected = [row for row in native_reclaim
                    if int(_score_value(scores, row.key)) >= threshold]
        baseline = [row for row in native_reclaim
                    if int(_score_value(scores, row.key)) < threshold]
        if not selected or not baseline:
            continue
        coverage = (_base_page_count(selected) /
                    native_reclaim_base_pages)
        selected_rate = _reuse_rate(selected)
        baseline_rate = _reuse_rate(baseline)
        if (coverage <= max_upgrade_coverage and
                selected_rate is not None and baseline_rate is not None and
                selected_rate > baseline_rate):
            hot_options.append((selected_rate - baseline_rate,
                                _base_page_count(selected), -threshold,
                                threshold,
                                selected, baseline))
    hot_options.sort(reverse=True, key=lambda item: item[:3])
    hot_choice = hot_options[0] if hot_options else None
    hot_2_choice = None
    hot_pairs = []
    for hot_1_option in hot_options:
        hot_1_threshold = hot_1_option[3]
        hot_1_rate = _reuse_rate(hot_1_option[4])
        hot_1_coverage = (_base_page_count(hot_1_option[4]) /
                          native_reclaim_base_pages)
        for threshold in unique_scores:
            if threshold <= hot_1_threshold:
                continue
            selected = [row for row in native_reclaim
                        if int(_score_value(scores, row.key)) >= threshold]
            baseline = [row for row in native_reclaim
                        if int(_score_value(scores, row.key)) < threshold]
            selected_rate = _reuse_rate(selected)
            baseline_rate = _reuse_rate(baseline)
            coverage = (_base_page_count(selected) /
                        native_reclaim_base_pages
                        if native_reclaim_base_pages else 1.0)
            if (selected and baseline and hot_1_rate is not None and
                    selected_rate is not None and baseline_rate is not None and
                    coverage < hot_1_coverage and
                    coverage <= max_upgrade_coverage / 2.0 and
                    selected_rate >= hot_1_rate and
                    selected_rate > baseline_rate):
                hot_pairs.append((
                    hot_1_option[0], selected_rate - baseline_rate,
                    _base_page_count(selected), -hot_1_threshold, -threshold,
                    hot_1_option, (threshold, selected, baseline,
                                   selected_rate, coverage)))
    hot_pairs.sort(reverse=True, key=lambda item: item[:5])
    if hot_pairs:
        hot_choice = hot_pairs[0][5]
        hot_2_choice = hot_pairs[0][6]
    hot_1 = hot_choice[3] if hot_choice else unique_scores[-1]
    hot_2 = hot_2_choice[0] if hot_2_choice else hot_1 + 1

    boundary = [row for row in candidates
                if not row.special_native_protect and
                row.native_tier == row.native_tier_idx + 1 and
                not row.censored_by_session_end]
    cold_options = []
    for threshold in unique_scores[:-1]:
        if threshold >= hot_1:
            continue
        selected = [row for row in boundary
                    if int(_score_value(scores, row.key)) <= threshold]
        baseline = [row for row in boundary
                    if int(_score_value(scores, row.key)) > threshold]
        if not selected or not baseline:
            continue
        selected_rate = _reuse_rate(selected)
        baseline_rate = _reuse_rate(baseline)
        positives = sum(row.folio_nr_pages for row in selected
                        if row.observed_within_horizon)
        selected_base_pages = _base_page_count(selected)
        upper = _wilson_upper(positives, selected_base_pages)
        if (selected_rate is not None and baseline_rate is not None and
                selected_rate < baseline_rate and upper is not None and
                upper <= max_downgrade_mistake_ci95_upper):
            cold_options.append((selected_base_pages,
                                 baseline_rate - selected_rate, -threshold,
                                 threshold, selected, baseline, upper))
    cold_options.sort(reverse=True, key=lambda item: item[:3])
    cold_choice = cold_options[0] if cold_options else None
    cold = cold_choice[3] if cold_choice else min(unique_scores) - 1
    if cold >= hot_1:
        cold = hot_1 - 1

    upgrade_bootstrap = (
        _session_cluster_rate_difference_ci(
            hot_choice[4], hot_choice[5], tag="upgrade-hot1",
            expected_direction="positive")
        if hot_choice else {
            "status": "THRESHOLD_NOT_SELECTED",
            "method": "session_cluster_bootstrap",
            "expected_direction": "positive",
            "gate_eligible": False,
            "direction_gate_pass": False,
        })
    downgrade_bootstrap = (
        _session_cluster_rate_difference_ci(
            cold_choice[4], cold_choice[5], tag="downgrade-cold",
            expected_direction="negative")
        if cold_choice else {
            "status": "THRESHOLD_NOT_SELECTED",
            "method": "session_cluster_bootstrap",
            "expected_direction": "negative",
            "gate_eligible": False,
            "direction_gate_pass": False,
        })
    protect_gate = bool(
        hot_choice is not None and hot_2_choice is not None and
        upgrade_bootstrap["gate_eligible"] is True and
        upgrade_bootstrap["direction_gate_pass"] is True)
    bidirectional_gate = bool(
        protect_gate and cold_choice is not None and
        downgrade_bootstrap["gate_eligible"] is True and
        downgrade_bootstrap["direction_gate_pass"] is True)
    thresholds_complete = bool(
        hot_choice is not None and hot_2_choice is not None and
        cold_choice is not None)

    return {
        "selected_on_split": "validation",
        "test_set_used": False,
        "cold_threshold": cold,
        "hot_threshold_1": hot_1,
        "hot_threshold_2": hot_2,
        "threshold_provenance": {
            "cold_threshold": ("VALIDATION_SELECTED" if cold_choice else
                               "DISABLED_FALLBACK_BELOW_MIN_SCORE"),
            "hot_threshold_1": ("VALIDATION_SELECTED" if hot_choice else
                                "DISABLED_FALLBACK_AT_MAX_SCORE"),
            "hot_threshold_2": ("VALIDATION_SELECTED" if hot_2_choice else
                                "DISABLED_FALLBACK_ABOVE_HOT_1"),
            "hot_threshold_3": "NOT_SELECTED_EXPERIMENTAL",
        },
        "all_runtime_thresholds_validation_selected": thresholds_complete,
        "hot_threshold_2_evidence_pass": hot_2_choice is not None,
        "protect_only_point_direction_pass": hot_choice is not None,
        "bidirectional_point_direction_pass": hot_choice is not None and
        cold_choice is not None,
        "protect_only_gate_pass": protect_gate,
        "bidirectional_gate_pass": bidirectional_gate,
        "fallback": (None if bidirectional_gate else
                     "PROTECT_ONLY" if protect_gate else "NATIVE_ONLY"),
        "session_cluster_bootstrap_ci": {
            "status": ("COMPLETE" if
                       upgrade_bootstrap["gate_eligible"] is True and
                       (cold_choice is None or
                        downgrade_bootstrap["gate_eligible"] is True)
                       else "INSUFFICIENT_SESSION_SUPPORT"),
            "gate_eligible": bool(
                upgrade_bootstrap["gate_eligible"] is True and
                (cold_choice is None or
                 downgrade_bootstrap["gate_eligible"] is True)),
            "upgrade_rate_difference": upgrade_bootstrap,
            "downgrade_rate_difference": downgrade_bootstrap,
        },
        "upgrade": ({
            "threshold": hot_1,
            "selected": _threshold_group_stats(hot_choice[4],
                                                native_reclaim,
                                                tag="upgrade-selected"),
            "keep_reclaim": _threshold_group_stats(hot_choice[5],
                                                    native_reclaim,
                                                    tag="upgrade-baseline"),
            "hot_2": ({
                "threshold": hot_2,
                "selected": _threshold_group_stats(
                    hot_2_choice[1], native_reclaim,
                    tag="upgrade-hot2-selected"),
                "keep_below_hot_2": _threshold_group_stats(
                    hot_2_choice[2], native_reclaim,
                    tag="upgrade-hot2-baseline"),
            } if hot_2_choice else None),
        } if hot_choice else None),
        "downgrade": ({
            "threshold": cold,
            "selected": _threshold_group_stats(cold_choice[4],
                                                boundary,
                                                tag="downgrade-selected"),
            "keep_protect": _threshold_group_stats(cold_choice[5],
                                                    boundary,
                                                    tag="downgrade-baseline"),
            "mistake_rate_ci95_upper": cold_choice[6],
        } if cold_choice else None),
        "score_percentiles": {
            "min": unique_scores[0],
            "median": unique_scores[len(unique_scores) // 2],
            "max": unique_scores[-1],
        },
    }


SCORER_CHECKSUM_SCOPE = (
    "sorted compact JSON of model_type, model_version, ordered "
    "feature_names, bin_boundaries by name, quantized_weights by name, "
    "and quantized_bias"
)

MODEL_PROVENANCE_STATUS: Mapping[str, str] = {
    "SYNTHETIC_TEST_FIXTURE": "SYNTHETIC_TEST_ONLY",
    "ENGINEERING_FIXTURE_UNTRAINED": "ENGINEERING_FIXTURE_UNTRAINED",
    "TRAINED_PAIRWISE_OFFLINE":
        "TRAINED_OFFLINE_CANDIDATE_NOT_DEPLOYED",
}


def scorer_parameter_projection(
        model_type: str, model_version: int, feature_names: Sequence[str],
        bin_boundaries: Mapping[str, Sequence[int]],
        quantized_weights: Mapping[str, Sequence[int]],
        quantized_bias: int,
) -> Dict[str, object]:
    """Return the trace-joinable embedded-parameter checksum projection."""

    names = [str(name) for name in feature_names]
    return {
        "model_type": str(model_type),
        "model_version": int(model_version),
        "feature_names": names,
        "bin_boundaries": {
            name: [int(value) for value in bin_boundaries[name]]
            for name in names
        },
        "quantized_weights": {
            name: [int(value) for value in quantized_weights[name]]
            for name in names
        },
        "quantized_bias": int(quantized_bias),
    }


def scorer_parameter_checksum(projection: Mapping[str, object]) -> str:
    encoded = json.dumps(projection, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_checksum(payload: Mapping[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("checksum", None)
    encoded = json.dumps(unsigned, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def make_model_document(
        model: QuantizedRankModel, thresholds: Mapping[str, object],
        pair_sampling: Mapping[str, object], *,
        model_version: int = 1,
        model_provenance: str = "SYNTHETIC_TEST_FIXTURE",
) -> Dict[str, object]:
    """Export the explicit non-probability, no-runtime-ranking contract."""

    if model_provenance not in MODEL_PROVENANCE_STATUS:
        raise RankingError("unsupported model provenance")
    validation_selected = model_provenance == "TRAINED_PAIRWISE_OFFLINE"
    if validation_selected:
        provenance = thresholds.get("threshold_provenance")
        exact_validation_provenance = bool(
            isinstance(provenance, Mapping) and all(
                provenance.get(name) == "VALIDATION_SELECTED"
                for name in ("cold_threshold", "hot_threshold_1",
                             "hot_threshold_2")))
        if (thresholds.get(
                "all_runtime_thresholds_validation_selected") is not True or
                thresholds.get("selected_on_split") != "validation" or
                thresholds.get("test_set_used") is not False or
                not exact_validation_provenance):
            raise RankingError(
                "trained offline model requires untouched test data and "
                "exact validation provenance for every runtime threshold")
    names = list(model.feature_names)
    boundaries = {
        name: list(edges) for name, edges in
        zip(model.feature_names, model.bin_boundaries)
    }
    weights = {
        name: list(values) for name, values in
        zip(model.feature_names, model.weights)
    }
    cold = int(thresholds["cold_threshold"])
    hot_1 = int(thresholds["hot_threshold_1"])
    hot_2 = int(thresholds["hot_threshold_2"])
    hot_3_raw = thresholds.get("hot_threshold_3")
    hot_3_selected_on = thresholds.get("hot_threshold_3_selected_on_split")
    if hot_3_raw is not None and hot_3_selected_on != "validation":
        raise RankingError(
            "experimental +3 threshold requires validation provenance")
    hot_3 = int(hot_3_raw) if hot_3_raw is not None else None
    delta = {
        "cold": -TIER_SCALE,
        "neutral": 0,
        "hot_1": TIER_SCALE,
        "hot_2": 2 * TIER_SCALE,
    }
    projection = scorer_parameter_projection(
        MODEL_TYPE, model_version, names, boundaries, weights, model.bias)
    document: Dict[str, object] = {
        "schema_version": 2,
        "primary_task": "pairwise_next-reuse_ranking",
        "model_type": MODEL_TYPE,
        "model_provenance": model_provenance,
        "training_status": MODEL_PROVENANCE_STATUS[model_provenance],
        "selected_for_live_use": False,
        "score_semantics": SCORE_SEMANTICS,
        "score_is_probability": False,
        "runtime_pairwise_comparison": False,
        "runtime_sorting": False,
        "runtime_candidate_sorting": False,
        "runtime_sigmoid": False,
        "feature_names": names,
        "bin_boundaries": boundaries,
        "quantized_weights": weights,
        "quantized_bias": model.bias,
        "score_q_format": "s16_lookup_weights_scale_%d" %
        model.weight_scale,
        "training_horizon_ns": model.horizon_ns,
        "tie_margin_ns": model.tie_margin_ns,
        "pair_sampling": dict(pair_sampling),
        "runtime_thresholds_validation_selected": validation_selected,
        "threshold_selection_split": (
            "validation" if validation_selected else None),
        "score_threshold_cold": cold,
        "score_threshold_hot_1": hot_1,
        "score_threshold_hot_2": hot_2,
        "score_threshold_hot_3": hot_3,
        # Deprecated v1 aliases retained for readers during schema migration.
        "cold_threshold": cold,
        "hot_threshold_1": hot_1,
        "hot_threshold_2": hot_2,
        "hot_threshold_3": hot_3,
        "delta_tier_q8": dict(delta),
        "predictive_delta_tier_q8": dict(delta),
        "experimental_plus3": {
            "status": ("VALIDATION_SELECTED" if hot_3 is not None else
                       "NOT_SELECTED"),
            "offline_ablation_supported": True,
            "shadow_supported": True,
            "apply_supported": False,
            "default_enabled": False,
            "threshold_selected_on_split": hot_3_selected_on,
            "score_threshold_hot_3": hot_3,
            "predictive_delta_tier_q8": 3 * TIER_SCALE,
        },
        "model_version": model_version,
        "feature_schema_version": 1,
        "kernel_shape_compatible_v1": tuple(model.feature_names) ==
            tuple(BASE_FEATURES),
        "kernel_deployable_v1": False,
        "scorer_checksum": scorer_parameter_checksum(projection),
        "scorer_checksum_scope": SCORER_CHECKSUM_SCOPE,
        "checksum": "",
    }
    document["checksum"] = _canonical_checksum(document)
    validate_model_document(document)
    return document


def validate_model_document(document: Mapping[str, object]) -> None:
    required = frozenset((
        "schema_version", "primary_task", "model_type",
        "model_provenance", "training_status", "selected_for_live_use",
        "score_semantics", "score_is_probability",
        "runtime_pairwise_comparison", "runtime_sorting",
        "runtime_candidate_sorting", "runtime_sigmoid", "feature_names",
        "bin_boundaries", "quantized_weights", "quantized_bias",
        "score_q_format", "training_horizon_ns", "tie_margin_ns",
        "pair_sampling", "runtime_thresholds_validation_selected",
        "threshold_selection_split", "score_threshold_cold",
        "score_threshold_hot_1", "score_threshold_hot_2",
        "score_threshold_hot_3", "cold_threshold", "hot_threshold_1",
        "hot_threshold_2", "hot_threshold_3", "delta_tier_q8",
        "predictive_delta_tier_q8", "experimental_plus3",
        "model_version", "feature_schema_version",
        "kernel_shape_compatible_v1", "kernel_deployable_v1",
        "scorer_checksum", "scorer_checksum_scope", "checksum",
    ))
    actual = frozenset(document)
    missing = required - actual
    extra = actual - required
    if missing or extra:
        details = []
        if missing:
            details.append("missing: %s" % ", ".join(sorted(missing)))
        if extra:
            details.append("unexpected: %s" % ", ".join(sorted(extra)))
        raise RankingError("ranking model fields invalid (%s)" %
                           "; ".join(details))
    if document["schema_version"] != 2:
        raise RankingError("ranking model schema_version must be 2")
    if document["primary_task"] != "pairwise_next-reuse_ranking":
        raise RankingError("ranking model primary task is invalid")
    if document["model_type"] != MODEL_TYPE:
        raise RankingError("model_type must be pairwise_linear_ranker")
    provenance = document["model_provenance"]
    if not isinstance(provenance, str) or \
            provenance not in MODEL_PROVENANCE_STATUS:
        raise RankingError("invalid model provenance")
    if document["training_status"] != MODEL_PROVENANCE_STATUS[provenance]:
        raise RankingError("training status disagrees with provenance")
    if document["selected_for_live_use"] is not False:
        raise RankingError("offline model cannot be selected for live use")
    if document["score_semantics"] != SCORE_SEMANTICS:
        raise RankingError("rank score direction is invalid")
    if document["score_is_probability"] is not False:
        raise RankingError("rank score cannot be declared a probability")
    for name in ("runtime_pairwise_comparison", "runtime_sorting",
                 "runtime_candidate_sorting", "runtime_sigmoid"):
        if document[name] is not False:
            raise RankingError("%s must be false" % name)
    names = document["feature_names"]
    edges_by_name = document["bin_boundaries"]
    weights_by_name = document["quantized_weights"]
    if (not isinstance(names, list) or not names or
            any(not isinstance(name, str) for name in names) or
            len(set(names)) != len(names) or
            not isinstance(edges_by_name, Mapping) or
            not isinstance(weights_by_name, Mapping)):
        raise RankingError("invalid feature metadata")
    name_tuple = tuple(names)
    if name_tuple not in frozenset(RANK_ABLATIONS.values()):
        raise RankingError("model feature set is not a declared ablation")
    if set(edges_by_name) != set(names) or set(weights_by_name) != set(names):
        raise RankingError("lookup table keys must exactly match features")
    if (isinstance(document["quantized_bias"], bool) or
            not isinstance(document["quantized_bias"], int)):
        raise RankingError("quantized bias must be an integer")
    minimum = int(document["quantized_bias"])
    maximum = int(document["quantized_bias"])
    if minimum < S16_MIN or maximum > S16_MAX:
        raise RankingError("quantized bias outside s16")
    for name in names:
        if name not in edges_by_name or name not in weights_by_name:
            raise RankingError("feature lookup table is missing")
        try:
            edges = list(edges_by_name[name])
            weights = list(weights_by_name[name])
        except TypeError as exc:
            raise RankingError("lookup rows must be arrays") from exc
        if (any(isinstance(value, bool) or not isinstance(value, int)
                for value in edges + weights) or
                edges != list(FEATURE_EDGES[name])):
            raise RankingError("lookup boundaries disagree with schema v1")
        if len(weights) != len(edges) + 1:
            raise RankingError("lookup weight/bin shape mismatch")
        if any(int(left) >= int(right)
               for left, right in zip(edges, edges[1:])):
            raise RankingError("bin boundaries must strictly increase")
        if any(int(value) < S16_MIN or int(value) > S16_MAX
               for value in weights):
            raise RankingError("quantized weight outside s16")
        minimum += min(int(value) for value in weights)
        maximum += max(int(value) for value in weights)
    if minimum < S32_MIN or maximum > S32_MAX:
        raise RankingError("model score range outside s32")
    primary_threshold_names = (
        "score_threshold_cold", "score_threshold_hot_1",
        "score_threshold_hot_2", "cold_threshold", "hot_threshold_1",
        "hot_threshold_2",
    )
    if any(isinstance(document[name], bool) or
           not isinstance(document[name], int)
           for name in primary_threshold_names):
        raise RankingError("score thresholds must be integers")
    if any(int(document[name]) < S32_MIN or int(document[name]) > S32_MAX
           for name in primary_threshold_names):
        raise RankingError("score thresholds must fit s32")
    cold = int(document["score_threshold_cold"])
    hot_1 = int(document["score_threshold_hot_1"])
    hot_2 = int(document["score_threshold_hot_2"])
    hot_3_value = document["score_threshold_hot_3"]
    if (hot_3_value is not None and
            (isinstance(hot_3_value, bool) or
             not isinstance(hot_3_value, int) or
             hot_3_value < S32_MIN or hot_3_value > S32_MAX)):
        raise RankingError("experimental +3 threshold must be null or s32")
    hot_3 = int(hot_3_value) if hot_3_value is not None else None
    if (document["cold_threshold"] != cold or
            document["hot_threshold_1"] != hot_1 or
            document["hot_threshold_2"] != hot_2 or
            document["hot_threshold_3"] != hot_3):
        raise RankingError("deprecated threshold aliases disagree")
    if not cold < hot_1 < hot_2 or (hot_3 is not None and
                                    hot_3 <= hot_2):
        raise RankingError("score thresholds must strictly increase")
    if (isinstance(document["training_horizon_ns"], bool) or
            not isinstance(document["training_horizon_ns"], int) or
            document["training_horizon_ns"] != RANKING_HORIZON_NS):
        raise RankingError("ranking model horizon must be 5s")
    if (isinstance(document["tie_margin_ns"], bool) or
            not isinstance(document["tie_margin_ns"], int) or
            document["tie_margin_ns"] not in RANKING_TIE_MARGINS_NS):
        raise RankingError("ranking model tie margin is unsupported")
    if (isinstance(document["model_version"], bool) or
            document["model_version"] != 1 or
            isinstance(document["feature_schema_version"], bool) or
            document["feature_schema_version"] != 1):
        raise RankingError("current model and feature schema versions are 1")
    shape_compatible = name_tuple == tuple(BASE_FEATURES)
    if document["kernel_shape_compatible_v1"] is not shape_compatible:
        raise RankingError(
            "kernel_shape_compatible_v1 disagrees with features")
    if document["kernel_deployable_v1"] is not False:
        raise RankingError(
            "offline candidate is not yet deployable to the kernel")
    scale = document["score_q_format"]
    if not isinstance(scale, str) or not scale.startswith(
            "s16_lookup_weights_scale_"):
        raise RankingError("invalid score_q_format")
    prefix = "s16_lookup_weights_scale_"
    try:
        scale_value = int(scale[len(prefix):])
    except ValueError as exc:
        raise RankingError("invalid score_q_format") from exc
    if scale_value < 1 or scale != "s16_lookup_weights_scale_%d" % scale_value:
        raise RankingError("invalid score_q_format")
    if not isinstance(document["pair_sampling"], Mapping):
        raise RankingError("pair_sampling must be an object")
    validation_selected = provenance == "TRAINED_PAIRWISE_OFFLINE"
    if (document["runtime_thresholds_validation_selected"] is not
            validation_selected or
            document["threshold_selection_split"] !=
            ("validation" if validation_selected else None)):
        raise RankingError("threshold-selection provenance is invalid")
    delta = {
        "cold": -TIER_SCALE, "neutral": 0, "hot_1": TIER_SCALE,
        "hot_2": 2 * TIER_SCALE,
    }
    if (document["delta_tier_q8"] != delta or
            document["predictive_delta_tier_q8"] != delta):
        raise RankingError("predictive delta mapping is invalid")
    plus3 = document["experimental_plus3"]
    expected_plus3 = {
        "status": ("VALIDATION_SELECTED" if hot_3 is not None else
                   "NOT_SELECTED"),
        "offline_ablation_supported": True,
        "shadow_supported": True,
        "apply_supported": False,
        "default_enabled": False,
        "threshold_selected_on_split": (
            "validation" if hot_3 is not None else None),
        "score_threshold_hot_3": hot_3,
        "predictive_delta_tier_q8": 3 * TIER_SCALE,
    }
    if plus3 != expected_plus3:
        raise RankingError("experimental +3 contract is invalid")
    if document["scorer_checksum_scope"] != SCORER_CHECKSUM_SCOPE:
        raise RankingError("scorer checksum scope is invalid")
    projection = scorer_parameter_projection(
        MODEL_TYPE, int(document["model_version"]), names,
        edges_by_name, weights_by_name, int(document["quantized_bias"]))
    if document["scorer_checksum"] != scorer_parameter_checksum(projection):
        raise RankingError("embedded scorer checksum mismatch")
    if document["checksum"] != _canonical_checksum(document):
        raise RankingError("ranking model checksum mismatch")


def score_all(model: object,
              candidates: Sequence[RankingCandidate]) -> Dict[
                  CandidateKey, float]:
    if not isinstance(model, (FloatRankModel, QuantizedRankModel)):
        raise RankingError("unsupported ranking model")
    return {candidate.key: model.score(candidate) for candidate in candidates}
