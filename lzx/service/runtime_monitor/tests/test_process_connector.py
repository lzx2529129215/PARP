from __future__ import annotations

import csv
import json
import os
import socket
import struct
import tempfile
import time
import unittest
from unittest.mock import Mock
from pathlib import Path

from runtime_monitor.collectors.process_events import GlobalProcessEventCollector, PROTOCOL_VERSION
from runtime_monitor.helpers.proc_connector_helper import (
    CN_IDX_PROC,
    CN_VAL_PROC,
    CONNECTOR_HEADER,
    FOUR_U32,
    PROC_EVENT_EXEC,
    PROC_EVENT_EXIT,
    PROC_EVENT_FORK,
    PROC_HEADER,
    SIX_U32,
    decode_connector_payload,
)
from runtime_monitor.monitor import RuntimeMonitorV0, parse_args


ROOT = Path(__file__).resolve().parents[1]


def connector_payload(what: int, body: bytes) -> bytes:
    process = PROC_HEADER.pack(what, 3, 123456789) + body
    return CONNECTOR_HEADER.pack(
        CN_IDX_PROC, CN_VAL_PROC, 7, 0, len(process), 0
    ) + process


class ProcessConnectorDecoderTests(unittest.TestCase):
    def test_decodes_process_leader_fork_exec_exit(self) -> None:
        fork = decode_connector_payload(
            connector_payload(PROC_EVENT_FORK, FOUR_U32.pack(10, 10, 20, 20))
        )
        self.assertEqual(fork["event_type"], "PROCESS_START")
        self.assertEqual(fork["parent_pid"], 10)
        self.assertEqual(fork["pid"], 20)

        execute = decode_connector_payload(
            connector_payload(PROC_EVENT_EXEC, struct.pack("=II", 20, 20))
        )
        self.assertEqual(execute["event_type"], "PROCESS_EXEC")
        self.assertEqual(execute["pid"], 20)

        exit_event = decode_connector_payload(
            connector_payload(
                PROC_EVENT_EXIT,
                SIX_U32.pack(20, 20, 0, 9, 10, 10),
            )
        )
        self.assertEqual(exit_event["event_type"], "PROCESS_EXIT")
        self.assertEqual(exit_event["exit_signal"], 9)

    def test_filters_thread_events(self) -> None:
        event = decode_connector_payload(
            connector_payload(PROC_EVENT_FORK, FOUR_U32.pack(10, 10, 21, 20))
        )
        self.assertIsNone(event)


class ProcessConnectorClientTests(unittest.TestCase):
    def test_accepts_only_expected_credential_and_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            received: list[dict[str, object]] = []
            collector = GlobalProcessEventCollector(
                received.append,
                socket_path=Path(tmp) / "events.sock",
                expected_uid=os.getuid(),
            )
            collector.start()
            self.assertEqual(
                os.lstat(Path(tmp) / "events.sock").st_mode & 0o777, 0o602
            )
            sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                sender.sendto(
                    json.dumps({
                        "protocol_version": PROTOCOL_VERSION,
                        "source": "proc-connector",
                        "kind": "SOURCE_STATUS",
                        "status": "STARTING",
                    }).encode(),
                    str(Path(tmp) / "events.sock"),
                )
                self.assertFalse(collector.wait_ready(0.05))

                sender.sendto(
                    json.dumps({
                        "protocol_version": PROTOCOL_VERSION,
                        "source": "proc-connector",
                        "kind": "SOURCE_STATUS",
                        "status": "READY",
                    }).encode(),
                    str(Path(tmp) / "events.sock"),
                )
                self.assertTrue(collector.wait_ready(1.0))
                deadline = time.time() + 1.0
                while len(received) < 2 and time.time() < deadline:
                    time.sleep(0.01)
                self.assertEqual(
                    [item["status"] for item in received], ["STARTING", "READY"]
                )

                sender.sendto(
                    json.dumps({
                        "protocol_version": 999,
                        "source": "proc-connector",
                    }).encode(),
                    str(Path(tmp) / "events.sock"),
                )
                time.sleep(0.05)
                self.assertEqual(len(received), 2)
                self.assertGreaterEqual(collector.rejected_datagrams, 1)
            finally:
                sender.close()
                collector.stop()

    def test_rejects_wrong_sender_uid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            received: list[dict[str, object]] = []
            collector = GlobalProcessEventCollector(
                received.append,
                socket_path=Path(tmp) / "events.sock",
                expected_uid=os.getuid() + 1,
            )
            collector.start()
            sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                sender.sendto(
                    json.dumps({
                        "protocol_version": PROTOCOL_VERSION,
                        "source": "proc-connector",
                        "kind": "SOURCE_STATUS",
                        "status": "READY",
                    }).encode(),
                    str(Path(tmp) / "events.sock"),
                )
                self.assertFalse(collector.wait_ready(0.05))
                self.assertEqual(received, [])
                self.assertGreaterEqual(collector.rejected_datagrams, 1)
            finally:
                sender.close()
                collector.stop()


