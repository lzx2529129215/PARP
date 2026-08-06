from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from automation.app_automation import Context, TraceWriter, load_scenario, quiet_window_trace, trace_marker, verify_foreground
from automation.semantic.compiler import CompileError, compile_scenario, load_operations


OPERATIONS = ROOT / "automation/semantic/operations"
ASSETS = ROOT / "automation/semantic/assets/assets_manifest.example.json"


class SemanticAutomationTests(unittest.TestCase):
    def test_semantic_operation_schema(self) -> None:
        catalog = load_operations(OPERATIONS)
        self.assertIn("BROWSER_LAUNCH", catalog)
        self.assertIn("WPS_LAUNCH", catalog)
        self.assertIn("WECHAT_SEND_TEXT", catalog)
        self.assertIn("BILIBILI_UPLOAD_VIDEO", catalog)

    def test_semantic_scenario_schema(self) -> None:
        result = compile_scenario(ROOT / "automation/semantic/scenarios/scenario_browser_multitab.json", OPERATIONS, asset_manifest_path=ASSETS)
        self.assertEqual(result.scenario_id, "scenario_browser_multitab")

    def test_compile_operation_reference_and_parameter_substitution(self) -> None:
        result = compile_scenario(ROOT / "automation/semantic/scenarios/scenario_browser_multitab.json", OPERATIONS, asset_manifest_path=ASSETS)
        command = next(action["command"] for action in result.compiled["actions"] if action.get("type") == "shell")
        self.assertIn("firefox", command)
        self.assertTrue(any(action.get("text") == "https://tv.cctv.com/" for action in result.compiled["actions"]))

    def test_compile_missing_variable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Path(tmp) / "missing.json"
            scenario.write_text(json.dumps({"scenario_id": "missing", "phases": [{"operation_ref": "BROWSER_LAUNCH"}]}), encoding="utf-8")
            with self.assertRaisesRegex(CompileError, "browser_command"):
                compile_scenario(scenario, OPERATIONS, asset_manifest_path=ASSETS)

    def test_compile_missing_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Path(tmp) / "asset.json"
            scenario.write_text(json.dumps({"scenario_id": "asset", "phases": [{"operation_ref": "WPS_PASTE_IMAGE", "optional": True}]}), encoding="utf-8")
            with self.assertRaisesRegex(CompileError, "素材不可用"):
                compile_scenario(scenario, OPERATIONS, asset_manifest_path=ASSETS)

    def test_compile_inserts_operation_markers_and_deterministic_ids(self) -> None:
        path = ROOT / "automation/semantic/scenarios/scenario_browser_multitab.json"
        first = compile_scenario(path, OPERATIONS, asset_manifest_path=ASSETS)
        second = compile_scenario(path, OPERATIONS, asset_manifest_path=ASSETS)
        starts = [item for item in first.compiled["actions"] if item.get("type") == "trace_marker" and item.get("event_type") == "OP_START"]
        dones = [item for item in first.compiled["actions"] if item.get("type") == "trace_marker" and item.get("event_type") == "OP_DONE"]
        self.assertEqual(len(starts), len(dones))
        self.assertEqual([item["action_id"] for item in first.compiled["actions"]], [item["action_id"] for item in second.compiled["actions"]])

    def test_existing_scenario_backward_compatible(self) -> None:
        scenario = load_scenario(ROOT / "configs/automation/scenario_local_files.json")
        self.assertGreater(len(scenario.actions), 0)

    def test_trace_marker_no_side_effect_and_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "trace.csv"
            writer = TraceWriter(output, "test", "semantic")
            ctx = Context(dry_run=True, trace=writer, session_id="test", scenario_id="semantic")
            trace_marker(ctx, event_type="OP_START", status="running", action={"app_key": "BROWSER", "operation_id": "BROWSER_LAUNCH", "operation_name": "启动浏览器", "operation_domain": "BROWSER", "requested_operation": "BROWSER_LAUNCH", "side_effect_level": "NONE"}, op_type="semantic_operation")
            writer.close()
            with output.open(encoding="utf-8", newline="") as handle: rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["event_type"], "OP_START")
            self.assertEqual(rows[0]["operation_id"], "BROWSER_LAUNCH")
            self.assertEqual(rows[0]["app_key"], "BROWSER")

    def test_side_effect_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Path(tmp) / "external.json"
            scenario.write_text(json.dumps({"scenario_id": "external", "variables": {"test_account": "a", "test_recipient_allowlist": "b"}, "phases": [{"operation_ref": "WECHAT_SEND_TEXT"}]}), encoding="utf-8")
            with self.assertRaisesRegex(CompileError, "默认关闭"):
                compile_scenario(scenario, OPERATIONS, asset_manifest_path=ASSETS)
            result = compile_scenario(scenario, OPERATIONS, asset_manifest_path=ASSETS, allow_external_side_effects=True)
            self.assertTrue(result.side_effect_operations)

    def test_window_relative_profile_and_bilibili_web_contract(self) -> None:
        profile = json.loads((ROOT / "automation/semantic/profiles/bilibili_1920x1080.json").read_text(encoding="utf-8"))
        self.assertIn("x_ratio", profile["coordinates"]["home_card"])
        browser = load_operations(OPERATIONS)["BROWSER_OPEN_URL"]
        self.assertEqual(browser["app_key"], "BROWSER")

    def test_operation_failed_pairing_for_uncalibrated_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Path(tmp) / "uncalibrated.json"
            scenario.write_text(json.dumps({"scenario_id": "uncalibrated", "phases": [{"operation_ref": "BILIBILI_OPEN_HOT", "optional": True}]}), encoding="utf-8")
            result = compile_scenario(scenario, OPERATIONS, asset_manifest_path=ASSETS)
        ends = [item for item in result.compiled["actions"] if item.get("type") == "trace_marker" and item.get("operation_id") == "BILIBILI_OPEN_HOT"]
        self.assertEqual([item["event_type"] for item in ends], ["OP_START", "OP_FAILED", "OP_FAILED"])

    def test_runtime_app_ids_no_collision(self) -> None:
        scope = json.loads((ROOT / "configs/runtime/runtime_app_scope.json").read_text(encoding="utf-8"))
        ids = [item["app_id"] for item in scope["apps"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_smoke_scenario_has_no_external_side_effect_and_phase_switches(self) -> None:
        path = ROOT / "automation/semantic/scenarios/scenario_semantic_ui_smoke_safe.json"
        overrides = {
            "collection_output_dir": "/tmp/semantic_smoke/output",
            "files_command": "nautilus --new-window /tmp/semantic_smoke/output",
            "wps_output_path": "/tmp/semantic_smoke/output/test.docx",
            "browser_command": "firefox --new-window http://127.0.0.1:18080/page1.html",
        }
        result = compile_scenario(path, OPERATIONS, asset_manifest_path=ASSETS, variable_overrides=overrides)
        actions = result.compiled["actions"]
        self.assertFalse(result.side_effect_operations)
        self.assertTrue(any(item.get("event_type") == "PHASE_START" for item in actions))
        self.assertTrue(any(item.get("event_type") == "APP_SWITCH_START" for item in actions))
        self.assertFalse(any(item.get("event_type") == "APP_SWITCH_DONE" for item in actions))
        action_ids = [item.get("action_id") for item in actions if item.get("action_id")]
        self.assertEqual(len(action_ids), len(set(action_ids)))

    def test_compiler_reference_specific_parameters(self) -> None:
        result = compile_scenario(ROOT / "automation/semantic/scenarios/scenario_semantic_ui_smoke_safe.json", OPERATIONS, asset_manifest_path=ASSETS)
        urls = [item.get("text") for item in result.compiled["actions"] if item.get("operation_id") == "BROWSER_OPEN_URL" and item.get("type") == "type"]
        self.assertEqual(urls, ["http://127.0.0.1:18080/page1.html", "http://127.0.0.1:18080/page2.html", "http://127.0.0.1:18080/page3.html"])

    def test_verify_foreground_dry_run_does_not_query_desktop(self) -> None:
        verify_foreground({"app_key": "WPS"}, Context(dry_run=True))

    def test_quiet_window_trace_tolerates_destroyed_window_property(self) -> None:
        class Outcome:
            def __init__(self, code: int, output: str) -> None:
                self.returncode = code
                self.stdout = output

        with patch.dict("os.environ", {"DISPLAY": ":3"}, clear=False), \
             patch("automation.app_automation.shutil.which", return_value="/usr/bin/xdotool"), \
             patch("automation.app_automation.subprocess.run", side_effect=[
                 Outcome(0, "1048576\n"), Outcome(1, "_NET_WM_PID:  not found.\n"),
             ]), \
             patch("automation.app_automation.proc_meta_for_pid", return_value={}):
            self.assertEqual(quiet_window_trace({"name": "firefox"}), {"window_id": "1048576"})


if __name__ == "__main__":
    unittest.main()
