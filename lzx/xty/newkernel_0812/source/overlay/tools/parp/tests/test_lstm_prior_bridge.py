#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
import time
import unittest

from lstm_prior_bridge import (AsyncPriorBridge, BatchBuilder,
                               DebugfsTransport, GenerationQueryError,
                               MockTransport, score_to_q15)


class LSTMPriorBridgeTest(unittest.TestCase):
    def setUp(self):
        self.whitelist = {"WPS": 1, "QQ": 2, "FILES": 3}
        self.rows = [
            {"app_key": "WPS", "probability": 0.8},
            {"app_key": "QQ", "probability": 0.6},
            {"app_key": "FILES", "probability": 0.1},
            {"app_key": "OUTSIDE", "probability": 1.0},
        ]

    def test_float_to_q15_and_complete_ranked_batch(self):
        self.assertEqual(score_to_q15(0.0), 0)
        self.assertEqual(score_to_q15(1.0), 32767)
        builder = BatchBuilder(self.whitelist, model_version=7)
        batch = builder.build(self.rows, "WPS", timestamp_ns=100,
                              horizon_ns=20, ttl_ns=30)
        self.assertEqual(len(batch["entries"]), 3)
        self.assertEqual([row["rank"] for row in batch["entries"]], [1, 2, 3])
        self.assertEqual(sum(row["foreground"] for row in batch["entries"]), 1)
        self.assertNotIn(4, [row["app_id"] for row in batch["entries"]])

    def test_event_generation_dedup_and_mock_transport(self):
        transport = MockTransport()
        bridge = AsyncPriorBridge(BatchBuilder(self.whitelist, 9), transport)
        first = bridge.submit_event("open:1", self.rows, "WPS").result(2)
        duplicate = bridge.submit_event("open:1", self.rows, "WPS").result(2)
        second = bridge.submit_event("foreground:2", self.rows, "QQ").result(2)
        self.assertTrue(first["submitted"])
        self.assertEqual(first["batch"]["prediction_generation"],
                         first["generation"])
        self.assertEqual(len(first["batch"]["entries"]), 3)
        self.assertTrue(duplicate["duplicate_event"])
        self.assertEqual(len(transport.batches), 2)
        self.assertGreater(second["generation"], first["generation"])
        bridge.close()

    def test_open_close_minimize_restore_events_and_failure_recovery(self):
        transport = MockTransport(fail_once=True)
        bridge = AsyncPriorBridge(BatchBuilder(self.whitelist, 3), transport)
        generations = []
        for event in ("open", "close", "foreground", "minimize", "restore"):
            result = bridge.submit_event(event, self.rows, "WPS").result(2)
            generations.append(result["generation"])
        self.assertEqual(generations, sorted(set(generations)))
        self.assertFalse(transport.attempts[0]["ok"])
        self.assertTrue(any(item["ok"] for item in transport.attempts[1:]))
        bridge.close()

    def test_dry_run_and_debugfs_single_write(self):
        transport = MockTransport()
        bridge = AsyncPriorBridge(BatchBuilder(self.whitelist, 1), transport,
                                  dry_run=True)
        result = bridge.submit_event("restore", self.rows, "FILES").result(2)
        self.assertTrue(result["dry_run"])
        self.assertEqual(transport.batches, [])
        bridge.close()
        line = DebugfsTransport.encode_batch(
            BatchBuilder(self.whitelist, 1).build(self.rows, "WPS",
                                                   timestamp_ns=10,
                                                   horizon_ns=20,
                                                   ttl_ns=30))
        self.assertEqual(line.count("\n"), 1)
        self.assertTrue(line.startswith("1 1 "))

    def test_ttl_horizon_and_foreground_contract(self):
        builder = BatchBuilder(self.whitelist, 1)
        with self.assertRaises(ValueError):
            builder.build(self.rows, "WPS", horizon_ns=0)
        with self.assertRaises(ValueError):
            builder.build(self.rows, "WPS", ttl_ns=0)
        batch = builder.build(self.rows, "OUTSIDE", timestamp_ns=10,
                              horizon_ns=20, ttl_ns=30)
        self.assertEqual(sum(row["foreground"]
                             for row in batch["entries"]), 0)

    def test_generation_resumes_from_kernel_across_bridge_restart(self):
        transport = MockTransport(current_generation=41)
        first_bridge = AsyncPriorBridge(
            BatchBuilder(self.whitelist, 1), transport)
        first = first_bridge.submit_event(
            "first-process", self.rows, "WPS").result(2)
        first_bridge.close()
        second_bridge = AsyncPriorBridge(
            BatchBuilder(self.whitelist, 1), transport)
        second = second_bridge.submit_event(
            "second-process", self.rows, "QQ").result(2)
        second_bridge.close()
        self.assertEqual(first["generation"], 42)
        self.assertEqual(second["generation"], 43)

    def test_generation_uses_max_of_local_and_kernel(self):
        transport = MockTransport(current_generation=41)
        bridge = AsyncPriorBridge(
            BatchBuilder(self.whitelist, 1, generation=50), transport)
        result = bridge.submit_event("local-ahead", self.rows, "WPS").result(2)
        bridge.close()
        self.assertEqual(result["generation"], 51)

    def test_generation_query_missing_or_malformed_fails_closed(self):
        class SubmitOnlyTransport:
            def submit(self, batch):
                return {"accepted": True}

        with self.assertRaises(GenerationQueryError):
            AsyncPriorBridge(BatchBuilder(self.whitelist, 1),
                             SubmitOnlyTransport())

    def test_debugfs_generation_query(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory) / "app_prior_batch"
            control.write_text(
                "schema=1 model=9 generation=77 timestamp_ns=1 "
                "horizon_ns=2 expiry_ns=3 entries=4\n", encoding="ascii")
            self.assertEqual(DebugfsTransport(control).current_generation(),
                             77)
            control.write_text("not metadata\n", encoding="ascii")
            with self.assertRaises(GenerationQueryError):
                DebugfsTransport(control).current_generation()


if __name__ == "__main__":
    unittest.main()
