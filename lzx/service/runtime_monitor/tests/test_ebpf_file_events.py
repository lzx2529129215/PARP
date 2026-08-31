from __future__ import annotations

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from runtime_monitor.collectors.ebpf_file_events import EBPFFileEventCollector


class EBPFFileEventCollectorTests(unittest.TestCase):
    def test_index_snapshot_and_authenticated_syscall_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_socket = root / "events.sock"
            control_socket = root / "control.sock"
            control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            control.bind(str(control_socket))
            callbacks: list[dict[str, object]] = []
            collector = EBPFFileEventCollector(
                event_socket=event_socket,
                control_socket=control_socket,
                path_mode="basename",
                expected_uid=os.getuid(),
                event_callback=callbacks.append,
            )
            sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                collector.start()
                fixture = SimpleNamespace(
                    app="FIREFOX",
                    role="fixture",
                    identity=SimpleNamespace(pid=123, start_time="99"),
                )
                self.assertTrue(collector.sync_processes([fixture]))
                control.settimeout(1.0)
                sync = json.loads(control.recv(65536).decode("utf-8"))
                self.assertEqual(sync["event_profile"], "full")
                self.assertEqual(sync["processes"], [{
                    "pid": 123,
                    "app": "FIREFOX",
                    "role": "fixture",
                    "start_time": "99",
                }])

                base = {
                    "protocol_version": 1,
                    "source": "ebpf-file-syscalls",
                    "source_instance_id": "helper-a",
                }
                sender.sendto(json.dumps({
                    **base,
                    "kind": "SOURCE_STATUS",
                    "status": "READY",
                    "source_seq": 0,
                }).encode(), str(event_socket))
                self.assertTrue(collector.wait_ready(1.0))
                sender.sendto(json.dumps({
                    **base,
                    "kind": "FILE_EVENT",
                    "event_type": "read",
                    "timestamp_ns": 1000,
                    "source_seq": 1,
                    "pid": 123,
                    "tid": 124,
                    "app": "FIREFOX",
                    "process_role": "fixture",
                    "comm": "python3",
                    "path": "/tmp/report.pdf",
                    "size": 4096,
                    "requested_size": 8192,
                    "returned_size": 4096,
                    "result": 4096,
                    "enter_boot_ns": 100,
                    "exit_boot_ns": 250,
                    "latency_ns": 150,
                    "device": 2050,
                    "device_major": 8,
                    "device_minor": 2,
                    "inode": 7,
                    "offset": 16384,
                    "requested_offset": 16384,
                    "file_position": 20480,
                    "offset_valid": 1,
                    "file_identity_valid": 1,
                }).encode(), str(event_socket))
                deadline = time.monotonic() + 1.0
                rows = []
                while not rows and time.monotonic() < deadline:
                    rows = collector.drain_events()
                    time.sleep(0.01)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["event"], "read")
                self.assertEqual(rows[0]["app"], "FIREFOX")
                self.assertEqual(rows[0]["process_role"], "fixture")
                self.assertEqual(rows[0]["path"], "report.pdf")
                self.assertEqual(rows[0]["size"], 4096)
                self.assertEqual(rows[0]["requested_size"], 8192)
                self.assertEqual(rows[0]["returned_size"], 4096)
                self.assertEqual(rows[0]["latency_ns"], 150)
                self.assertEqual(rows[0]["device_major"], 8)
                self.assertEqual(rows[0]["device_minor"], 2)
                self.assertEqual(rows[0]["inode"], 7)
                self.assertEqual(rows[0]["offset"], 16384)
                self.assertEqual(len(callbacks), 1)
                self.assertEqual(callbacks[0]["event"], "read")

                sender.sendto(json.dumps({
                    **base,
                    "kind": "EVENT_BATCH",
                    "events": [{
                        "kind": "FILE_EVENT",
                        "event_type": "eviction",
                        "timestamp_ns": 2000,
                        "boot_timestamp_ns": 1900,
                        "source_seq": 2,
                        "app": "FIREFOX",
                        "process_role": "fixture",
                        "pid": 0,
                        "tid": 99,
                        "device": 8388610,
                        "device_major": 8,
                        "device_minor": 2,
                        "inode": 7,
                        "offset": 4096,
                        "size": 4096,
                    }],
                }).encode(), str(event_socket))
                batched = []
                deadline = time.monotonic() + 1.0
                while not batched and time.monotonic() < deadline:
                    batched = collector.drain_events()
                    time.sleep(0.01)
                self.assertEqual(len(batched), 1)
                self.assertEqual(batched[0]["event"], "eviction")
                self.assertEqual(batched[0]["device_major"], 8)
                self.assertEqual(len(callbacks), 2)

                sender.sendto(json.dumps({
                    **base,
                    "kind": "PAGE_ACCESS_WINDOW",
                    "timestamp_ns": 3_000_000_000,
                    "source_seq": 3,
                    "app": "WPS",
                    "window_start_ns": 2_000_000_000,
                    "window_end_ns": 3_000_000_000,
                    "page_size": 4096,
                    "page_count": 2,
                    "page_access_events": 3,
                    "repeated_page_hits": 1,
                    "chunk_index": 0,
                    "chunk_count": 1,
                    "page_ranges": [{
                        "device_major": 8,
                        "device_minor": 2,
                        "inode": 7,
                        "start_page_index": 4,
                        "page_count": 2,
                    }],
                }).encode(), str(event_socket))
                deadline = time.monotonic() + 1.0
                while len(callbacks) < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(callbacks[-1]["event"], "page_access_window")
                self.assertEqual(callbacks[-1]["page_access_events"], 3)
                self.assertEqual(len(callbacks[-1]["page_ranges"]), 1)
            finally:
                collector.stop()
                sender.close()
                control.close()


if __name__ == "__main__":
    unittest.main()
