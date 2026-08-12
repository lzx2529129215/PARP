"""Session-scoped domain-level anonymous working-set summaries."""

from statistics import mean

PAGE_SIZE = 4096


def summarize_anon(rows, max_possible_accesses):
    rows = list(rows)
    if not rows:
        return {
            "anon_total_bytes": 0, "anon_observed_bytes": 0,
            "anon_hot_bytes": 0, "anon_warm_bytes": 0,
            "anon_cold_bytes": 0, "anon_active_region_count": 0,
            "anon_region_count": 0, "anon_mean_access_ratio": 0.0,
            "anon_max_access_ratio": 0.0, "anon_mean_age": 0.0,
            "anon_max_age": 0, "anon_working_set_delta": 0,
            "anon_recently_active_ratio": 0.0, "anon_cooling_ratio": 0.0,
            "unresolved_anon_bytes": 0, "unresolved_anon_ratio": 0.0,
        }
    if max_possible_accesses <= 0:
        raise ValueError("max_possible_accesses must be positive")
    for key in ("domain_id", "foreground_epoch", "mm_cookie", "boot_id"):
        values = {row.get(key) for row in rows if key in row}
        if len(values) > 1:
            raise ValueError(f"anonymous {key} identity cannot be merged")
    ratios = [min(1.0, max(0.0, row.get("nr_accesses", 0) /
                           max_possible_accesses)) for row in rows]
    sizes = [row.get("nr_pages", 0) * PAGE_SIZE for row in rows]
    total = sum(sizes)
    hot = sum(size for size, ratio in zip(sizes, ratios) if ratio >= .5)
    warm = sum(size for size, ratio in zip(sizes, ratios)
               if 0 < ratio < .5)
    cold = sum(size for size, ratio in zip(sizes, ratios) if ratio == 0)
    unresolved = sum(row.get("unresolved_bytes", 0) for row in rows)
    return {
        "anon_total_bytes": total, "anon_observed_bytes": total,
        "anon_hot_bytes": hot, "anon_warm_bytes": warm,
        "anon_cold_bytes": cold,
        "anon_active_region_count": sum(ratio > 0 for ratio in ratios),
        "anon_region_count": len(rows),
        "anon_mean_access_ratio": mean(ratios),
        "anon_max_access_ratio": max(ratios),
        "anon_mean_age": mean(row.get("age", 0) for row in rows),
        "anon_max_age": max(row.get("age", 0) for row in rows),
        "anon_working_set_delta": hot - cold,
        "anon_recently_active_ratio": (hot + warm) / total if total else 0.0,
        "anon_cooling_ratio": cold / total if total else 0.0,
        "unresolved_anon_bytes": unresolved,
        "unresolved_anon_ratio": unresolved / (total + unresolved)
        if total + unresolved else 0.0,
    }
