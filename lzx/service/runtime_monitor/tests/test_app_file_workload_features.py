from __future__ import annotations

import unittest
import sys
from pathlib import Path

MONITOR_DIR = Path(__file__).resolve().parents[1]
if str(MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(MONITOR_DIR))

from runtime_monitor.core.app_feature_builder import AppFeatureBuilder
from runtime_monitor.core.schema import APP_STATE_1S_FIELDS


class AppFileWorkloadFeatureTests(unittest.TestCase):
    @staticmethod
    def _read(ts_ns: int, offset: int, size: int = 4096) -> dict[str, int | str]:
        return {
            "event": "read",
            "ts_ns": ts_ns,
            "device": 2050,
            "inode": 77,
            "offset": offset,
            "returned_size": size,
            "offset_valid": 1,
            "file_identity_valid": 1,
        }

    def test_read_pattern_uses_device_inode_and_cross_window_offset(self) -> None:
        builder = AppFeatureBuilder(precise_file_events=True)
        first = builder._read_pattern_stats("WPS", [self._read(1, 0)])
        self.assertEqual(first["unknown"], 1)
        self.assertEqual(first["label"], "UNKNOWN")

        second = builder._read_pattern_stats("WPS", [
            self._read(2, 4096),   # 与上一窗口结尾相接：顺序读
            self._read(3, 0),      # 回到较小 offset：循环/回绕读
            self._read(4, 32768),  # 向前跳过空洞：随机读
        ])
        self.assertEqual(second["sequential"], 1)
        self.assertEqual(second["cyclic"], 1)
        self.assertEqual(second["random"], 1)
        self.assertEqual(second["label"], "MIXED")

    def test_latency_percentile_and_extended_schema(self) -> None:
        values = [10, 20, 30, 40, 100]
        self.assertEqual(AppFeatureBuilder._percentile(values, 0.95), 100)
        for field in (
            "read_requested_bytes_1s",
            "read_latency_ns_p95_1s",
            "lseek_cnt_1s",
            "read_access_pattern",
            "page_access_cnt_1s",
            "eviction_cnt_1s",
            "user_page_fault_cnt_1s",
            "attributed_block_io_bytes_1s",
            "offcpu_blocked_ns_1s",
            "iowait_ns_1s",
        ):
            self.assertIn(field, APP_STATE_1S_FIELDS)


if __name__ == "__main__":
    unittest.main()
