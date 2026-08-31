#!/usr/bin/env python3
"""Privileged eBPF file-syscall source for one unprivileged monitor UID."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import select
import socket
import stat
import struct
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bcc import BPF


PROTOCOL_VERSION = 1
HEARTBEAT_INTERVAL_S = 2.0
# Page-hotset windows can contain thousands of distinct file ranges during a
# GUI cold start.  A larger *aggregate* datagram keeps that one-second window
# from becoming hundreds of socket messages; this is still far below the Unix
# datagram payload limit and is matched by the unprivileged receiver.
MAX_DATAGRAM_BYTES = 64 * 1024
MAX_CONTROL_BYTES = 256 * 1024
# WPS cold starts can emit several MiB of file/cache/workload events in a few
# hundred milliseconds.  Ubuntu BCC's default-sized rings (and the previous
# explicit 256-page rings) overflow before user space can drain that burst.
# 1024 pages is 4 MiB per CPU/map: on the supported two-CPU test VM the three
# maps reserve 24 MiB, staying below the service's locked-memory ceiling while
# providing four times the previous burst capacity.
PERF_BUFFER_PAGES = 1024
PAGE_WINDOW_NS = 1_000_000_000
PAGE_WINDOW_FLUSH_DELAY_NS = 100_000_000
# A range serializes to roughly 100--140 bytes.  400 ranges leaves ample room
# below the 64 KiB envelope limit while cutting receiver wakeups by over 6x
# compared with the original 64-range chunks.
PAGE_RANGES_PER_CHUNK = 400
PAGE_CALLBACK_MAINTENANCE_NS = 50_000_000
# A just-closed page window has 400 ms before the monitor's 500 ms lateness
# cutoff.  Reserve a bounded portion of that budget for Unix-datagram flow
# control instead of silently losing a compressed chunk on EAGAIN/ENOBUFS.
PAGE_WINDOW_SEND_RETRY_S = 0.20
UCRED = struct.Struct("=iii")
# 必须和 ebpf/file_events.bpf.c 保持一致。完整 file_event_t 已移到 BPF
# per-CPU scratch map，128 字节路径上限仍能控制 perf 事件大小和传输带宽。
PATH_LEN = 128
OP_NAMES = {
    1: "openat",
    2: "mmap",
    3: "read",
    4: "write",
    5: "fsync",
    6: "rename",
    7: "close",
    8: "dup",
    9: "pread",
    10: "pwrite",
    11: "lseek",
    12: "access",
}
CACHE_NAMES = {1: "page_access", 2: "eviction"}
WORKLOAD_NAMES = {
    1: "page_fault",
    2: "block_io",
    3: "offcpu_sleep",
    4: "offcpu_blocked",
    5: "iowait",
}


class BPFFileEvent(ctypes.Structure):
    _fields_ = [
        ("enter_boot_ns", ctypes.c_uint64),
        ("exit_boot_ns", ctypes.c_uint64),
        ("inode", ctypes.c_uint64),
        ("offset", ctypes.c_int64),
        ("requested_offset", ctypes.c_int64),
        ("file_position", ctypes.c_int64),
        ("requested_size", ctypes.c_uint64),
        ("returned_size", ctypes.c_uint64),
        ("result", ctypes.c_int64),
        ("device", ctypes.c_uint64),
        ("app_tag", ctypes.c_uint32),
        ("tgid", ctypes.c_uint32),
        ("tid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("op", ctypes.c_uint32),
        ("fd", ctypes.c_int32),
        ("dirfd", ctypes.c_int32),
        ("dirfd2", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
        ("whence", ctypes.c_uint32),
        ("offset_valid", ctypes.c_uint8),
        ("file_identity_valid", ctypes.c_uint8),
        ("comm", ctypes.c_char * 16),
        # c_char struct field access会在首个 NUL 自动裁短，无法判断“正好填满
        # PATH_LEN”是否为 BPF 截断；ubyte 数组保留完整原始缓冲区。
        ("path", ctypes.c_ubyte * PATH_LEN),
        ("path2", ctypes.c_ubyte * PATH_LEN),
    ]


class BPFCacheEvent(ctypes.Structure):
    _fields_ = [
        ("boot_timestamp_ns", ctypes.c_uint64),
        ("device", ctypes.c_uint64),
        ("inode", ctypes.c_uint64),
        ("offset", ctypes.c_uint64),
        ("size", ctypes.c_uint64),
        ("app_tag", ctypes.c_uint32),
        ("tgid", ctypes.c_uint32),
        ("tid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("kind", ctypes.c_uint32),
        ("page_order", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
    ]


class BPFWorkloadEvent(ctypes.Structure):
    _fields_ = [
        ("boot_timestamp_ns", ctypes.c_uint64),
        ("value1", ctypes.c_uint64),
        ("value2", ctypes.c_uint64),
        ("value3", ctypes.c_uint64),
        ("device", ctypes.c_uint64),
        ("app_tag", ctypes.c_uint32),
        ("tgid", ctypes.c_uint32),
        ("tid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("kind", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("rwbs", ctypes.c_char * 10),
    ]


def _decode(raw: bytes | ctypes.Array[Any]) -> str:
    value = bytes(raw).split(b"\0", 1)[0]
    return value.decode("utf-8", errors="replace")


def _path_was_truncated(raw: bytes | ctypes.Array[Any]) -> bool:
    """BPF 字符数组中没有 NUL，表示 bpf_probe_read_user_str 已截断。"""
    return b"\0" not in bytes(raw)


class EBPFFileEventHelper:
    """Load tracepoints, accept authenticated PID sets, and forward file events."""

    def __init__(
        self,
        *,
        target_uid: int,
        event_socket: Path,
        control_socket: Path,
        bpf_source: Path,
    ) -> None:
        self.target_uid = int(target_uid)
        self.event_socket = Path(event_socket)
        self.control_socket = Path(control_socket)
        self.bpf_source = Path(bpf_source)
        self.instance_id = uuid.uuid4().hex
        self.source_seq = 0
        self.delivery_drops = 0
        self.perf_lost = 0
        self.file_perf_lost = 0
        self.cache_perf_lost = 0
        self.workload_perf_lost = 0
        self.event_profile = "full"
        self.page_size = int(os.sysconf("SC_PAGE_SIZE"))
        self.unattributed_events = 0
        self.path_truncations = 0
        # PID -> (APP_ID, role, /proc starttime)。归属来自普通用户 monitor 的
        # AppProcessIndex；helper 不再自行扫描进程或根据 exe 猜 App。
        self.tracked_processes: dict[int, tuple[str, str, str]] = {}
        self.tracked_tids: dict[int, int] = {}
        # app_tag 是本 helper 实例内稳定的无符号编号。BPF map 和所有 perf 事件
        # 只传这个紧凑编号，字符串 App ID 不会在每个内核 hook 中复制。
        self.app_tags: dict[tuple[str, str], int] = {}
        self.tag_owners: dict[int, tuple[str, str]] = {}
        self.next_app_tag = 1
        self.pending_events: list[dict[str, Any]] = []
        self.page_window_ranges: dict[
            tuple[int, str],
            dict[tuple[int, int, int], list[tuple[int, int]]],
        ] = defaultdict(lambda: defaultdict(list))
        self.page_window_event_counts: Counter[tuple[int, str]] = Counter()
        self._last_page_callback_maintenance_ns = 0
        self._next_heartbeat_monotonic = time.monotonic() + HEARTBEAT_INTERVAL_S
        self.fd_paths: dict[tuple[int, int], str] = {}
        self.output = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.output.setblocking(False)
        self.control: socket.socket | None = None
        self.bpf: BPF | None = None
        self._validate_paths()

    def _validate_paths(self) -> None:
        expected_parent = Path(f"/run/user/{self.target_uid}")
        if self.event_socket.parent != expected_parent:
            raise ValueError(f"event socket must be under {expected_parent}")
        parent = expected_parent.stat()
        if parent.st_uid != self.target_uid or not stat.S_ISDIR(parent.st_mode):
            raise PermissionError(f"unsafe target runtime directory: {expected_parent}")
        if self.control_socket.parent != Path("/run"):
            raise ValueError("control socket must be an immediate child of /run")

    def _bind_control(self) -> None:
        try:
            existing = os.lstat(self.control_socket)
        except OSError:
            existing = None
        if existing is not None:
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != 0:
                raise PermissionError(f"unsafe control socket: {self.control_socket}")
            os.unlink(self.control_socket)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        listener.setblocking(False)
        listener.bind(str(self.control_socket))
        # 任何用户都可尝试发送，但下方 SCM_CREDENTIALS 只接受 target_uid。
        os.chmod(self.control_socket, 0o622)
        self.control = listener

    def _load_bpf(self) -> None:
        source = self.bpf_source.read_text(encoding="utf-8")
        self.bpf = BPF(text=source)
        self.bpf["events"].open_perf_buffer(
            self._on_event, lost_cb=self._on_file_lost, page_cnt=PERF_BUFFER_PAGES
        )
        self.bpf["cache_events"].open_perf_buffer(
            self._on_cache_event, lost_cb=self._on_cache_lost, page_cnt=PERF_BUFFER_PAGES
        )
        self.bpf["workload_events"].open_perf_buffer(
            self._on_workload_event, lost_cb=self._on_workload_lost, page_cnt=PERF_BUFFER_PAGES
        )

    def _safe_event_socket(self) -> bool:
        try:
            target = os.lstat(self.event_socket)
        except OSError:
            return False
        return stat.S_ISSOCK(target.st_mode) and target.st_uid == self.target_uid

    def _send(
        self,
        values: dict[str, Any],
        *,
        count_drop: bool = True,
        retry_deadline: float | None = None,
    ) -> bool:
        if not self._safe_event_socket():
            if count_drop:
                self.delivery_drops += 1
            return False
        payload = {
            "protocol_version": PROTOCOL_VERSION,
            "source": "ebpf-file-syscalls",
            "source_instance_id": self.instance_id,
            **values,
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > MAX_DATAGRAM_BYTES:
            if count_drop:
                self.delivery_drops += 1
            return False
        while True:
            try:
                self.output.sendto(body, str(self.event_socket))
                return True
            except OSError as exc:
                if exc.errno not in {
                    errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOENT,
                    errno.ECONNREFUSED, errno.ENOBUFS,
                }:
                    raise
                remaining = (
                    float(retry_deadline) - time.monotonic()
                    if retry_deadline is not None else 0.0
                )
                if remaining > 0.0 and exc.errno not in {
                    errno.ENOENT, errno.ECONNREFUSED,
                }:
                    # ``select`` yields when the peer has room again.  The
                    # deadline is shared by all chunks of one page window, so
                    # a slow receiver cannot block BPF callbacks indefinitely.
                    try:
                        select.select([], [self.output], [], remaining)
                    except (OSError, ValueError):
                        pass
                    continue
                if count_drop:
                    self.delivery_drops += 1
                return False

    def _send_status(self, status: str = "READY", detail: str = "") -> None:
        self._send({
            "kind": "SOURCE_STATUS",
            "status": status,
            "detail": detail,
            "timestamp_ns": time.time_ns(),
            "source_seq": self.source_seq,
            "delivery_drops": self.delivery_drops,
            "perf_lost": self.perf_lost,
            "file_perf_lost": self.file_perf_lost,
            "cache_perf_lost": self.cache_perf_lost,
            "workload_perf_lost": self.workload_perf_lost,
            "event_profile": self.event_profile,
            "unattributed_events": self.unattributed_events,
            "path_truncations": self.path_truncations,
            "tracked_pids": len(self.tracked_processes),
            "helper_pid": os.getpid(),
        }, count_drop=False)

    def _flush_events(self) -> None:
        """把逐 hook 事件合并成有界 Unix datagram 批量发送。

        BPF perf buffer 已经是单向通道；这里再批量化 root helper 到普通用户
        monitor 的第二段通道，避免高峰期为每次 read/page access 单独 sendto。
        source_seq 仍逐事件连续，因此任一批次丢失都能精确量化缺口。
        """
        pending = self.pending_events
        self.pending_events = []
        batch: list[dict[str, Any]] = []
        for event in pending:
            candidate = [*batch, event]
            envelope = {
                "protocol_version": PROTOCOL_VERSION,
                "source": "ebpf-file-syscalls",
                "source_instance_id": self.instance_id,
                "kind": "EVENT_BATCH",
                "events": candidate,
            }
            encoded_size = len(json.dumps(
                envelope, ensure_ascii=False, separators=(",", ":")
            ).encode())
            if batch and encoded_size > MAX_DATAGRAM_BYTES:
                if not self._send(
                    {"kind": "EVENT_BATCH", "events": batch}, count_drop=False
                ):
                    self.delivery_drops += len(batch)
                batch = [event]
            else:
                batch = candidate
        if batch and not self._send(
            {"kind": "EVENT_BATCH", "events": batch}, count_drop=False
        ):
            self.delivery_drops += len(batch)

    @staticmethod
    def _compressed_page_ranges(
        grouped: dict[tuple[int, int, int], list[tuple[int, int]]],
    ) -> tuple[list[dict[str, int]], int, int]:
        ranges: list[dict[str, int]] = []
        unique_page_count = 0
        repeated_page_hits = 0
        for (major, minor, inode), intervals in sorted(grouped.items()):
            if not intervals:
                continue
            total_pages = sum(end - start + 1 for start, end in intervals)
            merged: list[tuple[int, int]] = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                    continue
                merged.append((start, end))
            unique = sum(end - start + 1 for start, end in merged)
            unique_page_count += unique
            repeated_page_hits += max(0, total_pages - unique)
            for start, end in merged:
                ranges.append({
                    "device_major": major,
                    "device_minor": minor,
                    "inode": inode,
                    "start_page_index": start,
                    "page_count": end - start + 1,
                })
        return ranges, unique_page_count, repeated_page_hits

    def _aggregate_page_access(self, event: BPFCacheEvent) -> None:
        owner = self._owner_for_tag(int(event.app_tag))
        if owner is None:
            self.unattributed_events += 1
            return
        app, _process_role = owner
        timestamp_ns = (
            time.time_ns() - time.monotonic_ns() + int(event.boot_timestamp_ns)
        )
        window_start_ns = timestamp_ns - timestamp_ns % PAGE_WINDOW_NS
        device = self._device_fields(int(event.device))
        inode = int(event.inode)
        offset = int(event.offset)
        size = int(event.size)
        if inode <= 0 or offset < 0 or size <= 0:
            return
        first_page = offset // self.page_size
        last_page = (offset + size - 1) // self.page_size
        key = (window_start_ns, app)
        ranges = self.page_window_ranges[key]
        ranges[(
            device["device_major"], device["device_minor"], inode
        )].append((first_page, last_page))
        self.page_window_event_counts[key] += 1
        # BCC may keep calling callbacks while a hot GUI process continuously
        # fills the perf ring, postponing the outer polling loop.  Periodic
        # maintenance here guarantees the completed 1s aggregate reaches the
        # monitor before its 500ms lateness cutoff and preserves heartbeats.
        now_mono_ns = time.monotonic_ns()
        if (
            now_mono_ns - self._last_page_callback_maintenance_ns
            >= PAGE_CALLBACK_MAINTENANCE_NS
        ):
            self._last_page_callback_maintenance_ns = now_mono_ns
            self._flush_page_windows()
            if time.monotonic() >= self._next_heartbeat_monotonic:
                self._send_status()
                self._next_heartbeat_monotonic = (
                    time.monotonic() + HEARTBEAT_INTERVAL_S
                )

    def _flush_page_windows(self, *, force: bool = False) -> None:
        if not self.page_window_ranges:
            return
        cutoff_ns = time.time_ns() - PAGE_WINDOW_FLUSH_DELAY_NS
        ready = sorted(
            key for key in self.page_window_ranges
            if force or key[0] + PAGE_WINDOW_NS <= cutoff_ns
        )
        for key in ready:
            window_start_ns, app = key
            page_ranges = self.page_window_ranges.pop(key)
            event_count = int(self.page_window_event_counts.pop(key, 0))
            ranges, page_count, repeated = self._compressed_page_ranges(page_ranges)
            if not ranges:
                continue
            chunks = [
                ranges[index:index + PAGE_RANGES_PER_CHUNK]
                for index in range(0, len(ranges), PAGE_RANGES_PER_CHUNK)
            ]
            retry_deadline = time.monotonic() + PAGE_WINDOW_SEND_RETRY_S
            for chunk_index, chunk in enumerate(chunks):
                self.source_seq += 1
                sent = self._send({
                    "kind": "PAGE_ACCESS_WINDOW",
                    "timestamp_ns": window_start_ns + PAGE_WINDOW_NS,
                    "source_seq": self.source_seq,
                    "app": app,
                    "window_start_ns": window_start_ns,
                    "window_end_ns": window_start_ns + PAGE_WINDOW_NS,
                    "page_size": self.page_size,
                    "page_count": page_count,
                    "page_access_events": event_count if chunk_index == 0 else 0,
                    "repeated_page_hits": repeated if chunk_index == 0 else 0,
                    "chunk_index": chunk_index,
                    "chunk_count": len(chunks),
                    "page_ranges": chunk,
                }, count_drop=False, retry_deadline=retry_deadline)
                if not sent:
                    self.delivery_drops += 1

    @staticmethod
    def _credentials(ancillary: list[tuple[int, int, bytes]]) -> tuple[int, int, int] | None:
        for level, kind, data in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS:
                if len(data) >= UCRED.size:
                    return UCRED.unpack_from(data)
        return None

    def _tag_for(self, app: str, role: str) -> int:
        """为一个 App/角色分配 helper 生命周期内稳定的内核 tag。"""
        owner = (str(app).upper(), "fixture" if role == "fixture" else "gui")
        existing = self.app_tags.get(owner)
        if existing is not None:
            return existing
        tag = self.next_app_tag
        self.next_app_tag += 1
        self.app_tags[owner] = tag
        self.tag_owners[tag] = owner
        return tag

    @staticmethod
    def _task_ids(pid: int) -> set[int]:
        """只在 AppProcessIndex 改变时枚举该 TGID 的线程，不做周期全局扫描。"""
        try:
            return {
                int(entry.name)
                for entry in os.scandir(f"/proc/{int(pid)}/task")
                if entry.name.isdigit()
            }
        except OSError:
            return set()

    def _drain_control(self) -> None:
        assert self.control is not None and self.bpf is not None
        while True:
            try:
                body, ancillary, flags, _address = self.control.recvmsg(
                    MAX_CONTROL_BYTES, socket.CMSG_SPACE(UCRED.size)
                )
            except BlockingIOError:
                return
            credentials = self._credentials(ancillary)
            if (
                credentials is None
                or credentials[1] != self.target_uid
                or flags & socket.MSG_TRUNC
            ):
                continue
            try:
                payload = json.loads(body.decode("utf-8"))
                requested_profile = str(
                    payload.get("event_profile", "full")
                ).strip().lower()
                if requested_profile not in {"full", "page-hotset"}:
                    requested_profile = "full"
                processes: dict[int, tuple[str, str, str]] = {}
                for item in payload.get("processes", []):
                    pid = int(item.get("pid", 0) or 0)
                    app = str(item.get("app", "")).strip().upper()
                    role = (
                        "fixture" if str(item.get("role", "")) == "fixture"
                        else "gui"
                    )
                    start_time = str(item.get("start_time", ""))
                    if pid > 0 and app:
                        processes[pid] = (app, role, start_time)
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                continue
            self.event_profile = requested_profile
            profile_table = self.bpf["page_hotset_only"]
            profile_table[ctypes.c_int(0)] = ctypes.c_uint(
                1 if self.event_profile == "page-hotset" else 0
            )
            table = self.bpf["target_tgids"]
            tid_table = self.bpf["target_tids"]
            old_pids = set(self.tracked_processes)
            new_pids = set(processes)
            for pid in old_pids - new_pids:
                try:
                    del table[ctypes.c_uint(pid)]
                except KeyError:
                    pass
            if old_pids - new_pids:
                removed_pids = old_pids - new_pids
                self.fd_paths = {
                    key: path for key, path in self.fd_paths.items()
                    if key[0] not in removed_pids
                }
            process_tags: dict[int, int] = {}
            for pid, (app, role, _start_time) in processes.items():
                tag = self._tag_for(app, role)
                process_tags[pid] = tag
                # 对仍存活但发生 EXEC/role 变化的 PID 也覆盖 value，不能只更新差集。
                table[ctypes.c_uint32(pid)] = ctypes.c_uint32(tag)

            new_tids: dict[int, int] = {}
            for pid, tag in process_tags.items():
                for tid in self._task_ids(pid):
                    new_tids[tid] = tag
            for tid in set(self.tracked_tids) - set(new_tids):
                try:
                    del tid_table[ctypes.c_uint32(tid)]
                except KeyError:
                    pass
            for tid, tag in new_tids.items():
                tid_table[ctypes.c_uint32(tid)] = ctypes.c_uint32(tag)
            self.tracked_tids = new_tids
            self.tracked_processes = processes
            self._send_status("READY", "target pid set synchronized")

    @staticmethod
    def _read_link(path: Path) -> str:
        try:
            return os.readlink(path)
        except OSError:
            return ""

    def _resolve_argument_path(self, tgid: int, raw: str, dirfd: int) -> str:
        if not raw:
            return ""
        if raw.startswith("/"):
            return os.path.normpath(raw)
        base_link = (
            Path("/proc") / str(tgid) / "cwd"
            if dirfd == -100
            else Path("/proc") / str(tgid) / "fd" / str(dirfd)
        )
        base = self._read_link(base_link)
        return os.path.normpath(os.path.join(base, raw)) if base.startswith("/") else raw

    def _resolve_fd_path(self, tgid: int, fd: int) -> str:
        if fd < 0:
            return ""
        cached = self.fd_paths.get((int(tgid), int(fd)), "")
        if cached:
            return cached
        value = self._read_link(Path("/proc") / str(tgid) / "fd" / str(fd))
        return value if value.startswith("/") else ""

    @staticmethod
    def _inode(path: str) -> int:
        try:
            return int(os.stat(path).st_ino) if path.startswith("/") else 0
        except OSError:
            return 0

    def _owner_for_tag(self, tag: int) -> tuple[str, str] | None:
        return self.tag_owners.get(int(tag))

    @staticmethod
    def _device_fields(device: int) -> dict[str, int]:
        encoded = max(0, int(device))
        # BPF 读取的是内核 dev_t（MAJOR=dev>>20、MINOR=低20位），不是 stat(2)
        # 返回给用户态后供 os.major/os.minor 使用的 new_encode_dev 格式。
        return {
            "device": encoded,
            "device_major": encoded >> 20,
            "device_minor": encoded & ((1 << 20) - 1),
        }

    def _send_tagged_event(
        self,
        *,
        app_tag: int,
        event_type: str,
        boot_timestamp_ns: int,
        values: dict[str, Any],
    ) -> None:
        owner = self._owner_for_tag(app_tag)
        if owner is None:
            self.unattributed_events += 1
            return
        app, process_role = owner
        self.source_seq += 1
        wall_offset_ns = time.time_ns() - time.monotonic_ns()
        self.pending_events.append({
            "kind": "FILE_EVENT",
            "event_type": event_type,
            "timestamp_ns": wall_offset_ns + int(boot_timestamp_ns),
            "boot_timestamp_ns": int(boot_timestamp_ns),
            "source_seq": self.source_seq,
            "app": app,
            "process_role": process_role,
            **values,
        })

    def _on_event(self, _cpu: int, data: int, _size: int) -> None:
        if self.event_profile == "page-hotset":
            return
        event = ctypes.cast(data, ctypes.POINTER(BPFFileEvent)).contents
        if int(event.uid) != self.target_uid:
            return
        op = OP_NAMES.get(int(event.op), "")
        if not op:
            return
        owner = self._owner_for_tag(event.app_tag)
        if owner is None:
            self.unattributed_events += 1
            return
        if op == "close":
            if int(event.result) >= 0:
                self.fd_paths.pop((int(event.tgid), int(event.fd)), None)
            return
        if op == "dup":
            if int(event.result) >= 0:
                old_path = self._resolve_fd_path(event.tgid, event.fd)
                if old_path:
                    self.fd_paths[(int(event.tgid), int(event.result))] = old_path
            return
        raw_path = _decode(event.path)
        raw_path2 = _decode(event.path2)
        path_truncated = int(
            _path_was_truncated(event.path)
            or _path_was_truncated(event.path2)
        )
        self.path_truncations += path_truncated
        if op in {"openat", "rename", "access"}:
            path = self._resolve_argument_path(event.tgid, raw_path, event.dirfd)
        else:
            path = self._resolve_fd_path(event.tgid, event.fd)
        new_path = (
            self._resolve_argument_path(event.tgid, raw_path2, event.dirfd2)
            if op == "rename" else ""
        )
        # 即使进程在 perf 消费前已 close，内核捕获的 device+inode 仍是精确身份；
        # 只有既没有普通文件身份又没有绝对路径时才排除 pipe/socket/匿名 mmap。
        if (
            op in {"read", "pread", "write", "pwrite", "fsync", "mmap", "lseek"}
            and not int(event.file_identity_valid)
            and not path.startswith("/")
        ):
            return
        if op == "openat" and path.startswith("/") and int(event.fd) >= 0:
            self.fd_paths[(int(event.tgid), int(event.fd))] = path
        inode = int(event.inode) if int(event.file_identity_valid) else self._inode(new_path or path)
        returned_size = int(event.returned_size)
        legacy_size = (
            returned_size
            if op in {"read", "pread", "write", "pwrite"}
            else int(event.requested_size)
        )
        self._send_tagged_event(
            app_tag=int(event.app_tag),
            event_type=op,
            boot_timestamp_ns=int(event.exit_boot_ns),
            values={
            "pid": int(event.tgid),
            "tid": int(event.tid),
            "comm": _decode(event.comm),
            "fd": int(event.fd),
            "result": int(event.result),
            "enter_boot_ns": int(event.enter_boot_ns),
            "exit_boot_ns": int(event.exit_boot_ns),
            "latency_ns": max(0, int(event.exit_boot_ns) - int(event.enter_boot_ns)),
            "requested_size": int(event.requested_size),
            "returned_size": returned_size,
            "size": legacy_size,
            "offset": int(event.offset),
            "requested_offset": int(event.requested_offset),
            "file_position": int(event.file_position),
            "offset_valid": int(event.offset_valid),
            "file_identity_valid": int(event.file_identity_valid),
            "flags": int(event.flags),
            "whence": int(event.whence),
            "path": path,
            "new_path": new_path,
            "path_truncated": path_truncated,
            "inode": inode,
            **self._device_fields(event.device),
        })

    def _on_cache_event(self, _cpu: int, data: int, _size: int) -> None:
        """转发内核 accessFile/evictFile hook 的真实页缓存事件。"""
        event = ctypes.cast(data, ctypes.POINTER(BPFCacheEvent)).contents
        event_type = CACHE_NAMES.get(int(event.kind), "")
        if not event_type:
            return
        # ``page_access`` is never forwarded as a raw per-hook event.  Both
        # profiles share the same compressed one-second window transport;
        # ``full`` continues to deliver all non-page cache/workload telemetry.
        if event_type == "page_access":
            self._aggregate_page_access(event)
            return
        if self.event_profile == "page-hotset":
            return
        self._send_tagged_event(
            app_tag=int(event.app_tag),
            event_type=event_type,
            boot_timestamp_ns=int(event.boot_timestamp_ns),
            values={
                "pid": int(event.tgid),
                "tid": int(event.tid),
                "comm": _decode(event.comm),
                "inode": int(event.inode),
                "offset": int(event.offset),
                "size": int(event.size),
                "requested_size": int(event.size),
                "returned_size": 0,
                "offset_valid": 1,
                "file_identity_valid": 1,
                "page_order": int(event.page_order),
                **self._device_fields(event.device),
            },
        )

    def _on_workload_event(self, _cpu: int, data: int, _size: int) -> None:
        if self.event_profile == "page-hotset":
            return
        event = ctypes.cast(data, ctypes.POINTER(BPFWorkloadEvent)).contents
        event_type = WORKLOAD_NAMES.get(int(event.kind), "")
        if not event_type:
            return
        values: dict[str, Any] = {
            "pid": int(event.tgid),
            "tid": int(event.tid),
            "comm": _decode(event.comm),
            "value1": int(event.value1),
            "value2": int(event.value2),
            "value3": int(event.value3),
            **self._device_fields(event.device),
        }
        if event_type == "page_fault":
            values.update({
                "address": int(event.value1),
                "instruction_pointer": int(event.value2),
                "fault_error_code": int(event.value3),
            })
        elif event_type == "block_io":
            values.update({
                "sector": int(event.value1),
                "sector_count": int(event.value2),
                "size": int(event.value3),
                "rwbs": _decode(event.rwbs),
                "attribution_scope": "issuing-task-only",
            })
        else:
            values["delay_ns"] = int(event.value1)
        self._send_tagged_event(
            app_tag=int(event.app_tag),
            event_type=event_type,
            boot_timestamp_ns=int(event.boot_timestamp_ns),
            values=values,
        )

    def _record_lost(self, channel: str, *callback_args: int) -> None:
        # BCC's Python API is version-dependent: Ubuntu 22.04 invokes lost_cb
        # with only ``lost``, while newer bindings may pass ``cpu, lost``.
        # Reading the final argument supports both ABIs and ensures a perf-ring
        # loss invalidates affected page-hotset windows instead of being hidden
        # behind a TypeError in the callback.
        if not callback_args:
            return
        count = int(callback_args[-1])
        if channel == "workload":
            # Workload events use a separate ring and are not page_access
            # observations. Keep their loss visible without invalidating an
            # otherwise complete file-page window.
            self.workload_perf_lost += count
            self._send_status(
                "WORKLOAD_PERF_LOST",
                f"workload ring lost {count} eBPF perf event(s)",
            )
            return
        if channel == "cache":
            self.cache_perf_lost += count
        else:
            self.file_perf_lost += count
        self.perf_lost += count
        self._send_status(
            "PERF_LOST", f"{channel} ring lost {count} eBPF perf event(s)"
        )

    def _on_file_lost(self, *callback_args: int) -> None:
        self._record_lost("file", *callback_args)

    def _on_cache_lost(self, *callback_args: int) -> None:
        self._record_lost("cache", *callback_args)

    def _on_workload_lost(self, *callback_args: int) -> None:
        self._record_lost("workload", *callback_args)

    def _on_lost(self, *callback_args: int) -> None:
        """Compatibility alias for older direct callers (file ring)."""
        self._on_file_lost(*callback_args)

    def run(self) -> int:
        self._bind_control()
        try:
            self._load_bpf()
            self._send_status("READY", "eBPF file/cache/workload tracepoints attached")
            assert self.bpf is not None and self.control is not None
            self._next_heartbeat_monotonic = time.monotonic() + HEARTBEAT_INTERVAL_S
            while True:
                self.bpf.perf_buffer_poll(timeout=100)
                self._flush_events()
                self._flush_page_windows()
                readable, _, _ = select.select([self.control], [], [], 0)
                if readable:
                    self._drain_control()
                if time.monotonic() >= self._next_heartbeat_monotonic:
                    self._flush_events()
                    self._flush_page_windows()
                    self._send_status()
                    self._next_heartbeat_monotonic = time.monotonic() + HEARTBEAT_INTERVAL_S
        finally:
            self.output.close()
            if self.control is not None:
                self.control.close()
            try:
                existing = os.lstat(self.control_socket)
                if stat.S_ISSOCK(existing.st_mode) and existing.st_uid == 0:
                    os.unlink(self.control_socket)
            except OSError:
                pass


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-uid", type=int, required=True)
    parser.add_argument("--event-socket", type=Path)
    parser.add_argument("--control-socket", type=Path)
    parser.add_argument("--bpf-source", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if os.geteuid() != 0:
        raise SystemExit("eBPF file helper must run as root")
    uid = int(args.target_uid)
    if uid <= 0:
        raise SystemExit("target uid must be a positive non-root UID")
    return EBPFFileEventHelper(
        target_uid=uid,
        event_socket=args.event_socket or Path(f"/run/user/{uid}/parp-file-events.sock"),
        control_socket=args.control_socket or Path(f"/run/parp-file-events-{uid}.sock"),
        bpf_source=args.bpf_source,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
