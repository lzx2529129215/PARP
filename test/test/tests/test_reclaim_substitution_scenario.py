import argparse
import importlib.util
import tempfile
import unittest
from pathlib import Path


TEST_DIR = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReclaimSubstitutionScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_module(
            "parp_real_pc_substitution_test",
            TEST_DIR / "parp-real-pc-experiment-lzx.py",
        )
        cls.fixture_module = load_module(
            "parp_reclaim_substitution_fixture_test",
            TEST_DIR / "reclaim-substitution-fixture-lzx.py",
        )

    def test_capacity_contract_and_action_order(self):
        config = self.runner.load_config(
            TEST_DIR / "parp-cold-dirty-preserve-config-lzx.json"
        )
        layout = config["substitution_layout"]
        cold_clean = sum(layout[app]["clean_mib"] for app in config["cold_apps"])
        cold_dirty = sum(layout[app]["dirty_mib"] for app in config["cold_apps"])
        hot_clean = sum(layout[app]["clean_mib"] for app in config["hot_apps"])
        target = config["reclaim_target_mib"]
        self.assertLess(cold_clean, target)
        self.assertGreaterEqual(cold_clean + cold_dirty, target)
        self.assertGreaterEqual(cold_clean + hot_clean, target)

        with tempfile.TemporaryDirectory() as directory:
            scenario = self.runner.generate_scenario(
                config, "cold_dirty_preserve_hot_clean", Path(directory),
                Path("/sys/fs/cgroup/user.slice"), 20260830, False, 3,
                "parp-substitution-unit",
            )
        labels = [action.get("label", "") for action in scenario["actions"]]
        self.assertEqual(sum("SUBSTITUTION_COLDIFY" in value for value in labels), 8)
        self.assertEqual(sum("SUBSTITUTION_REDIRTY" in value for value in labels), 5)
        self.assertEqual(sum("RESIDENT_BEFORE" in value for value in labels), 8)
        self.assertEqual(sum("RESIDENT_AFTER" in value for value in labels), 8)
        self.assertEqual(sum("_CLEAN_" in value and "SNAPSHOT" not in value for value in labels), 3)
        self.assertEqual(sum("_WARM_" in value and "SNAPSHOT" not in value for value in labels), 3)
        penultimate_switch = labels.index("REAL_TRAINED_04_SWITCH_FIREFOX")
        coldify_indexes = [
            index for index, value in enumerate(labels)
            if "SUBSTITUTION_COLDIFY" in value
        ]
        self.assertTrue(all(index < penultimate_switch for index in coldify_indexes))
        self.assertGreater(min(coldify_indexes), labels.index("REAL_TRAINED_HOT_03_THUNDERBIRD"))
        profile_mark = labels.index("REAL_PREDICTION_PROFILE_MARK")
        final_switch = labels.index("REAL_TRAINED_05_SWITCH_VLC")
        self.assertLess(max(
            index for index, value in enumerate(labels)
            if "SUBSTITUTION_REDIRTY" in value
        ), profile_mark)
        self.assertLess(profile_mark, final_switch)
        pressure_index = labels.index("REAL_PRESSURE_HOLDING")
        first_reuse = next(
            index for index, value in enumerate(labels)
            if "REUSE_SUBSTITUTION" in value and "SWITCH" in value
        )
        self.assertGreater(first_reuse, pressure_index)

    def test_fixture_creates_separate_resident_regions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(
                app="FIREFOX", socket=str(root / "fixture.sock"),
                log=str(root / "fixture.csv"), clean_file=str(root / "clean.data"),
                dirty_file=str(root / "dirty.data"), hot_file=str(root / "hot.data"),
                clean_bytes=2 * 1024 * 1024, dirty_bytes=2 * 1024 * 1024,
                hot_bytes=1024 * 1024,
            )
            fixture = self.fixture_module.Fixture(args)
            try:
                fixture.start()
                self.assertTrue(fixture.prepare().startswith("OK "))
                self.assertTrue(fixture.coldify().startswith("OK "))
                self.assertTrue(fixture.redirty().startswith("OK "))
                status = fixture.residency("RESIDENCY_BEFORE")
                self.assertIn("clean_resident_pages=512", status)
                self.assertIn("dirty_resident_pages=512", status)
                self.assertIn("hot_resident_pages=256", status)
                self.assertIn("touched_bytes=2097152", fixture.handle("TOUCH_CLEAN"))
                self.assertIn("touched_bytes=2097152", fixture.handle("TOUCH_CLEAN_WARM"))
            finally:
                fixture.close()

    def test_writeback_gate_scenario_enforces_targeted_pressure_contract(self):
        config = self.runner.load_config(
            TEST_DIR / "parp-cold-writeback-gate-config-lzx.json"
        )
        layout = config["substitution_layout"]
        cold_clean = sum(layout[app]["clean_mib"] for app in config["cold_apps"])
        cold_dirty = sum(layout[app]["dirty_mib"] for app in config["cold_apps"])
        hot_clean = sum(layout[app]["clean_mib"] for app in config["hot_apps"])
        target = config["reclaim_target_mib"]
        self.assertLess(cold_clean, target)
        self.assertGreaterEqual(cold_clean + cold_dirty, target)
        self.assertGreaterEqual(cold_clean + hot_clean, target)
        self.assertGreater(config["reclaim_writeback_gate"]["laptop_mode"], 0)
        self.assertEqual(config["workload_profile_miss_budget"], 16)
        self.assertEqual(config["unexpected_workload_pass_budget"], 16)
        self.assertEqual(config["post_pressure_settle_seconds"], 0.0)

        with tempfile.TemporaryDirectory() as directory:
            scenario = self.runner.generate_scenario(
                config, "cold_writeback_gate_hot_reuse", Path(directory),
                Path("/sys/fs/cgroup/user.slice"), 20260830, True, 3,
                "parp-writeback-gate-unit", require_workload_profiles=True,
            )
        labels = [action.get("label", "") for action in scenario["actions"]]
        gate_index = labels.index("REAL_WRITEBACK_GATE_EVIDENCE")
        pressure_index = labels.index("REAL_PRESSURE_LAUNCH")
        self.assertLess(gate_index, pressure_index)
        prediction_gate = labels.index("REAL_PREDICTION_GATE")
        final_redirty = [
            index for index, label in enumerate(labels)
            if "WRITEBACK_GATE_FINAL_REDIRTY" in label
        ]
        first_residency = min(
            index for index, label in enumerate(labels)
            if "RESIDENT_BEFORE" in label
        )
        self.assertEqual(len(final_redirty), 5)
        self.assertTrue(all(prediction_gate < index < first_residency for index in final_redirty))
        self.assertIn("REAL_SET_PRESSURE_BOUNDARY", labels)
        self.assertNotIn("REAL_SET_GLOBAL_PRESSURE_BOUNDARY", labels)
        pressure_action = next(
            action for action in scenario["actions"]
            if action.get("label") == "REAL_PRESSURE_LAUNCH"
        )
        self.assertIn("--target-mib", pressure_action["command"])
        self.assertIn("--reclaim-probe-mib 0", pressure_action["command"])
        self.assertEqual(
            scenario["metadata"]["pressure_kind"],
            "controlled_memcg_allocator",
        )


if __name__ == "__main__":
    unittest.main()
