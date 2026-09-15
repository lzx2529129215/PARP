from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import numpy as np
import torch

RUNTIME = Path(__file__).resolve().parents[1]
PREDICTOR = RUNTIME.parents[1] / "tool/operation_predictor"
sys.path.insert(0, str(RUNTIME))
sys.path.insert(0, str(PREDICTOR))
from v3.models.app_lstm_visit_window import (AppLSTMVisitWindow, HORIZONS, MODEL_TYPE, FORMAT,
    VISIT_DEFINITION, visit_probabilities, masked_loss, encode_features, tensor_features)
from v3.src.data.build_app_dataset_visit_window import window_labels, build_dataset
from v3.train.train_app_lstm_visit_window import average_precision, metrics
from predictor_visit_window import OnlineVisitWindowPredictor, thermal_state, classify_rows
from online_visit_window import OnlineVisitWindowRunner

VOCAB = {"A": 0, "B": 1, "C": 2, "<PAD>": 3, "<UNKNOWN>": 4}
GROUPS = {"通用用户": 0}
T = dt.datetime(2020, 1, 1)


def options(**extra):
    return argparse.Namespace(history_len=5, duration_cap_s=600., max_session_gap_s=3600.,
        periodic_anchor_s=180., anchor_mode="event_plus_periodic", enable_debug_dwell_buckets=False, **extra)


def checkpoint(root):
    args = dict(num_apps=len(VOCAB), num_user_groups=1, pad_id=VOCAB["<PAD>"])
    model = AppLSTMVisitWindow(**args).eval()
    ckpt = {"model_type": MODEL_TYPE, "schema_version": 1, "prediction_format": FORMAT,
        "horizons_s": list(HORIZONS), "visit_definition": VISIT_DEFINITION,
        "probability_parameterization": "p30_plus_survival30_times_sigmoid_z2",
        "model_args": args, "app_vocab": VOCAB, "group_vocab": GROUPS, "history_len": 5,
        "model_state_dict": model.state_dict()}
    torch.save(ckpt, root / "model.pt")
    (root / "apps.json").write_text(json.dumps(VOCAB))
    (root / "groups.json").write_text(json.dumps(GROUPS))
    return model


