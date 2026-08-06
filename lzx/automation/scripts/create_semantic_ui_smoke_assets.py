#!/usr/bin/env python3
"""创建本轮隔离的本地 Smoke 素材，不访问网络、不覆盖既有文件。"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path


PNG_1X1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL0NwAAAABJRU5ErkJggg=="


def main() -> int:
    parser = argparse.ArgumentParser(description="创建语义 UI Smoke 本地素材")
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--sid", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    assets = args.work_dir / "assets"; pages = assets / "browser_pages"; wps = assets / "wps"; output = args.work_dir / "smoke" / "output"
    pages.mkdir(parents=True, exist_ok=True); wps.mkdir(parents=True, exist_ok=True); output.mkdir(parents=True, exist_ok=True)
    for index in range(1, 4):
        body = "\n".join(f"<p>本地 Smoke 页面 {index}，滚动段落 {line}。</p>" for line in range(1, 121))
        (pages / f"page{index}.html").write_text(f"<!doctype html><html><head><meta charset='utf-8'><title>Semantic Smoke Page {index}</title><style>body{{font-family:sans-serif;margin:2rem}}.badge{{background:#146c94;color:#fff;padding:.4rem}}</style></head><body><h1 class='badge'>本地页面 {index}</h1>{body}</body></html>\n", encoding="utf-8")
    text = "\n\n".join("这是语义自动化 Smoke 的本地测试文本，用于验证 WPS 创建、输入、滚动和保存路径。该文本不包含个人信息、账号信息或网络请求。" for _ in range(18))
    (wps / "smoke_text.txt").write_text(text + "\n", encoding="utf-8")
    image = wps / "smoke_image.png"; image.write_bytes(base64.b64decode(PNG_1X1))
    urls = {f"page{index}": f"http://127.0.0.1:{args.port}/page{index}.html" for index in range(1, 4)}
    (assets / "browser_smoke_urls.json").write_text(json.dumps(urls, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"test_text_path": str(wps / "smoke_text.txt"), "test_image": str(image), "wps_output_dir": str(output), "wps_word_output": str(output / f"semantic_smoke_{args.sid}.docx"), "wps_ppt_output": str(output / f"semantic_smoke_{args.sid}.pptx"), "wps_excel_output": str(output / f"semantic_smoke_{args.sid}.xlsx"), "browser_pages": str(pages), "browser_urls": urls}
    if any(Path(manifest[key]).exists() for key in ("wps_word_output", "wps_ppt_output", "wps_excel_output")):
        raise SystemExit("本轮输出文件已存在，拒绝覆盖")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