class RuntimeMonitorProcessEventTests(unittest.TestCase):
    def test_every_valid_start_reaches_create_process(self) -> None:
        """每条通过序列校验的创建事件都必须进入 createProcess。"""
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args([
                "--config", str(ROOT / "config.yaml"),
                "--app-scope-config", "",
                "--target-app", "WPS",
                "--output-dir", tmp,
                "--session-id", "all-starts-submit",
                "--foreground-backend", "manual",
            ])
            monitor = RuntimeMonitorV0(args)
            # createProcess 是所有新进程的统一业务入口。测试同时发送可映射与
            # 不可映射进程，确保 handler 不会在调用该入口之前按 App 过滤。
            create_process = Mock(wraps=monitor.createProcess)
            monitor.createProcess = create_process
            base = {
                "protocol_version": 1,
                "source": "proc-connector",
                "source_instance_id": "instance-all-starts",
                "kind": "PROCESS_EVENT",
                "timestamp_ns": time.time_ns(),
                "event_type": "PROCESS_START",
                "native_event": "FORK",
                "cgroup_path": "/user.slice/session.scope",
            }
            processes = [
                (701, "wps", "/usr/bin/wps"),
                (702, "not-in-lstm", "/usr/bin/not-in-lstm"),
                (703, "vlc", "/usr/bin/vlc"),
            ]
            try:
                for source_seq, (pid, comm, exe_path) in enumerate(processes, 1):
                    monitor._handle_global_process_event({
                        **base,
                        "source_seq": source_seq,
                        "pid": pid,
                        "tgid": pid,
                        "comm": comm,
                        "exe_path": exe_path,
                    })
            finally:
                monitor._close_writers()

            self.assertEqual(create_process.call_count, 3)
            handled_pids = [
                call.args[1].pid
                for call in create_process.call_args_list
            ]
            self.assertEqual(handled_pids, [701, 702, 703])

    def test_every_unmapped_start_still_calls_create_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args([
                "--config", str(ROOT / "config.yaml"),
                "--app-scope-config", "",
                "--target-app", "WPS",
                "--output-dir", tmp,
                "--session-id", "unmapped-create",
                "--foreground-backend", "manual",
            ])
            monitor = RuntimeMonitorV0(args)
            create_process = Mock(wraps=monitor.createProcess)
            monitor.createProcess = create_process
            event = {
                "protocol_version": 1,
                "source": "proc-connector",
                "source_instance_id": "instance-a",
                "kind": "PROCESS_EVENT",
                "timestamp_ns": time.time_ns(),
                "event_type": "PROCESS_START",
                "native_event": "FORK",
                "source_seq": 1,
                "pid": 654,
                "tgid": 654,
                "comm": "not-in-lstm",
                "exe_path": "/usr/bin/not-in-lstm",
                "cgroup_path": "/user.slice/session.scope",
            }
            monitor._handle_global_process_event(event)
            monitor._close_writers()

            create_process.assert_called_once()
            identity = create_process.call_args.args[1]
            self.assertEqual(identity.pid, 654)
            model = Path(tmp) / "unmapped-create" / "model"
            with (model / "process_events.csv").open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["app"], "")
            with (model / "process_cgroup_routes.csv").open(encoding="utf-8", newline="") as f:
                self.assertEqual(list(csv.DictReader(f)), [])

    def test_strict_mode_requires_ready_and_stops_on_overflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args([
                "--config", str(ROOT / "config.yaml"),
                "--app-scope-config", "",
                "--target-app", "WPS",
                "--output-dir", tmp,
                "--session-id", "strict-source",
                "--foreground-backend", "manual",
                "--process-event-source", "connector",
                "--require-process-connector",
            ])
            monitor = RuntimeMonitorV0(args)
            source = {
                "protocol_version": 1,
                "source": "proc-connector",
                "source_instance_id": "instance-a",
                "kind": "SOURCE_STATUS",
                "timestamp_ns": time.time_ns(),
                "source_seq": 0,
                "delivery_drops": 0,
                "kernel_overflows": 0,
            }
            try:
                monitor._handle_global_process_event({
                    **source, "status": "STARTING",
                })
                self.assertFalse(monitor.process_connector_active)
                monitor._handle_global_process_event({
                    **source, "status": "READY",
                })
                self.assertTrue(monitor.process_connector_active)
                monitor._handle_global_process_event({
                    **source,
                    "status": "KERNEL_OVERFLOW",
                    "kernel_overflows": 1,
                })
                self.assertFalse(monitor.process_connector_active)
                self.assertTrue(monitor.stop_requested)
            finally:
                monitor._stop_global_process_events()
                monitor._close_writers()

    def test_writes_realtime_event_and_delivery_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = parse_args([
                "--config", str(ROOT / "config.yaml"),
                "--app-scope-config", "",
                "--target-app", "WPS",
                "--output-dir", tmp,
                "--session-id", "proc-events",
                "--foreground-backend", "manual",
            ])
            monitor = RuntimeMonitorV0(args)
            create_process = Mock(wraps=monitor.createProcess)
            monitor.createProcess = create_process
            base = {
                "protocol_version": 1,
                "source": "proc-connector",
                "source_instance_id": "instance-a",
                "kind": "PROCESS_EVENT",
                "timestamp_ns": time.time_ns(),
                "boot_timestamp_ns": 123,
                "event_type": "PROCESS_START",
                "native_event": "FORK",
                "pid": 321,
                "tgid": 321,
                "parent_pid": 1,
                "parent_tgid": 1,
                "comm": "wps",
                "exe_path": "/usr/bin/wps",
                "cgroup_path": "/user.slice/test.scope",
                "cgroup_unit": "test.scope",
                "cpu": 2,
            }
            monitor._handle_global_process_event({**base, "source_seq": 1})
            monitor._handle_global_process_event({
                **base,
                "source_seq": 3,
                "event_type": "PROCESS_EXIT",
                "native_event": "EXIT",
                "exit_code": 0,
                "exit_signal": 0,
            })
            monitor._close_writers()

            create_process.assert_called_once()
            self.assertEqual(create_process.call_args.args[0]["event_type"], "PROCESS_START")

            model = Path(tmp) / "proc-events" / "model"
            with (model / "process_events.csv").open(encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual([row["event_type"] for row in rows], [
                "PROCESS_START", "PROCESS_EXIT",
            ])
            self.assertEqual(rows[0]["native_event"], "FORK")
            self.assertEqual(rows[0]["parent_pid"], "1")
            self.assertEqual(rows[0]["app"], "WPS")

            with (model / "process_event_source.csv").open(encoding="utf-8", newline="") as f:
                statuses = list(csv.DictReader(f))
            self.assertIn("DELIVERY_GAP", [row["status"] for row in statuses])


if __name__ == "__main__":
    unittest.main()
