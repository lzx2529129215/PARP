from __future__ import annotations

import ctypes
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from runtime_monitor.core.parp_myfs import (
    ENTRY_FOREGROUND,
    PARP_PREDICT_GET_STATE,
    PARP_PREDICT_GET_STATE_V2,
    PARP_PREDICT_GET_STATE_V3,
    PARP_PREDICT_SET_STATE,
    PARP_PREDICT_SET_STATE_V2,
    PARP_PREDICT_SET_STATE_V3,
    PredictBindingV1,
    PredictBindingV3,
    PredictEntryV1,
    PredictStateV1,
    PredictStateV2,
    PredictStateV3,
    PARPMyfsBridge,
)
from runtime_monitor.core.working_set_predictor import WorkingSetPrediction
from runtime_monitor.core.reclaim_workload import ReclaimWorkloadProfile, WORKLOAD_FILE_DIRTY  # lzx-note


def _scope():
    apps = [
        SimpleNamespace(
            app_key="FIREFOX", app_id=1, vocab_name="Firefox",
            prediction_enabled=True, binding_scope_names=["automation-fixture-firefox.scope"],
        ),
        SimpleNamespace(
            app_key="VLC", app_id=3, vocab_name="VLC",
            prediction_enabled=True, binding_scope_names=[],
        ),
    ]
    return SimpleNamespace(apps=apps)


class PARPMyfsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def bridge(self) -> PARPMyfsBridge:
        return PARPMyfsBridge(
            mode="dry-run", device=self.root / "myfs", runtime_scope=_scope(),
            output_dir=self.root, session_id="unit", model_version=401,
        )

    def test_uapi_layout_matches_kernel_contract(self) -> None:
        self.assertEqual(ctypes.sizeof(PredictEntryV1), 16)
        self.assertEqual(ctypes.sizeof(PredictBindingV1), 32)
        self.assertEqual(ctypes.sizeof(PredictStateV1), 2656)
        self.assertEqual(PredictStateV1.predictions.offset, 96)
        self.assertEqual(PredictStateV1.bindings.offset, 608)
        self.assertEqual(PARP_PREDICT_SET_STATE, 0x4A60B701)
        self.assertEqual(PARP_PREDICT_GET_STATE, 0x8A60B702)
        self.assertEqual(ctypes.sizeof(PredictStateV2), 2656)
        self.assertEqual(PredictStateV2.predictions.offset, 96)
        self.assertEqual(PredictStateV2.bindings.offset, 608)
        self.assertEqual(PARP_PREDICT_SET_STATE_V2, 0x4A60B703)
        self.assertEqual(PARP_PREDICT_GET_STATE_V2, 0x8A60B704)
        self.assertEqual(ctypes.sizeof(PredictBindingV3), 32)
        self.assertEqual(ctypes.sizeof(PredictStateV3), 2656)
        self.assertEqual(PredictStateV3.predictions.offset, 96)
        self.assertEqual(PredictStateV3.bindings.offset, 608)
        self.assertEqual(PARP_PREDICT_SET_STATE_V3, 0x4A60B705)
        self.assertEqual(PARP_PREDICT_GET_STATE_V3, 0x8A60B706)

    def test_foreground_and_lstm_candidates_are_ranked_once(self) -> None:
        bridge = self.bridge()
        try:
            entries, current = bridge._prediction_entries(
                {"foreground_app": "FIREFOX"},
                {
                    "mapped_foreground_app": "Firefox",
                    "all_probabilities": [
                        {"app": "VLC", "probability": 0.75},
                        {"app": "Firefox", "probability": 0.25},
                    ],
                },
            )
            self.assertEqual(current, "FIREFOX")
            self.assertEqual(entries[0], (1, 32767, 1, ENTRY_FOREGROUND))
            self.assertEqual(entries[1], (3, round(0.75 * 32767), 2, 0))
        finally:
            bridge.close()

    def test_same_cgroup_with_multiple_apps_is_not_misbound(self) -> None:
        bridge = self.bridge()
        cgroup_path = ""
        for line in Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[0] == "0":
                cgroup_path = parts[2]
                break
        samples = [
            SimpleNamespace(app_id="FIREFOX", identity=SimpleNamespace(cgroup_path=cgroup_path)),
            SimpleNamespace(app_id="VLC", identity=SimpleNamespace(cgroup_path=cgroup_path)),
        ]
        try:
            bindings, apps, ambiguous = bridge._bindings(samples, {1, 3})
            self.assertEqual(bindings, [])
            self.assertEqual(apps, set())
            self.assertEqual(ambiguous, 1)
        finally:
            bridge.close()

    def test_gui_and_fixture_domains_bind_to_same_app_id(self) -> None:
        cgroup_root = self.root / "two-domains"
        gui = cgroup_root / "test" / "automation-firefox.scope"
        fixture = cgroup_root / "test" / "automation-fixture-firefox.scope"
        gui.mkdir(parents=True)
        fixture.mkdir()
        bridge = PARPMyfsBridge(
            mode="dry-run", device=self.root / "myfs", runtime_scope=_scope(),
            output_dir=self.root / "two-domain-output", session_id="two-domain",
            model_version=401, cgroup_root=cgroup_root,
        )
        samples = [
            SimpleNamespace(
                app_id="FIREFOX",
                identity=SimpleNamespace(cgroup_path="/test/automation-firefox.scope"),
            ),
            SimpleNamespace(
                app_id="FIREFOX",
                identity=SimpleNamespace(cgroup_path="/test/automation-fixture-firefox.scope"),
            ),
        ]
        try:
            bindings, apps, ambiguous = bridge._bindings(samples, {1, 3})
            self.assertEqual(
                bindings,
                sorted([(gui.stat().st_ino, 1), (fixture.stat().st_ino, 1)]),
            )
            self.assertEqual(apps, {"FIREFOX"})
            self.assertEqual(ambiguous, 0)
        finally:
            bridge.close()

    def test_live_fixture_scope_is_added_as_binding_only_alias(self) -> None:
        cgroup_root = self.root / "cgroup"
        fixture = cgroup_root / "test.slice" / "automation-fixture-firefox.scope"
        fixture.mkdir(parents=True)
        bridge = PARPMyfsBridge(
            mode="dry-run", device=self.root / "myfs", runtime_scope=_scope(),
            output_dir=self.root / "alias", session_id="alias", model_version=401,
            cgroup_root=cgroup_root,
        )
        try:
            bindings, apps, ambiguous = bridge._bindings([], {1, 3})
            self.assertEqual(bindings, [(fixture.stat().st_ino, 1)])
            self.assertEqual(apps, {"FIREFOX"})
            self.assertEqual(ambiguous, 0)
            self.assertEqual(bridge._last_alias_binding_count, 1)
        finally:
            bridge.close()

    def test_dry_run_event_builds_atomic_state(self) -> None:
        bridge = self.bridge()
        try:
            bridge.submit_prediction(
                {"foreground_app": "FIREFOX"},
                {
                    "status": "success", "prediction_id": "p1",
                    "mapped_foreground_app": "Firefox",
                    "all_probabilities": [{"app": "VLC", "probability": 1.0}],
                },
                event={"event_type": "APP_SWITCH", "ts_ns": 123},
            )
            self.assertEqual(bridge.generation, 1)
            self.assertEqual(bridge._stats["dry_runs"], 1)
            text = bridge.audit_path.read_text(encoding="utf-8")
            self.assertIn("APP_SWITCH", text)
            self.assertIn("DRY_RUN", text)
            self.assertIn("workload_binding_details", text)
        finally:
            bridge.close()

    def test_v2_state_carries_valid_workingset_atomically(self) -> None:
        bridge = self.bridge()
        try:
            bridge.generation = 7
            prediction = WorkingSetPrediction(
                valid=True,
                policy_domain_id=101,
                predicted_workingset_bytes=512 << 20,
                predicted_resident_bytes=384 << 20,
                confidence_q15=24576,
                action_hint="NORMAL",
            )
            state = bridge._make_state_v2(
                [(1, 32767, 1, ENTRY_FOREGROUND)],
                [(202, 1)],
                {"ts_ns": 303},
                404,
                prediction,
            )
            self.assertEqual(state.abi_version, 2)
            self.assertEqual(state.generation, 7)
            self.assertEqual(state.flags, 1)
            self.assertEqual(state.policy_domain_id, 101)
            self.assertEqual(state.predicted_workingset_bytes, 512 << 20)
            self.assertEqual(state.predicted_resident_bytes, 384 << 20)
            self.assertEqual(state.workingset_confidence_q15, 24576)
            self.assertEqual(state.bindings[0].domain_id, 202)
        finally:
            bridge.close()

    def test_v3_state_keeps_workload_per_cgroup_binding(self) -> None:
        bridge = self.bridge()
        try:
            bridge.generation = 8
            state = bridge._make_state_v3(
                [(1, 32767, 1, ENTRY_FOREGROUND)], [(202, 1)], {"ts_ns": 303}, 404,
                WorkingSetPrediction(),
                {202: ReclaimWorkloadProfile(
                    domain_id=202, workload_class=WORKLOAD_FILE_DIRTY,
                    swappiness=20, confidence_q8=240, allow_writepage=True,
                )},
            )
            self.assertEqual(state.abi_version, 3)
            self.assertEqual(state.bindings[0].flags, 3)
            self.assertEqual(state.bindings[0].workload_hint & 0x0F, WORKLOAD_FILE_DIRTY)
            self.assertEqual((state.bindings[0].workload_hint >> 8) & 0xFF, 20)
            self.assertTrue(state.bindings[0].workload_hint & (1 << 24))
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main()
