"""Authenticated client for the privileged eBPF file-syscall helper."""

from __future__ import annotations

import json
import os
import select
import socket
import stat
import struct
import threading
import time
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from collectors.file_events import file_ext, path_for_mode


PROTOCOL_VERSION = 1
# Must match the root helper.  Page-hotset mode sends already-compressed
# ranges, so accepting a 64 KiB aggregate avoids loss from a burst of tiny
# per-window chunks without reintroducing raw page_access transport.
MAX_DATAGRAM_BYTES = 64 * 1024
UCRED = struct.Struct("=iii")


class EBPFFileEventCollector:
    """接收真实 syscall 返回事件，并把 PID 归属同步给 root helper。

    monitor 仍以普通用户运行。root helper 只负责加载 eBPF 和解析 ``/proc``
    fd 链接；它通过本用户 0700 runtime 目录内的 datagram socket 投递数据。
    每个数据包还必须携带 uid=0 的 ``SCM_CREDENTIALS``，因此仅凭能写 socket
    并不能伪造文件事件。

    PID 集合完全来自 AppProcessIndex 的 START/EXEC/EXIT 更新，不枚举 fd/maps，
    也不按秒猜测“新出现的文件”。一次成功的 syscall 对应一次事件。
    """

    def __init__(
        self,
        *,
        event_socket: str | Path,
        control_socket: str | Path,
        path_mode: str = "hash",
        expected_uid: int = 0,
        queue_capacity: int = 200_000,
        event_profile: str = "full",
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.event_socket = Path(event_socket)
        self.control_socket = Path(control_socket)
        self.path_mode = str(path_mode)
        self.expected_uid = int(expected_uid)
        self.queue_capacity = max(1, int(queue_capacity))
        self.event_profile = str(event_profile).strip().lower()
        if self.event_profile not in {"full", "page-hotset"}:
            raise ValueError(f"unsupported eBPF event profile: {event_profile}")
        self.event_callback = event_callback
        self._socket: socket.socket | None = None
        self._socket_inode = 0
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque()
        self._statuses: deque[dict[str, Any]] = deque()
        self._processes: dict[int, tuple[str, str, str]] = {}
        self.last_message_monotonic = 0.0
        self.rejected_datagrams = 0
        self.local_queue_drops = 0
        self._source_instance_id = ""
        self._last_source_seq = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._bind_socket()
        self._thread = threading.Thread(
            target=self._run,
            name="runtime-monitor-ebpf-file-events",
            daemon=True,
        )
        self._thread.start()

    def wait_ready(self, timeout_s: float) -> bool:
        return self._ready.wait(max(0.0, float(timeout_s)))

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def started(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_stale(self, timeout_s: float) -> bool:
        if not self.ready or self.last_message_monotonic <= 0:
            return True
        return time.monotonic() - self.last_message_monotonic > max(1.0, timeout_s)

    def stop(self) -> None:
        self._stop.set()
        try:
            os.write(self._wake_w, b"x")
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass
        self._wake_r = self._wake_w = -1
        self._unlink_own_socket()

    def sync_processes(self, processes: Iterable[Any]) -> bool:
        """把索引的完整 PID 快照原子同步给 BPF helper。

        ``processes`` 是 ``IndexedProcess`` 列表。同步包含 app/role/starttime，
        helper 会把 app/role 原样附到在该快照下捕获的 syscall 上。这样 EXIT
        与 perf-buffer 投递发生跨 socket 乱序时，也不会丢掉已发生事件的归属。
        """
        entries: list[dict[str, Any]] = []
        process_map: dict[int, tuple[str, str, str]] = {}
        for item in processes:
            identity = item.identity
            pid = int(identity.pid)
            app = str(item.app).strip().upper()
            role = "fixture" if str(item.role) == "fixture" else "gui"
            start_time = str(identity.start_time or "")
            if pid <= 0 or not app:
                continue
            entries.append({
                "pid": pid,
                "app": app,
                "role": role,
                "start_time": start_time,
            })
            process_map[pid] = (app, role, start_time)
        body = json.dumps(
            {
                "protocol_version": PROTOCOL_VERSION,
                "event_profile": self.event_profile,
                "processes": entries,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(body) > 256 * 1024:
            raise ValueError("eBPF process sync exceeds control datagram limit")
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sender.sendto(body, str(self.control_socket))
        except OSError:
            return False
        finally:
            sender.close()
        with self._lock:
            self._processes = process_map
        return True

    def drain_events(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._events)
            self._events.clear()
        return rows

    def drain_statuses(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = list(self._statuses)
            self._statuses.clear()
        return rows

    def _bind_socket(self) -> None:
        parent = self.event_socket.parent
        parent.mkdir(parents=False, exist_ok=True)
        parent_stat = parent.stat()
        if parent_stat.st_uid != os.getuid() or not stat.S_ISDIR(parent_stat.st_mode):
            raise PermissionError(f"unsafe eBPF event socket parent: {parent}")
        if self.event_socket.exists() or self.event_socket.is_symlink():
            existing = os.lstat(self.event_socket)
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                raise PermissionError(
                    f"refusing to replace unsafe eBPF socket: {self.event_socket}"
                )
            os.unlink(self.event_socket)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        listener.bind(str(self.event_socket))
        os.chmod(self.event_socket, 0o602)
        self._socket_inode = os.lstat(self.event_socket).st_ino
        self._socket = listener

    def _unlink_own_socket(self) -> None:
        try:
            current = os.lstat(self.event_socket)
        except OSError:
            return
        if (
            stat.S_ISSOCK(current.st_mode)
            and current.st_uid == os.getuid()
            and current.st_ino == self._socket_inode
        ):
            os.unlink(self.event_socket)

    @staticmethod
    def _credentials(
        ancillary: list[tuple[int, int, bytes]],
    ) -> tuple[int, int, int] | None:
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
                if len(data) >= UCRED.size:
                    return UCRED.unpack_from(data)
        return None

    def _run(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            readable, _, _ = select.select(
                [self._socket.fileno(), self._wake_r], [], [], None
            )
            if self._wake_r in readable:
                try:
                    os.read(self._wake_r, 4096)
                except OSError:
                    pass
                return
            if self._socket.fileno() not in readable:
                continue
            self._receive_one()

    def _receive_one(self) -> None:
        assert self._socket is not None
        try:
            body, ancillary, flags, _address = self._socket.recvmsg(
                MAX_DATAGRAM_BYTES, socket.CMSG_SPACE(UCRED.size)
            )
        except OSError:
            return
        credentials = self._credentials(ancillary)
        if credentials is None or credentials[1] != self.expected_uid:
            self.rejected_datagrams += 1
            return
        if flags & socket.MSG_TRUNC:
            self.rejected_datagrams += 1
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.rejected_datagrams += 1
            return
        if (
            not isinstance(payload, dict)
            or int(payload.get("protocol_version", 0) or 0) != PROTOCOL_VERSION
            or payload.get("source") != "ebpf-file-syscalls"
        ):
            self.rejected_datagrams += 1
            return
        self.last_message_monotonic = time.monotonic()
        kind = str(payload.get("kind", ""))
        if kind == "SOURCE_STATUS":
            self._accept_source_watermark(payload)
            if payload.get("status") == "READY":
                self._ready.set()
            with self._lock:
                self._statuses.append(dict(payload))
            return
        if kind == "EVENT_BATCH":
            events = payload.get("events", [])
            if not isinstance(events, list):
                self.rejected_datagrams += 1
                return
            common = {
                "protocol_version": PROTOCOL_VERSION,
                "source": "ebpf-file-syscalls",
                "source_instance_id": str(payload.get("source_instance_id", "")),
            }
            for item in events:
                if not isinstance(item, dict) or item.get("kind") != "FILE_EVENT":
                    self.rejected_datagrams += 1
                    continue
                self._accept_file_event_payload({**common, **item})
            return
        if kind == "PAGE_ACCESS_WINDOW":
            self._accept_page_window_payload(payload)
            return
        if kind == "FILE_EVENT":
            self._accept_file_event_payload(payload)
            return
        self.rejected_datagrams += 1

    def _accept_page_window_payload(self, payload: dict[str, Any]) -> None:
        """Accept one helper-compressed window chunk without raw event replay."""
        if not self._accept_file_sequence(payload):
            return
        app = str(payload.get("app", "")).strip().upper()
        ranges = payload.get("page_ranges", [])
        if not app or not isinstance(ranges, list):
            self.rejected_datagrams += 1
            return
        row = {
            "event": "page_access_window",
            "ts_ns": int(payload.get("timestamp_ns", 0) or time.time_ns()),
            "app": app,
            "window_start_ns": int(payload.get("window_start_ns", 0) or 0),
            "window_end_ns": int(payload.get("window_end_ns", 0) or 0),
            "page_size": int(payload.get("page_size", 0) or 0),
            "page_count": int(payload.get("page_count", 0) or 0),
            "page_access_events": int(payload.get("page_access_events", 0) or 0),
            "repeated_page_hits": int(payload.get("repeated_page_hits", 0) or 0),
            "chunk_index": int(payload.get("chunk_index", 0) or 0),
            "chunk_count": int(payload.get("chunk_count", 0) or 0),
            "page_ranges": ranges,
            "source_seq": int(payload.get("source_seq", 0) or 0),
            "source_instance_id": str(payload.get("source_instance_id", "")),
            "source": "ebpf-file-syscalls",
        }
        if self.event_callback is not None:
            self.event_callback(row)

    def _accept_file_event_payload(self, payload: dict[str, Any]) -> None:
        """校验批内逐事件序号，并追加统一内存行。"""
        if not self._accept_file_sequence(payload):
            return
        app = str(payload.get("app", "")).strip().upper()
        if not app:
            # helper 必须以捕获时使用的 PID 快照完成归属；不再从当前 /proc 猜测。
            self.rejected_datagrams += 1
            return
        path = str(payload.get("new_path") or payload.get("path") or "")
        row = {
            "ts_ns": int(payload.get("timestamp_ns", 0) or time.time_ns()),
            "boot_ts_ns": int(payload.get("boot_timestamp_ns", 0) or 0),
            "enter_boot_ns": int(payload.get("enter_boot_ns", 0) or 0),
            "exit_boot_ns": int(payload.get("exit_boot_ns", 0) or 0),
            "latency_ns": int(payload.get("latency_ns", 0) or 0),
            "pid": int(payload.get("pid", 0) or 0),
            "tgid": int(payload.get("pid", 0) or 0),
            "tid": int(payload.get("tid", 0) or 0),
            "app": app,
            "process_role": str(payload.get("process_role", "gui")),
            "comm": str(payload.get("comm", "")),
            "event": str(payload.get("event_type", "")),
            "path": path_for_mode(path, self.path_mode),
            "ext": file_ext(path),
            "inode": int(payload.get("inode", 0) or 0),
            "device": int(payload.get("device", 0) or 0),
            "device_major": int(payload.get("device_major", 0) or 0),
            "device_minor": int(payload.get("device_minor", 0) or 0),
            "offset": int(payload.get("offset", 0) or 0),
            "requested_offset": int(payload.get("requested_offset", 0) or 0),
            "file_position": int(payload.get("file_position", 0) or 0),
            "offset_valid": int(payload.get("offset_valid", 0) or 0),
            "file_identity_valid": int(
                payload.get("file_identity_valid", 0) or 0
            ),
            "size": int(payload.get("size", 0) or 0),
            "requested_size": int(payload.get("requested_size", 0) or 0),
            "returned_size": int(payload.get("returned_size", 0) or 0),
            "result": int(payload.get("result", 0) or 0),
            "flags": int(payload.get("flags", 0) or 0),
            "whence": int(payload.get("whence", 0) or 0),
            "page_order": int(payload.get("page_order", 0) or 0),
            "delay_ns": int(payload.get("delay_ns", 0) or 0),
            "address": int(payload.get("address", 0) or 0),
            "instruction_pointer": int(
                payload.get("instruction_pointer", 0) or 0
            ),
            "fault_error_code": int(payload.get("fault_error_code", 0) or 0),
            "sector": int(payload.get("sector", 0) or 0),
            "sector_count": int(payload.get("sector_count", 0) or 0),
            "rwbs": str(payload.get("rwbs", "")),
            "attribution_scope": str(payload.get("attribution_scope", "")),
            "source_seq": int(payload.get("source_seq", 0) or 0),
            "source_instance_id": str(payload.get("source_instance_id", "")),
            "path_truncated": int(payload.get("path_truncated", 0) or 0),
            "source": "ebpf-file-syscalls",
        }
        with self._lock:
            if len(self._events) >= self.queue_capacity:
                self._events.popleft()
                self.local_queue_drops += 1
            self._events.append(row)
        # 回调只能做非阻塞入队/唤醒；CSV、模型和结构化日志仍由 monitor 主线程
        # 执行，避免接收线程与秒级采样并发修改状态。
        if self.event_callback is not None and row["event"] in {
            "read", "pread", "page_access", "access", "eviction"
        }:
            self.event_callback(row)

    def _accept_source_watermark(self, payload: dict[str, Any]) -> None:
        instance = str(payload.get("source_instance_id", ""))
        if instance and instance != self._source_instance_id:
            self._source_instance_id = instance
            self._last_source_seq = 0
        watermark = int(payload.get("source_seq", 0) or 0)
        if self._last_source_seq == 0 and watermark > 0:
            # helper 可能比 monitor 更早启动；首条 READY 是当前连续性水位。
            self._last_source_seq = watermark

    def _accept_file_sequence(self, payload: dict[str, Any]) -> bool:
        instance = str(payload.get("source_instance_id", ""))
        if instance and instance != self._source_instance_id:
            self._source_instance_id = instance
            self._last_source_seq = 0
        seq = int(payload.get("source_seq", 0) or 0)
        if seq <= 0:
            self.rejected_datagrams += 1
            return False
        if self._last_source_seq and seq > self._last_source_seq + 1:
            status = {
                **payload,
                "kind": "SOURCE_STATUS",
                "status": "DELIVERY_GAP",
                "detail": (
                    f"missed {seq - self._last_source_seq - 1} file event(s)"
                ),
                "local_queue_drops": self.local_queue_drops,
            }
            with self._lock:
                self._statuses.append(status)
        elif seq <= self._last_source_seq:
            self.rejected_datagrams += 1
            return False
        self._last_source_seq = seq
        return True
