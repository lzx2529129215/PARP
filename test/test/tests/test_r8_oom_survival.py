import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = ROOT / "test/test/parp-real-pc-experiment-lzx.py"
ASSET_PATH = ROOT / "test/automation/create_real_pc_assets_lzx.py"
CONFIG_PATH = ROOT / "test/test/parp-r8-oom-survival-config-lzx.json"
LLM_CONFIG_PATH = ROOT / "test/test/parp-r8-llm-oom-survival-config-lzx.json"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


RUNNER = load_module("parp_r8_runner_test", RUNNER_PATH)
ASSETS = load_module("parp_r8_assets_test", ASSET_PATH)
R8 = RUNNER.R8


def frozen_config():
    config = RUNNER.load_config(CONFIG_PATH)
    config = copy.deepcopy(config)
    config["r8_oom"]["memory_max_mib"] = 3072
    config["r8_oom"]["burst_mib"] = 512
    config["r8_oom"]["calibration"]["frozen"] = True
    config["r8_oom"]["calibration"].pop("frozen_config_sha256", None)
    config["r8_oom"]["calibration"]["frozen_config_sha256"] = R8.canonical_sha256(
        R8.frozen_config_contract(config)
    )
    return config


def frozen_llm_config():
    config = RUNNER.load_config(LLM_CONFIG_PATH)
    config = copy.deepcopy(config)
    llm = config["r8_llm"]
    llm["status"] = "ready"
    llm["runtime_sha256"] = "a" * 64
    llm["gguf_sha256"] = "b" * 64
    llm["model_size_bytes"] = llm["minimum_model_bytes"]
    config["r8_oom"]["memory_max_mib"] = 3072
    config["r8_oom"]["calibration"]["frozen"] = True
    config["r8_oom"]["calibration"].pop("frozen_config_sha256", None)
    config["r8_oom"]["calibration"]["frozen_config_sha256"] = R8.canonical_sha256(
        R8.frozen_config_contract(config)
    )
    return config


