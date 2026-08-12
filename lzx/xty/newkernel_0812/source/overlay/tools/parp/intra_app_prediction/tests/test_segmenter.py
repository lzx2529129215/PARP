import unittest

from intra_app_prediction.file_identity import FileIdentity, PartitionTracker
from intra_app_prediction.segmenter import page_to_segment, segment_bounds


class SegmenterTests(unittest.TestCase):
    def test_ten_bins(self):
        self.assertEqual(page_to_segment(55, 100, 10).segment_id, 5)

    def test_hundred_bins(self):
        self.assertEqual(page_to_segment(555, 1000, 100).segment_id, 55)

    def test_thousand_bins(self):
        self.assertEqual(page_to_segment(5555, 10000, 1000).segment_id, 555)

    def test_first_page(self):
        spec = page_to_segment(0, 103, 10)
        self.assertEqual((spec.segment_id, spec.start_page), (0, 0))

    def test_last_page(self):
        spec = page_to_segment(102, 103, 10)
        self.assertEqual((spec.segment_id, spec.end_page_exclusive), (9, 103))

    def test_even_division(self):
        self.assertEqual(segment_bounds(4, 100, 10), (40, 50))

    def test_uneven_division_is_complete(self):
        bounds = [segment_bounds(i, 103, 10) for i in range(10)]
        self.assertEqual(bounds[0][0], 0)
        self.assertEqual(bounds[-1][1], 103)
        self.assertTrue(all(a[1] == b[0] for a, b in zip(bounds, bounds[1:])))

    def test_small_file_caps_effective_bins(self):
        spec = page_to_segment(2, 3, 1000)
        self.assertEqual((spec.effective_bins, spec.segment_id), (3, 2))

    def test_single_page(self):
        spec = page_to_segment(0, 1, 1000)
        self.assertEqual((spec.effective_bins, spec.start_page, spec.end_page_exclusive), (1, 0, 1))

    def test_out_of_range_and_empty_rejected(self):
        for args in ((0, 0, 10), (-1, 10, 10), (10, 10, 10), (0, 10, 0)):
            with self.assertRaises(ValueError):
                page_to_segment(*args)

    def test_u64_scale_mapping_avoids_overflow(self):
        pages = (1 << 64) - 1
        spec = page_to_segment(pages - 1, pages, 1000)
        self.assertEqual((spec.segment_id, spec.end_page_exclusive), (999, pages))

    def test_partition_generation_changes_on_size_or_version(self):
        tracker = PartitionTracker()
        identity = FileIdentity(8, 5, 1, 7)
        a = tracker.observe(identity, 100, 409600)
        b = tracker.observe(identity, 100, 409600)
        c = tracker.observe(identity, 101, 413696)
        d = tracker.observe(FileIdentity(8, 5, 1, 8), 101, 413696)
        self.assertEqual((a, b, c, d), (1, 1, 2, 1))


if __name__ == "__main__":
    unittest.main()
