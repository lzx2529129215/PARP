from __future__ import annotations

import json
import os
import array
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from runtime_monitor.collectors.ebpf_file_events import EBPFFileEventCollector


class EBPFFileEventCollectorTests(unittest.TestCase):
    def test_page_capture_passes_private_fds_and_finalizes_only_after_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_socket = root / "events.sock"
            control_socket = root / "control.sock"
            control = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            control.bind(str(control_socket))
            collector = EBPFFileEventCollector(
                event_socket=event_socket,
                control_socket=control_socket,
                event_profile="page-access-window",
                expected_uid=os.getuid(),
            )
            failures: list[str] = []

            def responder() -> None:
                sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    body, ancillary, _flags, _address = control.recvmsg(
                        65536, socket.CMSG_SPACE(5 * array.array("i").itemsize)
                    )
                    payload = json.loads(body)
                    rights = array.array("i")
                    for level, kind, data in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            rights.frombytes(data[:len(data) - len(data) % rights.itemsize])
                    if payload.get("command") != "START_PAGE_ACCESS_CAPTURE" or len(rights) != 5:
                        failures.append("invalid START command or fd count")
                        return
                    contents = [
                        b"windows", b"lifecycle", b"{}\n", b"valid\n",
                        json.dumps({"capture_status": "COMPLETE"}).encode(),
                    ]
                    for fd, content in zip(rights, contents):
                        values = os.fstat(fd)
                        if stat.S_IMODE(values.st_mode) != 0o600:
                            failures.append("output fd is not mode 0600")
                        os.write(fd, content)
                        os.close(fd)
                    base = {
                        "protocol_version": 1,
                        "source": "ebpf-file-syscalls",
                        "source_instance_id": "test-helper",
                        "kind": "SOURCE_STATUS",
                        "source_seq": 0,
                    }
                    sender.sendto(json.dumps({
                        **base, "status": "PAGE_CAPTURE_READY",
                    }).encode(), str(event_socket))
                    stop = json.loads(control.recv(65536))
                    if stop.get("command") != "STOP_PAGE_ACCESS_CAPTURE":
                        failures.append("missing STOP command")
                    sender.sendto(json.dumps({
                        **base, "status": "PAGE_CAPTURE_STOPPED",
                    }).encode(), str(event_socket))
                finally:
                    sender.close()

            thread = threading.Thread(target=responder, daemon=True)
            try:
                collector.start()
                thread.start()
                dataset = root / "dataset"
                self.assertTrue(collector.start_page_access_capture(
                    output_dir=dataset, session_id="s1", target_app="WPS",
                    app_id=1, timeout_s=2.0,
                ))
                self.assertTrue(collector.stop_page_access_capture(timeout_s=2.0))
                thread.join(timeout=2.0)
                self.assertFalse(failures)
                self.assertFalse(list(dataset.glob("*.partial")))
                self.assertTrue((dataset / "page_access_windows.bin").exists())
                manifest = json.loads((dataset / "manifest.json").read_text())
                self.assertIn("page_access_windows.bin", manifest["sha256"])
            finally:
                collector.stop()
                control.close()

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