class LabelsTest(unittest.TestCase):
    def test_entry_boundaries_and_return_to_current(self):
        entries = [(T, "A"), (T + dt.timedelta(seconds=30), "B"),
                   (T + dt.timedelta(seconds=31), "C"), (T + dt.timedelta(seconds=180), "A"),
                   (T + dt.timedelta(seconds=181), "C")]
        y, m = window_labels(entries, T, T + dt.timedelta(seconds=400), VOCAB)
        self.assertEqual(y[30], ["B"])
        self.assertEqual(y[180], ["A", "B", "C"])
        self.assertEqual(m[30], [1, 1, 1, 0, 0])

    def test_persistence_and_all_negative(self):
        y, m = window_labels([(T, "A")], T, T + dt.timedelta(seconds=180), VOCAB)
        self.assertEqual(y, {30: [], 180: []})
        self.assertEqual(m[180], [1, 1, 1, 0, 0])

    def test_censoring_per_app_and_window(self):
        y, m = window_labels([(T + dt.timedelta(seconds=20), "B")], T, T + dt.timedelta(seconds=40), VOCAB)
        self.assertEqual(y, {30: ["B"], 180: ["B"]})
        self.assertEqual(m[30], [1, 1, 1, 0, 0])
        self.assertEqual(m[180], [0, 1, 0, 0, 0])
        y, m = window_labels([(T + dt.timedelta(seconds=20), "B")], T, T + dt.timedelta(seconds=10), VOCAB)
        self.assertEqual(y[30], [])
        self.assertEqual(sum(m[30]), 0)

    def test_split_labels_do_not_read_next_partition(self):
        rows = [{"user_id": "u", "timestamp": (T + dt.timedelta(seconds=i * 20)).isoformat(sep=" "),
                 "foreground_app": "A" if i % 2 == 0 else "B", "raw_foreground_app": "A",
                 "opened_apps": "A;B", "user_group": "通用用户"} for i in range(20)]
        data, _, _ = build_dataset(rows, VOCAB, GROUPS, options())
        last = data["train"][-1]
        self.assertEqual(last["labels_visit_30s"], "")
        self.assertEqual(sum(map(int, last["valid_visit_30s"].split("|"))), 0)
        self.assertLess(last["timestamp"], data["val"][0]["timestamp"])
        # Current-app return two switches later must be a positive before the boundary.
        self.assertIn("A", data["train"][0]["labels_visit_180s"].split("|"))

    def test_final_repeated_events_are_observation_not_new_entries(self):
        rows = [{"user_id": "u", "timestamp": (T + dt.timedelta(seconds=i * 20)).isoformat(sep=" "),
                 "foreground_app": "A" if i < 10 else "B", "raw_foreground_app": "A",
                 "opened_apps": "A;B", "user_group": "通用用户"} for i in range(30)]
        data, segments, _ = build_dataset(rows, VOCAB, GROUPS, options())
        self.assertEqual(segments[-1]["observed_until"], rows[-1]["timestamp"])
        last = data["test"][-1]
        self.assertEqual(last["labels_visit_180s"], "")
        self.assertEqual(last["valid_visit_180s"], "1|1|1|0|0")

    def test_session_gap_does_not_supply_future_labels(self):
        rows = []
        for offset, app in [(0, "A"), (10, "A"), (4000, "B"), (4010, "B"), (8000, "C"), (8010, "C")]:
            rows.append({"user_id": "u", "timestamp": (T + dt.timedelta(seconds=offset)).isoformat(sep=" "),
                "foreground_app": app, "raw_foreground_app": app, "opened_apps": "A;B;C", "user_group": "通用用户"})
        data, segments, stats = build_dataset(rows, VOCAB, GROUPS, options())
        self.assertEqual(stats["session_count"], 3)
        for split in data.values():
            for row in split:
                self.assertEqual(row["labels_visit_180s"], "")
                self.assertEqual(row["valid_visit_180s"], "0|0|0|0|0")


class ModelTest(unittest.TestCase):
    def test_nested_random_and_extreme_gradients(self):
        torch.manual_seed(7)
        logits = torch.randn(20, 5, 2, requires_grad=True)
        p = visit_probabilities(logits)
        self.assertTrue(((p >= 0) & (p <= 1)).all())
        self.assertTrue((p[:, :, 0] <= p[:, :, 1]).all())
        # Not a cross-app softmax: all apps can have high probability together.
        self.assertTrue((visit_probabilities(torch.ones(1, 5, 2) * 10) > .9).all())
        logits = torch.tensor([[[-1000., 1000.], [1000., -1000.], [0., 0.]]], requires_grad=True)
        y = torch.tensor([[[0., 1.], [1., 1.], [0., 0.]]])
        loss = masked_loss(logits, y, torch.ones_like(y))
        loss.backward()
        self.assertTrue(torch.isfinite(loss))
        self.assertTrue(torch.isfinite(logits.grad).all())

    def test_long_window_supervises_both_logits_and_mask(self):
        logits = torch.zeros(1, 1, 2, requires_grad=True)
        loss = masked_loss(logits, torch.ones_like(logits), torch.tensor([[[0., 1.]]]))
        loss.backward()
        self.assertTrue((logits.grad != 0).all())
        logits = torch.randn(1, 2, 2, requires_grad=True)
        masked_loss(logits, torch.zeros_like(logits), torch.zeros_like(logits)).backward()
        self.assertTrue((logits.grad == 0).all())

    def test_short_history_padding_invariance(self):
        model = AppLSTMVisitWindow(num_apps=5, num_user_groups=1, pad_id=3).eval()
        values = encode_features(["A", "B"], [12., 3.], [1, 1], ["A", "B"], "B", T.isoformat(), "通用用户", VOCAB, GROUPS)
        batch = tensor_features(values)
        padded = model(**batch)
        for key in ("history_apps", "history_mask", "history_durations"):
            batch[key] = batch[key][:, -2:]
        self.assertTrue(torch.allclose(padded, model(**batch), atol=1e-6))

    def test_checkpoint_contract_and_online_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model = checkpoint(root)
            predictor = OnlineVisitWindowPredictor(root / "model.pt", root / "apps.json", root / "groups.json")
            inputs = (["A", "B"], [12., 3.], [1, 1], ["A", "B"], "B", T.isoformat())
            expected = visit_probabilities(model(**predictor.encode_inputs(*inputs))).detach().numpy()[0]
            actual = predictor.predict_bundle(*inputs)["all_probabilities"]
            for row in actual:
                self.assertAlmostEqual(expected[row["app_id"], 0], row["p_visit_30s"], places=6)
            vocab = dict(VOCAB, A=1, B=0)
            (root / "apps.json").write_text(json.dumps(vocab))
            with self.assertRaisesRegex(ValueError, "mapping mismatch"):
                OnlineVisitWindowPredictor(root / "model.pt", root / "apps.json", root / "groups.json")
            (root / "apps.json").write_text(json.dumps(VOCAB))
            torch.save({"model_type": "app_switch_v3"}, root / "old.pt")
            with self.assertRaisesRegex(ValueError, "not a supported"):
                OnlineVisitWindowPredictor(root / "old.pt", root / "apps.json", root / "groups.json")


