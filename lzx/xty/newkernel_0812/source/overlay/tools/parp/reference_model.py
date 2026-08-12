#!/usr/bin/env python3
"""Bit-exact reference operations for the PARP Q15 kernel implementation."""

Q15_ONE = 32767


def q15_mul(a: int, b: int) -> int:
    a = min(max(a, 0), Q15_ONE)
    b = min(max(b, 0), Q15_ONE)
    return min((a * b + (1 << 14)) >> 15, Q15_ONE)


def file_future_score(app_prior: int, next_state: int, support: int,
                      stability: int, freshness: int) -> int:
    score = q15_mul(app_prior, next_state)
    score = q15_mul(score, support)
    score = q15_mul(score, stability)
    return q15_mul(score, freshness)


def anon_cold_score(accesses_10s: int, accesses_30s: int,
                    accesses_60s: int, active_ratio: int,
                    app_prior: int, evidence_valid: bool = True) -> int:
    if not evidence_valid:
        return 0
    recent = min(accesses_10s * 4 + accesses_30s * 2 + accesses_60s,
                 Q15_ONE)
    cold = (Q15_ONE - recent + Q15_ONE - active_ratio) // 2
    return q15_mul(cold, Q15_ONE - min(app_prior, Q15_ONE))


def assign_state(features, centers, unknown_threshold):
    distances = [
        sum((x - y) ** 2 for x, y in zip(features, center))
        for center in centers
    ]
    state = min(range(len(distances)), key=distances.__getitem__)
    return -1 if distances[state] > unknown_threshold else state
