#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
import unittest

from window_analyze import WindowState, aggregate


class WindowTest(unittest.TestCase):
    def test_nested_windows_duplicate_and_disorder(self):
        now = 100_000_000_000
        state = WindowState()
        samples = [
            {"sample_id": 1, "timestamp_ns": now - 50_000_000_000,
             "nr_accesses": 1},
            {"sample_id": 2, "timestamp_ns": now - 20_000_000_000,
             "nr_accesses": 2},
            {"sample_id": 3, "timestamp_ns": now,
             "nr_accesses": 3},
        ]
        for sample in samples:
            self.assertTrue(state.add(sample))
        summary = state.summarize(now)
        self.assertEqual(summary["access_evidence_10s"], 3)
        self.assertEqual(summary["access_evidence_30s"], 5)
        self.assertEqual(summary["access_evidence_60s"], 6)
        self.assertFalse(state.add(samples[-1]))
        self.assertFalse(state.add({"sample_id": 4,
                                    "timestamp_ns": now - 10_000_000_000,
                                    "nr_accesses": 4}))
        self.assertEqual(state.duplicate_count, 1)
        self.assertEqual(state.out_of_order_count, 1)

    def test_file_move_same_logical_offset(self):
        common = {"region_type": "FILE", "domain_id": 1, "dev_major": 8,
                  "dev_minor": 1, "inode": 9, "file_version": 2,
                  "start_index": 4, "nr_pages": 2, "nr_accesses": 1}
        records = [dict(common, sample_id=1, timestamp_ns=1, region_start=0x1000),
                   dict(common, sample_id=2, timestamp_ns=2, region_start=0x9000)]
        summaries, _ = aggregate(records)
        self.assertEqual(len(summaries), 1)

    def test_anon_epoch_invalidates_identity(self):
        common = {"region_type": "ANON", "domain_id": 1, "mm_cookie": 2,
                  "vma_signature": 3, "relative_start_pages": 0,
                  "nr_pages": 4, "nr_accesses": 1}
        records = [dict(common, foreground_epoch_id=1, sample_id=1, timestamp_ns=1),
                   dict(common, foreground_epoch_id=2, sample_id=2, timestamp_ns=2)]
        summaries, _ = aggregate(records)
        self.assertEqual(len(summaries), 2)


if __name__ == "__main__":
    unittest.main()
