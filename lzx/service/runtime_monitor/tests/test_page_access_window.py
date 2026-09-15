from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from runtime_monitor.core.page_access_window import (
    ATTR_DIRECT_WPS,
    ATTR_WPS_MAPPED_SHARED,
    FileCatalog,
    FilePageKey,
    FramedBinaryWriter,
    PageAccessWindowCapture,
    PageIdleBackend,
    RegisteredPage,
    SOURCE_DIRECT_WPS_ACCESS,
    SOURCE_EVICT_BEFORE_SAMPLE,
    SOURCE_FILE_FAULT,
    SOURCE_NEW_RESIDENT,
    SOURCE_PFN_CHANGED,
    SOURCE_PAGE_IDLE_CLEARED,
    WINDOW_MAGIC,
    compress_access_ranges,
    read_framed_binary,
)


class FakeIdleBackend:
    def __init__(self, *, accessed: set[int], shared: set[int] | None = None) -> None:
        self.accessed = set(accessed)
        self.shared = set(shared or set())
        self.armed: list[set[int]] = []
        self.closed = False

    def arm(self, pfns):
        self.armed.append(set(pfns))
        return 2_000_000, []

    def sample(self, pfns):
        candidates = set(pfns)
        return (
            candidates & self.accessed,
            {pfn: 2 for pfn in candidates & self.shared},
            0,
            [],
        )

    def close(self) -> None:
        self.closed = True


class FakeScanner:
    def __init__(self, rows: dict[int, list[RegisteredPage]]) -> None:
        self.rows = rows

    def scan_pid(self, pid: int):
        yield from self.rows.get(pid, [])


