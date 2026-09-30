"""Canonical WPS 90-event plan and static integrity checks.

The source workbook named by the stage-4 brief is not present in the VM.
This plan transcribes the 90-row sequence reproduced in that brief and keeps
all uncertain UI details explicit in ``source_detail``.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List


@dataclass(frozen=True)
class OperationSpec:
    op_id: int
    source_event: str
    event_type: str
    planned_offset_ms: int
    delay_from_prev_ms: int
    handler: str
    args: Dict[str, str]
    precondition: str
    verification: str
    measurement_role: str
    source_detail: str = "complete"


_DELAYS_S = [
    0.000, 8.148, 19.374, 5.679, 7.099, 5.725, 6.247, 6.463, 5.742,
    5.835, 5.823, 5.740, 8.760, 5.684, 5.917, 5.724, 5.818, 6.223,
    5.858, 5.810, 5.721, 5.668, 10.916, 10.040, 8.919, 9.106, 9.018,
    8.943, 8.871, 8.930, 8.918, 9.033, 8.796, 12.290, 8.628, 6.568,
    11.646, 21.056, 19.893, 16.221, 19.313, 6.001, 5.442, 5.863, 5.846,
    5.710, 5.689, 5.890, 5.751, 5.733, 8.777, 5.911, 5.862, 5.716,
    6.050, 5.984, 6.068, 5.914, 5.904, 5.841, 15.638, 5.841, 5.986,
    10.075, 16.541, 19.435, 5.771, 5.874, 5.757, 5.879, 5.928, 6.104,
    5.703, 5.884, 5.820, 8.725, 5.861, 5.827, 5.928, 5.716, 5.731,
    5.797, 5.724, 5.700, 5.736, 8.698, 6.818, 9.326, 14.502, 15.395,
]


def _ms(value: float) -> int:
    return int(round(value * 1000))


def _operation(op_id: int, event_type: str, source_event: str,
               handler: str, planned_offset_ms: int, delay_ms: int,
               args=None, precondition="WPS target window is active",
               verification="active WPS title/state is checked",
               source_detail="complete") -> OperationSpec:
    role = "runtime feature + optional Referenced_5s aligned label"
    return OperationSpec(
        op_id=op_id, source_event=source_event, event_type=event_type,
        planned_offset_ms=planned_offset_ms, delay_from_prev_ms=delay_ms,
        handler=handler, args=args or {}, precondition=precondition,
        verification=verification, measurement_role=role,
        source_detail=source_detail,
    )


def build_plan() -> List[OperationSpec]:
    plan: List[OperationSpec] = []
    planned = 0
    for op_id, delay_s in enumerate(_DELAYS_S, 1):
        delay_ms = _ms(delay_s)
        if op_id > 1:
            planned += delay_ms
        if op_id == 1:
            plan.append(_operation(op_id, "key_input", "输入 WPS 文件路径后回车",
                                   "start_open_ppt", planned, delay_ms,
                                   {"document": "ppt_200m.pptx"},
                                   "WPS home is active and the open dialog can be verified",
                                   "PPT path is sent only to the verified wpsoffice dialog",
                                   "requested 1Gppt.pptx is absent; existing ppt_200m.pptx is configured"))
        elif op_id == 2:
            plan.append(_operation(op_id, "open", "打开文件 1Gppt.pptx", "verify_ppt_open",
                                   planned, delay_ms, {"document": "ppt_200m.pptx"},
                                   precondition="op1 submitted the configured PPT path",
                                   verification="PPT title becomes active and stays stable",
                                   source_detail="requested 1Gppt.pptx absent; existing ppt_200m.pptx is configured"))
        elif 3 <= op_id <= 12:
            plan.append(_operation(op_id, "scroll", "鼠标滚轮向下浏览 PPT 大纲",
                                   "scroll_down", planned, delay_ms,
                                   {"document_type": "ppt", "scroll_clicks_per_event": "3"},
                                   verification="PPT title remains active",
                                   source_detail="wheel delta absent from source; fixed 3 key events is explicit assumption"))
        elif 13 <= op_id <= 22:
            plan.append(_operation(op_id, "scroll", "鼠标滚轮向上浏览 PPT 大纲",
                                   "scroll_up", planned, delay_ms,
                                   {"document_type": "ppt", "scroll_clicks_per_event": "3"},
                                   verification="PPT title remains active",
                                   source_detail="wheel delta absent from source; fixed 3 key events is explicit assumption"))
        elif op_id == 23:
            plan.append(_operation(op_id, "click", "点击放映按钮，放映 PPT",
                                   "click_slideshow", planned, delay_ms,
                                   verification="slideshow state or WPS process set is present"))
        elif 24 <= op_id <= 33:
            plan.append(_operation(op_id, "click", "点击屏幕播放 PPT",
                                   "click_slideshow_next", planned, delay_ms,
                                   verification="slideshow window remains active"))
        elif op_id == 34:
            plan.append(_operation(op_id, "click", "按 Esc 退出放映模式", "escape_slideshow",
                                   planned, delay_ms, verification="editor window is active"))
        elif op_id == 35:
            plan.append(_operation(op_id, "click", "点击插入", "click_insert", planned, delay_ms,
                                   verification="Insert UI is active"))
        elif op_id == 36:
            plan.append(_operation(op_id, "click", "点击文本框", "click_textbox", planned, delay_ms,
                                   verification="text-box insertion mode is active"))
        elif op_id == 37:
            plan.append(_operation(op_id, "other", "文本框放置在页面右上角", "place_textbox",
                                   planned, delay_ms, {"x_ratio": "0.82", "y_ratio": "0.24"},
                                   verification="relative placement command completes",
                                   source_detail="text content not supplied; placement only"))
        elif op_id == 38:
            plan.append(_operation(op_id, "other", "通过 Ctrl+S 保存 PPT", "save_ctrl_s",
                                   planned, delay_ms, verification="save command returns"))
        elif op_id == 39:
            plan.append(_operation(op_id, "other", "进入任务中心", "open_task_center",
                                   planned, delay_ms, verification="task-center title/state is checked"))
        elif op_id == 40:
            plan.append(_operation(op_id, "open", "打开文件 Excel_100M_1.xlsx", "open_excel",
                                   planned, delay_ms, {"document": "excel_200m.xlsx"},
                                   verification="Excel title becomes active",
                                   source_detail="requested Excel_100M_1.xlsx absent; existing excel_200m.xlsx is configured"))
        elif 41 <= op_id <= 50:
            plan.append(_operation(op_id, "scroll", "鼠标滚轮向下浏览 Excel 文件", "excel_scroll_down",
                                   planned, delay_ms, {"scroll_clicks_per_event": "3"},
                                   verification="Excel title remains active",
                                   source_detail="wheel delta absent from source; fixed 3 key events is explicit assumption"))
        elif 51 <= op_id <= 60:
            plan.append(_operation(op_id, "scroll", "鼠标滚轮向上浏览 Excel 文件", "excel_scroll_up",
                                   planned, delay_ms, {"scroll_clicks_per_event": "3"},
                                   verification="Excel title remains active",
                                   source_detail="wheel delta absent from source; fixed 3 key events is explicit assumption"))
        elif op_id == 61:
            plan.append(_operation(op_id, "other", "对 Excel 文件字体加粗", "excel_bold", planned, delay_ms,
                                   verification="Excel title remains active",
                                   source_detail="exact cell/range absent from source; current selection is used"))
        elif op_id == 62:
            plan.append(_operation(op_id, "other", "对 Excel 文件字体取消加粗", "excel_unbold", planned, delay_ms,
                                   verification="Excel title remains active",
                                   source_detail="exact cell/range absent from source; current selection is used"))
        elif op_id == 63:
            plan.append(_operation(op_id, "other", "对 Excel 进行筛选", "excel_filter", planned, delay_ms,
                                   verification="Excel title remains active",
                                   source_detail="selection range and filter condition absent from source; filter UI is invoked"))
        elif op_id == 64:
            plan.append(_operation(op_id, "other", "进入任务中心", "open_task_center",
                                   planned, delay_ms, verification="task-center title/state is checked"))
        elif op_id == 65:
            plan.append(_operation(op_id, "open", "打开文件 1Gword.docx", "open_word", planned, delay_ms,
                                   {"document": "word_200m.docx"}, verification="Word title becomes active",
                                   source_detail="requested 1Gword.docx absent; existing word_200m.docx is configured"))
        elif 66 <= op_id <= 75:
            plan.append(_operation(op_id, "scroll", "鼠标滚轮向下浏览 Word 文件", "word_scroll_down",
                                   planned, delay_ms, {"scroll_clicks_per_event": "3"},
                                   verification="Word title remains active",
                                   source_detail="wheel delta absent from source; fixed 3 key events is explicit assumption"))
        elif 76 <= op_id <= 85:
            plan.append(_operation(op_id, "scroll", "鼠标滚轮向上浏览 Word 文件", "word_scroll_up",
                                   planned, delay_ms, {"scroll_clicks_per_event": "3"},
                                   verification="Word title remains active",
                                   source_detail="wheel delta absent from source; fixed 3 key events is explicit assumption"))
        elif op_id == 86:
            plan.append(_operation(op_id, "click", "点击文件", "file_menu", planned, delay_ms,
                                   verification="File menu is active"))
        elif op_id == 87:
            plan.append(_operation(op_id, "click", "点击另存为", "save_as", planned, delay_ms,
                                   verification="Save As UI is active"))
        elif op_id == 88:
            plan.append(_operation(op_id, "click", "点击下载", "download", planned, delay_ms,
                                   verification="download action returns"))
        elif op_id == 89:
            plan.append(_operation(op_id, "other", "下滑另存为下拉菜单", "save_as_dropdown",
                                   planned, delay_ms, verification="Save As menu remains active",
                                   source_detail="exact menu item/scroll delta absent from source"))
        elif op_id == 90:
            plan.append(_operation(op_id, "click", "点击保存", "save", planned, delay_ms,
                                   verification="save command returns"))
    return plan


OPERATIONS = build_plan()
HANDLER_NAMES = {
    "open_word", "open_ppt", "start_open_ppt", "verify_ppt_open",
    "scroll_down", "scroll_up", "click_slideshow",
    "click_slideshow_next", "escape_slideshow", "click_insert", "click_textbox",
    "place_textbox", "save_ctrl_s", "open_task_center", "open_excel",
    "excel_scroll_down", "excel_scroll_up", "excel_bold", "excel_unbold",
    "excel_filter", "word_scroll_down", "word_scroll_up", "file_menu", "save_as",
    "download", "save_as_dropdown", "save",
}


def validate_plan(operations: Iterable[OperationSpec] = OPERATIONS) -> dict:
    rows = list(operations)
    ids = [row.op_id for row in rows]
    missing = [row.op_id for row in rows if row.handler not in HANDLER_NAMES]
    return {
        "total_operations": len(rows),
        "mapped_operations": sum(row.handler in HANDLER_NAMES for row in rows),
        "missing_handlers": missing,
        "contiguous_ids": ids == list(range(1, len(rows) + 1)),
        "source_timeline_ms": rows[-1].planned_offset_ms if rows else 0,
        "event_type_counts": {kind: sum(row.event_type == kind for row in rows)
                              for kind in sorted({row.event_type for row in rows})},
    }


def get_operation(op_id: int) -> OperationSpec:
    if op_id < 1 or op_id > len(OPERATIONS):
        raise ValueError(f"operation id out of range: {op_id}")
    return OPERATIONS[op_id - 1]
