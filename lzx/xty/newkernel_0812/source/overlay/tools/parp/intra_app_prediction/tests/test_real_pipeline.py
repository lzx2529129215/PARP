import hashlib
import tempfile
import unittest
from pathlib import Path

from intra_app_prediction.real_pipeline import (
    ACCEPTED_SESSIONS,
    Checkpoint,
    cumulative_future_labels,
    enforce_probability_monotonicity,
    fixture_lineage,
    parse_real_trace_line,
    probability_to_q15,
    quality_gates_pass,
    raw_hashes,
)
from intra_app_prediction.real_models import validate_prediction_contract


class RealTraceTests(unittest.TestCase):
    LINE = (
        " worker [003] ..... 8481.799470: parp_region_evidence: "
        "sample=14798 sample_time=8481799297338 pid=18439 tgid=18439 "
        "domain=5917 app=1 bind_generation=8 foreground_epoch=1 model=27 "
        "mm_cookie=4090792638006158612 type=0 align=1 "
        "region_start=94461792993280 region_end=94461793177600 "
        "logical_start=0 nr_pages=45 dev_major=8 dev_minor=5 inode=524382 "
        "file_version=1 file_size=1183448 file_pages=289 vma_signature=0 "
        "sample_us=5000 aggregation_us=1000000 access_evidence=0 age=1 "
        "confidence_q15=32767 reasons=0x0")

    def test_real_schema_sample_decodes(self):
        row = parse_real_trace_line(self.LINE, "wps_01", "boot", "run")
        self.assertEqual(row["event_time_ns"], 8481799297338)
        self.assertEqual(row["collection_time_ns"], 8481799470000)
        self.assertEqual(row["file_page_end_exclusive"], 45)
        self.assertEqual(row["file_id_tuple"], (8, 5, 524382, 1))

    def test_five_success_sessions_are_fixed(self):
        self.assertEqual(set(ACCEPTED_SESSIONS),
                         {"wps_01", "wps_02", "wps_03", "files_01", "files_02"})


class LabelAndOnlineTests(unittest.TestCase):
    def test_tail_availability_and_cumulative_monotonicity(self):
        states = {1: (True, .2, .1), 2: (False, 0, 0), 3: (True, .8, .7),
                  4: (True, .1, .1), 5: (True, .2, .2), 6: (True, .3, .3)}
        labels = cumulative_future_labels(states, 0, 6)
        self.assertLessEqual(labels[10]["access"], labels[30]["access"])
        self.assertLessEqual(labels[30]["access"], labels[60]["access"])
        tail = cumulative_future_labels(states, 6, 6)
        self.assertFalse(tail[10]["available"])
        self.assertIsNone(tail[10]["access"])

    def test_probability_and_q15_contract(self):
        calibrated, changed = enforce_probability_monotonicity(.8, .2, .5)
        self.assertEqual(calibrated, (.8, .8, .8))
        self.assertTrue(changed)
        self.assertEqual((probability_to_q15(-1), probability_to_q15(.5),
                          probability_to_q15(2)), (0, 16384, 32767))

    def test_not_observed_is_not_a_negative_label(self):
        labels = cumulative_future_labels({}, 0, 6)
        self.assertFalse(labels[10]["available"])
        self.assertIsNone(labels[10]["access"])

    def test_online_prediction_contract_checks_required_payload(self):
        payload = {
            "schema_version": 1, "run_id": "run", "app_id": 1,
            "domain_id": 9, "session_id": "wps_03",
            "model_type": "page_state_markov", "model_version": 1,
            "prediction_generation": 1, "generated_ns": 100,
            "ttl_ns": 60, "kernel_write": False,
            "future_features_used": False, "unknown_reason": None,
            "file_segments": [{
                "dev_major": 8, "dev_minor": 5, "inode": 1,
                "file_version": 1, "partition_generation": 1,
                "file_page_count": 20, "requested_bins": 100,
                "effective_bins": 20, "segment_id": 2,
                "probability_10s_q15": 1,
                "probability_30s_q15": 2,
                "probability_60s_q15": 3, "confidence_q15": 4,
            }],
            "anon_prediction": {
                "hot_bytes_10s": 10, "hot_bytes_30s": 10,
                "hot_bytes_60s": 10, "cooling_probability_q15": 7,
            },
        }
        validate_prediction_contract(payload)
        invalid = dict(payload)
        invalid.pop("run_id")
        with self.assertRaises(ValueError):
            validate_prediction_contract(invalid)


class RecoveryAndProvenanceTests(unittest.TestCase):
    def test_negative_named_audit_fact_cannot_invert_quality_gate(self):
        self.assertTrue(quality_gates_pass({
            "sessions_5_of_5": True,
            "no_future_labels_cross_session": True,
        }))
        with self.assertRaises(ValueError):
            quality_gates_pass({"future_labels_cross_session": False})

    def test_checkpoint_does_not_repeat_completed_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "checkpoint.json"
            checkpoint = Checkpoint(path, "manifest")
            checkpoint.complete_session("wps_01", {"events": 10})
            restored = Checkpoint(path, "manifest")
            self.assertTrue(restored.session_complete("wps_01"))
            self.assertFalse(restored.session_complete("wps_02"))

    def test_fixture_lineage_recognizes_controlled_copies(self):
        rows = fixture_lineage([("wps_01", "a"), ("wps_02", "b")],
                               controlled_source_hash="source")
        self.assertTrue(rows["same_document_temporal_generalization"])
        self.assertEqual(rows["unseen_document_evaluation"], "NOT_AVAILABLE")

    def test_raw_hash_detects_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "raw" / "x"
            path.parent.mkdir()
            path.write_text("a")
            before = raw_hashes(root, [path])
            path.write_text("b")
            after = raw_hashes(root, [path])
            self.assertNotEqual(before, after)
            self.assertEqual(before[0][1], hashlib.sha256(b"a").hexdigest())


if __name__ == "__main__":
    unittest.main()
