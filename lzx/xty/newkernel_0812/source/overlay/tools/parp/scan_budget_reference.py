#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Bit-exact reference for the PARP memcg scan-budget pure function."""

Q15_ONE = 32767
Q15_SCALE = 32768
U64_MAX = (1 << 64) - 1

FOREGROUND_MULTIPLIER = 16384
HIGH_MULTIPLIER = 19661
MEDIUM_MULTIPLIER = 26214
LOW_MULTIPLIER = 39321
MINIMUM_MULTIPLIER = 16384
MAXIMUM_MULTIPLIER = 49152
HIGH_THRESHOLD = 24576
MEDIUM_THRESHOLD = 12288


def _native(native, reason):
    return {"valid": False, "native": native, "proposed": native,
            "applied": native, "multiplier": Q15_SCALE, "reason": reason,
            "clamp_min": False, "clamp_max": False}


def _scaled(native, multiplier):
    value = (native * multiplier + Q15_SCALE // 2) >> 15
    return min(value, U64_MAX)


def compute_scan_budget(native, score, foreground, pressure, mode,
                        scope="TARGET_MEMCG", bind_valid=True,
                        prior_valid=True, generation_valid=True,
                        model_compatible=True, circuit_ok=True,
                        minimum_units=1, maximum_extra_units=4096):
    native = min(max(int(native), 0), U64_MAX)
    score = min(max(int(score), 0), Q15_ONE)
    if mode == "DISABLED":
        return _native(native, "DISABLED")
    if scope not in ("TARGET_MEMCG", "PROACTIVE_MEMCG"):
        return _native(native, "NOT_TARGET_MEMCG")
    for valid, reason in ((circuit_ok, "CIRCUIT_BREAKER"),
                          (bind_valid, "STALE_BIND"),
                          (prior_valid, "EXPIRED_PRIOR"),
                          (generation_valid, "STALE_GENERATION"),
                          (model_compatible, "MODEL_VERSION")):
        if not valid:
            return _native(native, reason)
    if native == 0:
        result = _native(0, "NATIVE")
        result["valid"] = True
        return result
    if pressure == "EMERGENCY":
        return _native(native, "PRESSURE_BYPASS")
    if foreground:
        multiplier, reason = FOREGROUND_MULTIPLIER, "FOREGROUND"
    elif score >= HIGH_THRESHOLD:
        multiplier, reason = HIGH_MULTIPLIER, "HIGH_PRIOR"
    elif score >= MEDIUM_THRESHOLD:
        multiplier, reason = MEDIUM_MULTIPLIER, "MEDIUM_PRIOR"
    else:
        multiplier, reason = LOW_MULTIPLIER, "LOW_PRIOR"
    multiplier = min(max(multiplier, MINIMUM_MULTIPLIER), MAXIMUM_MULTIPLIER)
    if pressure == "ELEVATED":
        multiplier = (multiplier + Q15_SCALE + 1) // 2
    elif pressure == "HIGH":
        multiplier = max(multiplier, 3 * Q15_SCALE // 4)
    proposed = _scaled(native, multiplier)
    clamp_min = proposed < minimum_units
    if clamp_min:
        proposed = min(native, minimum_units)
    max_units = min(native + maximum_extra_units, U64_MAX)
    clamp_max = proposed > max_units
    if clamp_max:
        proposed = max_units
    return {"valid": True, "native": native, "proposed": proposed,
            "applied": proposed if mode == "APPLY" else native,
            "multiplier": multiplier, "reason": reason,
            "clamp_min": clamp_min, "clamp_max": clamp_max}
