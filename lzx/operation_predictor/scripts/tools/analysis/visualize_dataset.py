#!/usr/bin/env python3
"""Export readable previews and an HTML report for generated pickle datasets."""

from __future__ import annotations

import argparse
import html
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.io_utils import load_json, load_pickle, read_csv, save_csv


def reverse_vocab(vocab: dict[str, int]) -> dict[int, str]:
    return {int(value): key for key, value in vocab.items()}


def multihot_names(vec: list[int], id_to_name: dict[int, str]) -> list[str]:
    return [id_to_name.get(idx, f"<ID:{idx}>") for idx, value in enumerate(vec) if value]


def app_sample_row(sample: dict[str, Any], app_id_to_name: dict[int, str]) -> dict[str, Any]:
    return {
        "user_id": sample["user_id"],
        "timestamp": sample["timestamp"],
        "hour": sample["time_feature"][0],
        "weekday": sample["time_feature"][1],
        "is_weekend": sample["time_feature"][2],
        "user_group_id": sample["user_group"],
        "history_apps": " -> ".join(app_id_to_name.get(int(app_id), f"<ID:{app_id}>") for app_id in sample["history_apps"]),
        "opened_apps": ";".join(multihot_names(sample["opened_apps"], app_id_to_name)),
        "label_3min": ";".join(multihot_names(sample.get("label_3min", []), app_id_to_name)),
        "label_5min": ";".join(multihot_names(sample.get("label_5min", []), app_id_to_name)),
        "label_10min": ";".join(multihot_names(sample.get("label_10min", []), app_id_to_name)),
    }


def op_sample_row(sample: dict[str, Any], op_vocab: dict[str, dict[str, int]]) -> dict[str, Any]:
    app = sample["app"]
    op_id_to_name = reverse_vocab(op_vocab.get(app, {}))
    return {
        "user_id": sample["user_id"],
        "timestamp": sample["timestamp"],
        "app": app,
        "hour": sample["time_feature"][0],
        "weekday": sample["time_feature"][1],
        "is_weekend": sample["time_feature"][2],
        "user_group_id": sample["user_group"],
        "history_ops": " -> ".join(op_id_to_name.get(int(op_id), f"<ID:{op_id}>") for op_id in sample["history_ops"]),
        "label_next_op": op_id_to_name.get(int(sample["label_next_op"]), f"<ID:{sample['label_next_op']}>") ,
    }


def html_table(rows: list[dict[str, Any]], title: str, limit: int = 20) -> str:
    if not rows:
        return f"<section><h2>{html.escape(title)}</h2><p>无数据</p></section>"
    rows = rows[:limit]
    headers = list(rows[0].keys())
    head = "".join(f"<th>{html.escape(str(header))}</th>" for header in headers)
    body_parts = []
    for row in rows:
        cells = "".join(f"<td>{html.escape(str(row.get(header, '')))}</td>" for header in headers)
        body_parts.append(f"<tr>{cells}</tr>")
    body = "\n".join(body_parts)
    return f"""
<section>
  <h2>{html.escape(title)}</h2>
  <div class="table-wrap">
    <table>
      <thead><tr>{head}</tr></thead>
      <tbody>{body}</tbody>
    </table>
  </div>
</section>
"""


def bar_chart(counter: Counter[str], title: str, limit: int = 12) -> str:
    items = counter.most_common(limit)
    if not items:
        return f"<section><h2>{html.escape(title)}</h2><p>无数据</p></section>"
    max_value = max(value for _, value in items)
    bars = []
    for name, value in items:
        width = 100 * value / max_value if max_value else 0
        bars.append(
            f"""
            <div class="bar-row">
              <div class="bar-label">{html.escape(str(name))}</div>
              <div class="bar-track"><div class="bar-fill" style="width:{width:.1f}%"></div></div>
              <div class="bar-value">{value}</div>
            </div>
            """
        )
    return f"<section><h2>{html.escape(title)}</h2>{''.join(bars)}</section>"


def split_counts(args: argparse.Namespace) -> list[dict[str, Any]]:
    rows = []
    for task in ("app", "op"):
        for split in ("train", "val", "test"):
            path = Path(args.processed_dir) / f"{split}_{task}.pkl"
            count = len(load_pickle(path)) if path.exists() else 0
            rows.append({"task": task, "split": split, "count": count, "path": str(path)})
    return rows


def raw_timeline(args: argparse.Namespace, user_id: str, limit: int) -> list[dict[str, Any]]:
    rows = [row for row in read_csv(args.raw_app) if row["user_id"] == user_id]
    preview = []
    previous: set[str] = set()
    for row in rows[:limit]:
        opened = [app for app in row["opened_apps"].split(";") if app]
        current = set(opened)
        preview.append(
            {
                "timestamp": row["timestamp"],
                "foreground_app": row["foreground_app"],
                "opened_count": len(opened),
                "newly_opened": ";".join(sorted(current - previous)),
                "closed": ";".join(sorted(previous - current)),
                "opened_apps": ";".join(opened),
            }
        )
        previous = current
    return preview