class R8OOMSurvivalTests(unittest.TestCase):
    def test_llm_snapshot_records_live_scope_from_resident_pids(self) -> None:
        with mock.patch.object(R8, "_scope_path", return_value=Path("/fake/llm.scope")), \
             mock.patch.object(R8, "_scope_row", return_value={"valid": True, "pids": [101, 102]}):
            payload = R8.snapshot(Path("/fake/parent"), [], {}, "after_pressure", include_llm=True)
        self.assertTrue(payload["llm"]["scope_alive"])
        self.assertEqual(payload["llm"]["pids"], [101, 102])

    def test_configuration_has_the_exact_15_apps_and_calibration_formula(self) -> None:
        config = RUNNER.load_config(CONFIG_PATH)
        self.assertEqual(len(config["apps"]), 15)
        self.assertEqual(set(config["apps"]), R8.ALL_R8_APPS)
        self.assertEqual(R8.memory_limit_from_p95(1536 * 1024 * 1024), 2560 * 1024 * 1024)
        invalid = copy.deepcopy(config)
        invalid["r8_oom"]["pressure_chunk_mib"] = 32
        with self.assertRaisesRegex(ValueError, "64 MiB"):
            R8.validate_config(invalid)

    def test_llm_configuration_is_independent_and_missing_assets_block(self) -> None:
        config = copy.deepcopy(RUNNER.load_config(LLM_CONFIG_PATH))
        self.assertEqual(R8.configured_scenario(config), R8.LLM_SCENARIO)
        self.assertEqual(config["r8_oom"]["aggressor_app"], "LLM")
        self.assertEqual(set(config["r8_oom"]["victim_apps"]), R8.ALL_R8_APPS)
        config["r8_llm"]["status"] = "blocked_missing_local_assets"
        config["r8_llm"]["runtime_path"] = "/__r8_test_missing_runtime__"
        config["r8_llm"]["model_path"] = "/__r8_test_missing_model__"
        preflight = R8.llm_asset_preflight(config)
        self.assertEqual(preflight["status"], "BLOCKED")
        self.assertFalse(preflight["checks"]["runtime_executable"])
        self.assertFalse(preflight["checks"]["gguf_regular_file"])

    def test_llm_actions_use_one_real_weight_loader_and_all_gui_apps_are_victims(self) -> None:
        config = frozen_llm_config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("native", "bin"):
                run_dir = root / name
                (run_dir / "fixtures").mkdir(parents=True)
                (run_dir / "asset-manifest.json").write_text(json.dumps({
                    "schema_version": 6,
                    "assets": {"local-page.html": {"sha256": "c" * 64}},
                }), encoding="utf-8")
            native, native_plan = R8.generate_scenario(
                config, root / "native", Path("/sys/fs/cgroup/fake"), 20261001,
                "native_kernel", burst_mib=0, baseline_only=False,
            )
            binary, bin_plan = R8.generate_scenario(
                config, root / "bin", Path("/sys/fs/cgroup/fake"), 20261001,
                "bin_lstm", burst_mib=0, baseline_only=False,
            )
        gui_launches = [
            action for action in native["actions"]
            if str(action.get("label", "")).startswith("R8_LAUNCH_")
        ]
        self.assertEqual(len(gui_launches), 15)
        self.assertTrue(all("--score 1000" in action["command"] for action in gui_launches))
        firefox = next(action for action in gui_launches if action["app_key"] == "FIREFOX")
        self.assertIn("local-page.html", firefox["command"])
        self.assertNotIn("oom-pressure.html", firefox["command"])
        llm_launches = [
            action for action in native["actions"] if action.get("label") == "R8_LLM_LAUNCH"
        ]
        self.assertEqual(len(llm_launches), 1)
        self.assertEqual(llm_launches[0]["scope_name"], "llm-aggressor")
        self.assertIn("r8_llm_pressure_lzx.py", llm_launches[0]["command"])
        self.assertIn("--score 0", llm_launches[0]["command"])
        self.assertEqual(native_plan["pressure_kind"], "llama_cpp_no_mmap_weight_load")
        self.assertEqual(native_plan["llm_contract"]["runtime_sha256"], "a" * 64)
        self.assertEqual(native_plan["llm_contract"]["gguf_sha256"], "b" * 64)
        self.assertEqual(native_plan["sha256"], bin_plan["sha256"])
        self.assertEqual(len(native["actions"]), len(binary["actions"]))

    def test_llm_pressure_record_requires_load_token_hashes_and_residency(self) -> None:
        config = frozen_llm_config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            state_path = root / "state.json"
            output_path = root / "pressure.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            state_path.write_text(json.dumps({
                "status": "FIRST_TOKEN_COMPLETE",
                "runtime_sha256": config["r8_llm"]["runtime_sha256"],
                "gguf_sha256": config["r8_llm"]["gguf_sha256"],
                "model_size_bytes": config["r8_llm"]["model_size_bytes"],
                "fadvise_dontneed_applied": True,
                "cold_cache_gate": {"valid": True},
                "no_mmap": True,
                "oom_score_gate": {"valid": True},
                "resident_after_load_bytes": config["r8_llm"]["minimum_resident_delta_mib"] * 1024 * 1024,
                "peak_memory_delta_bytes": 700 * 1024 * 1024,
            }), encoding="utf-8")
            status = R8.command_llm_pressure_record(types.SimpleNamespace(
                config=config_path, state=state_path, output=output_path,
            ))
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(status, 0)
        self.assertTrue(payload["pressure_complete"])
        self.assertEqual(
            payload["pressure_requested_bytes"], config["r8_llm"]["model_size_bytes"],
        )
        self.assertEqual(payload["pressure_committed_bytes"], payload["pressure_requested_bytes"])

    def test_r8_asset_profile_is_separate_from_default_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pressure = root / "oom-pressure.html"
            ASSETS.write_oom_pressure_html(pressure)
            content = pressure.read_text(encoding="utf-8")
        self.assertIn("const chunkMiB = 64", content)
        self.assertIn("offset += pageBytes", content)
        self.assertIn("new Uint8Array(chunkBytes)", content)
        self.assertIn("PARP R8 FAILED", content)

    def test_r8_asset_build_does_not_change_legacy_profile_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            r8 = root / "r8"
            subprocess.run([sys.executable, str(ASSET_PATH), "--output", str(legacy)], check=True)
            first = json.loads((legacy / "manifest.json").read_text(encoding="utf-8"))
            subprocess.run([sys.executable, str(ASSET_PATH), "--profile", "r8", "--output", str(r8)], check=True)
            subprocess.run([sys.executable, str(ASSET_PATH), "--output", str(legacy)], check=True)
            second = json.loads((legacy / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 5)
        self.assertNotIn("oom-pressure.html", first["assets"])

    def test_actions_launch_all_apps_with_group_oom_and_score_wrapper(self) -> None:
        config = frozen_config()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("first", "second"):
                run_dir = root / name
                (run_dir / "fixtures").mkdir(parents=True)
                (run_dir / "asset-manifest.json").write_text(json.dumps({
                    "schema_version": 6, "assets": {"oom-pressure.html": {"sha256": "a"}},
                }), encoding="utf-8")
            native, native_plan = R8.generate_scenario(
                config, root / "first", Path("/sys/fs/cgroup/fake"), 20261001,
                "native_kernel", burst_mib=512, baseline_only=False,
            )
            binary, bin_plan = R8.generate_scenario(
                config, root / "second", Path("/sys/fs/cgroup/fake"), 20261001,
                "bin_lstm", burst_mib=512, baseline_only=False,
            )
        launches = [action for action in native["actions"] if str(action.get("label", "")).startswith("R8_LAUNCH_")]
        self.assertEqual(len(launches), 15)
        self.assertTrue(all(action.get("scope_properties") == {
            "MemoryOOMGroup": "yes", "TimeoutStopSec": "5s",
        } for action in launches))
        firefox = next(action for action in launches if action["app_key"] == "FIREFOX")
        self.assertIn("--score 0", firefox["command"])
        self.assertIn("oom-pressure.html", firefox["command"])
        self.assertIn("firefox-pressure-a-profile", firefox["command"])
        self.assertIn("firefox-pressure-b-profile", firefox["command"])
        self.assertIn("WEBKIT_DISABLE_MEMORY_PRESSURE_MONITOR=1", firefox["command"])
        self.assertIn("& exec", firefox["command"])
        self.assertTrue(all("--score 500" in action["command"] for action in launches if action["app_key"] != "FIREFOX"))
        labels = {str(action.get("label", "")) for action in native["actions"]}
        self.assertNotIn("R8_PRESSURE_NAVIGATE_ADDRESS", labels)
        self.assertNotIn("R8_PRESSURE_NAVIGATE_URI", labels)
        self.assertNotIn("R8_PRESSURE_NAVIGATE_OPEN", labels)
        anchor_index = next(
            index for index, action in enumerate(native["actions"])
            if action.get("label") == "R8_PREDICTION_ANCHOR_SWITCH_VLC"
        )
        marker_index = next(
            index for index, action in enumerate(native["actions"])
            if action.get("label") == "R8_PREDICTION_MARK"
        )
        trained_index = next(
            index for index, action in enumerate(native["actions"])
            if action.get("label") == "R8_TRAINED_01_SWITCH_THUNDERBIRD"
        )
        self.assertLess(anchor_index, marker_index)
        self.assertLess(marker_index, trained_index)
        published = [
            action for action in native["actions"]
            if "_PUBLISH_" in str(action.get("label", ""))
        ]
        self.assertEqual(len(published), 5)
        self.assertTrue(all("r8-publish-verified-switch" in action["command"] for action in published))
        trained_firefox = next(
            action for action in native["actions"]
            if action.get("label") == "R8_TRAINED_02_SWITCH_FIREFOX"
        )
        self.assertEqual(trained_firefox["pid_cmdline_contains"], "/firefox-profile")
        pressure_switch_a = next(
            action for action in native["actions"]
            if action.get("label") == "R8_PRESSURE_A_SWITCH_FIREFOX"
        )
        self.assertEqual(
            pressure_switch_a["pid_cmdline_contains"], "firefox-pressure-a-profile"
        )
        pressure_switch_b = next(
            action for action in native["actions"]
            if action.get("label") == "R8_PRESSURE_B_SWITCH_FIREFOX"
        )
        self.assertEqual(
            pressure_switch_b["pid_cmdline_contains"], "firefox-pressure-b-profile"
        )
        ready = [action for action in native["actions"] if "R8_PRESSURE_CHUNK_" in str(action.get("label", "")) and "_READY_" in str(action.get("label", ""))]
        self.assertEqual(len(ready), 8)
        requests = [action for action in native["actions"] if "R8_PRESSURE_CHUNK_" in str(action.get("label", "")) and "_REQUEST_" in str(action.get("label", ""))]
        self.assertTrue(all(action.get("activate_before_click") is True for action in requests))
        self.assertEqual(
            [row["lane"] for row in native_plan["pressure_chunk_order"]],
            ["A", "B"] * 4,
        )
        self.assertEqual(native_plan["sha256"], bin_plan["sha256"])
        self.assertEqual(native_plan["workset_actions"], bin_plan["workset_actions"])
        self.assertEqual(len(binary["actions"]), len(native["actions"]))

    def _evaluation_files(
        self, directory: Path, *, unknown: bool = False, firefox_dead: bool = False,
        pressure_incomplete: bool = False,
    ) -> tuple[dict, dict]:
        config = frozen_config()
        before_apps = {
            "FIREFOX": {"scope_alive": True, "window_alive": True, "processes": [{"pid": 100, "oom_score_adj": 0}]},
            "THUNDERBIRD": {"scope_alive": True, "window_alive": True, "processes": [{"pid": 101, "oom_score_adj": 500}]},
        }
        after_apps = {
            "FIREFOX": {"scope_alive": not firefox_dead, "window_alive": not firefox_dead, "processes": [] if firefox_dead else [{"pid": 100, "oom_score_adj": 0}]},
            "THUNDERBIRD": {"scope_alive": False, "window_alive": False, "processes": []},
        }
        write = lambda name, value: (directory / name).write_text(json.dumps(value), encoding="utf-8")
        write("r8-before-pressure.json", {"cgroup": {"memory_events": {"oom": 0, "oom_kill": 0, "oom_group_kill": 0}}, "apps": before_apps})
        write("r8-after-pressure.json", {"cgroup": {"memory_events": {"oom": 1, "oom_kill": 1, "oom_group_kill": 1}}, "apps": after_apps})
        write("r8-workset-gate.json", {"valid": True, "reasons": []})
        write("prediction-gate.json", {"valid": True, "reasons": []})
        write("r8-pressure.json", {
            "pressure_requested_bytes": 512 * 1024 * 1024,
            "pressure_committed_bytes": 448 * 1024 * 1024 if pressure_incomplete else 512 * 1024 * 1024,
            "pressure_complete": not pressure_incomplete,
        })
        (directory / "trace-stats.txt").write_text("entries: 0 overrun: 0\n", encoding="utf-8")
        pid = 999 if unknown else 101
        score = 1000 if unknown else 500
        (directory / "trace.txt").write_text(f"oom: mark_victim: pid={pid} comm=test oom_score_adj={score}\n", encoding="utf-8")
        return config, {100: {"app": "FIREFOX", "pid": 100, "oom_score_adj": 0}, 101: {"app": "THUNDERBIRD", "pid": 101, "oom_score_adj": 500}}

    def test_oom_trace_is_attributed_to_one_application_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config, history = self._evaluation_files(Path(directory))
            result = R8.evaluate_result(config, "native_kernel", Path(directory), 0, 1, {}, {}, 0, "", history, baseline_only=False)
        self.assertTrue(result["valid"], result["invalid_reasons"])
        self.assertEqual(result["victim_apps"], ["THUNDERBIRD"])
        self.assertEqual(result["distinct_oom_victim_apps"], 1)
        self.assertEqual(result["oom_group_kill_delta"], 1)

    def test_non_leader_thread_tid_is_attributed_from_cgroup_threads(self) -> None:
        mapping = R8._pid_map({
            "apps": {
                "LIBREOFFICE": {
                    "processes": [{"pid": 101, "comm": "soffice.bin"}],
                    "threads": [101, 202],
                },
            },
        })
        self.assertEqual(mapping[202]["app"], "LIBREOFFICE")
        self.assertTrue(mapping[202]["thread"])

    def test_unknown_or_host_oom_invalidates_the_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config, history = self._evaluation_files(Path(directory), unknown=True)
            result = R8.evaluate_result(config, "native_kernel", Path(directory), 0, 1, {}, {}, 0, "", history, baseline_only=False)
        self.assertFalse(result["valid"])
        self.assertTrue(result["host_or_unknown_oom"])

    def test_firefox_death_and_incomplete_pressure_are_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config, history = self._evaluation_files(Path(directory), firefox_dead=True)
            result = R8.evaluate_result(config, "native_kernel", Path(directory), 0, 1, {}, {}, 0, "", history, baseline_only=False)
            self.assertFalse(result["valid"])
            self.assertIn("Firefox aggressor did not survive", result["invalid_reasons"])
        with tempfile.TemporaryDirectory() as directory:
            config, history = self._evaluation_files(Path(directory), pressure_incomplete=True)
            result = R8.evaluate_result(config, "native_kernel", Path(directory), 0, 1, {}, {}, 0, "", history, baseline_only=False)
            self.assertFalse(result["valid"])
            self.assertIn("Firefox pressure was not fully committed", result["invalid_reasons"])

    def test_report_requires_ten_pairs_and_30_percent_reduction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            native = root / "native"
            binary = root / "bin"
            native.mkdir()
            binary.mkdir()
            runs_native = []
            runs_bin = []
            for seed in range(10):
                common = {"scenario": R8.SCENARIO, "seed": seed, "valid": True, "action_plan_sha256": "same", "pressure_complete": True, "pressure_requested_bytes": 1, "pressure_committed_bytes": 1}
                runs_native.append({**common, "policy": "native_kernel", "distinct_oom_victim_apps": 2})
                runs_bin.append({**common, "policy": "bin_lstm", "distinct_oom_victim_apps": 1})
            (native / "summary.json").write_text(json.dumps({"runs": runs_native}), encoding="utf-8")
            (binary / "summary.json").write_text(json.dumps({"runs": runs_bin}), encoding="utf-8")
            result = R8.report(native, binary)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["reduction"], 0.5)


if __name__ == "__main__":
    unittest.main()