class RuntimeTest(unittest.TestCase):
    def test_thermal_boundaries_and_expiry(self):
        self.assertEqual(thermal_state(.9, .95), "hot")
        self.assertEqual(thermal_state(.1, .2), "neutral")
        self.assertEqual(thermal_state(.1, .199), "cold")
        self.assertEqual(thermal_state(.01, .1, foreground=True), "foreground")
        self.assertEqual(thermal_state(None, None), "unavailable")
        self.assertEqual(thermal_state(.01, .1, running=False), "not_running")
        self.assertEqual(thermal_state(.8, .6), "unavailable")
        row = {"app": "B", "p_visit_30s": .1, "p_visit_180s": .1,
               "expires_at": (T + dt.timedelta(seconds=30)).isoformat(), "probability_source": "test"}
        rows, lists = classify_rows([row], "A", ["A", "B"], (T + dt.timedelta(seconds=30)).isoformat())
        self.assertEqual(rows[0]["thermal_state"], "unavailable")
        self.assertEqual(lists["cold_apps"], [])

    def test_event_refresh_and_completed_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkpoint(root)
            args = options(session_id="test", lstm_checkpoint=root / "model.pt", app_vocab=root / "apps.json",
                           group_vocab=root / "groups.json", user_group="通用用户", device="cpu")
            runner = OnlineVisitWindowRunner(args, root / "model", root / "review")
            def row(seconds, app="A", opened="A|B"):
                return {"timestamp": (T + dt.timedelta(seconds=seconds)).isoformat(),
                        "foreground_app": app, "open_apps": opened}
            try:
                self.assertEqual(runner.process_event(row(0), "APP_SWITCH")["status"], "success")
                self.assertFalse(runner.process_sample(row(29))["inference_executed"])
                self.assertEqual(runner.process_sample(row(30))["trigger_type"], "periodic_refresh_30s")
                late = runner.process_event(row(29.5, "B"), "APP_SWITCH")
                self.assertEqual(late["status"], "success")
                self.assertEqual(late["history_durations_s"], "0|0|0|29.5|1")
                self.assertEqual(runner.process_event(row(29, "A"), "APP_SWITCH")["skip_reason"], "out_of_order")
                runner.reset_history()
                runner.process_event(row(0), "APP_SWITCH")
                result = runner.process_event(row(40, "B"), "APP_SWITCH")
                self.assertEqual(result["history_durations_s"], "0|0|0|40|1")
                result = runner.process_sample(row(41, "B", "B"))
                self.assertTrue(result["inference_executed"])
                a = next(r for r in result["all_probabilities"] if r["app"] == "A")
                self.assertEqual(a["thermal_state"], "not_running")
                self.assertEqual(runner.process_sample(row(39))["skip_reason"], "out_of_order")
                with patch.object(runner.predictor, "predict_bundle", side_effect=ValueError("broken")):
                    failed = runner.process_sample(row(71, "B", "B"))
                self.assertEqual(failed["status"], "error")
                self.assertEqual(failed["cold_apps"], [])
            finally:
                runner.close()

    def test_legacy_sinks_reject_new_format_before_access(self):
        from core.parp_myfs import PARPMyfsBridge
        from core.parp_bridge import PARPDebugfsBridge
        from monitor import RuntimeMonitorV0
        result = {"status": "success", "prediction_format": "visit_window"}
        # Uninitialized instances fail immediately if legacy work is attempted.
        PARPMyfsBridge.__new__(PARPMyfsBridge).submit_prediction({}, result)
        PARPDebugfsBridge.__new__(PARPDebugfsBridge).submit_prediction({}, result)
        RuntimeMonitorV0.__new__(RuntimeMonitorV0)._maybe_write_mglru_predictions(result)

    def test_monitor_direct_tick_uses_authoritative_event_snapshot(self):
        from monitor import RuntimeMonitorV0
        monitor = RuntimeMonitorV0.__new__(RuntimeMonitorV0)
        monitor.direct_x11_events = True
        monitor._direct_event_state = Mock()
        monitor._direct_event_state.snapshot.return_value = {"foreground_app": "B", "open_apps": "B|C"}
        monitor.online_lstm = Mock()
        monitor._run_visit_window_tick({"timestamp": T.isoformat(), "foreground_app": "A", "open_apps": "A"})
        row = monitor.online_lstm.process_sample.call_args.args[0]
        self.assertEqual(row["foreground_app"], "B")
        self.assertEqual(row["open_apps"], "B|C")
        monitor.direct_x11_events = False
        monitor._run_visit_window_tick({"timestamp": T.isoformat(), "foreground_app": "A", "open_apps": "A"})
        self.assertEqual(monitor.online_lstm.process_sample.call_args.args[0]["foreground_app"], "A")

    def test_monitor_direct_result_bypasses_legacy_chain(self):
        from monitor import RuntimeMonitorV0
        monitor = RuntimeMonitorV0.__new__(RuntimeMonitorV0)
        monitor.session_id = "test"
        monitor._direct_event_state = Mock()
        monitor._direct_event_state.snapshot.return_value = {"foreground_app": "", "foreground_window_id": "",
            "foreground_pid": "", "foreground_window_title": "", "open_apps": "B"}
        monitor.online_lstm = Mock(model_type="visit_window")
        monitor.online_lstm.process_event.return_value = {"status": "success", "prediction_format": "visit_window"}
        result = monitor._run_app_event_prediction({"timestamp": T.isoformat(), "event_type": "APP_CLOSE", "app": "A"})
        self.assertEqual(result["prediction_format"], "visit_window")
        self.assertEqual(monitor.online_lstm.process_event.call_args.args[0]["foreground_app"], "")

    def test_output_only_cli_contract(self):
        from monitor import parse_args
        self.assertEqual(parse_args(["--lstm-model-type", "visit_window"]).lstm_model_type, "visit_window")
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parse_args(["--lstm-model-type", "visit_window", "--enable-parp-myfs"])


class MetricsTest(unittest.TestCase):
    def test_average_precision_ties_and_empty_selection(self):
        self.assertEqual(average_precision(np.array([1, 0]), np.array([.5, .5])), .5)
        self.assertIsNone(average_precision(np.zeros(2), np.array([.2, .1])))
        report = metrics(np.zeros((1, 5, 2)), np.zeros((1, 5, 2)), np.ones((1, 5, 2)),
                         np.zeros((1, 5), bool), VOCAB)
        self.assertIsNone(report["thermal"]["hot_precision"])
        self.assertIsNone(report["thermal"]["cold_actual_visit_rate"])


if __name__ == "__main__":
    torch.set_num_threads(1)
    unittest.main()