def write_html(args: argparse.Namespace, sections: list[str]) -> None:
    output = Path(args.output_html)
    output.parent.mkdir(parents=True, exist_ok=True)
    content = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>第一阶段数据集可视化预览</title>
  <style>
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      color: #202124;
      background: #f5f7fa;
    }}
    header {{
      padding: 28px 40px 18px;
      background: #ffffff;
      border-bottom: 1px solid #d9dee8;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 26px;
      font-weight: 650;
    }}
    p {{
      margin: 0;
      color: #5f6673;
    }}
    main {{
      padding: 24px 40px 40px;
      display: grid;
      gap: 22px;
    }}
    section {{
      background: #ffffff;
      border: 1px solid #d9dee8;
      border-radius: 8px;
      padding: 18px;
      overflow: hidden;
    }}
    h2 {{
      margin: 0 0 14px;
      font-size: 18px;
      font-weight: 650;
    }}
    .table-wrap {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      table-layout: auto;
    }}
    th, td {{
      padding: 9px 10px;
      border-bottom: 1px solid #edf0f5;
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
    }}
    th {{
      background: #f8fafc;
      font-weight: 650;
      color: #394150;
    }}
    .bar-row {{
      display: grid;
      grid-template-columns: minmax(120px, 220px) 1fr 64px;
      align-items: center;
      gap: 10px;
      margin: 8px 0;
      font-size: 13px;
    }}
    .bar-track {{
      height: 12px;
      background: #e7ebf2;
      border-radius: 999px;
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      background: #2f6fed;
    }}
    .bar-value {{
      color: #5f6673;
      text-align: right;
    }}
  </style>
</head>
<body>
  <header>
    <h1>第一阶段数据集可视化预览</h1>
    <p>把 pickle 样本翻译成中文应用名、操作名和可检查的时间线。</p>
  </header>
  <main>
    {''.join(sections)}
  </main>
</body>
</html>
"""
    output.write_text(content, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Visualize first-stage pickle datasets.")
    parser.add_argument("--app-samples", default="data/processed/app_samples.pkl")
    parser.add_argument("--op-samples", default="data/processed/op_samples.pkl")
    parser.add_argument("--processed-dir", default="data/processed")
    parser.add_argument("--app-vocab", default="data/vocab/app_vocab.json")
    parser.add_argument("--op-vocab", default="data/vocab/op_vocab.json")
    parser.add_argument("--raw-app", default="data/raw/app_events.csv")
    parser.add_argument("--user-id", default="u001")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output-html", default="outputs/results/dataset_preview.html")
    parser.add_argument("--output-app-csv", default="outputs/results/app_samples_preview.csv")
    parser.add_argument("--output-op-csv", default="outputs/results/op_samples_preview.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app_vocab = load_json(args.app_vocab)
    op_vocab = load_json(args.op_vocab)
    app_id_to_name = reverse_vocab(app_vocab)
    app_samples = load_pickle(args.app_samples)
    op_samples = load_pickle(args.op_samples)

    app_preview = [app_sample_row(sample, app_id_to_name) for sample in app_samples[: args.limit]]
    op_preview = [op_sample_row(sample, op_vocab) for sample in op_samples[: args.limit]]
    timeline = raw_timeline(args, args.user_id, args.limit)
    split_rows = split_counts(args)

    save_csv(app_preview, args.output_app_csv)
    save_csv(op_preview, args.output_op_csv)

    opened_count_counter = Counter(str(sum(1 for value in sample["opened_apps"] if value)) for sample in app_samples)
    app_label_counter: Counter[str] = Counter()
    for sample in app_samples:
        for app_name in multihot_names(sample.get("label_10min", []), app_id_to_name):
            app_label_counter[app_name] += 1

    op_app_counter = Counter(sample["app"] for sample in op_samples)

    sections = [
        html_table(split_rows, "训练/验证/测试集数量", limit=10),
        html_table(timeline, f"{args.user_id} 原始 opened_apps 时间线", limit=args.limit),
        bar_chart(opened_count_counter, "应用样本中的 opened_apps 数量分布"),
        bar_chart(app_label_counter, "应用间样本 label_10min 高频应用"),
        bar_chart(op_app_counter, "应用内操作样本数量 Top 应用"),
        html_table(app_preview, "应用间样本预览", limit=args.limit),
        html_table(op_preview, "应用内操作样本预览", limit=args.limit),
    ]
    write_html(args, sections)
    print(f"app sample preview: {args.output_app_csv}")
    print(f"op sample preview: {args.output_op_csv}")
    print(f"html report: {args.output_html}")


if __name__ == "__main__":
    main()
