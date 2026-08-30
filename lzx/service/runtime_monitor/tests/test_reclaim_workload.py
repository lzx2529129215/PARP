from __future__ import annotations

import unittest

from runtime_monitor.core.reclaim_workload import (  # lzx-note
    WORKLOAD_ANON_HEAVY,
    WORKLOAD_FILE_CLEAN,
    WORKLOAD_FILE_DIRTY,
    WORKLOAD_MIXED,
    classify_memory_stat,
)


MIB = 1024 * 1024


class ReclaimWorkloadTests(unittest.TestCase):
    def test_anon_heavy_profile_biases_anon_reclaim(self) -> None:
        profile = classify_memory_stat(11, {"anon": 96 * MIB, "file": 16 * MIB})
        self.assertTrue(profile.valid)
        self.assertEqual(profile.workload_class, WORKLOAD_ANON_HEAVY)
        self.assertEqual(profile.swappiness, 140)
        self.assertFalse(profile.allow_writepage)

    def test_clean_file_profile_biases_file_reclaim(self) -> None:
        profile = classify_memory_stat(12, {"anon": 16 * MIB, "file": 96 * MIB})
        self.assertTrue(profile.valid)
        self.assertEqual(profile.workload_class, WORKLOAD_FILE_CLEAN)
        self.assertEqual(profile.swappiness, 40)
        self.assertFalse(profile.allow_writepage)

    def test_dirty_file_profile_permits_bounded_writeback(self) -> None:
        profile = classify_memory_stat(
            13, {"anon": 16 * MIB, "file": 96 * MIB, "file_dirty": 72 * MIB}
        )
        self.assertTrue(profile.valid)
        self.assertEqual(profile.workload_class, WORKLOAD_FILE_DIRTY)
        self.assertEqual(profile.swappiness, 20)
        self.assertTrue(profile.allow_writepage)
        self.assertEqual(profile.workload_hint() & 0x0F, WORKLOAD_FILE_DIRTY)
        self.assertTrue(profile.workload_hint() & (1 << 24))

    def test_balanced_scope_keeps_decisive_dirty_profile_valid(self) -> None:
        profile = classify_memory_stat(
            16, {"anon": 101 * MIB, "file": 100 * MIB, "file_dirty": 40 * MIB}
        )
        self.assertTrue(profile.valid)
        self.assertEqual(profile.workload_class, WORKLOAD_FILE_DIRTY)
        self.assertEqual(profile.confidence_q8, 128)
        self.assertTrue(profile.allow_writepage)

    def test_mixed_profile_preserves_native_page_type_balance(self) -> None:
        profile = classify_memory_stat(14, {"anon": 48 * MIB, "file": 48 * MIB})
        self.assertTrue(profile.valid)
        self.assertEqual(profile.workload_class, WORKLOAD_MIXED)
        self.assertEqual(profile.swappiness, 60)

    def test_tiny_scope_is_not_allowed_to_change_reclaim(self) -> None:
        profile = classify_memory_stat(15, {"anon": 2 * MIB, "file": 1 * MIB})
        self.assertFalse(profile.valid)
        self.assertEqual(profile.workload_hint(), 0)


if __name__ == "__main__":
    unittest.main()
