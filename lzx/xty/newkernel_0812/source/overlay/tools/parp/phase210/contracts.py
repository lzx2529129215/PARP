"""Pure, deterministic Phase2.10 safety and evaluation contracts."""

import math
import random


APP_MODELS = {
    "WPS": "WPS_REUSE_RANKER",
    "FILES": "FILES_REUSE_RANKER",
    "QQ": "QQ_REUSE_RANKER",
}
WRONG_MODELS = {
    "WPS": "FILES_REUSE_RANKER",
    "FILES": "QQ_REUSE_RANKER",
    "QQ": "WPS_REUSE_RANKER",
}
BASE_FEATURES = (
    "delta_since_last_access", "delta_between_recent_accesses",
    "segment_access_ema", "file_access_ema", "reuse_interval_mean",
    "reuse_interval_cv", "recent_access_count", "consecutive_active_windows",
    "consecutive_inactive_windows", "segment_age", "file_age",
    "current_coverage", "weighted_coverage", "normalized_position",
    "distance_from_recent_file_access", "file_size_log", "segment_size_log",
    "generation_tier_proxy", "dirty", "writeback", "page_type",
)
FORBIDDEN = (
    "app_id", "application", "operation", "automation", "session", "repeat",
    "path", "filename", "content", "contact", "message", "document",
)


def app_model(app):
    return APP_MODELS.get(str(app).upper(), "GENERIC_CROSS_APP_RANKER")


def healthy(**overrides):
    state = {
        "model_exists": True, "generic_exists": True, "schema_ok": True,
        "version_ok": True, "features_ok": True, "stable_app": True,
        "score_timeout": False, "confidence_ok": True, "ttl_ok": True,
        "score_finite": True,
    }
    state.update(overrides)
    return state


def route(app, state):
    required = ("model_exists", "schema_ok", "version_ok", "features_ok",
                "stable_app", "confidence_ok", "ttl_ok", "score_finite")
    specific_ok = all(state.get(name, False) for name in required)
    specific_ok = specific_ok and not state.get("score_timeout", False)
    if specific_ok and str(app).upper() in APP_MODELS:
        return APP_MODELS[str(app).upper()]
    if state.get("generic_exists", False):
        return "GENERIC_CROSS_APP_RANKER"
    return "NATIVE_MGLRU"


def features_allowed(names):
    lowered = tuple(str(name).lower() for name in names)
    return not any(token in name for name in lowered for token in FORBIDDEN)


def causal_feature_names(names):
    return not any("future" in str(name).lower() or "next_" in str(name).lower()
                   for name in names)


def sessions_isolated(train, validation, test):
    return not (set(train) & set(validation) or set(train) & set(test) or
                set(validation) & set(test))


def ab_excluded(training, ab_sessions):
    return not set(training) & set(ab_sessions)


def all_equal(values):
    values = list(values)
    return len(set(values)) <= 1


def rankings_differ(left, right):
    return list(left) != list(right)


def cgroup_members_safe(target_pids, member_pids, non_test_pids):
    return set(target_pids) == set(member_pids) and not set(member_pids) & set(non_test_pids)


def limits_restored(before, after):
    return tuple(before) == tuple(after)


def apply_scope_safe(configured_parent, expected_parent):
    return configured_parent == expected_parent and expected_parent not in ("/", "")


def reorder_only(native, selected):
    return len(native) == len(selected) and sorted(native) == sorted(selected)


def model_should_run(strategy):
    return strategy != "NATIVE_MGLRU"


def automation_paths_safe(paths, fixture_root):
    prefix = fixture_root.rstrip("/") + "/"
    return all(str(path).startswith(prefix) for path in paths)


def fixtures_independent(run_ids):
    return len(run_ids) == len(set(run_ids)) and all(run_ids)


def timestamps_monotonic(intervals):
    previous = -1
    for start, end in intervals:
        if start < previous or end < start:
            return False
        previous = end
    return True


def counter_delta(before, after):
    if after < before:
        raise ValueError("counter decreased")
    return after - before


def normalized_refault(refault, reclaimed):
    if reclaimed <= 0:
        return None
    return float(refault) * 1000.0 / float(reclaimed)


def trapezoid(samples):
    total = 0.0
    for (t0, y0), (t1, y1) in zip(samples, samples[1:]):
        if t1 < t0 or not all(math.isfinite(float(x)) for x in (t0, y0, t1, y1)):
            raise ValueError("invalid PSI samples")
        total += (t1 - t0) * (y0 + y1) / 2.0
    return total


def safety_gate(events):
    return events.get("oom", 0) == 0 and events.get("oom_kill", 0) == 0


def watchdog(events, p99_ratio, timeout_rate):
    return (not safety_gate(events) or p99_ratio > 3.0 or timeout_rate > 0.10 or
            any(events.get(name, 0) for name in ("panic", "oops", "bug", "hung_task")))


def latin_square(size, seed):
    if size < 1:
        return []
    base = list(range(size))
    random.Random(seed).shuffle(base)
    return [base[offset:] + base[:offset] for offset in range(size)]


def block_bootstrap(blocks, rounds, seed):
    rng = random.Random(seed)
    blocks = [list(block) for block in blocks]
    output = []
    for _ in range(rounds):
        sample = [value for _ in blocks for value in rng.choice(blocks)]
        output.append(sum(sample) / len(sample) if sample else 0.0)
    return output


def threshold_scope(split):
    return split in ("train", "validation")


def manifests_equal(before, after):
    return before == after


def cleanup_complete(state):
    return (not state.get("processes") and not state.get("scopes") and
            not state.get("trace_instances") and not state.get("apply"))


def strictly_better(candidate, baseline, lower_is_better=False):
    return candidate < baseline if lower_is_better else candidate > baseline
