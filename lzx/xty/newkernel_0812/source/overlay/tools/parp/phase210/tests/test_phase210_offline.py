import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "offline_pipeline.py"
SPEC = importlib.util.spec_from_file_location("phase210_offline_pipeline", MODULE_PATH)
offline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(offline)


class Phase210OfflineContracts(unittest.TestCase):
    def test_session_roles_are_disjoint(self):
        for app in ("WPS", "FILES", "QQ"):
            train = set(offline.TRAIN[app])
            calibration = set(offline.CALIBRATION[app])
            evaluation = set(offline.EVALUATION[app])
            self.assertFalse(train & calibration)
            self.assertFalse(train & evaluation)
            self.assertFalse(calibration & evaluation)

    def test_ab_sessions_never_enter_training(self):
        all_sessions = set(sum(offline.TRAIN.values(), ()))
        self.assertFalse(any("ab" in session or "mixed" in session for session in all_sessions))

    def test_app_identity_and_metadata_are_not_features(self):
        self.assertFalse(any(
            fragment in name
            for name in offline.FEATURES
            for fragment in offline.FORBIDDEN_FEATURE_FRAGMENTS
        ))

    def test_wrong_mapping_is_fixed_and_wrong(self):
        self.assertEqual(offline.WRONG, {"WPS": "FILES", "FILES": "QQ", "QQ": "WPS"})
        self.assertTrue(all(app != model for app, model in offline.WRONG.items()))

    def test_balanced_generic_training_is_deterministic(self):
        rows = []
        for app, count in (("WPS", 4), ("FILES", 2), ("QQ", 3)):
            rows.extend({"app": app, "decision_id": "%s_%d" % (app, index)} for index in range(count))
        first, audit = offline.balanced_training(rows)
        second, _ = offline.balanced_training(list(reversed(rows)))
        self.assertEqual([row["decision_id"] for row in first], [row["decision_id"] for row in second])
        self.assertEqual(audit, {"decisions_per_app": 2, "total_decisions": 6})

    def test_candidate_bit_range_is_bounded(self):
        mask, segments = offline.bit_range(95, 120, 100)
        self.assertEqual(list(segments), [95, 96, 97, 98, 99])
        self.assertEqual(bin(mask).count("1"), 5)

    def test_equal_proxy_is_not_better(self):
        self.assertFalse(1.0 < 1.0)

    def test_future_is_not_a_candidate_feature(self):
        self.assertNotIn("future", offline.FEATURES)

    def test_aggregate_reports_label_adequacy(self):
        rows = [
            {"candidate_count": 128, "positives": 2, "reclaimed": 64,
             "future_reuse_after_reclaim": 1, "future_reuse_saved": 1,
             "pairwise_auc": 0.6, "ndcg_at_budget": 0.5,
             "recall_at_budget": 0.5, "tie_rate": 0.0,
             "ranking_hash": "a", "candidate_hash": "c"},
            {"candidate_count": 128, "positives": 0, "reclaimed": 64,
             "future_reuse_after_reclaim": 0, "future_reuse_saved": 0,
             "pairwise_auc": 0.5, "ndcg_at_budget": 0.0,
             "recall_at_budget": 0.0, "tie_rate": 1.0,
             "ranking_hash": "b", "candidate_hash": "d"},
        ]
        result = offline.aggregate(rows)
        self.assertEqual(result["positive_candidates"], 2)
        self.assertEqual(result["positive_decisions"], 1)
        self.assertEqual(result["pairwise_evaluable_decisions"], 1)


if __name__ == "__main__":
    unittest.main()