class PageAccessWindowTests(unittest.TestCase):
    def test_framed_binary_round_trip_and_truncation_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "windows.bin"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            writer = FramedBinaryWriter(fd, magic=WINDOW_MAGIC, metadata={"kind": "test"})
            writer.write({"window_id": 1, "pages": [1, 2, 3]})
            writer.close()

            metadata, rows = read_framed_binary(path)
            self.assertEqual(metadata["magic"], "PARPPGW1")
            self.assertEqual(rows, [{"pages": [1, 2, 3], "window_id": 1}])

            broken = Path(tmp) / "broken.bin"
            broken.write_bytes(path.read_bytes()[:-1])
            with self.assertRaisesRegex(ValueError, "truncated frame payload"):
                read_framed_binary(broken)
            recovered_metadata, recovered = read_framed_binary(
                broken, recover_truncated=True
            )
            self.assertTrue(recovered_metadata["truncated_tail_recovered"])
            self.assertEqual(recovered, [])

    def test_fixture_catalog_id_is_stable_between_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = root / "fixture.pdf"
            fixture.write_bytes(b"fixed input")
            values = fixture.stat()
            key = FilePageKey(
                os.major(values.st_dev), os.minor(values.st_dev), values.st_ino, 0
            )
            fixture_catalog = {
                str(fixture): {
                    "logical_id": "wps-input:fixture.pdf",
                    "content_sha256": "prepared-before-capture",
                }
            }
            ids = []
            for index in range(2):
                fd = os.open(
                    root / f"catalog-{index}.jsonl",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                catalog = FileCatalog(
                    fd, session_id=f"s{index}", boot_id="boot",
                    fixture_catalog=fixture_catalog,
                )
                ids.append(catalog.resolve(key))
                catalog.close()
            self.assertEqual(ids[0], ids[1])

    def test_range_compression_requires_contiguous_page_and_pfn(self) -> None:
        rows = compress_access_ranges([
            {
                "file_catalog_id": 7, "page_index": 10, "pfn": 100,
                "source_mask": SOURCE_DIRECT_WPS_ACCESS,
                "attribution_mask": ATTR_DIRECT_WPS,
            },
            {
                "file_catalog_id": 7, "page_index": 11, "pfn": 101,
                "source_mask": SOURCE_DIRECT_WPS_ACCESS,
                "attribution_mask": ATTR_DIRECT_WPS,
            },
            {
                "file_catalog_id": 7, "page_index": 12, "pfn": 500,
                "source_mask": SOURCE_DIRECT_WPS_ACCESS,
                "attribution_mask": ATTR_DIRECT_WPS,
            },
        ])
        self.assertEqual([int(row["page_count"]) for row in rows], [2, 1])
        self.assertEqual(rows[0]["source_mask"], ["DIRECT_WPS_ACCESS"])

    def test_word_masks_touch_only_requested_pfn_words(self) -> None:
        self.assertEqual(
            PageIdleBackend._word_masks([1, 63, 64, 65]),
            {0: (1 << 1) | (1 << 63), 1: 3},
        )

    def test_capture_merges_direct_and_page_idle_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mapped_path = root / "fixture.docx"
            mapped_path.write_bytes(b"x" * 8192)
            values = mapped_path.stat()
            major = os.major(values.st_dev)
            minor = os.minor(values.st_dev)
            page0 = RegisteredPage(
                FilePageKey(major, minor, values.st_ino, 0),
                pfn=100,
                path=str(mapped_path),
                mapped_pids={123},
            )
            page1 = RegisteredPage(
                FilePageKey(major, minor, values.st_ino, 1),
                pfn=101,
                path=str(mapped_path),
                mapped_pids={123},
            )
            fake_idle = FakeIdleBackend(accessed={100}, shared={100})
            names = [
                "page_access_windows.bin", "page_lifecycle.bin",
                "file_catalog.jsonl", "window_summary.csv", "manifest.json",
            ]
            fds = [
                os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                for name in names
            ]
            capture = PageAccessWindowCapture(
                window_fd=fds[0], lifecycle_fd=fds[1], catalog_fd=fds[2],
                summary_fd=fds[3], manifest_fd=fds[4],
                session_id="test-session", target_app="WPS", app_id=1,
                window_ms=100,
                idle_backend=fake_idle,
                scanner=FakeScanner({123: [page0, page1]}),
                lock_path=root / "page-idle.lock",
            )
            capture.update_processes({123: ("WPS", "gui", "1")})
            capture.arm_initial_window()
            record = capture.rotate([{
                "device_major": major,
                "device_minor": minor,
                "inode": values.st_ino,
                "page_index": 1,
                "pfn": 101,
                "source_mask": SOURCE_DIRECT_WPS_ACCESS,
                "first_boot_ns": capture.window_start_boot_ns + 1,
                "last_boot_ns": capture.window_start_boot_ns + 2,
                "tgid": 123,
                "tid": 124,
            }])
            capture.close()

            self.assertTrue(record["quality"]["valid"])
            self.assertEqual(record["accessed_page_count"], 2)
            decoded_ranges = record["page_ranges"]
            shared = next(
                row for row in decoded_ranges
                if "WPS_MAPPED_SHARED" in row["attribution_mask"]
            )
            direct = next(
                row for row in decoded_ranges
                if "DIRECT_WPS" in row["attribution_mask"]
            )
            self.assertIn("PAGE_IDLE_CLEARED", shared["source_mask"])
            self.assertIn("DIRECT_WPS_ACCESS", direct["source_mask"])
            _metadata, windows = read_framed_binary(root / "page_access_windows.bin")
            self.assertEqual(len(windows), 1)
            catalog_text = (root / "file_catalog.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(str(mapped_path), catalog_text)
            self.assertIn("path-sha256:", catalog_text)
            self.assertTrue(fake_idle.closed)

    def test_duplicate_direct_hits_are_deduplicated_and_unresolved_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = [
                "page_access_windows.bin", "page_lifecycle.bin",
                "file_catalog.jsonl", "window_summary.csv", "manifest.json",
            ]
            fds = [
                os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                for name in names
            ]
            capture = PageAccessWindowCapture(
                window_fd=fds[0], lifecycle_fd=fds[1], catalog_fd=fds[2],
                summary_fd=fds[3], manifest_fd=fds[4],
                session_id="dedup", target_app="WPS", app_id=1,
                window_ms=100, idle_backend=FakeIdleBackend(accessed=set()),
                scanner=FakeScanner({}), lock_path=root / "page-idle.lock",
            )
            capture.arm_initial_window()
            row = {
                "device_major": 8, "device_minor": 1, "inode": 77,
                "page_index": 9, "pfn": 0,
                "source_mask": SOURCE_DIRECT_WPS_ACCESS,
                "first_boot_ns": capture.window_start_boot_ns + 1,
                "last_boot_ns": capture.window_start_boot_ns + 2,
                "tgid": 123, "tid": 123,
            }
            record = capture.rotate([row, row])
            capture.close()
            self.assertEqual(record["accessed_page_count"], 1)
            self.assertFalse(record["quality"]["valid"])
            self.assertIn("PFN_UNRESOLVED", record["quality"]["invalid_reasons"])
            self.assertIn(
                "UNRESOLVED", record["page_ranges"][0]["attribution_mask"]
            )

    def test_evicted_or_reused_pfn_is_not_false_idle_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mapped = root / "mapped.bin"
            mapped.write_bytes(b"x" * 4096)
            values = mapped.stat()
            key = FilePageKey(
                os.major(values.st_dev), os.minor(values.st_dev), values.st_ino, 0
            )
            page = RegisteredPage(key, pfn=200, path=str(mapped), mapped_pids={44})
            names = ["w.bin", "l.bin", "c.jsonl", "s.csv", "m.json"]
            fds = [os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600) for name in names]
            capture = PageAccessWindowCapture(
                window_fd=fds[0], lifecycle_fd=fds[1], catalog_fd=fds[2],
                summary_fd=fds[3], manifest_fd=fds[4], session_id="reuse",
                target_app="WPS", app_id=1, window_ms=100,
                idle_backend=FakeIdleBackend(accessed={200}),
                scanner=FakeScanner({44: [page]}), lock_path=root / "lock",
            )
            capture.update_processes({44: ("WPS", "gui", "1")})
            capture.arm_initial_window()
            capture.record_lifecycle({
                "event_type": "EVICT", "device_major": key.device_major,
                "device_minor": key.device_minor, "inode": key.inode,
                "page_index": 0, "pfn": 200, "page_order": 0,
            })
            record = capture.rotate([])
            capture.close()
            self.assertEqual(record["accessed_page_count"], 0)

    def test_fault_add_evict_keeps_transient_pfn_and_readd_marks_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ["w.bin", "l.bin", "c.jsonl", "s.csv", "m.json"]
            fds = [
                os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                for name in names
            ]
            capture = PageAccessWindowCapture(
                window_fd=fds[0], lifecycle_fd=fds[1], catalog_fd=fds[2],
                summary_fd=fds[3], manifest_fd=fds[4], session_id="transient",
                target_app="WPS", app_id=1, window_ms=100,
                idle_backend=FakeIdleBackend(accessed=set()),
                scanner=FakeScanner({}), lock_path=root / "lock",
            )
            capture.arm_initial_window()
            common = {
                "device_major": 8, "device_minor": 2, "inode": 99,
                "page_index": 7, "page_order": 0, "tgid": 11, "tid": 12,
            }
            capture.record_lifecycle({**common, "event_type": "FAULT", "pfn": 0})
            capture.record_lifecycle({**common, "event_type": "CACHE_ADD", "pfn": 300})
            capture.record_lifecycle({**common, "event_type": "EVICT", "pfn": 300})
            first = capture.rotate([])
            first_range = first["page_ranges"][0]
            self.assertTrue(first["quality"]["valid"])
            self.assertEqual(first_range["pfn_start"], 300)
            self.assertEqual(
                int(first_range["source_mask_raw"])
                & (SOURCE_FILE_FAULT | SOURCE_NEW_RESIDENT | SOURCE_EVICT_BEFORE_SAMPLE),
                SOURCE_FILE_FAULT | SOURCE_NEW_RESIDENT | SOURCE_EVICT_BEFORE_SAMPLE,
            )

            capture.record_lifecycle({**common, "event_type": "CACHE_ADD", "pfn": 500})
            capture.record_lifecycle({**common, "event_type": "FAULT", "pfn": 0})
            second = capture.rotate([])
            capture.close()
            self.assertEqual(second["page_ranges"][0]["pfn_start"], 500)
            self.assertTrue(
                int(second["page_ranges"][0]["source_mask_raw"])
                & SOURCE_PFN_CHANGED
            )
            _metadata, lifecycle = read_framed_binary(root / "l.bin")
            self.assertIn("PFN_CHANGE", {row["event_type"] for row in lifecycle})


if __name__ == "__main__":
    unittest.main()
