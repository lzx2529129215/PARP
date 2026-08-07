#!/usr/bin/env python3
"""Verify the saved workbook, filter checkpoints, screenshots, and trace for 0060."""

from __future__ import annotations

import argparse
import csv
import itertools
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"x": MAIN_NS, "r": DOC_REL_NS}


@dataclass(frozen=True)
class Cell:
    value: object
    formula: str
    style: int


CELL_REFERENCE_RE = re.compile(r"^(?P<column>[A-Z]+)(?P<row>[1-9][0-9]*)$")
FORMULA_REFERENCE_RE = re.compile(r"(?<![A-Z0-9_])(?P<abs_col>\$?)(?P<column>[A-Z]{1,3})(?P<abs_row>\$?)(?P<row>[1-9][0-9]*)")


def column_number(name: str) -> int:
    result = 0
    for character in name:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def translate_shared_formula(formula: str, anchor: str, target: str) -> str:
    anchor_match = CELL_REFERENCE_RE.fullmatch(anchor)
    target_match = CELL_REFERENCE_RE.fullmatch(target)
    if anchor_match is None or target_match is None:
        return formula
    row_delta = int(target_match.group("row")) - int(anchor_match.group("row"))
    column_delta = column_number(target_match.group("column")) - column_number(anchor_match.group("column"))

    def replace(match: re.Match[str]) -> str:
        column = match.group("column")
        row = int(match.group("row"))
        if not match.group("abs_col"):
            column = column_name(column_number(column) + column_delta)
        if not match.group("abs_row"):
            row += row_delta
        return f"{match.group('abs_col')}{column}{match.group('abs_row')}{row}"

    return FORMULA_REFERENCE_RE.sub(replace, formula)


def change_ratio(first: Path, second: Path) -> float:
    left = first.read_bytes()
    right = second.read_bytes()
    length = max(len(left), len(right), 1)
    changed = sum(a != b for a, b in itertools.zip_longest(left, right, fillvalue=-1))
    return changed / length


class XlsxReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.archive = zipfile.ZipFile(path)
        self.shared_strings = self._shared_strings()
        self.sheet_paths = self._sheet_paths()
        self.fonts, self.cell_fonts = self._styles()

    def close(self) -> None:
        self.archive.close()

    def _shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self.archive.namelist():
            return []
        root = ET.fromstring(self.archive.read("xl/sharedStrings.xml"))
        return ["".join(item.itertext()) for item in root.findall("x:si", NS)]

    def _sheet_paths(self) -> dict[str, str]:
        workbook = ET.fromstring(self.archive.read("xl/workbook.xml"))
        rels = ET.fromstring(self.archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in rels.findall(f"{{{PKG_REL_NS}}}Relationship")
        }
        result: dict[str, str] = {}
        for sheet in workbook.findall("x:sheets/x:sheet", NS):
            rel_id = sheet.attrib[f"{{{DOC_REL_NS}}}id"]
            target = targets[rel_id].lstrip("/")
            if not target.startswith("xl/"):
                target = posixpath.normpath(posixpath.join("xl", target))
            result[sheet.attrib["name"]] = target
        return result

    def _styles(self) -> tuple[list[tuple[str, float]], list[int]]:
        root = ET.fromstring(self.archive.read("xl/styles.xml"))
        fonts: list[tuple[str, float]] = []
        for font in root.findall("x:fonts/x:font", NS):
            name = font.find("x:name", NS)
            size = font.find("x:sz", NS)
            fonts.append(
                (
                    "" if name is None else name.attrib.get("val", ""),
                    0.0 if size is None else float(size.attrib.get("val", 0)),
                )
            )
        cell_fonts = [
            int(xf.attrib.get("fontId", 0))
            for xf in root.findall("x:cellXfs/x:xf", NS)
        ]
        return fonts, cell_fonts

    def sheet_xml(self, name: str) -> ET.Element:
        return ET.fromstring(self.archive.read(self.sheet_paths[name]))

    def cells(self, name: str) -> dict[str, Cell]:
        root = self.sheet_xml(name)
        result: dict[str, Cell] = {}
        shared_formulas: dict[str, tuple[str, str]] = {}
        for element in root.findall(".//x:sheetData/x:row/x:c", NS):
            reference = element.attrib["r"]
            cell_type = element.attrib.get("t", "n")
            style = int(element.attrib.get("s", 0))
            formula_element = element.find("x:f", NS)
            formula = ""
            if formula_element is not None:
                formula = formula_element.text or ""
                if formula_element.attrib.get("t") == "shared":
                    shared_id = formula_element.attrib.get("si", "")
                    if formula:
                        shared_formulas[shared_id] = (reference, formula)
                    elif shared_id in shared_formulas:
                        anchor, anchor_formula = shared_formulas[shared_id]
                        formula = translate_shared_formula(anchor_formula, anchor, reference)
            if cell_type == "inlineStr":
                inline = element.find("x:is", NS)
                value: object = "" if inline is None else "".join(inline.itertext())
            else:
                value_element = element.find("x:v", NS)
                raw = "" if value_element is None else (value_element.text or "")
                if cell_type == "s" and raw:
                    value = self.shared_strings[int(raw)]
                elif cell_type in ("str", "b"):
                    value = raw
                elif raw:
                    try:
                        value = float(raw)
                        if value.is_integer():
                            value = int(value)
                    except ValueError:
                        value = raw
                else:
                    value = ""
            result[reference] = Cell(value=value, formula=formula, style=style)
        return result

    def hidden_data_rows(self, name: str) -> int:
        root = self.sheet_xml(name)
        return sum(
            1
            for row in root.findall(".//x:sheetData/x:row", NS)
            if 2 <= int(row.attrib["r"]) <= 81 and row.attrib.get("hidden") == "1"
        )

    def filter_details(self, name: str) -> tuple[str, list[tuple[str, str, str]]]:
        root = self.sheet_xml(name)
        auto_filter = root.find("x:autoFilter", NS)
        if auto_filter is None:
            return "", []
        details: list[tuple[str, str, str]] = []
        for column in auto_filter.findall("x:filterColumn", NS):
            column_id = column.attrib.get("colId", "")
            for item in column.findall(".//x:customFilter", NS):
                details.append(
                    (column_id, item.attrib.get("operator", "equal"), item.attrib.get("val", ""))
                )
        return auto_filter.attrib.get("ref", ""), details

    def font_for_cell(self, cell: Cell) -> tuple[str, float]:
        if cell.style >= len(self.cell_fonts):
            return "", 0.0
        font_id = self.cell_fonts[cell.style]
        if font_id >= len(self.fonts):
            return "", 0.0
        return self.fonts[font_id]


def required_files(paths: list[Path]) -> None:
    for path in paths:
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Required verification artifact is missing: {path}")


