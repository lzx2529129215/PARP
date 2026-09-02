import copy
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = ROOT / "test/test/parp-real-pc-experiment-lzx.py"
ASSET_PATH = ROOT / "test/automation/create_real_pc_assets_lzx.py"
CONFIG_PATH = ROOT / "test/test/parp-real-interaction-config-lzx.json"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = load_module("parp_real_interaction_test", RUNNER_PATH)
ASSETS = load_module("parp_real_interaction_assets_test", ASSET_PATH)


class RealInteractionScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = RUNNER.load_config(CONFIG_PATH)

    def generate(self, name: str, root: Path):
        return RUNNER.generate_scenario(
            self.config, name, root / name, Path("/sys/fs/cgroup/fake"),
            20260831, False, 2, "trace-unit",
        )

    def test_all_seven_scenarios_use_only_application_native_working_sets(self) -> None:
        forbidden = (
            "memory-fixture-lzx.py", "reclaim-substitution-fixture-lzx.py",
            "oom_threshold_pressure_lzx.py", "TOUCH_FILE", "TOUCH_CLEAN",
            "REDIRTY", "MADV_COLD",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in self.config["scenarios"]:
                scenario = self.generate(name, root)
                commands = "\n".join(
                    str(action.get("command", "")) for action in scenario["actions"]
                )
                self.assertFalse(scenario["metadata"]["synthetic_app_working_set"])
                self.assertEqual(
                    scenario["metadata"]["working_set_kind"], "application_native_ui",
                )
                self.assertEqual(
                    scenario["metadata"]["pressure_kind"],
                    "memory_reclaim_of_application_native_working_sets",
                )
                self.assertIn("app-native-reclaim", commands)
                for token in forbidden:
                    self.assertNotIn(token, commands, f"{name} contains {token}")

    def test_prepare_and_reuse_are_real_ui_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            r1 = self.generate("r1_app_cold_retire", root)
            r2 = self.generate("r2_app_predicted_return", root)
            r3 = self.generate("r3_app_source_distribution", root)
        prepared = {
            action.get("app_key") for action in r1["actions"]
            if str(action.get("label", "")).startswith("REAL_APP_PREPARE_")
            and action.get("metadata", {}).get("working_set_origin") == "application_ui"
        }
        self.assertEqual(prepared, set(self.config["apps"]))
        self.assertFalse(any(
            str(action.get("label", "")).startswith("REAL_REUSE")
            for action in r1["actions"]
        ))
        r2_reuse = {
            action.get("app_key") for action in r2["actions"]
            if str(action.get("label", "")).startswith("REAL_REUSE")
            and action.get("app_key")
        }
        self.assertEqual(r2_reuse, {"FIREFOX"})
        r3_reuse = {
            action.get("app_key") for action in r3["actions"]
            if str(action.get("label", "")).startswith("REAL_REUSE")
            and action.get("app_key")
        }
        self.assertEqual(r3_reuse, {"FIREFOX", "THUNDERBIRD", "VLC"})

    def test_dirty_and_writeback_gates_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            r4 = self.generate("r4_app_dirty_substitution", root)
            r5 = self.generate("r5_app_writeback_gate", root)
        r4_commands = "\n".join(str(a.get("command", "")) for a in r4["actions"])
        r5_commands = "\n".join(str(a.get("command", "")) for a in r5["actions"])
        self.assertIn("app-native-gate", r4_commands)
        self.assertIn("--require-dirty", r4_commands)
        self.assertIn("--minimum-cold-costly-mib 384", r4_commands)
        self.assertIn("app-native-gate", r5_commands)
        self.assertIn("--require-dirty", r5_commands)
        self.assertIn("--minimum-cold-costly-mib 384", r5_commands)
        self.assertIn("writeback-gate", r5_commands)
        self.assertNotIn("writeback-gate", r4_commands)

    def test_invalid_native_threshold_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.config)
        invalid["app_native"]["minimum_total_working_set_mib"] = 0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "working-set gate"):
                RUNNER.load_config(path)

    def test_odt_asset_is_native_editable_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.odt"
            ASSETS.write_odt(path, "offline application content ", paragraphs=3)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(
                    archive.read("mimetype"),
                    b"application/vnd.oasis.opendocument.text",
                )
                self.assertIn(b"PARP section 3", archive.read("content.xml"))

    def test_action_plan_hash_is_path_and_policy_transport_independent(self) -> None:
        manifest = {
            "schema_version": 3,
            "assets": {
                "local-page.html": {"sha256": "abc"},
                "serial-reuse.html": {"sha256": "def"},
            },
        }
        for name in (
            "r2_app_predicted_return", "r6_app_serial_major_reuse",
            "r7_app_fairness_misprediction",
        ):
            with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
                scenario_native = RUNNER.generate_scenario(
                    self.config, name, Path(first), Path("/sys/fs/cgroup/native"),
                    20260831, False, 2, "trace-native",
                )
                scenario_apply = RUNNER.generate_scenario(
                    self.config, name, Path(second), Path("/sys/fs/cgroup/apply"),
                    20260831, True, 3, "trace-apply",
                )
                native_plan = RUNNER.action_plan_payload(
                    self.config, name, 20260831, scenario_native, manifest,
                )
                apply_plan = RUNNER.action_plan_payload(
                    self.config, name, 20260831, scenario_apply, manifest,
                )
            self.assertEqual(native_plan["sha256"], apply_plan["sha256"], name)
            self.assertEqual(native_plan["actions"], apply_plan["actions"], name)

    def test_r6_uses_browser_main_thread_serial_page_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = self.generate("r6_app_serial_major_reuse", Path(directory))
        actions = scenario["actions"]
        serial = self.config["serial_reuse"]
        expected_steps = serial["allocation_mib"] // serial["chunk_mib"]
        requests = [
            action for action in actions
            if "REAL_REUSE_SERIAL_STEP_" in str(action.get("label", ""))
            and "_REQUEST_" in str(action.get("label", ""))
        ]
        ready = [
            action for action in actions
            if "REAL_REUSE_SERIAL_STEP_" in str(action.get("label", ""))
            and "_READY_" in str(action.get("label", ""))
        ]
        self.assertEqual(len(requests), expected_steps)
        self.assertEqual(len(ready), expected_steps)
        self.assertTrue(all(action["type"] == "click_window" for action in requests))
        self.assertTrue(all(action["type"] == "wait_window_title" for action in ready))
        self.assertTrue(any(
            action.get("type") == "launch"
            and action.get("app_key") == "FIREFOX"
            and "serial-reuse.html" in str(action.get("command", ""))
            for action in actions
        ))
        reclaim_commands = "\n".join(
            str(action.get("command", "")) for action in actions
        )
        self.assertIn("--swappiness max", reclaim_commands)

    def test_r7_has_warm_fairness_baseline_and_designed_wrong_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scenario = self.generate("r7_app_fairness_misprediction", Path(directory))
        participants = set(self.config["fairness_misprediction"]["participants"])
        warm = {
            action.get("app_key") for action in scenario["actions"]
            if str(action.get("label", "")).startswith("REAL_FAIR_WARM_")
            and "_READY_" in str(action.get("label", ""))
        }
        post = {
            action.get("app_key") for action in scenario["actions"]
            if str(action.get("label", "")).startswith("REAL_REUSE_FAIR_")
            and "_READY_" in str(action.get("label", ""))
        }
        self.assertEqual(warm, participants)
        self.assertEqual(post, participants)
        fairness_ready = [
            action for action in scenario["actions"]
            if "_READY_" in str(action.get("label", ""))
            and (
                str(action.get("label", "")).startswith("REAL_FAIR_WARM_")
                or str(action.get("label", "")).startswith("REAL_REUSE_FAIR_")
            )
        ]
        self.assertTrue(fairness_ready)
        self.assertTrue(all(
            action.get("type") == "wait_cgroup_pagein_stable"
            and action.get("metadata", {}).get("latency_endpoint")
            == "application_cgroup_pagein_stable"
            for action in fairness_ready
        ))
        self.assertGreaterEqual(sum(
            action.get("app_key") == "GIMP"
            and action.get("type") == "type"
            and action.get("text") == "Invert"
            for action in scenario["actions"]
        ), 12)
        reclaim_commands = "\n".join(
            str(action.get("command", "")) for action in scenario["actions"]
        )
        self.assertIn("--swappiness max", reclaim_commands)
        self.assertIn(
            self.config["fairness_misprediction"]["unexpected_reuse_app"], post,
        )

    def test_latency_parser_subtracts_script_pacing_and_measures_visual_ready(self) -> None:
        scenario = {
            "actions": [
                {"type": "switch", "label": "REAL_REUSE_APP_01_SWITCH_FIREFOX"},
                {"type": "wait", "seconds": 0.2, "label": "REAL_REUSE_APP_01_DWELL_FIREFOX"},
                {
                    "type": "key", "repeat": 3, "interval": 0.1,
                    "label": "REAL_REUSE_APP_01_REUSE_01_FIREFOX",
                },
                {"type": "wait_visual_stable", "label": "REAL_REUSE_APP_01_READY_FIREFOX"},
            ],
        }
        fields = ["step_id", "label", "phase", "ts_ns", "status", "app_key", "action"]
        rows = [
            (1, "REAL_REUSE_APP_01_SWITCH_FIREFOX", "start", 0, "running", "FIREFOX", "switch"),
            (1, "REAL_REUSE_APP_01_SWITCH_FIREFOX", "end", 100_000_000, "success", "FIREFOX", "switch"),
            (2, "REAL_REUSE_APP_01_DWELL_FIREFOX", "start", 100_000_000, "running", "FIREFOX", "wait"),
            (2, "REAL_REUSE_APP_01_DWELL_FIREFOX", "end", 300_000_000, "success", "FIREFOX", "wait"),
            (3, "REAL_REUSE_APP_01_REUSE_01_FIREFOX", "start", 300_000_000, "running", "FIREFOX", "key"),
            (3, "REAL_REUSE_APP_01_REUSE_01_FIREFOX", "end", 550_000_000, "success", "FIREFOX", "key"),
            (4, "REAL_REUSE_APP_01_READY_FIREFOX", "start", 550_000_000, "running", "FIREFOX", "wait_visual_stable"),
            (4, "REAL_REUSE_APP_01_READY_FIREFOX", "end", 700_000_000, "success", "FIREFOX", "wait_visual_stable"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.csv"
            with trace.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(dict(zip(fields, row)) for row in rows)
            result = RUNNER.parse_action_latencies(trace, scenario)
        self.assertAlmostEqual(result["successful_total_ms"], 500.0)
        self.assertAlmostEqual(result["successful_net_total_ms"], 300.0)
        self.assertAlmostEqual(result["responsive_spans"][0]["gross_ms"], 700.0)
        self.assertAlmostEqual(
            result["responsive_spans"][0]["net_responsive_ms"], 300.0,
        )

    def test_r7_pagein_wait_parser_separates_guard_from_recovery(self) -> None:
        scenario = {
            "actions": [
                {
                    "type": "wait_cgroup_pagein_stable",
                    "minimum_wait_seconds": 0.2,
                    "label": "REAL_FAIR_WARM_04_READY_GIMP",
                },
                {
                    "type": "wait_cgroup_pagein_stable",
                    "minimum_wait_seconds": 0.2,
                    "label": "REAL_REUSE_FAIR_04_READY_GIMP",
                },
            ],
        }
        fields = ["step_id", "label", "phase", "ts_ns", "status"]
        rows = [
            (1, "REAL_FAIR_WARM_04_READY_GIMP", "start", 0, "running"),
            (1, "REAL_FAIR_WARM_04_READY_GIMP", "end", 250_000_000, "success"),
            (2, "REAL_REUSE_FAIR_04_READY_GIMP", "start", 300_000_000, "running"),
            (2, "REAL_REUSE_FAIR_04_READY_GIMP", "end", 1_000_000_000, "success"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "trace.csv"
            with trace.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(dict(zip(fields, row)) for row in rows)
            parsed = RUNNER.parse_fairness_pagein_waits(trace, scenario)
        self.assertAlmostEqual(
            parsed["REAL_FAIR_WARM_04"]["net_pagein_wait_ms"], 50.0,
        )
        self.assertAlmostEqual(
            parsed["REAL_REUSE_FAIR_04"]["net_pagein_wait_ms"], 500.0,
        )

    def test_serial_reuse_asset_exposes_deterministic_ready_titles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "serial-reuse.html"
            ASSETS.write_serial_reuse_html(path)
            text = path.read_text(encoding="utf-8")
        self.assertIn("window.parpSerialBuffers", text)
        self.assertIn("PARP R6 READY", text)
        self.assertIn("PARP R6 STEP", text)


if __name__ == "__main__":
    unittest.main()
