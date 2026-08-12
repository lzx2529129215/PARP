import tempfile
import unittest
from pathlib import Path

from intra_app_prediction.anon_features import summarize_anon
from intra_app_prediction.baselines import GlobalFrequency, LastWindow, RecentFrequency
from intra_app_prediction.online_dry_run import DryRunPredictor, probability_to_q15
from intra_app_prediction.state_markov import PageStateMarkov


class AnonTests(unittest.TestCase):
    def rows(self):
        return [
            {"domain_id": 1, "foreground_epoch": 2, "mm_cookie": 3, "nr_pages": 10, "nr_accesses": 0, "age": 5},
            {"domain_id": 1, "foreground_epoch": 2, "mm_cookie": 3, "nr_pages": 10, "nr_accesses": 5, "age": 1},
        ]

    def test_domain_summary(self):
        out = summarize_anon(self.rows(), 10)
        self.assertEqual((out["anon_total_bytes"], out["anon_region_count"]), (81920, 2))

    def test_epoch_isolation(self):
        rows = self.rows() + [{**self.rows()[0], "foreground_epoch": 4}]
        with self.assertRaises(ValueError):
            summarize_anon(rows, 10)

    def test_mm_cookie_isolation(self):
        rows = self.rows() + [{**self.rows()[0], "mm_cookie": 4}]
        with self.assertRaises(ValueError):
            summarize_anon(rows, 10)

    def test_boot_isolation(self):
        rows = [{**r, "boot_id": "a"} for r in self.rows()]
        rows.append({**rows[0], "boot_id": "b"})
        with self.assertRaises(ValueError):
            summarize_anon(rows, 10)


class PredictorTests(unittest.TestCase):
    def test_last_window(self):
        self.assertEqual(LastWindow().predict([{1, 2}]), {1: 1.0, 2: 1.0})

    def test_recent_frequency(self):
        self.assertEqual(RecentFrequency(2).predict([{1}, {1, 2}]), {1: 1.0, 2: .5})

    def test_global_frequency(self):
        model = GlobalFrequency().fit([{1}, {1, 2}, {2}])
        self.assertAlmostEqual(model.predict([])[1], 2 / 3)

    def test_markov_transition(self):
        model = PageStateMarkov().fit([(1, "0_10", 2), (1, "0_10", 2), (1, "0_10", 3)])
        self.assertGreater(model.predict(1, "0_10")[2], model.predict(1, "0_10")[3])

    def test_unknown_state(self):
        self.assertEqual(PageStateMarkov().predict(99, "0_10"), {})

    def test_q15_range(self):
        self.assertEqual((probability_to_q15(-1), probability_to_q15(1), probability_to_q15(.5)), (0, 32767, 16384))

    def test_generation_continues(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "generation.json"
            predictor = DryRunPredictor(state)
            self.assertEqual((predictor.next_generation(), predictor.next_generation()), (1, 2))

    def test_unknown_file_has_no_strong_prediction(self):
        with tempfile.TemporaryDirectory() as d:
            predictor = DryRunPredictor(Path(d) / "g.json", known_files={"known"})
            self.assertEqual(predictor.file_prediction("unknown", {}), {"status": "UNKNOWN"})

    def test_dry_run_has_no_kernel_write_method(self):
        self.assertFalse(hasattr(DryRunPredictor, "write_kernel"))


if __name__ == "__main__":
    unittest.main()
