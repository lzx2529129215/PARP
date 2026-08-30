#!/usr/bin/env python3
"""Privileged Linux proc-connector source for system-wide process events.

The helper deliberately has one narrow responsibility: subscribe to
``NETLINK_CONNECTOR/CN_IDX_PROC`` as root and forward process-leader
FORK/EXEC/EXIT records to one user's credential-checked Unix datagram socket.
It performs no prediction, cgroup mutation, tracing configuration, or file
logging.  The unprivileged Runtime Monitor owns CSV output and app mapping.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import select
import socket
import stat
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


PROTOCOL_VERSION = 1
NETLINK_CONNECTOR = 11
CN_IDX_PROC = 1
CN_VAL_PROC = 1
NLMSG_DONE = 3
NLMSG_ERROR = 2
NLMSG_OVERRUN = 4
PROC_CN_MCAST_LISTEN = 1
PROC_CN_MCAST_IGNORE = 2
PROC_EVENT_NONE = 0x00000000
PROC_EVENT_FORK = 0x00000001
PROC_EVENT_EXEC = 0x00000002
PROC_EVENT_EXIT = 0x80000000
NETLINK_HEADER = struct.Struct("=IHHII")
CONNECTOR_HEADER = struct.Struct("=IIIIHH")
PROC_HEADER = struct.Struct("=IIQ")
FOUR_U32 = struct.Struct("=IIII")
SIX_U32 = struct.Struct("=IIIIII")
HEARTBEAT_INTERVAL_S = 2.0
MAX_DATAGRAM_BYTES = 16 * 1024


def _aligned4(value: int) -> int:
    return (int(value) + 3) & ~3


def iter_netlink_payloads(data: bytes) -> Iterable[tuple[int, bytes]]:
    """Yield ``(nlmsg_type, payload)`` while rejecting truncated messages."""
    offset = 0
    while offset + NETLINK_HEADER.size <= len(data):
        length, message_type, _flags, _seq, _pid = NETLINK_HEADER.unpack_from(
            data, offset
        )
        if length < NETLINK_HEADER.size or offset + length > len(data):
            return
        yield int(message_type), data[offset + NETLINK_HEADER.size : offset + length]
        offset += _aligned4(length)


def decode_connector_payload(payload: bytes) -> dict[str, Any] | None:
    """Decode one connector payload into a process-leader event dictionary."""
    if len(payload) < CONNECTOR_HEADER.size:
        return None
    idx, value, connector_seq, connector_ack, data_len, _flags = (
        CONNECTOR_HEADER.unpack_from(payload, 0)
    )
    if idx != CN_IDX_PROC or value != CN_VAL_PROC:
        return None
    start = CONNECTOR_HEADER.size
    end = start + int(data_len)
    if data_len < PROC_HEADER.size or end > len(payload):
        return None
    process_payload = payload[start:end]
    what, cpu, boot_timestamp_ns = PROC_HEADER.unpack_from(process_payload, 0)
    body = process_payload[PROC_HEADER.size :]
    common = {
        "cpu": int(cpu),
        "boot_timestamp_ns": int(boot_timestamp_ns),
        "connector_seq": int(connector_seq),
        "connector_ack": int(connector_ack),
    }
    if what == PROC_EVENT_NONE:
        return {**common, "native_event": "ACK"}
    if what == PROC_EVENT_FORK and len(body) >= FOUR_U32.size:
        parent_pid, parent_tgid, child_pid, child_tgid = FOUR_U32.unpack_from(body)
        # proc connector reports kernel tasks as well as user-visible processes.
        # A thread has pid != tgid; the requested contract is one event per
        # process, so only thread-group leaders leave the privileged helper.
        if child_pid != child_tgid:
            return None
        return {
            **common,
            "event_type": "PROCESS_START",
            "native_event": "FORK",
            "pid": int(child_tgid),
            "tgid": int(child_tgid),
            "parent_pid": int(parent_pid),
            "parent_tgid": int(parent_tgid),
        }
    if what == PROC_EVENT_EXEC and len(body) >= 8:
        process_pid, process_tgid = struct.unpack_from("=II", body, 0)
        if process_pid != process_tgid:
            return None
        return {
            **common,
            "event_type": "PROCESS_EXEC",
            "native_event": "EXEC",
            "pid": int(process_tgid),
            "tgid": int(process_tgid),
        }
    if what == PROC_EVENT_EXIT and len(body) >= SIX_U32.size:
        (
            process_pid,
            process_tgid,
            exit_code,
            exit_signal,
            parent_pid,
            parent_tgid,
        ) = SIX_U32.unpack_from(body)
        if process_pid != process_tgid:
            return None
        return {
            **common,
            "event_type": "PROCESS_EXIT",
            "native_event": "EXIT",
            "pid": int(process_tgid),
            "tgid": int(process_tgid),
            "parent_pid": int(parent_pid),
            "parent_tgid": int(parent_tgid),
            "exit_code": int(exit_code),
            "exit_signal": int(exit_signal),
        }
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_link(path: Path) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


def _cmdline_hash(path: Path) -> str:
    try:
        raw = path.read_bytes().replace(b"\0", b" ")
    except OSError:
        return ""
    return hashlib.sha256(raw).hexdigest()[:16]


def _process_start_time(path: Path) -> str:
    """读取 /proc/<pid>/stat 字段 22，作为一次进程实例的稳定标识。"""
    text = _read_text(path)
    try:
        # comm 位于括号内且可包含空格和右括号，所以必须从最后一个 ')' 后解析。
        # 后半段从字段 3（state）开始，starttime（字段 22）对应索引 19。
        return str(text.rsplit(")", 1)[1].strip().split()[19])
    except (IndexError, ValueError):
        return ""


def _cgroup_path(pid: int) -> str:
    for line in _read_text(Path("/proc") / str(pid) / "cgroup").splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            return parts[2]
    return ""


def _cgroup_unit(path: str) -> str:
    for component in reversed([item for item in path.split("/") if item]):
        if component.endswith((".scope", ".service")):
            return component
    return ""


def read_process_metadata(pid: int, tgid: int | None = None) -> dict[str, Any]:
    """Read bounded, privacy-preserving process metadata; failures stay blank."""
    proc = Path("/proc") / str(pid)
    cgroup_path = _cgroup_path(pid)
    return {
        "pid": int(pid),
        "tgid": int(tgid if tgid is not None else pid),
        "comm": _read_text(proc / "comm").strip(),
        "exe_path": _read_link(proc / "exe"),
        "cmdline_hash": _cmdline_hash(proc / "cmdline"),
        "start_time": _process_start_time(proc / "stat"),
        "cgroup_path": cgroup_path,
        "cgroup_unit": _cgroup_unit(cgroup_path),
    }


def _subscription_message(operation: int, port_id: int) -> bytes:
    connector = CONNECTOR_HEADER.pack(
        CN_IDX_PROC, CN_VAL_PROC, 0, 0, 4, 0
    ) + struct.pack("=I", int(operation))
    return NETLINK_HEADER.pack(
        NETLINK_HEADER.size + len(connector), NLMSG_DONE, 0, 0, int(port_id)
    ) + connector


class ProcessConnectorHelper:
    """Own the privileged connector socket and forward authenticated events."""

    def __init__(self, *, target_uid: int, socket_path: Path) -> None:
        self.target_uid = int(target_uid)
        self.socket_path = Path(socket_path)
        self.instance_id = uuid.uuid4().hex
        self.boot_id = _read_text(Path("/proc/sys/kernel/random/boot_id")).strip()
        self.source_seq = 0
        self.delivery_drops = 0
        self.kernel_overflows = 0
        self.metadata: dict[int, dict[str, Any]] = {}
        self.netlink: socket.socket | None = None
        self.output = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.output.setblocking(False)
        self._validate_target()

    def _validate_target(self) -> None:
        expected_parent = Path(f"/run/user/{self.target_uid}")
        if self.socket_path.parent != expected_parent:
            raise ValueError(
                f"socket must be an immediate child of {expected_parent}: {self.socket_path}"
            )
        parent_stat = expected_parent.stat()
        if parent_stat.st_uid != self.target_uid or not stat.S_ISDIR(parent_stat.st_mode):
            raise PermissionError(f"unsafe target runtime directory: {expected_parent}")

    def _open_connector(self) -> None:
        connector = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_CONNECTOR)
        connector.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        connector.bind((os.getpid(), CN_IDX_PROC))
        connector.sendto(
            _subscription_message(PROC_CN_MCAST_LISTEN, os.getpid()), (0, 0)
        )
        connector.setblocking(False)
        self.netlink = connector

    def _preload_metadata(self) -> None:
        # Subscribe first, then preload. Kernel events generated during this
        # bounded scan remain queued and are processed immediately afterwards.
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            metadata = read_process_metadata(pid)
            if metadata.get("comm") or metadata.get("cgroup_path"):
                self.metadata[pid] = metadata

    def _target_is_safe_socket(self) -> bool:
        try:
            target = os.lstat(self.socket_path)
        except OSError:
            return False
        return stat.S_ISSOCK(target.st_mode) and target.st_uid == self.target_uid

    def _send(self, payload: dict[str, Any], *, count_drop: bool) -> bool:
        if not self._target_is_safe_socket():
            if count_drop:
                self.delivery_drops += 1
            return False
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > MAX_DATAGRAM_BYTES:
            if count_drop:
                self.delivery_drops += 1
            return False
        try:
            self.output.sendto(body, str(self.socket_path))
            return True
        except OSError as exc:
            if exc.errno not in {
                errno.EAGAIN,
                errno.EWOULDBLOCK,
                errno.ENOENT,
                errno.ECONNREFUSED,
                errno.ENOBUFS,
            }:
                raise
            if count_drop:
                self.delivery_drops += 1
            return False

    def _envelope(self, values: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocol_version": PROTOCOL_VERSION,
            "source": "proc-connector",
            "source_instance_id": self.instance_id,
            "boot_id": self.boot_id,
            **values,
        }

    def _send_status(self, status: str = "READY", detail: str = "") -> None:
        self._send(
            self._envelope({
                "kind": "SOURCE_STATUS",
                "status": status,
                "detail": detail,
                "timestamp_ns": time.time_ns(),
                "source_seq": self.source_seq,
                "delivery_drops": self.delivery_drops,
                "kernel_overflows": self.kernel_overflows,
                "helper_pid": os.getpid(),
            }),
            count_drop=False,
        )

    def _forward_event(self, event: dict[str, Any]) -> None:
        native_event = str(event.get("native_event", ""))
        if native_event == "ACK":
            self._send_status("READY", "kernel subscription acknowledged")
            return
        pid = int(event.get("pid", 0) or 0)
        if native_event in {"FORK", "EXEC"}:
            metadata = read_process_metadata(pid, int(event.get("tgid", pid) or pid))
            if metadata.get("comm") or metadata.get("cgroup_path"):
                self.metadata[pid] = metadata
        else:
            metadata = self.metadata.pop(pid, {})
        self.source_seq += 1
        boot_timestamp_ns = int(event.get("boot_timestamp_ns", 0) or 0)
        wall_offset_ns = time.time_ns() - time.monotonic_ns()
        payload = self._envelope({
            "kind": "PROCESS_EVENT",
            **event,
            **metadata,
            "timestamp_ns": wall_offset_ns + boot_timestamp_ns,
            "source_seq": self.source_seq,
        })
        self._send(payload, count_drop=True)

    def run(self) -> int:
        self._open_connector()
        self._preload_metadata()
        self._send_status("STARTING", "connector subscribed; metadata preloaded")
        assert self.netlink is not None
        try:
            while True:
                readable, _, _ = select.select(
                    [self.netlink], [], [], HEARTBEAT_INTERVAL_S
                )
                if not readable:
                    self._send_status()
                    continue
                try:
                    packet = self.netlink.recv(1 << 20)
                except OSError as exc:
                    if exc.errno == errno.ENOBUFS:
                        self.kernel_overflows += 1
                        self._send_status(
                            "KERNEL_OVERFLOW", "netlink receive buffer overflow"
                        )
                        return 1
                    raise
                for message_type, connector_payload in iter_netlink_payloads(packet):
                    if message_type == NLMSG_OVERRUN:
                        self.kernel_overflows += 1
                        self._send_status("KERNEL_OVERFLOW", "NLMSG_OVERRUN")
                        # 已丢失的内核事件无法补回。退出并让 systemd 重建订阅，
                        # 同时让用户服务切分 session，避免在同一个文件中混入
                        # 一段看似连续、实际存在未知缺口的数据。
                        return 1
                    if message_type == NLMSG_ERROR:
                        self._send_status("ERROR", "NLMSG_ERROR")
                        return 1
                    event = decode_connector_payload(connector_payload)
                    if event is not None:
                        self._forward_event(event)
        finally:
            try:
                self.netlink.sendto(
                    _subscription_message(PROC_CN_MCAST_IGNORE, os.getpid()), (0, 0)
                )
            except OSError:
                pass
            self.netlink.close()
            self.output.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-uid", type=int, required=True)
    parser.add_argument("--socket", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if os.geteuid() != 0:
        raise SystemExit("proc connector helper must run as root")
    if args.target_uid <= 0:
        raise SystemExit("target uid must be a positive non-root uid")
    socket_path = args.socket or Path(
        f"/run/user/{args.target_uid}/parp-process-events.sock"
    )
    return ProcessConnectorHelper(
        target_uid=args.target_uid, socket_path=socket_path
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
