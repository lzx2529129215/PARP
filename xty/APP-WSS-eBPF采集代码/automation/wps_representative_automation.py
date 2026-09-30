#!/usr/bin/env python3
"""Independent WPS representative-operation automation for phase 9.

The module deliberately reuses the proven xdotool/window primitives from the
90-operation implementation while exposing semantic operations instead of
individual clicks, keys, waits, and repeated scrolls.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

try:
    from .wps_actions import (
        DISPLAY_ENV,
        active_title,
        begin_open_document,
        focus_token,
        key_sequence,
        relative_click,
        require_gui_environment,
        require_active,
        wait_active_title,
        wait_active_title_exact,
        wait_document_ready,
        wait_saved_copy,
        xdo,
    )
except ImportError:
    from wps_actions import (
        DISPLAY_ENV,
        active_title,
        begin_open_document,
        focus_token,
        key_sequence,
        relative_click,
        require_gui_environment,
        require_active,
        wait_active_title,
        wait_active_title_exact,
        wait_document_ready,
        wait_saved_copy,
        xdo,
    )


PROJECT = Path(__file__).resolve().parents[1]
SAMPLES = Path(os.environ.get("WPS_SAMPLES_DIR", PROJECT / "samples")).expanduser()


@dataclass(frozen=True)
class RepresentativeOperation:
    new_operation_id: str
    application: str
    operation_category: str
    operation_name: str
    description: str
    atomic_steps: str
    source_old_operation_ids: str
    source_user_event: str
    expected_memory_behavior: str
    prerequisites: str
    automation_status: str
    notes: str
    sequence_order: int
    handler: str


OPERATIONS = [
    RepresentativeOperation(
        "REP001", "PPT", "Open", "打开大型PPT演示文稿",
        "从WPS首页提交固定PPT工作副本，并观测加载初期。",
        "聚焦WPS首页 → Ctrl+O → 输入固定路径 → Enter → 等待标题稳定",
        "1;2", "Perf_WPS_0050 S2",
        "文件读取、文件映射、解析和匿名对象分配。",
        "WPS首页可见；固定PPT工作副本存在。", "PENDING",
        "正式采集窗口测量提交后的前5秒；窗口外等待完整加载。", 1, "open_ppt"),
    RepresentativeOperation(
        "REP002", "PPT", "Navigate", "PPT页面代表性导航",
        "在编辑器中前进两页并回退一页，替代20次连续上下滚动。",
        "Home → PageDown → PageDown → PageUp",
        "3-22", "Perf_WPS_0050 S5;S6",
        "重新访问不同幻灯片的已映射文件页、渲染缓存和UI对象。",
        "REP001完成且PPT标题稳定。", "PENDING", "保留单个双向导航语义。", 2, "ppt_navigate"),
    RepresentativeOperation(
        "REP003", "PPT", "Slideshow", "PPT放映会话",
        "进入放映、前进三页、回退两页并退出，合并连续放映点击。",
        "F5 → Next×3 → Prior×2 → Esc",
        "23-34", "Perf_WPS_0050 S3;S4",
        "放映窗口创建、全屏渲染资源与幻灯片工作集切换。",
        "PPT编辑器处于活动状态。", "PENDING", "不再把每次单击作为独立operation。", 3, "ppt_slideshow"),
    RepresentativeOperation(
        "REP004", "PPT", "Insert", "PPT插入文本框并输入文本",
        "复用已验证的插入/文本框/相对位置动作，并输入固定短文本。",
        "打开插入菜单 → 选择文本框 → 相对位置放置 → 输入固定文本",
        "35-37", "Perf_WPS_0020 S3; Perf_WPS_0050 S8",
        "匿名页、COW、文本布局和UI渲染对象分配。",
        "PPT编辑器处于活动状态。", "PENDING", "固定ASCII文本避免输入法状态依赖。", 4, "ppt_insert_textbox"),
    RepresentativeOperation(
        "REP005", "PPT", "Save", "保存PPT",
        "保存当前PPT工作副本。", "Ctrl+S → 返回编辑器",
        "38", "Perf_WPS_0050 S11;S14",
        "文件写回、临时文件和缓存状态变化。",
        "REP004完成；工作副本可写。", "PENDING", "不覆盖参考原件。", 5, "ppt_save"),
    RepresentativeOperation(
        "REP006", "Excel", "Open", "打开大型Excel工作簿",
        "从当前WPS组件提交固定Excel工作副本，观测加载初期。",
        "当前WPS组件 → Ctrl+O → 输入固定路径 → Enter → 等待标题稳定",
        "39;40", "Perf_WPS_0060 S2",
        "压缩包读取、工作表解析、文件映射和匿名计算对象分配。",
        "PPT已保存；Excel工作副本存在。", "PENDING",
        "正式采集窗口测量提交后的前5秒；窗口外等待完整加载。", 6, "open_excel"),
    RepresentativeOperation(
        "REP007", "Excel", "Navigate", "Excel大范围单元格导航",
        "跳至已用区域末端后返回首格，替代20次连续滚动。",
        "Ctrl+End → Ctrl+Home",
        "41-60", "Perf_WPS_0060 S10;S11",
        "大范围工作表工作集切换、单元格渲染与文件页复用。",
        "REP006完成且Excel标题稳定。", "PENDING", "保留一个大跨度导航操作。", 7, "excel_navigate"),
    RepresentativeOperation(
        "REP008", "Excel", "Calculate", "Excel公式输入与计算",
        "在D1输入固定公式并触发计算。",
        "Ctrl+Home → 右移3格 → 输入=A1+B1 → Enter",
        "", "Perf_WPS_0060 S7; 已验证EXCEL_FORMULA_FILL",
        "匿名计算对象、公式依赖图、单元格更新和COW。",
        "A1/B1具有固定值；Excel工作副本可写。", "PENDING", "来源为已验证pilot和真实用例表。", 8, "excel_formula"),
    RepresentativeOperation(
        "REP009", "Excel", "Format", "Excel单元格格式修改",
        "选择公式单元格并应用加粗。",
        "Ctrl+Home → 右移3格 → Ctrl+B",
        "61;62", "Perf_WPS_0060 S9",
        "样式对象、共享格式表和UI重绘。",
        "REP008完成。", "PENDING", "旧62的立即取消加粗作为低价值逆操作删除。", 9, "excel_format"),
    RepresentativeOperation(
        "REP010", "Excel", "Filter", "Excel筛选开关",
        "在首行启用筛选，复用旧操作63的稳定快捷键。",
        "Ctrl+Home → Ctrl+Shift+L",
        "63", "Perf_WPS_0060 S3;S4",
        "列元数据、筛选UI对象和可见行状态更新。",
        "Excel工作簿活动。", "PENDING", "不伪造具体数据条件。", 10, "excel_filter"),
    RepresentativeOperation(
        "REP011", "Excel", "Save", "保存Excel工作簿",
        "保存公式、格式和筛选状态。", "Ctrl+S → 等待返回",
        "", "Perf_WPS_0060 S7;S9",
        "压缩容器重写、文件缓存和临时文件变化。",
        "REP008-REP010已完成；工作副本可写。", "PENDING", "来源为真实用例和公式pilot。", 11, "excel_save"),
    RepresentativeOperation(
        "REP012", "Word", "Open", "打开大型Word文档",
        "从当前WPS组件提交固定Word工作副本，观测加载初期。",
        "当前WPS组件 → Ctrl+O → 输入固定路径 → Enter → 等待标题稳定",
        "64;65", "Perf_WPS_0040 S2",
        "文档包读取、图片资源映射、分页解析和匿名布局对象。",
        "Excel已保存；Word工作副本存在。", "PENDING",
        "正式采集窗口测量提交后的前5秒；窗口外等待完整加载。", 12, "open_word"),
    RepresentativeOperation(
        "REP013", "Word", "Navigate", "Word代表性翻页浏览",
        "从文首向下三页再回退一页，替代20次连续滚动。",
        "Ctrl+Home → PageDown×3 → PageUp",
        "66-85", "Perf_WPS_0040 S6;S10",
        "分页渲染、图片工作集访问和文件/匿名页复用。",
        "REP012完成且Word标题稳定。", "PENDING", "保留单个双向翻页操作。", 13, "word_navigate"),
    RepresentativeOperation(
        "REP014", "Word", "Edit", "Word段落复制粘贴",
        "选择首段、复制并粘贴，复现真实文本编辑用例。",
        "Ctrl+Home → Ctrl+Shift+Down → Ctrl+C → End → Ctrl+V",
        "", "Perf_WPS_0040 S3",
        "匿名页、COW、编辑缓冲区和布局重算。",
        "Word工作副本活动且包含固定首段。", "PENDING", "来源为真实用例表。", 14, "word_copy_paste"),
    RepresentativeOperation(
        "REP015", "Word", "Insert", "Word插入固定图片",
        "在文末粘贴固定1MiB图片。",
        "Ctrl+End → Enter → 准备固定图片剪贴板 → Ctrl+V",
        "", "Perf_WPS_0040 S4; 已验证WORD_INSERT_IMAGE",
        "图片解码、文件读取、匿名图形对象与布局分配。",
        "固定图片存在；Word工作副本可写。", "PENDING", "复用已验证剪贴板辅助程序。", 15, "word_insert_image"),
    RepresentativeOperation(
        "REP016", "Word", "Find", "Word查找并定位末页文本",
        "查找固定文档中的末页标签并跳转。",
        "Ctrl+F → 输入Telemetry page 12 → Enter → Esc",
        "", "Perf_WPS_0040 S8",
        "文本索引扫描、文档定位和目标页工作集访问。",
        "固定Word样本包含Telemetry page 12。", "PENDING", "搜索词已从真实样本文档核验。", 16, "word_find"),
    RepresentativeOperation(
        "REP017", "Word", "Save", "保存Word文档",
        "保存已完成文本、图片和定位操作的Word工作副本。",
        "Ctrl+S → 返回编辑器",
        "", "Perf_WPS_0040 S5; 已验证WORD_INSERT_IMAGE保存步骤",
        "文件写回、临时文件和缓存状态变化。",
        "Word工作副本已修改且可写。", "PENDING",
        "本机另存为对话框两种入口均验证失败；按规则用稳定保存替代。", 17, "word_save"),
    RepresentativeOperation(
        "REP018", "Word", "Create", "新建空白Word文档",
        "从当前Writer会话新建空白文档。", "Ctrl+N → 验证标题/窗口变化",
        "", "Perf_WPS_0020 S2; 已验证NEW_WORD",
        "新文档模型、匿名对象、编辑与渲染初始化。",
        "Writer会话活动。", "PENDING", "放在序列末尾，避免影响后续前置状态。", 18, "word_new_document"),
]


BY_ID = {row.new_operation_id: row for row in OPERATIONS}


def paths_from_env() -> dict[str, Path]:
    return {
        "ppt": Path(os.environ.get("PHASE9_PPT", SAMPLES / "ppt_200m.pptx")).resolve(),
        "excel": Path(os.environ.get("PHASE9_EXCEL", SAMPLES / "excel_200m.xlsx")).resolve(),
        "word": Path(os.environ.get("PHASE9_WORD", SAMPLES / "word_200m.docx")).resolve(),
        "image": Path(os.environ.get("PHASE9_IMAGE", SAMPLES / "image_sample.png")).resolve(),
        "word_save": Path(os.environ.get(
            "PHASE9_WORD_SAVE", Path.cwd() / "phase9_word_saved.docx")).expanduser().resolve(),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_marker(path: Path | None, payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    print(line, flush=True)
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def home_then_submit(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    before = active_title()
    if not before:
        raise RuntimeError("no active WPS component for document open")
    xdo("key", "ctrl+o")
    deadline = time.monotonic() + 20
    allowed_dialogs = {"wpsoffice", "wpp", "et", "wps"}
    dialog = active_title()
    while time.monotonic() < deadline:
        dialog = active_title()
        if dialog.strip().lower() in allowed_dialogs:
            xdo("type", "--delay", "8", str(path))
            xdo("key", "Return")
            return dialog
        time.sleep(0.25)
    raise RuntimeError(f"WPS open dialog not ready: before={before}; title={dialog}")


def token(path: Path) -> str:
    return path.stem


def op_open(kind: str, values: dict[str, Path], measurement: bool) -> str:
    path = values[kind]
    home_then_submit(path)
    if measurement:
        return active_title()
    return wait_document_ready(token(path), 300, 3)


def op_ppt_navigate(values, _measurement):
    require_active(token(values["ppt"]))
    key_sequence(["Home", "Next", "Next", "Prior"])
    return require_active(token(values["ppt"]))


def op_ppt_slideshow(values, _measurement):
    require_active(token(values["ppt"]))
    xdo("key", "F5")
    time.sleep(0.5)
    key_sequence(["Next", "Next", "Next", "Prior", "Prior"])
    xdo("key", "Escape")
    return wait_active_title(token(values["ppt"]), 20)


def op_ppt_insert_textbox(values, _measurement):
    ppt_token = token(values["ppt"])
    require_active(ppt_token)
    xdo("key", "alt+i")
    key_sequence(["x", "h"])
    relative_click(0.82, 0.24, ppt_token)
    xdo("type", "--delay", "20", "Phase9 representative text")
    return require_active(ppt_token)


def op_ppt_save(values, _measurement):
    require_active(token(values["ppt"]))
    xdo("key", "ctrl+s")
    return active_title()


def op_excel_navigate(values, _measurement):
    require_active(token(values["excel"]))
    key_sequence(["ctrl+End", "ctrl+Home"])
    return require_active(token(values["excel"]))


def select_excel_d1(values):
    require_active(token(values["excel"]))
    xdo("key", "ctrl+Home")
    key_sequence(["Right", "Right", "Right"])


def op_excel_formula(values, _measurement):
    select_excel_d1(values)
    xdo("type", "--delay", "35", "=A1+B1")
    xdo("key", "Return")
    return require_active(token(values["excel"]))


def op_excel_format(values, _measurement):
    select_excel_d1(values)
    xdo("key", "ctrl+b")
    return require_active(token(values["excel"]))


def op_excel_filter(values, _measurement):
    require_active(token(values["excel"]))
    xdo("key", "ctrl+Home")
    xdo("key", "ctrl+shift+l")
    return require_active(token(values["excel"]))


def op_excel_save(values, _measurement):
    require_active(token(values["excel"]))
    xdo("key", "ctrl+s")
    return active_title()


def op_word_navigate(values, _measurement):
    require_active(token(values["word"]))
    key_sequence(["ctrl+Home", "Next", "Next", "Next", "Prior"])
    return require_active(token(values["word"]))


def op_word_copy_paste(values, _measurement):
    require_active(token(values["word"]))
    key_sequence(["ctrl+Home", "ctrl+shift+Down", "ctrl+c", "End", "ctrl+v"])
    return require_active(token(values["word"]))


def op_word_insert_image(values, _measurement):
    word_token = token(values["word"])
    require_active(word_token)
    helper = Path(__file__).with_name("image_clipboard.py")
    process = subprocess.Popen(
        [sys.executable, str(helper), str(values["image"])], env=DISPLAY_ENV,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if process.stdout.readline().strip() != "READY":
            stderr = process.stderr.read()
            raise RuntimeError(f"image clipboard helper not ready: {stderr}")
        key_sequence(["ctrl+End", "Return", "ctrl+v"])
        time.sleep(0.5)
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
    return require_active(word_token)


def op_word_find(values, _measurement):
    require_active(token(values["word"]))
    xdo("key", "ctrl+f")
    time.sleep(0.2)
    xdo("type", "--delay", "25", "Telemetry page 12")
    key_sequence(["Return", "Escape"])
    return require_active(token(values["word"]))


def op_word_save(values, _measurement):
    require_active(token(values["word"]))
    xdo("key", "ctrl+s")
    return active_title()


def op_word_new_document(values, _measurement):
    before = active_title()
    if not before:
        raise RuntimeError("no active Writer window")
    xdo("key", "ctrl+n")
    deadline = time.monotonic() + 5
    after = active_title()
    while time.monotonic() < deadline:
        after = active_title()
        if after and after != before:
            return after
        time.sleep(0.2)
    raise RuntimeError(f"new Word document title did not change: before={before}; after={after}")


HANDLERS: dict[str, Callable] = {
    "open_ppt": lambda values, measurement: op_open("ppt", values, measurement),
    "ppt_navigate": op_ppt_navigate,
    "ppt_slideshow": op_ppt_slideshow,
    "ppt_insert_textbox": op_ppt_insert_textbox,
    "ppt_save": op_ppt_save,
    "open_excel": lambda values, measurement: op_open("excel", values, measurement),
    "excel_navigate": op_excel_navigate,
    "excel_formula": op_excel_formula,
    "excel_format": op_excel_format,
    "excel_filter": op_excel_filter,
    "excel_save": op_excel_save,
    "open_word": lambda values, measurement: op_open("word", values, measurement),
    "word_navigate": op_word_navigate,
    "word_copy_paste": op_word_copy_paste,
    "word_insert_image": op_word_insert_image,
    "word_find": op_word_find,
    "word_save": op_word_save,
    "word_new_document": op_word_new_document,
}


def select_operations(args) -> list[RepresentativeOperation]:
    selected = list(OPERATIONS)
    requested_app = "all" if args.all else args.app
    if requested_app != "all":
        selected = [row for row in selected if row.application.lower() == requested_app]
    if args.single_op:
        if args.single_op not in BY_ID:
            raise ValueError(f"unknown operation id: {args.single_op}")
        selected = [BY_ID[args.single_op]]
    if args.start_op:
        start = BY_ID[args.start_op].sequence_order if args.start_op in BY_ID else int(args.start_op)
        selected = [row for row in selected if row.sequence_order >= start]
    if args.end_op:
        end = BY_ID[args.end_op].sequence_order if args.end_op in BY_ID else int(args.end_op)
        selected = [row for row in selected if row.sequence_order <= end]
    return selected


def execute(row: RepresentativeOperation, args, values: dict[str, Path]) -> int:
    marker_path = Path(args.marker_log).resolve() if args.marker_log else None
    started_ns = time.time_ns()
    append_marker(marker_path, {
        "event": "operation_start", "timestamp": utc_now(), "timestamp_ns": started_ns,
        "operation_id": row.new_operation_id, "operation_name": row.operation_name,
        "application": row.application, "round": args.round, "status": "STARTED",
        "window_title": active_title(),
    })
    status, error, after = "PASS", "", active_title()
    try:
        after = HANDLERS[row.handler](values, args.measurement_trigger)
    except Exception as exc:
        status = "FAIL"
        error = f"{type(exc).__name__}: {exc}"
        after = active_title()
    ended_ns = time.time_ns()
    append_marker(marker_path, {
        "event": "operation_end", "timestamp": utc_now(), "timestamp_ns": ended_ns,
        "operation_id": row.new_operation_id, "operation_name": row.operation_name,
        "application": row.application, "round": args.round, "status": status,
        "error": error, "duration_ms": (ended_ns - started_ns) / 1e6,
        "window_title": after,
    })
    return 0 if status == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", choices=("ppt", "excel", "word", "all"), default="all")
    parser.add_argument("--all", action="store_true", help="run all applications")
    parser.add_argument("--start-op")
    parser.add_argument("--end-op")
    parser.add_argument("--single-op")
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--marker-log")
    parser.add_argument("--measurement-trigger", action="store_true",
                        help="return immediately after open/save submission for a fixed collector window")
    args = parser.parse_args()
    try:
        selected = select_operations(args)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    if not selected:
        parser.error("selected operation range is empty")
    payload = {
        "operation_count": len(selected),
        "operations": [asdict(row) for row in selected],
        "handlers_complete": all(row.handler in HANDLERS for row in selected),
    }
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["handlers_complete"] else 1
    require_gui_environment()
    values = paths_from_env()
    required = {"ppt", "excel", "word", "image"}
    missing = [str(values[name]) for name in required if not values[name].is_file()]
    if missing:
        print(json.dumps({"status": "FAIL", "missing_files": missing}, ensure_ascii=False))
        return 1
    for row in selected:
        rc = execute(row, args, values)
        if rc:
            return rc
        if len(selected) > 1:
            time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
