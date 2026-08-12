import math
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

from phase210 import contracts as c


class Phase210Contracts(unittest.TestCase):
    def test_01_app_model_mapping(self):
        self.assertEqual(c.app_model("WPS"), "WPS_REUSE_RANKER")

    def test_02_wps_loads_wps(self):
        self.assertEqual(c.route("WPS", c.healthy()), "WPS_REUSE_RANKER")

    def test_03_files_loads_files(self):
        self.assertEqual(c.route("FILES", c.healthy()), "FILES_REUSE_RANKER")

    def test_04_qq_loads_qq(self):
        self.assertEqual(c.route("QQ", c.healthy()), "QQ_REUSE_RANKER")

    def test_05_wrong_mapping(self):
        self.assertEqual(c.WRONG_MODELS, {"WPS": "FILES_REUSE_RANKER", "FILES": "QQ_REUSE_RANKER", "QQ": "WPS_REUSE_RANKER"})

    def test_06_generic_fallback(self):
        self.assertEqual(c.route("WPS", c.healthy(model_exists=False)), "GENERIC_CROSS_APP_RANKER")

    def test_07_native_fallback(self):
        self.assertEqual(c.route("WPS", c.healthy(model_exists=False, generic_exists=False)), "NATIVE_MGLRU")

    def test_08_no_app_shortcut(self):
        self.assertTrue(c.features_allowed(c.BASE_FEATURES))
        self.assertFalse(c.features_allowed(c.BASE_FEATURES + ("app_id",)))

    def test_09_train_test_isolation(self):
        self.assertTrue(c.sessions_isolated({"a"}, {"b"}, {"c"}))
        self.assertFalse(c.sessions_isolated({"a"}, {"b"}, {"a"}))

    def test_10_ab_not_training(self):
        self.assertTrue(c.ab_excluded({"a"}, {"b"}))
        self.assertFalse(c.ab_excluded({"a"}, {"a"}))

    def test_11_candidate_hashes_equal(self):
        self.assertTrue(c.all_equal({"S0": "x", "S1": "x"}.values()))

    def test_12_reclaim_budgets_equal(self):
        self.assertFalse(c.all_equal([10, 11]))

    def test_13_rankings_differ(self):
        self.assertTrue(c.rankings_differ([1, 2], [2, 1]))

    def test_14_cgroup_only_test_processes(self):
        self.assertTrue(c.cgroup_members_safe({1, 2}, {1, 2}, set()))

    def test_15_no_target_escape(self):
        self.assertFalse(c.cgroup_members_safe({1, 2}, {1}, set()))

    def test_16_no_non_test_process(self):
        self.assertFalse(c.cgroup_members_safe({1}, {1, 2}, {2}))

    def test_17_memory_limits_restored(self):
        self.assertTrue(c.limits_restored(("max", "max"), ("max", "max")))

    def test_18_apply_parent_only(self):
        self.assertTrue(c.apply_scope_safe("/test.slice", "/test.slice"))

    def test_19_reorder_not_count(self):
        self.assertTrue(c.reorder_only([1, 2, 3], [3, 1, 2]))

    def test_20_native_no_model(self):
        self.assertFalse(c.model_should_run("NATIVE_MGLRU"))

    def test_21_timeout_fallback(self):
        self.assertEqual(c.route("WPS", c.healthy(score_timeout=True)), "GENERIC_CROSS_APP_RANKER")

    def test_22_schema_fallback(self):
        self.assertEqual(c.route("FILES", c.healthy(schema_ok=False)), "GENERIC_CROSS_APP_RANKER")

    def test_23_version_fallback(self):
        self.assertEqual(c.route("QQ", c.healthy(version_ok=False)), "GENERIC_CROSS_APP_RANKER")

    def test_24_ttl_fallback(self):
        self.assertEqual(c.route("WPS", c.healthy(ttl_ok=False)), "GENERIC_CROSS_APP_RANKER")

    def test_25_nan_overflow_fallback(self):
        self.assertEqual(c.route("WPS", c.healthy(score_finite=False)), "GENERIC_CROSS_APP_RANKER")

    def test_26_automation_no_real_data(self):
        self.assertTrue(c.automation_paths_safe(["/fixtures/run_01/doc.docx"], "/fixtures"))
        self.assertFalse(c.automation_paths_safe(["/home/user/Documents/private.docx"], "/fixtures"))

    def test_27_fixture_independence(self):
        self.assertTrue(c.fixtures_independent(["run_01", "run_02"]))

    def test_28_monotonic_timestamps(self):
        self.assertTrue(c.timestamps_monotonic([(1, 2), (2, 3)]))

    def test_29_refault_delta(self):
        self.assertEqual(c.counter_delta(10, 17), 7)

    def test_30_normalized_refault(self):
        self.assertEqual(c.normalized_refault(5, 100), 50.0)

    def test_31_psi_integration(self):
        self.assertAlmostEqual(c.trapezoid([(0, 0), (2, 2)]), 2.0)

    def test_32_oom_gate(self):
        self.assertFalse(c.safety_gate({"oom": 1, "oom_kill": 0}))

    def test_33_watchdog(self):
        self.assertTrue(c.watchdog({"oom_kill": 1}, p99_ratio=1, timeout_rate=0))

    def test_34_latin_square_deterministic(self):
        self.assertEqual(c.latin_square(5, 210), c.latin_square(5, 210))

    def test_35_bootstrap_deterministic(self):
        self.assertEqual(c.block_bootstrap([[1, 2], [3]], 20, 210), c.block_bootstrap([[1, 2], [3]], 20, 210))

    def test_36_test_not_thresholds(self):
        self.assertTrue(c.threshold_scope("validation"))
        self.assertFalse(c.threshold_scope("test"))

    def test_37_no_future(self):
        self.assertTrue(c.causal_feature_names(c.BASE_FEATURES))
        self.assertFalse(c.causal_feature_names(("future_reuse_60",)))

    def test_38_old_hashes_unchanged(self):
        self.assertTrue(c.manifests_equal({"a": "1"}, {"a": "1"}))

    def test_39_cleanup_no_residual(self):
        self.assertTrue(c.cleanup_complete({"processes": [], "scopes": [], "trace_instances": [], "apply": False}))

    def test_40_equal_not_better(self):
        self.assertFalse(c.strictly_better(1.0, 1.0, lower_is_better=True))

    def test_41_phase210_qq_actions_require_strict_window_match(self):
        builder_path = Path(__file__).resolve().parents[1] / "build_qq_collection.py"
        spec = importlib.util.spec_from_file_location("phase210_qq_builder", builder_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        actions = module.scenario("qq_train_01", 21001)["actions"]
        window_actions = [action for action in actions if action.get("class") == module.QQ_CLASS]
        self.assertTrue(window_actions)
        self.assertTrue(all(action.get("strict_window_match") is True for action in window_actions))

    def test_42_strict_qq_match_has_no_generic_fallback(self):
        automation_path = Path(__file__).resolve().parents[8] / "automation" / "app_automation.py"
        spec = importlib.util.spec_from_file_location("phase210_app_automation", automation_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=1, stdout="")

        with mock.patch.object(module.shutil, "which", return_value="/usr/bin/xdotool"), \
             mock.patch.object(module.subprocess, "run", side_effect=fake_run):
            self.assertEqual(module.list_window_candidates({
                "class": "^parp-phase210-qq$", "strict_window_match": True,
            }, "QQ"), [])
        self.assertEqual(calls, [[
            "xdotool", "search", "--onlyvisible", "--class", "^parp-phase210-qq$",
        ]])

        calls.clear()
        with mock.patch.object(module.shutil, "which", return_value="/usr/bin/xdotool"), \
             mock.patch.object(module.subprocess, "run", side_effect=fake_run):
            self.assertEqual(module.list_window_candidates({
                "class": "^parp-phase210-qq$", "strict_window_match": True,
                "include_hidden": True,
            }, "QQ"), [])
        self.assertEqual(calls, [[
            "xdotool", "search", "--class", "^parp-phase210-qq$",
        ]])

    def test_43_root_collector_enforces_dedicated_qq_leaf(self):
        collector = (Path(__file__).resolve().parents[1] / "qq_collection_root.sh").read_text()
        self.assertIn("parp-qq-targets", collector)
        self.assertIn("stable >= 10", collector)
        self.assertIn("QQ target escaped dedicated cgroup", collector)
        self.assertIn("non-test process entered QQ target cgroup", collector)
        self.assertIn("parp-control", collector)
        self.assertIn("QQ target leaf lacks memory controller", collector)
        self.assertIn("[[ -n $current_cgroup ]] || continue", collector)
        self.assertIn("failed to attach live QQ target", collector)

    def test_44_fixture_search_does_not_hide_qq_window(self):
        builder_path = Path(__file__).resolve().parents[1] / "build_qq_collection.py"
        spec = importlib.util.spec_from_file_location("phase210_qq_builder_no_escape", builder_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        actions = module.scenario("qq_train_01", 21001)["actions"]
        self.assertFalse(any(action.get("type") == "key" and action.get("key") == "Escape" for action in actions))

    def test_45_find_window_can_include_hidden_windows(self):
        automation_path = Path(__file__).resolve().parents[8] / "automation" / "app_automation.py"
        spec = importlib.util.spec_from_file_location("phase210_app_automation_hidden", automation_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        calls = []

        def fake_run(command, ctx, check=False):
            calls.append(command)
            return mock.Mock(returncode=0, stdout="42\n")

        with mock.patch.object(module, "require_xdotool"), mock.patch.object(module, "run", side_effect=fake_run):
            result = module.find_window({"class": "^parp-phase210-qq$", "include_hidden": True}, module.Context(dry_run=False))
        self.assertEqual(result, "42")
        self.assertEqual(calls, [["xdotool", "search", "--class", "^parp-phase210-qq$"]])

    def test_46_every_window_state_can_recover_hidden_qq(self):
        automation_path = Path(__file__).resolve().parents[8] / "automation" / "app_automation.py"
        source = automation_path.read_text()
        self.assertIn('search_action["include_hidden"] = True', source)
        self.assertIn('run(["xdotool", "windowmap", window_id], ctx, check=False)', source)

    def test_47_test_qq_window_is_visibly_placed(self):
        builder = (Path(__file__).resolve().parents[1] / "build_qq_collection.py").read_text()
        self.assertIn("PARP Phase2.10 QQ Test (Read-only)", builder)
        self.assertIn('xdotool windowsize \\"$wid\\" 900 700', builder)
        self.assertIn('xdotool windowmove \\"$wid\\" 120 80', builder)
        self.assertIn('xdotool windowactivate --sync \\"$wid\\"', builder)

    def test_48_all_test_qq_actions_rediscover_isolated_profile(self):
        builder_path = Path(__file__).resolve().parents[1] / "build_qq_collection.py"
        spec = importlib.util.spec_from_file_location("phase210_qq_builder_rediscovery", builder_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        actions = module.scenario("qq_train_01", 21001)["actions"]
        window_actions = [action for action in actions if action.get("class") == module.QQ_CLASS]
        self.assertTrue(all(action.get("include_hidden") is True for action in window_actions))
        self.assertTrue(all(action.get("pid_cmdline_contains") == "${QQ_PROFILE}/chromium" for action in window_actions))
        focus_actions = [action for action in window_actions if action.get("type") == "focus"]
        self.assertTrue(focus_actions)
        self.assertTrue(all(action.get("ensure_visible_geometry") is True for action in focus_actions))

    def test_49_focus_maps_and_places_rediscovered_window(self):
        automation_path = Path(__file__).resolve().parents[8] / "automation" / "app_automation.py"
        source = automation_path.read_text()
        self.assertIn('pid_cmdline_contains = get_str(action, "pid_cmdline_contains")', source)
        self.assertIn('["xdotool", "search", *visibility, "--pid", str(pid)]', source)
        self.assertIn('if bool(action.get("ensure_visible_geometry")):', source)
        self.assertIn('["xdotool", "windowraise", window_id]', source)

    def test_50_cgroup_stop_is_nonblocking(self):
        automation_path = Path(__file__).resolve().parents[8] / "automation" / "app_automation.py"
        source = automation_path.read_text()
        self.assertIn('["systemctl", "--user", "stop", "--no-block", unit]', source)

    def test_51_collector_requires_fresh_authenticated_ui_per_session(self):
        collector = (Path(__file__).resolve().parents[1] / "qq_collection_root.sh").read_text()
        self.assertIn("qq_authenticated_profile", collector)
        self.assertIn("qq_login_gate.py", collector)
        self.assertIn("AUTHENTICATION_VALIDATED", collector)
        self.assertIn("credentials_logged=false", collector)

    def test_52_login_gate_keeps_credentials_off_argv_and_evidence(self):
        gate = (Path(__file__).resolve().parents[1] / "qq_login_gate.py").read_text()
        self.assertIn('"--file", "-"', gate)
        self.assertIn("input_text=value", gate)
        self.assertIn('"credential_values_logged": False', gate)
        self.assertNotIn('print(account', gate)
        self.assertNotIn('print(password', gate)

    def test_53_formal_scenarios_are_authenticated_but_send_nothing(self):
        builder_path = Path(__file__).resolve().parents[1] / "build_qq_collection.py"
        spec = importlib.util.spec_from_file_location("phase210_qq_builder_auth", builder_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = module.scenario("qq_train_01", 21001)
        self.assertEqual(payload["privacy_mode"], "AUTHORIZED_QQ_ACCOUNT_READ_ONLY_NO_SEND")
        self.assertIn("send_message", payload["forbidden"])

    def test_54_root_children_are_removed_before_scope_stop_completes(self):
        collector = (Path(__file__).resolve().parents[1] / "qq_collection_root.sh").read_text()
        function = collector.split("stop_active_scope()", 1)[1].split("cleanup_runtime()", 1)[0]
        self.assertLess(function.index('rmdir "$active_target_cgroup"'),
                        function.index('systemctl --user stop --no-block'))
        self.assertIn('systemctl --user reset-failed "$active_scope"', function)
        session_tail = collector.split('wait "$automation_pid"', 1)[1]
        self.assertIn("stop_active_scope", session_tail)


if __name__ == "__main__":
    unittest.main()
