import json
import unittest
from pathlib import Path
import sys


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from core.app_mapper import AppMapper, ProcessIdentity
from core.app_process_index import AppProcessIndex
from core.runtime_scope import load_runtime_app_scope
from collectors.cgroup import _exact_cgroup_paths
from collectors.process import ProcessSample


class ResidentServiceScopeTest(unittest.TestCase):
    def test_resident_scope_is_cross_slice_and_has_expanded_apps(self) -> None:
        # lzx-note: The boot service must observe both test and automation slices.
        path = Path(__file__).resolve().parents[2] / "configs" / "runtime" / "runtime_app_scope.service.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["slice"], "")
        self.assertEqual(len(data["apps"]), 16)
        self.assertIn("lzx-note", data["implementation_note"])

        desktop = next(app for app in data["apps"] if app["app_key"] == "DESKTOP")
        self.assertEqual(desktop["app_id"], 16)
        self.assertEqual(desktop["vocab_name"], "Desktop")
        self.assertFalse(desktop["prediction_enabled"])
        self.assertFalse(desktop["workload_enabled"])
        self.assertEqual(desktop["scope_name"], "")
        self.assertIn("gnome-shell", desktop["window_keywords"])

    def test_desktop_is_mappable_without_entering_lstm_vocab_or_cgroup_routing(self) -> None:
        path = Path(__file__).resolve().parents[2] / "configs" / "runtime" / "runtime_app_scope.service.json"
        vocab = (
            Path(__file__).resolve().parents[3]
            / "tool/operation_predictor/data/vocab/lsapp_expanded/app_vocab_duration.json"
        )
        scope = load_runtime_app_scope(path, vocab)
        desktop = next(app for app in scope.apps if app.app_key == "DESKTOP")
        mapper = AppMapper(
            scope.as_process_mapper_config({}),
            target_app="FIREFOX",
            target_apps=scope.target_apps,
        )

        self.assertEqual(
            mapper.map_process(ProcessIdentity(
                pid=10, tgid=10, comm="gnome-shell", exe_path="/usr/bin/gnome-shell",
            )),
            "DESKTOP",
        )
        self.assertNotIn("DESKTOP", scope.prediction_apps)
        self.assertNotIn(16, scope.prediction_enabled_app_ids)
        self.assertFalse(any("DESKTOP" in warning for warning in scope.vocab_warnings))

    def test_gnome_extension_emits_desktop_when_no_app_window_has_focus(self) -> None:
        extension = (
            Path(__file__).resolve().parents[1]
            / "gnome_extension/extension.js"
        ).read_text(encoding="utf-8")
        self.assertIn("this._emitDesktopFocus();", extension)
        self.assertIn('window_id: focusId', extension)
        self.assertIn('wm_class: "gnome-shell"', extension)

    def test_fixture_scope_alias_maps_to_app_with_separate_fixture_role(self) -> None:
        path = Path(__file__).resolve().parents[2] / "configs" / "runtime" / "runtime_app_scope.service.json"
        scope = load_runtime_app_scope(path)
        firefox = next(app for app in scope.apps if app.app_key == "FIREFOX")
        self.assertEqual(
            firefox.binding_scope_names,
            ["automation-fixture-firefox.scope"],
        )
        mapper = AppMapper(
            scope.as_process_mapper_config({}),
            target_app="FIREFOX",
            target_apps=scope.target_apps,
        )
        fixture = ProcessIdentity(
            pid=10,
            tgid=10,
            comm="python3",
            exe_path="/usr/bin/python3",
            cgroup_path=(
                "/user.slice/parp-predictive-reclaim.slice/"
                "automation-fixture-firefox.scope"
            ),
        )
        self.assertEqual(mapper.map_process(fixture), "FIREFOX")
        # AppMapper 只回答“属于哪个固定 App”；GUI/fixture 角色由统一索引保存，
        # 从而 fixture 可以进入 App cgroup，但不会制造 APP_OPEN/APP_CLOSE。
        index = AppProcessIndex(
            mapper,
            scope.target_apps,
            fixture_scope_to_app=scope.fixture_scope_to_app_key,
        )
        change = index.process_exec(fixture, source_seq=1)
        self.assertEqual((change.current_app, change.current_role), ("FIREFOX", "fixture"))
        self.assertEqual(index.pids_for_app("FIREFOX", role="fixture"), {10})
        self.assertEqual(index.pids_for_app("FIREFOX", role="gui"), set())

    def test_solitaire_short_name_does_not_match_systemd_resolved(self) -> None:
        # lzx-note: Resident discovery must not contaminate Solitaire metrics.
        mapper = AppMapper(
            {"apps": {"SOLITAIRE": {"keywords": ["sol", "aisleriot"]}}},
            target_app="SOLITAIRE",
        )
        resolved = ProcessIdentity(
            pid=1,
            tgid=1,
            comm="systemd-resolve",
            exe_path="/usr/lib/systemd/systemd-resolved",
        )
        solitaire = ProcessIdentity(
            pid=2,
            tgid=2,
            comm="sol",
            exe_path="/usr/games/sol",
        )
        self.assertEqual(mapper.map_process(resolved), "")
        self.assertEqual(mapper.map_process(solitaire), "SOLITAIRE")

    def test_app_resources_keep_distinct_leaf_cgroups(self) -> None:
        # lzx-note: Evince and evinced must not collapse to their shared parent slice.
        def sample(pid: int, cgroup: str) -> ProcessSample:
            identity = ProcessIdentity(
                pid=pid, tgid=pid, comm="evince", exe_path="/usr/bin/evince",
                cgroup_path=cgroup,
            )
            return ProcessSample(identity=identity, app_id="EVINCE", io={}, status={}, stat={})

        paths = _exact_cgroup_paths([
            sample(1, "/user.slice/test.slice/automation-evince.scope"),
            sample(2, "/user.slice/app.slice/dbus-evince.service"),
            sample(3, "/user.slice/test.slice/automation-evince.scope"),
        ])
        rendered = [str(path) for path in paths]
        self.assertEqual(len(rendered), 2)
        self.assertFalse(any(path.endswith("/user.slice") for path in rendered))


if __name__ == "__main__":
    unittest.main()
