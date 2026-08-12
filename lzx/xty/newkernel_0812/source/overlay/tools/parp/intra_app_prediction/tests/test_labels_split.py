import unittest

from intra_app_prediction.future_labels import attach_future_labels
from intra_app_prediction.splitter import chronological_split


class FutureLabelTests(unittest.TestCase):
    def rows(self):
        return [{"window_start_ns": i * 10_000_000_000, "coverage_ratio": float(i % 2),
                 "weighted_coverage_ratio": 0.0, "window_id": str(i), "session_id": "s"}
                for i in range(10)]

    def test_next_10_label(self):
        out = attach_future_labels(self.rows())
        self.assertEqual(out[0]["label_next_10s_access"], 1)

    def test_30_and_60_max(self):
        out = attach_future_labels(self.rows())
        self.assertEqual((out[0]["label_next_30s_max_coverage"], out[0]["label_next_60s_max_coverage"]), (1.0, 1.0))

    def test_tail_availability_not_zero_filled(self):
        tail = attach_future_labels(self.rows())[-1]
        self.assertFalse(tail["label_available_10s"])
        self.assertIsNone(tail["label_next_10s_access"])

    def test_future_active_thresholds(self):
        out = attach_future_labels(self.rows())
        self.assertTrue(out[0]["future_active_80_10s"])


class SplitTests(unittest.TestCase):
    def sessions(self, count=10):
        return [{"window_id": f"{s}-{i}", "session_id": f"s{s}",
                 "window_start_ns": (s * 1000 + i * 10) * 1_000_000_000}
                for s in range(count) for i in range(12)]

    def test_session_does_not_cross_split(self):
        split = chronological_split(self.sessions())
        sets = [{r["session_id"] for r in split[x]} for x in ("train", "val", "test")]
        self.assertFalse((sets[0] & sets[1]) | (sets[0] & sets[2]) | (sets[1] & sets[2]))

    def test_sixty_second_purge_gap(self):
        split = chronological_split(self.sessions(), purge_gap_ns=60_000_000_000)
        self.assertGreaterEqual(min(r["window_start_ns"] for r in split["val"]) - max(r["window_start_ns"] for r in split["train"]), 60_000_000_000)

    def test_window_ids_unique(self):
        rows = self.sessions()
        rows.append(dict(rows[0]))
        with self.assertRaises(ValueError):
            chronological_split(rows)

    def test_train_vocab_excludes_test_files(self):
        split = chronological_split(self.sessions())
        train_vocab = {r["session_id"] for r in split["train"]}
        self.assertNotIn(split["test"][0]["session_id"], train_vocab)

    def test_normalization_is_train_only(self):
        split = chronological_split(self.sessions())
        self.assertEqual(split["audit"]["normalization_source"], "train")

    def test_future_horizon_does_not_cross_boundary(self):
        split = chronological_split(self.sessions(), purge_gap_ns=60_000_000_000)
        self.assertTrue(split["audit"]["future_horizon_isolated"])


if __name__ == "__main__":
    unittest.main()
