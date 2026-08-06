#!/usr/bin/env python3
"""编译语义自动化场景，不执行任何 UI 操作。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation.semantic.compiler import CompileError, compile_scenario, write_compile_result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="语义自动化场景编译器")
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--action-map-output", type=Path, default=None)
    parser.add_argument("--report-output", type=Path, default=None)
    parser.add_argument("--operations-dir", type=Path, default=ROOT / "automation/semantic/operations")
    parser.add_argument("--asset-manifest", type=Path, default=ROOT / "automation/semantic/assets/assets_manifest.example.json")
    parser.add_argument("--allow-external-side-effects", action="store_true")
    parser.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE", help="覆盖场景变量；VALUE 可为 JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    action_map = args.action_map_output or args.output.with_name("compiled_action_map.csv")
    report = args.report_output or args.output.with_name("compile_report.json")
    overrides = {}
    for item in args.overrides:
        if "=" not in item:
            print(f"compile error: --set 必须为 KEY=VALUE: {item}", file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        try:
            overrides[key] = json.loads(value)
        except json.JSONDecodeError:
            overrides[key] = value
    try:
        result = compile_scenario(
            args.scenario, args.operations_dir,
            asset_manifest_path=args.asset_manifest,
            allow_external_side_effects=args.allow_external_side_effects,
            variable_overrides=overrides,
        )
        write_compile_result(result, args.output, action_map, report)
    except CompileError as exc:
        print(f"compile error: {exc}", file=sys.stderr)
        return 2
    print(f"compiled={args.output} actions={len(result.compiled['actions'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
