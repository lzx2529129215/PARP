from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "runtime_monitor") not in sys.path:
    sys.path.insert(0, str(ROOT / "runtime_monitor"))

from core.memory_shadow import MemoryShadowObserver, counter_delta, memory_value, read_smaps_rollup


@dataclass
class Scope:
    app_key_to_scope_name: dict[str, str]
    app_key_to_app_id: dict[str, int]
    app_key_to_vocab_name: dict[str, str]
    slice_name: str = "huawei-test.slice"


class MemoryShadowTests(unittest.TestCase):
    def test_counter_reset_is_not_reported_as_negative_activity(self) -> None:
        self.assertEqual(counter_delta(13, 10), (3, "OK"))
        self.assertEqual(counter_delta(1, 10), (0, "COUNTER_RESET"))

    def test_working_set_prefers_referenced_then_pss(self) -> None:
        self.assertEqual(memory_value({"referenced_bytes": 10, "pss_bytes": 20}), 10)
        self.assertEqual(memory_value({"referenced_bytes": 0, "pss_bytes": 20}), 20)

    def test_smaps_rollup_accepts_kb_suffix_format(self) -> None:
        values, error = read_smaps_rollup(os.getpid())
        self.assertEqual(error, "")
        self.assertGreater(values["Rss"], 0)
        self.assertGreater(values["Pss"], 0)

    def test_prediction_batch_keeps_full_batch_but_only_top_k_is_sampled(self) -> None:
        scope = Scope(
            {"A": "a.scope", "B": "b.scope", "C": "c.scope"},
            {"A": 1, "B": 2, "C": 3},
            {"A": "a", "B": "b", "C": "c"},
        )
        with tempfile.TemporaryDirectory() as directory:
            observer = MemoryShadowObserver(
                session_id="s", output_dir=Path(directory), runtime_scope=scope, top_k=2
            )
            observer.record_prediction(
                event={"ts_ns": 1_000_000_000},
                feature_row={"foreground_app": "A", "open_apps": "A"},
                result={
                    "status": "success", "inference_executed": True, "prediction_id": "p1",
                    "trigger_type": "direct_app_switch", "prediction_format": "app_probability",
                    "all_probabilities": [
                        {"app": "b", "rank": 1, "probability": 0.7},
                        {"app": "c", "rank": 2, "probability": 0.2},
                        {"app": "a", "rank": 3, "probability": 0.1},
                    ],
                },
                process_samples=[],
            )
            self.assertEqual(len(observer.batches), 1)
            self.assertEqual(len(observer.active.candidates), 3)  # type: ignore[union-attr]
            self.assertEqual(observer.active.monitored_apps, {"B", "C"})  # type: ignore[union-attr]
            observer.observe_event({"event_type": "APP_SWITCH", "ts_ns": 2_000_000_000, "new_app": "B"})
            self.assertEqual(observer.episodes[0].terminal_reason, "NEXT_APP_SWITCH")
            observer.close()


if __name__ == "__main__":
    unittest.main()