def normalize_formula(formula: str) -> str:
    return formula.lstrip("=").replace("$", "").upper()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--numeric-checkpoint", required=True, type=Path)
    parser.add_argument("--text-checkpoint", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--initial", required=True, type=Path)
    parser.add_argument("--numeric-shot", required=True, type=Path)
    parser.add_argument("--text-shot", required=True, type=Path)
    parser.add_argument("--scroll-down", required=True, type=Path)
    parser.add_argument("--scroll-up", required=True, type=Path)
    parser.add_argument("--last-cell", required=True, type=Path)
    parser.add_argument("--first-cell", required=True, type=Path)
    parser.add_argument("--maximized", required=True, type=Path)
    parser.add_argument("--tab-closed", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    files = [
        args.source,
        args.numeric_checkpoint,
        args.text_checkpoint,
        args.result,
        args.trace,
        args.initial,
        args.numeric_shot,
        args.text_shot,
        args.scroll_down,
        args.scroll_up,
        args.last_cell,
        args.first_cell,
        args.maximized,
        args.tab_closed,
    ]
    required_files(files)

    with args.trace.open(encoding="utf-8", newline="") as source:
        completed = [row for row in csv.DictReader(source) if row["phase"] == "end"]
    failures = [row for row in completed if row["status"] != "success"]
    labels = {row["label"] for row in completed}
    required_labels = {
        "WPS_EXCEL_S1_LAUNCH",
        "WPS_EXCEL_S2_OPEN_FILE",
        "WPS_EXCEL_S2_ASSERT_WORKBOOK_WINDOW",
        "WPS_EXCEL_S3_APPLY_NUMERIC_FILTER",
        "WPS_EXCEL_S3_CANCEL_NUMERIC_FILTER",
        "WPS_EXCEL_S4_APPLY_TEXT_FILTER",
        "WPS_EXCEL_S4_CANCEL_TEXT_FILTER",
        "WPS_EXCEL_S5_SORT_ASCENDING",
        "WPS_EXCEL_S6_INSERT_COPIED_COLUMN",
        "WPS_EXCEL_S7_FILL_FORMULA_DOWN",
        "WPS_EXCEL_S8_NEW_SHEET",
        "WPS_EXCEL_S8_PASTE_TO_NEW_SHEET",
        "WPS_EXCEL_S9_APPLY_FONT_SIZE",
        "WPS_EXCEL_S10_SCROLL_DOWN_3",
        "WPS_EXCEL_S10_SCROLL_UP_3",
        "WPS_EXCEL_S11_LAST_CELL",
        "WPS_EXCEL_S12_FIRST_CELL",
        "WPS_EXCEL_S13_WAIT_10_SECONDS",
        "WPS_EXCEL_S14_CLOSE_TAB",
        "WPS_EXCEL_S15_CLOSE_WPS",
    }

    numeric = XlsxReader(args.numeric_checkpoint)
    text = XlsxReader(args.text_checkpoint)
    result = XlsxReader(args.result)
    try:
        numeric_ref, numeric_filters = numeric.filter_details("原始数据")
        text_ref, text_filters = text.filter_details("原始数据")
        numeric_hidden = numeric.hidden_data_rows("原始数据")
        text_hidden = text.hidden_data_rows("原始数据")

        sheet_names = list(result.sheet_paths)
        original_cells = result.cells("原始数据")
        new_sheet_name = next((name for name in sheet_names if name.lower().startswith("sheet")), "")
        new_cells = result.cells(new_sheet_name) if new_sheet_name else {}
        scores = [original_cells[f"B{row}"].value for row in range(2, 82)]
        formulas_ok = all(
            normalize_formula(original_cells[f"L{row}"].formula) == f"A{row}+B{row}"
            for row in range(2, 82)
        )
        compared_refs = [
            f"{column}{row}"
            for row in range(1, 82)
            for column in "ABCDEFGHIJKLMN"
        ]
        copied_sheet_matches = all(
            (original_cells.get(ref, Cell("", "", 0)).value,
             normalize_formula(original_cells.get(ref, Cell("", "", 0)).formula))
            ==
            (new_cells.get(ref, Cell("", "", 0)).value,
             normalize_formula(new_cells.get(ref, Cell("", "", 0)).formula))
            for ref in compared_refs
        )
        used_new_cells = [cell for ref, cell in new_cells.items() if re.fullmatch(r"[A-N](?:[1-9]|[1-7][0-9]|8[01])", ref)]
        font_pairs = [result.font_for_cell(cell) for cell in used_new_cells]
        songti_font_ok = bool(font_pairs) and all(
            name in {"Noto Serif CJK SC", "SimSun", "宋体"} and abs(size - 14) < 0.01
            for name, size in font_pairs
        )
    finally:
        numeric.close()
        text.close()
        result.close()

    numeric_visual_change = change_ratio(args.initial, args.numeric_shot)
    text_visual_change = change_ratio(args.initial, args.text_shot)
    scroll_change = change_ratio(args.scroll_down, args.scroll_up)
    first_last_change = change_ratio(args.first_cell, args.last_cell)
    tab_close_change = change_ratio(args.maximized, args.tab_closed)

    checks = {
        "NO_FAILED_ACTIONS": not failures,
        "ALL_S1_TO_S15_ACTIONS_COMPLETED": required_labels <= labels,
        "WORKBOOK_WINDOW_OPENED_BEFORE_OPERATIONS": (
            "WPS_EXCEL_S2_OPEN_FILE" in labels
            and "WPS_EXCEL_S2_ASSERT_WORKBOOK_WINDOW" in labels
        ),
        "NUMERIC_FILTER_GREATER_EQUAL_ZERO_VISIBLY_APPLIED": (
            "WPS_EXCEL_S3_GREATER_EQUAL_CONDITION" in labels
            and "WPS_EXCEL_S3_NUMERIC_VALUE" in labels
            and numeric_visual_change >= 0.04
        ),
        "NUMERIC_FILTER_51_ROW_RESULT_VISIBLY_CAPTURED": (
            "WPS_EXCEL_CAPTURE_NUMERIC_FILTER" in labels
            and numeric_visual_change >= 0.04
        ),
        "TEXT_FILTER_CONTAINS_KEY_SAVED": any(
            col == "2" and "key" in val.lower() for col, _op, val in text_filters
        ),
        "TEXT_FILTER_SHOWS_20_ROWS": text_hidden == 60,
        "FILTER_SCREENSHOTS_VISIBLY_CHANGED": numeric_visual_change >= 0.02 and text_visual_change >= 0.02,
        "NUMERIC_COLUMN_SORTED_ASCENDING": scores == sorted(scores),
        "COLUMN_10_COPIED_AND_INSERTED": original_cells["J1"].value == "备注" and original_cells["K1"].value == "备注",
        "BLANK_COLUMN_INSERTED_WITH_SUM_HEADER": original_cells["L1"].value == "两列之和",
        "SUM_FORMULA_FILLED_TO_ALL_80_ROWS": formulas_ok,
        "NEW_SHEET_CREATED_AND_CONTENT_COPIED": len(sheet_names) >= 3 and copied_sheet_matches,
        "NEW_SHEET_FONT_IS_SONGTI_14": songti_font_ok,
        "SCROLL_DOWN_AND_UP_VISIBLY_DIFFER": scroll_change >= 0.01,
        "FIRST_AND_LAST_CELL_VISIBLY_DIFFER": first_last_change >= 0.02,
        "TAB_CLOSE_VISIBLY_CHANGED_UI": tab_close_change >= 0.10,
    }
    lines = [
        f"NUMERIC_FILTER_REF={numeric_ref}",
        f"NUMERIC_HIDDEN_ROWS={numeric_hidden}",
        f"TEXT_FILTER_REF={text_ref}",
        f"TEXT_HIDDEN_ROWS={text_hidden}",
        f"SHEET_NAMES={','.join(sheet_names)}",
        f"NUMERIC_FILTER_VISUAL_CHANGE={numeric_visual_change:.6f}",
        f"TEXT_FILTER_VISUAL_CHANGE={text_visual_change:.6f}",
        f"SCROLL_CHANGE_RATIO={scroll_change:.6f}",
        f"FIRST_LAST_CHANGE_RATIO={first_last_change:.6f}",
        f"TAB_CLOSE_CHANGE_RATIO={tab_close_change:.6f}",
        f"COMPLETED_ACTIONS={len(completed)}",
    ]
    lines.extend(f"{name}={str(passed).lower()}" for name, passed in checks.items())
    output = "\n".join(lines) + "\n"
    print(output, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
