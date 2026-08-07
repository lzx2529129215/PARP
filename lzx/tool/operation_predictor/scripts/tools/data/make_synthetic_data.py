#!/usr/bin/env python3
"""Generate reproducible synthetic app and in-app operation logs."""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.io_utils import load_json, save_csv, save_json


DEFAULT_GROUP_VOCAB = {
    "学生": 0,
    "程序员": 1,
    "工程师": 2,
    "网红": 3,
    "办公人群": 4,
    "游戏用户": 5,
    "影音娱乐用户": 6,
    "金融用户": 7,
    "通用用户": 8,
}

GROUP_PREFERENCES = {
    "办公人群": ["WPS", "飞书", "腾讯QQ", "华为浏览器", "百度网盘"],
    "游戏用户": ["米哈游", "逆水寒", "明日方舟", "燕云十六声", "青云诀"],
    "影音娱乐用户": ["抖音", "哔哩哔哩", "腾讯视频", "爱奇艺", "优酷视频", "斗鱼", "虎牙直播"],
    "金融用户": ["同花顺", "华为钱包", "腾讯QQ", "华为浏览器"],
}


def _weighted_apps(app_names: list[str], group: str) -> list[str]:
    preferred = [name for name in GROUP_PREFERENCES.get(group, []) if name in app_names]
    if group == "通用用户" or not preferred:
        return app_names
    weighted = list(app_names)
    for name in preferred:
        weighted.extend([name] * 5)
    return weighted


def _valid_ops(op_vocab: dict[str, dict[str, int]], app: str) -> list[str]:
    return [op for op in op_vocab.get(app, {}) if op not in {"<PAD>", "<UNK>"}]


def _choose_foreground_app(rng: random.Random, candidates: list[str], opened: list[str]) -> str:
    if opened and rng.random() < 0.45:
        return rng.choice(opened)
    return rng.choice(candidates)


def _update_opened_apps(rng: random.Random, opened: list[str], foreground_app: str, max_opened: int) -> None:
    if foreground_app not in opened:
        if len(opened) >= max_opened:
            close_candidates = [app for app in opened if app != foreground_app]
            if close_candidates:
                opened.remove(rng.choice(close_candidates))
        opened.append(foreground_app)

    close_candidates = [app for app in opened if app != foreground_app]
    if close_candidates and rng.random() < 0.20:
        opened.remove(rng.choice(close_candidates))


def generate(args: argparse.Namespace) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if args.max_opened_apps < 1:
        raise ValueError("--max-opened-apps must be >= 1")

    rng = random.Random(args.seed)
    app_vocab = load_json(args.app_vocab)
    op_vocab = load_json(args.op_vocab)
    if Path(args.group_vocab).exists():
        group_vocab = load_json(args.group_vocab)
    else:
        group_vocab = DEFAULT_GROUP_VOCAB
        save_json(group_vocab, args.group_vocab)
        print(f"created default user group vocab: {args.group_vocab}")

    app_names = list(app_vocab.keys())
    op_app_names = [app for app in app_names if app in op_vocab and _valid_ops(op_vocab, app)]
    if not app_names or not op_app_names:
        raise ValueError("app_vocab 或 op_vocab 为空，无法生成模拟数据")

    groups = list(group_vocab.keys())
    base_time = datetime(2026, 6, 8, 9, 0, 0)
    app_rows: list[dict[str, str]] = []
    op_rows: list[dict[str, str]] = []

    for user_index in range(1, args.num_users + 1):
        user_id = f"u{user_index:03d}"
        group = groups[(user_index - 1) % len(groups)]
        current = base_time + timedelta(minutes=user_index * 3)
        end_time = current + timedelta(hours=args.hours)
        opened: list[str] = []

        while current < end_time:
            candidates = _weighted_apps(op_app_names, group)
            foreground_app = _choose_foreground_app(rng, candidates, opened)
            _update_opened_apps(rng, opened, foreground_app, args.max_opened_apps)

            app_rows.append(
                {
                    "user_id": user_id,
                    "timestamp": current.strftime("%Y-%m-%d %H:%M:%S"),
                    "foreground_app": foreground_app,
                    "opened_apps": ";".join(opened),
                    "user_group": group,
                }
            )

            op_time = current
            ops = _valid_ops(op_vocab, foreground_app)
            for _ in range(rng.randint(3, 10)):
                op_time += timedelta(seconds=rng.randint(5, 60))
                operation = rng.choice(ops)
                op_rows.append(
                    {
                        "user_id": user_id,
                        "timestamp": op_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "app": foreground_app,
                        "operation": operation,
                        "user_group": group,
                    }
                )

            current += timedelta(minutes=rng.randint(1, 5))

    return app_rows, op_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synthetic Huawei user behavior logs.")
    parser.add_argument("--num-users", type=int, default=10)
    parser.add_argument("--hours", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--app-vocab", default="data/vocab/app_vocab.json")
    parser.add_argument("--op-vocab", default="data/vocab/op_vocab.json")
    parser.add_argument("--group-vocab", default="data/vocab/user_group_vocab.json")
    parser.add_argument("--output-app", default="data/raw/app_events.csv")
    parser.add_argument("--output-op", default="data/raw/op_events.csv")
    parser.add_argument("--max-opened-apps", type=int, default=6)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app_rows, op_rows = generate(args)
    save_csv(app_rows, args.output_app, ["user_id", "timestamp", "foreground_app", "opened_apps", "user_group"])
    save_csv(op_rows, args.output_op, ["user_id", "timestamp", "app", "operation", "user_group"])
    print(f"generated app events: {len(app_rows)} -> {args.output_app}")
    print(f"generated op events: {len(op_rows)} -> {args.output_op}")


if __name__ == "__main__":
    main()
