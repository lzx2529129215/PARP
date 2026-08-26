from __future__ import annotations

from typing import Any

from .readers import sum_keys


REQUIRED_CGROUP_FILES = ("memory_stat", "memory_events", "cpu_stat", "io_stat")


def delta(before: dict[str, int], after: dict[str, int], key: str) -> int:
    return max(0, after.get(key, 0) - before.get(key, 0))


def delta_sum(before: dict[str, int], after: dict[str, int], keys: list[str]) -> int:
    return max(0, sum_keys(after, keys) - sum_keys(before, keys))


def safe_ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _matching_keys(values: dict[str, int], prefixes: tuple[str, ...]) -> list[str]:
    return sorted(key for key in values if key.startswith(prefixes))


def memory_metrics(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    refault_keys = ["workingset_refault_anon", "workingset_refault_file"]
    if not any(key in before or key in after for key in refault_keys):
        refault_keys = ["workingset_refault"]

    pgsteal_keys = _matching_keys(after, ("pgsteal_",))
    # Exclude aggregate pgsteal when per-source fields coexist, preventing double-counting.
    if "pgsteal" in after and pgsteal_keys:
        eviction_keys = pgsteal_keys
    elif "pgsteal" in after:
        eviction_keys = ["pgsteal"]
    else:
        eviction_keys = pgsteal_keys

    direct_scan_keys = (
        ["pgscan_direct"]
        if "pgscan_direct" in after
        else _matching_keys(after, ("pgscan_direct_",))
    )
    kswapd_scan_keys = (
        ["pgscan_kswapd"]
        if "pgscan_kswapd" in after
        else _matching_keys(after, ("pgscan_kswapd_",))
    )
    allocstall_keys = _matching_keys(after, ("allocstall",))

    refault = delta_sum(before, after, refault_keys)
    evicted = delta_sum(before, after, eviction_keys)
    direct_pages = delta_sum(before, after, direct_scan_keys)
    kswapd_pages = delta_sum(before, after, kswapd_scan_keys)
    direct_events_proxy = delta_sum(before, after, allocstall_keys)

    return {
        "page_refault_count": refault,
        "evicted_pages": evicted,
        "page_refault_ratio": safe_ratio(refault, evicted),
        "direct_reclaim_allocstall_count": direct_events_proxy,
        "direct_reclaim_scanned_pages": direct_pages,
        "kswapd_scanned_pages": kswapd_pages,
        "direct_reclaim_page_ratio": safe_ratio(direct_pages, direct_pages + kswapd_pages),
        "sources": {
            "refault": refault_keys,
            "evicted": eviction_keys,
            "direct_scan": direct_scan_keys,
            "kswapd_scan": kswapd_scan_keys,
            "allocstall": allocstall_keys,
        },
    }


def cgroup_metrics(before: dict[str, Any], after: dict[str, Any], elapsed_s: float, cpu_count: int) -> dict[str, Any]:
    memory = memory_metrics(before.get("memory_stat", {}), after.get("memory_stat", {}))
    cpu_before = before.get("cpu_stat", {})
    cpu_after = after.get("cpu_stat", {})
    io_before = before.get("io_stat", {})
    io_after = after.get("io_stat", {})
    events_before = before.get("memory_events", {})
    events_after = after.get("memory_events", {})

    usage_usec = delta(cpu_before, cpu_after, "usage_usec")
    read_bytes = delta(io_before, io_after, "rbytes")
    write_bytes = delta(io_before, io_after, "wbytes")
    memory.update(
        {
            "cpu_usage_usec": usage_usec,
            "cpu_one_core_percent": safe_ratio(usage_usec / 1_000_000 * 100, elapsed_s),
            "cpu_machine_percent": safe_ratio(usage_usec / 1_000_000 * 100, elapsed_s * max(cpu_count, 1)),
            "io_read_bytes": read_bytes,
            "io_write_bytes": write_bytes,
            "io_read_throughput_mb_s": safe_ratio(read_bytes / (1024 * 1024), elapsed_s),
            "io_write_throughput_mb_s": safe_ratio(write_bytes / (1024 * 1024), elapsed_s),
            "oom_count": delta(events_before, events_after, "oom"),
            "oom_kill_count": delta(events_before, events_after, "oom_kill"),
        }
    )
    return memory


def system_cpu_metrics(before: dict[str, int], after: dict[str, int], cpu_count: int) -> dict[str, float | None]:
    keys = set(before) & set(after)
    if not keys:
        return {"cpu_one_core_percent": None, "cpu_machine_percent": None}
    total_keys = keys - {"guest", "guest_nice"}
    total_delta = sum(after[key] - before[key] for key in total_keys)
    idle_delta = sum(after.get(key, 0) - before.get(key, 0) for key in ("idle", "iowait") if key in keys)
    busy_delta = total_delta - idle_delta
    machine_percent = safe_ratio(max(0, busy_delta) * 100, total_delta)
    return {
        "cpu_machine_percent": machine_percent,
        "cpu_one_core_percent": machine_percent * max(cpu_count, 1) if machine_percent is not None else None,
    }


def cgroup_validity(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if before.get("path") != after.get("path"):
        reasons.append("cgroup path changed during collection")
    before_identity = before.get("identity")
    after_identity = after.get("identity")
    if before_identity is not None or after_identity is not None:
        if before_identity is None or after_identity is None:
            reasons.append("cgroup disappeared during collection")
        elif before_identity != after_identity:
            reasons.append("cgroup was recreated during collection")
    for phase, snapshot in (("before", before), ("after", after)):
        statuses = snapshot.get("read_status")
        if not isinstance(statuses, dict):
            continue  # Backward-compatible analysis of old raw snapshots.
        for name in REQUIRED_CGROUP_FILES:
            status = statuses.get(name, {})
            if not status.get("ok", False):
                reasons.append(f"{phase} {name} unavailable: {status.get('error', 'unknown error')}")
        memory_stat = snapshot.get("memory_stat", {})
        if not any(key in memory_stat for key in ("workingset_refault", "workingset_refault_anon", "workingset_refault_file")):
            reasons.append(f"{phase} memory.stat lacks workingset refault counters")
        if "pgscan_direct" not in memory_stat or "pgscan_kswapd" not in memory_stat:
            reasons.append(f"{phase} memory.stat lacks direct/kswapd scan counters")
        if not any(key.startswith("pgsteal") for key in memory_stat):
            reasons.append(f"{phase} memory.stat lacks eviction counters")
        if "usage_usec" not in snapshot.get("cpu_stat", {}):
            reasons.append(f"{phase} cpu.stat lacks usage_usec")
        if "oom_kill" not in snapshot.get("memory_events", {}):
            reasons.append(f"{phase} memory.events lacks oom_kill")
    return not reasons, reasons


def summarize(
    before: dict[str, Any],
    after: dict[str, Any],
    cpu_count: int = 1,
    elapsed_s_override: float | None = None,
) -> dict[str, Any]:
    snapshot_elapsed_s = max((after["monotonic_ns"] - before["monotonic_ns"]) / 1e9, 1e-9)
    elapsed_s = max(elapsed_s_override, 1e-9) if elapsed_s_override is not None else snapshot_elapsed_s
    system = memory_metrics(before["vmstat"], after["vmstat"])
    system.update(system_cpu_metrics(before.get("cpu_stat", {}), after.get("cpu_stat", {}), cpu_count))
    result: dict[str, Any] = {
        "elapsed_s": elapsed_s,
        "snapshot_elapsed_s": snapshot_elapsed_s,
        "system": system,
    }
    if before.get("cgroup") is not None or after.get("cgroup") is not None:
        if before.get("cgroup") is None or after.get("cgroup") is None:
            result["cgroup"] = {"valid": False, "invalid_reasons": ["cgroup snapshot missing at one endpoint"]}
        else:
            valid, reasons = cgroup_validity(before["cgroup"], after["cgroup"])
            if valid:
                result["cgroup"] = {"valid": True, "invalid_reasons": [], **cgroup_metrics(before["cgroup"], after["cgroup"], elapsed_s, cpu_count)}
            else:
                result["cgroup"] = {"valid": False, "invalid_reasons": reasons}
    return result
