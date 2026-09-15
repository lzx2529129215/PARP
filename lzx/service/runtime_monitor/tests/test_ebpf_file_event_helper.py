from __future__ import annotations

import unittest
from pathlib import Path
import sys
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_monitor.helpers.ebpf_file_event_helper import (
    EBPFFileEventHelper,
    PERF_BUFFER_PAGES,
)


class EBPFFileEventHelperTests(unittest.TestCase):
    def test_service_has_bounded_memlock_for_expanded_perf_rings(self) -> None:
        unit = (ROOT / "systemd/parp-file-events@.service").read_text(encoding="utf-8")
        self.assertIn("LimitMEMLOCK=128M", unit)
        self.assertIn("ReadWritePaths=/sys/kernel/mm/page_idle/bitmap", unit)
        self.assertNotIn("CAP_SYSLOG", unit)
        self.assertIn("ProtectKernelLogs=true", unit)

    def test_bpf_source_has_separate_compact_profiles_and_double_buffer(self) -> None:
        source = (ROOT / "ebpf/file_events.bpf.c").read_text(encoding="utf-8")
        self.assertIn("BPF_ARRAY(page_hotset_only, u32, 1)", source)
        self.assertIn("is_compact_page_profile()", source)
        self.assertIn("BPF_HASH(page_window_epoch0", source)
        self.assertIn("BPF_HASH(page_window_epoch1", source)
        self.assertIn("BPF_HASH(page_window_ranges_epoch0", source)
        self.assertIn("BPF_HASH(page_window_ranges_epoch1", source)
        self.assertIn("record_page_window_range(", source)
        self.assertIn("note_page_window_overflow(epoch)", source)
        self.assertIn("TRACEPOINT_PROBE(filemap, mm_filemap_get_pages)", source)
        self.assertIn("TRACEPOINT_PROBE(filemap, mm_filemap_fault)", source)
        self.assertIn("kprobe__filemap_fault", source)
        self.assertIn("kretprobe__filemap_fault", source)
        self.assertIn("TRACEPOINT_PROBE(filemap, mm_filemap_add_to_page_cache)", source)
        self.assertIn("kprobe__folio_mark_accessed_native", source)
        self.assertIn("update_page_window_pfn_if_present", source)
        self.assertIn("page_pointer_to_pfn", source)
        self.assertIn("BPF_ARRAY(vmemmap_calibrated_base", source)
        self.assertNotIn("int kprobe__folio_mark_accessed(", source)

    def test_perf_buffer_capacity_covers_bursty_gui_startup(self) -> None:
        self.assertEqual(PERF_BUFFER_PAGES, 1024)
        self.assertEqual(PERF_BUFFER_PAGES & (PERF_BUFFER_PAGES - 1), 0)

    def test_lost_callback_accepts_bcc_one_and_two_argument_abis(self) -> None:
        helper = object.__new__(EBPFFileEventHelper)
        helper.perf_lost = 0
        helper.file_perf_lost = 0
        helper.cache_perf_lost = 0
        helper.workload_perf_lost = 0
        helper._send_status = Mock()

        helper._on_lost(7)
        helper._on_lost(3, 5)

        self.assertEqual(helper.perf_lost, 12)
        self.assertEqual(helper.file_perf_lost, 12)
        self.assertEqual(
            helper._send_status.call_args_list[0].args,
            ("PERF_LOST", "file ring lost 7 eBPF perf event(s)"),
        )
        self.assertEqual(
            helper._send_status.call_args_list[1].args,
            ("PERF_LOST", "file ring lost 5 eBPF perf event(s)"),
        )

    def test_workload_ring_loss_is_audited_without_poisoning_page_integrity(self) -> None:
        helper = object.__new__(EBPFFileEventHelper)
        helper.perf_lost = 0
        helper.file_perf_lost = 0
        helper.cache_perf_lost = 0
        helper.workload_perf_lost = 0
        helper._send_status = Mock()

        helper._on_workload_lost(123)

        self.assertEqual(helper.perf_lost, 0)
        self.assertEqual(helper.workload_perf_lost, 123)
        self.assertEqual(
            helper._send_status.call_args.args,
            (
                "WORKLOAD_PERF_LOST",
                "workload ring lost 123 eBPF perf event(s)",
            ),
        )

    def test_page_hotset_profile_discards_workload_before_decoding(self) -> None:
        helper = object.__new__(EBPFFileEventHelper)
        helper.event_profile = "page-hotset"
        # Invalid ctypes data would fail immediately if the profile guard did
        # not return before decoding the workload event.
        self.assertIsNone(helper._on_workload_event(0, 0, 0))

    def test_helper_page_window_ranges_merge_without_per_page_expansion(self) -> None:
        ranges, page_count, repeated = EBPFFileEventHelper._compressed_page_ranges({
            (8, 1, 77): [(0, 4), (3, 8), (20, 20)],
        })
        self.assertEqual(page_count, 10)
        self.assertEqual(repeated, 2)
        self.assertEqual(
            [(row["start_page_index"], row["page_count"]) for row in ranges],
            [(0, 9), (20, 1)],
        )


if __name__ == "__main__":
    unittest.main()
