#!/usr/bin/env python3
"""Create a small deterministic fixture for the v4.1 counterfactual test."""

from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "fixture"


def write_csv(name: str, fields: list[str], rows: list[dict[str, object]]) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    write_csv(
        "samples.csv",
        [
            "sample_id",
            "timestamp",
            "current_app_id",
            "current_app",
            "actual_next_app_id",
            "actual_next_app",
            "available_pages",
            "base_headroom_pages",
            "burst_pages",
        ],
        [
            {"sample_id": "s001", "timestamp": "2026-08-05 09:00:00", "current_app_id": 0, "current_app": "WPS", "actual_next_app_id": 2, "actual_next_app": "腾讯QQ", "available_pages": 2500, "base_headroom_pages": 3500, "burst_pages": 500},
            {"sample_id": "s002", "timestamp": "2026-08-05 09:05:00", "current_app_id": 2, "current_app": "腾讯QQ", "actual_next_app_id": 0, "actual_next_app": "WPS", "available_pages": 3000, "base_headroom_pages": 3500, "burst_pages": 500},
            {"sample_id": "s003", "timestamp": "2026-08-05 09:10:00", "current_app_id": 1, "current_app": "飞书", "actual_next_app_id": 2, "actual_next_app": "腾讯QQ", "available_pages": 5000, "base_headroom_pages": 3500, "burst_pages": 500},
            {"sample_id": "s004", "timestamp": "2026-08-05 09:15:00", "current_app_id": 0, "current_app": "WPS", "actual_next_app_id": 1, "actual_next_app": "飞书", "available_pages": 2200, "base_headroom_pages": 3500, "burst_pages": 500},
            {"sample_id": "s005", "timestamp": "2026-08-05 09:20:00", "current_app_id": 2, "current_app": "腾讯QQ", "actual_next_app_id": 0, "actual_next_app": "WPS", "available_pages": 6000, "base_headroom_pages": 3500, "burst_pages": 500},
            {"sample_id": "s006", "timestamp": "2026-08-05 09:25:00", "current_app_id": 1, "current_app": "飞书", "actual_next_app_id": 0, "actual_next_app": "WPS", "available_pages": 2400, "base_headroom_pages": 3500, "burst_pages": 500},
        ],
    )
    state_rows: list[dict[str, object]] = []
    for sample_id, current_id, next_id in [
        ("s001", 0, 2),
        ("s002", 2, 0),
        ("s003", 1, 2),
        ("s004", 0, 1),
        ("s005", 2, 0),
        ("s006", 1, 0),
    ]:
        for app_id, app, domain_id, launch_pages in [
            (0, "WPS", 100, 7000),
            (1, "飞书", 101, 6000),
            (2, "腾讯QQ", 102, 8000),
        ]:
            running = app_id == current_id or (app_id + current_id) % 2 == 0
            state_rows.append(
                {
                    "sample_id": sample_id,
                    "app_id": app_id,
                    "app": app,
                    "domain_id": domain_id if running else 0,
                    "running": int(running),
                    "foreground": int(app_id == current_id),
                    "reclaimable_pages": 5000 + app_id * 1000 if running else 0,
                    "launch_pages": launch_pages,
                }
            )
    write_csv(
        "app_states.csv",
        [
            "sample_id",
            "app_id",
            "app",
            "domain_id",
            "running",
            "foreground",
            "reclaimable_pages",
            "launch_pages",
        ],
        state_rows,
    )

    prediction_rows: list[dict[str, object]] = []
    candidate_sequences = {
        "s001": [(2, "腾讯QQ", 0.78), (1, "飞书", 0.12), (0, "WPS", 0.06)],
        "s002": [(0, "WPS", 0.72), (1, "飞书", 0.18), (2, "腾讯QQ", 0.05)],
        "s003": [(2, "腾讯QQ", 0.65), (0, "WPS", 0.22), (1, "飞书", 0.08)],
        "s004": [(1, "飞书", 0.61), (2, "腾讯QQ", 0.21), (0, "WPS", 0.10)],
        "s005": [(1, "飞书", 0.44), (0, "WPS", 0.36), (2, "腾讯QQ", 0.12)],
        "s006": [(0, "WPS", 0.56), (2, "腾讯QQ", 0.25), (1, "飞书", 0.13)],
    }
    for sample_id, candidates in candidate_sequences.items():
        for rank, (app_id, app, score) in enumerate(candidates, start=1):
            prediction_rows.append(
                {
                    "sample_id": sample_id,
                    "horizon_ms": 300000,
                    "rank": rank,
                    "app_id": app_id,
                    "app": app,
                    "raw_score": score,
                    "use_score": score,
                    "score_mode": "softmax",
                    "model_version": 401,
                    "ttl_ms": 30000,
                }
            )
    write_csv(
        "lstm_predictions.csv",
        [
            "sample_id",
            "horizon_ms",
            "rank",
            "app_id",
            "app",
            "raw_score",
            "use_score",
            "score_mode",
            "model_version",
            "ttl_ms",
        ],
        prediction_rows,
    )
    write_csv(
        "lstm_input.csv",
        [
            "sample_id",
            "timestamp",
            "history_apps",
            "opened_apps",
            "user_group",
            "current_foreground_app",
        ],
        [
            {"sample_id": "s001", "timestamp": "2026-08-05 09:00:00", "history_apps": "WPS|飞书", "opened_apps": "WPS|飞书|腾讯QQ", "user_group": "办公人群", "current_foreground_app": "WPS"},
            {"sample_id": "s002", "timestamp": "2026-08-05 09:05:00", "history_apps": "WPS|腾讯QQ", "opened_apps": "WPS|腾讯QQ", "user_group": "办公人群", "current_foreground_app": "腾讯QQ"},
            {"sample_id": "s003", "timestamp": "2026-08-05 09:10:00", "history_apps": "腾讯QQ|飞书", "opened_apps": "飞书|腾讯QQ", "user_group": "办公人群", "current_foreground_app": "飞书"},
            {"sample_id": "s004", "timestamp": "2026-08-05 09:15:00", "history_apps": "飞书|WPS", "opened_apps": "WPS|飞书", "user_group": "办公人群", "current_foreground_app": "WPS"},
            {"sample_id": "s005", "timestamp": "2026-08-05 09:20:00", "history_apps": "WPS|腾讯QQ", "opened_apps": "WPS|腾讯QQ|飞书", "user_group": "办公人群", "current_foreground_app": "腾讯QQ"},
            {"sample_id": "s006", "timestamp": "2026-08-05 09:25:00", "history_apps": "腾讯QQ|飞书", "opened_apps": "飞书|腾讯QQ", "user_group": "办公人群", "current_foreground_app": "飞书"},
        ],
    )
    print(f"created fixture under {OUT}")


if __name__ == "__main__":
    main()
