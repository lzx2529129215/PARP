import unittest

from phase210b.labeler import label_selection, support


def window(active=False, observed=True):
    return {"files": {("8:0:1:1", "1:4"): {"observed": 1 if observed else 0, "active": 1 if active else 0}}}


class LabelerContracts(unittest.TestCase):
    def setUp(self):
        self.candidate = {
            "decision_id": "d1", "identity": "8:0:1:1|1:4|0",
            "file_key_metadata": "8:0:1:1", "partition_generation": "1:4", "segment_id": 0,
        }

    def test_positive_when_future_observed_active(self):
        rows = label_selection([self.candidate], [window(), window(active=True), window()], 0, (10,))
        self.assertEqual(rows[0]["status"], "positive")

    def test_not_observed_is_unknown_not_negative(self):
        rows = label_selection([self.candidate], [window(), window(observed=False), window()], 0, (10,))
        self.assertEqual(rows[0]["status"], "unknown")
        self.assertFalse(rows[0]["available"])

    def test_incomplete_horizon_is_unknown(self):
        rows = label_selection([self.candidate], [window()], 0, (10,))
        self.assertEqual(rows[0]["status"], "unknown")

    def test_pairwise_requires_available_positive_and_negative(self):
        rows = [
            {"selector_id": "S3", "quota_template": "Q_BALANCED", "app": "QQ", "session_id": "q", "horizon_seconds": 60, "decision_id": "d", "available": True, "status": "positive"},
            {"selector_id": "S3", "quota_template": "Q_BALANCED", "app": "QQ", "session_id": "q", "horizon_seconds": 60, "decision_id": "d", "available": True, "status": "negative"},
        ]
        self.assertEqual(support(rows)[0]["pairwise_evaluable_decisions"], 1)


if __name__ == "__main__":
    unittest.main()
