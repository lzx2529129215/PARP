import unittest

from intra_app_prediction.weighted_coverage import IntervalEvidence, coverage_summary


class WeightedCoverageTests(unittest.TestCase):
    def test_single_interval(self):
        out = coverage_summary(0, 10, [IntervalEvidence(2, 7, 1.0, 3)])
        self.assertEqual((out.accessed_unique_pages, out.coverage_ratio), (5, 0.5))

    def test_non_overlapping(self):
        out = coverage_summary(0, 10, [IntervalEvidence(0, 2, .5, 1), IntervalEvidence(8, 10, .5, 2)])
        self.assertAlmostEqual(out.weighted_coverage_ratio, .2)

    def test_complete_overlap_uses_max(self):
        out = coverage_summary(0, 10, [IntervalEvidence(0, 10, .2, 1), IntervalEvidence(0, 10, .7, 2)])
        self.assertAlmostEqual(out.weighted_coverage_ratio, .7)

    def test_partial_overlap(self):
        out = coverage_summary(0, 10, [IntervalEvidence(0, 6, .5, 1), IntervalEvidence(4, 10, 1.0, 2)])
        self.assertAlmostEqual(out.weighted_access_pages, 8.0)

    def test_clips_across_segment(self):
        out = coverage_summary(10, 20, [IntervalEvidence(0, 15, 1.0, 1), IntervalEvidence(18, 30, 1.0, 2)])
        self.assertEqual(out.accessed_unique_pages, 7)

    def test_weight_zero_is_observed_not_accessed(self):
        out = coverage_summary(0, 10, [IntervalEvidence(0, 10, 0.0, 5)])
        self.assertEqual((out.observed_unique_pages, out.accessed_unique_pages), (10, 0))

    def test_weight_one(self):
        self.assertEqual(coverage_summary(0, 7, [IntervalEvidence(0, 7, 1, 1)]).weighted_access_pages, 7)

    def test_multiple_weights(self):
        rows = [IntervalEvidence(0, 3, .2, 1), IntervalEvidence(3, 6, .4, 2), IntervalEvidence(6, 10, .8, 3)]
        self.assertAlmostEqual(coverage_summary(0, 10, rows).weighted_access_pages, 5.0)

    def test_file_tail_is_clipped(self):
        self.assertEqual(coverage_summary(90, 100, [IntervalEvidence(95, 110, 1, 1)]).accessed_unique_pages, 5)

    def test_duplicate_samples_do_not_double_count(self):
        row = IntervalEvidence(0, 10, .5, 3)
        self.assertAlmostEqual(coverage_summary(0, 10, [row, row]).weighted_coverage_ratio, .5)

    def test_ratios_never_exceed_one(self):
        rows = [IntervalEvidence(0, 10, 1, i) for i in range(20)]
        out = coverage_summary(0, 10, rows)
        self.assertLessEqual(max(out.coverage_ratio, out.weighted_coverage_ratio), 1.0)

    def test_invalid_interval_or_weight_rejected(self):
        for args in ((2, 1, .5, 1), (0, 1, -1, 1), (0, 1, 1.1, 1)):
            with self.assertRaises(ValueError):
                IntervalEvidence(*args)


if __name__ == "__main__":
    unittest.main()
