#!/usr/bin/env python3
"""Privileged eBPF file-syscall source for one unprivileged monitor UID."""

from __future__ import annotations

import argparse
import array
import ctypes
import errno
import json
import mmap
import os
import select
import socket
import stat
import struct
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bcc import BPF

try:
    from runtime_monitor.core.page_access_window import PageAccessWindowCapture
except ImportError:
    # install_service.sh 会把独立 helper 与该模块一起安装到 /usr/local/libexec。
    from parp_page_access_window import PageAccessWindowCapture


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
# 翻转 epoch 后给已经读到旧 epoch 的在途 BPF hook 一个很短的退出宽限，
# 再枚举旧 Map；否则极端边界下可能在 items() 之后向旧 Map 补写一页。
PAGE_EPOCH_FLIP_GRACE_S = 0.005
PAGEMAP_ENTRY = struct.Struct("<Q")
PAGEMAP_PRESENT = 1 << 63
PAGEMAP_PFN_MASK = (1 << 55) - 1
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
CACHE_NAMES = {
    1: "page_access",
    2: "eviction",
    3: "FAULT",
    4: "CACHE_ADD",
}
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
        ("pfn", ctypes.c_uint64),
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
        self.page_capture: PageAccessWindowCapture | None = None
        self.page_capture_id = ""
        self.page_capture_target_app = ""
        self.page_capture_last_manifest: dict[str, Any] = {}
        self.page_capture_start_counters: dict[str, int] = {}
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
        self._calibrate_vmemmap_base()
        self.bpf["events"].open_perf_buffer(
            self._on_event, lost_cb=self._on_file_lost, page_cnt=PERF_BUFFER_PAGES
        )
        self.bpf["cache_events"].open_perf_buffer(
            self._on_cache_event, lost_cb=self._on_cache_lost, page_cnt=PERF_BUFFER_PAGES
        )
        self.bpf["workload_events"].open_perf_buffer(
            self._on_workload_event, lost_cb=self._on_workload_lost, page_cnt=PERF_BUFFER_PAGES
        )

    def _calibrate_vmemmap_base(self) -> None:
        """用两个受控文件页交叉校验 ``folio* -> PFN`` 换算基址。

        PFN 来自 helper 自身 pagemap，folio 指针来自只匹配同一
        TGID/device/inode/page_index 的 BPF calibration 分支。两页算出的
        基址必须完全一致，否则 helper 拒绝启动，不会猜测 PFN。
        """
        assert self.bpf is not None
        zero = ctypes.c_int(0)
        helper_pid = os.getpid()
        temporary: Any | None = None
        mapping: mmap.mmap | None = None
        address_holder: Any | None = None
        pagemap_fd = -1
        try:
            temporary = tempfile.TemporaryFile(prefix="parp-pfn-", dir="/var/tmp")
            temporary.truncate(self.page_size * 2)
            mapping = mmap.mmap(
                temporary.fileno(), self.page_size * 2, access=mmap.ACCESS_WRITE
            )
            mapping[0] = 0x51
            mapping[self.page_size] = 0x52
            mapping.flush()
            address_holder = ctypes.c_char.from_buffer(mapping)
            base_address = ctypes.addressof(address_holder)
            pagemap_fd = os.open(
                f"/proc/{helper_pid}/pagemap", os.O_RDONLY | os.O_CLOEXEC
            )
            values = os.fstat(temporary.fileno())
            kernel_device = (
                int(os.major(values.st_dev)) << 20
            ) | int(os.minor(values.st_dev))
            candidates: list[int] = []
            page_struct_sizes: list[int] = []
            for page_index in (0, 1):
                raw = os.pread(
                    pagemap_fd,
                    PAGEMAP_ENTRY.size,
                    ((base_address // self.page_size) + page_index)
                    * PAGEMAP_ENTRY.size,
                )
                if len(raw) != PAGEMAP_ENTRY.size:
                    raise RuntimeError("short pagemap read during PFN calibration")
                entry = PAGEMAP_ENTRY.unpack(raw)[0]
                pfn = int(entry & PAGEMAP_PFN_MASK)
                if not entry & PAGEMAP_PRESENT or pfn <= 0:
                    raise RuntimeError("calibration file page has no visible PFN")
                self.bpf["pfn_calibration_device"][zero] = ctypes.c_uint64(
                    kernel_device
                )
                self.bpf["pfn_calibration_inode"][zero] = ctypes.c_uint64(
                    int(values.st_ino)
                )
                self.bpf["pfn_calibration_index"][zero] = ctypes.c_uint64(
                    page_index
                )
                self.bpf["pfn_calibration_folio"][zero] = ctypes.c_uint64(0)
                self.bpf["kernel_page_struct_size"][zero] = ctypes.c_uint64(0)
                self.bpf["pfn_calibration_tgid"][zero] = ctypes.c_uint32(
                    helper_pid
                )
                os.pread(temporary.fileno(), 1, page_index * self.page_size)
                folio = self._ctypes_int(
                    self.bpf["pfn_calibration_folio"][zero]
                )
                page_struct_size = self._ctypes_int(
                    self.bpf["kernel_page_struct_size"][zero]
                )
                if folio <= 0 or page_struct_size <= 0:
                    raise RuntimeError("folio calibration hook did not match its file page")
                candidates.append(folio - pfn * page_struct_size)
                page_struct_sizes.append(page_struct_size)
            if (
                len(set(candidates)) != 1
                or len(set(page_struct_sizes)) != 1
                or candidates[0] <= 0
                or candidates[0] % self.page_size != 0
            ):
                raise RuntimeError(
                    "two-page folio/PFN calibration produced inconsistent bases"
                )
            self.bpf["vmemmap_calibrated_base"][zero] = ctypes.c_uint64(
                candidates[0]
            )
        finally:
            if self.bpf is not None:
                self.bpf["pfn_calibration_tgid"][zero] = ctypes.c_uint32(0)
                self.bpf["pfn_calibration_device"][zero] = ctypes.c_uint64(0)
                self.bpf["pfn_calibration_inode"][zero] = ctypes.c_uint64(0)
                self.bpf["pfn_calibration_index"][zero] = ctypes.c_uint64(0)
                self.bpf["pfn_calibration_folio"][zero] = ctypes.c_uint64(0)
            if pagemap_fd >= 0:
                os.close(pagemap_fd)
            if address_holder is not None:
                del address_holder
            if mapping is not None:
                mapping.close()
            if temporary is not None:
                temporary.close()

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

    def _send_status(
        self, status: str = "READY", detail: str = "", **extra: Any
    ) -> None:
        capture = self.page_capture
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
            "page_capture_active": self.page_capture is not None,
            "page_capture_id": self.page_capture_id,
            "page_capture_target_app": self.page_capture_target_app,
            "page_capture_window_id": int(capture.window_id) if capture else 0,
            "page_capture_invalid_windows": (
                int(capture.total_invalid_windows) if capture else 0
            ),
            **extra,
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

    @staticmethod
    def _file_descriptors(
        ancillary: list[tuple[int, int, bytes]],
    ) -> list[int]:
        result: list[int] = []
        for level, kind, data in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                continue
            usable = len(data) - len(data) % array.array("i").itemsize
            values = array.array("i")
            values.frombytes(data[:usable])
            result.extend(int(value) for value in values)
        return result

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
                    MAX_CONTROL_BYTES,
                    socket.CMSG_SPACE(UCRED.size)
                    + socket.CMSG_SPACE(5 * array.array("i").itemsize),
                )
            except BlockingIOError:
                return
            credentials = self._credentials(ancillary)
            received_fds = self._file_descriptors(ancillary)
            if (
                credentials is None
                or credentials[1] != self.target_uid
                or flags & socket.MSG_TRUNC
            ):
                for fd in received_fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                continue
            try:
                payload = json.loads(body.decode("utf-8"))
                command = str(payload.get("command", "SYNC_PROCESSES")).upper()
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                for fd in received_fds:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                continue
            if command == "START_PAGE_ACCESS_CAPTURE":
                self._start_page_access_capture(payload, received_fds)
                continue
            for fd in received_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if command == "STOP_PAGE_ACCESS_CAPTURE":
                self._stop_page_access_capture(
                    expected_capture_id=str(payload.get("capture_id", ""))
                )
                continue
            if command == "GET_PAGE_ACCESS_CAPTURE_STATUS":
                self._send_status(
                    "PAGE_CAPTURE_STATUS",
                    "active" if self.page_capture is not None else "inactive",
                )
                continue
            if command != "SYNC_PROCESSES":
                self._send_status("CONTROL_REJECTED", f"unknown command: {command}")
                continue
            try:
                requested_profile = str(
                    payload.get("event_profile", "full")
                ).strip().lower()
                if requested_profile not in {
                    "full", "page-hotset", "page-access-window"
                }:
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
                1 if self.event_profile == "page-hotset"
                else 2 if self.event_profile == "page-access-window"
                else 0
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
            self._refresh_page_capture_tags()
            if self.page_capture is not None:
                # 控制 socket 可能在 WPS 启动时连续收到 create/exec 快照。
                # 这里只维护增量索引并标 dirty，真正的 /proc VMA+pagemap 扫描
                # 统一放到下个窗口边界，避免阻塞 eBPF epoch 的准时切换。
                self.page_capture.update_processes(processes, refresh=False)
            self._send_status("READY", "target pid set synchronized")

    @staticmethod
    def _close_fds(fds: list[int]) -> None:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass

    def _refresh_page_capture_tags(self) -> None:
        if self.bpf is None:
            return
        table = self.bpf["page_capture_tags"]
        try:
            table.clear()
        except AttributeError:
            for key in list(table.keys()):
                del table[key]
        target = self.page_capture_target_app
        if not target:
            return
        for tag, (app, _role) in self.tag_owners.items():
            if app == target:
                table[ctypes.c_uint32(tag)] = ctypes.c_ubyte(1)

    def _start_page_access_capture(
        self, payload: dict[str, Any], received_fds: list[int]
    ) -> None:
        assert self.bpf is not None
        if self.page_capture is not None:
            self._close_fds(received_fds)
            self._send_status("PAGE_CAPTURE_START_FAILED", "capture already active")
            return
        if len(received_fds) != 5:
            self._close_fds(received_fds)
            self._send_status(
                "PAGE_CAPTURE_START_FAILED",
                f"expected five output fds, received {len(received_fds)}",
            )
            return
        try:
            for fd in received_fds:
                values = os.fstat(fd)
                if (
                    not stat.S_ISREG(values.st_mode)
                    or values.st_uid != self.target_uid
                    or values.st_mode & 0o077
                ):
                    raise PermissionError("unsafe page capture output descriptor")
            capture_id = str(payload.get("capture_id", "")).strip()
            session_id = str(payload.get("session_id", "")).strip()
            target_app = str(payload.get("target_app", "")).strip().upper()
            if not capture_id or not session_id or not target_app:
                raise ValueError("capture_id, session_id and target_app are required")
            fixture_catalog = payload.get("fixture_catalog", {})
            if not isinstance(fixture_catalog, dict):
                raise ValueError("fixture_catalog must be an object")
            metadata = payload.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("metadata must be an object")
            if str(payload.get("event_profile", "")) != "page-access-window":
                raise ValueError("START requires page-access-window event profile")
            self.event_profile = "page-access-window"
            self.bpf["page_hotset_only"][ctypes.c_int(0)] = ctypes.c_uint32(2)
            self.page_capture_target_app = target_app
            self.page_capture_id = capture_id
            self._tag_for(target_app, "gui")
            self.page_capture_start_counters = self._page_capture_counters()
            self._refresh_page_capture_tags()
            self.page_capture = PageAccessWindowCapture(
                window_fd=received_fds[0],
                lifecycle_fd=received_fds[1],
                catalog_fd=received_fds[2],
                summary_fd=received_fds[3],
                manifest_fd=received_fds[4],
                session_id=session_id,
                target_app=target_app,
                app_id=int(payload.get("app_id", 0) or 0),
                window_ms=int(payload.get("window_ms", 1000) or 1000),
                fixture_catalog=fixture_catalog,
                metadata=metadata,
            )
            # PageAccessWindowCapture 已取得 fd 所有权，异常路径才由本函数关闭。
            received_fds = []
            self.page_capture.update_processes(self.tracked_processes)
            self._clear_page_capture_maps()
            self.page_capture.arm_initial_window()
            self.bpf["page_window_enabled"][ctypes.c_int(0)] = ctypes.c_uint32(1)
            self._send_status("PAGE_CAPTURE_READY", "Page Idle window capture started")
        except Exception as exc:
            self._close_fds(received_fds)
            failed_capture = self.page_capture
            if failed_capture is not None:
                try:
                    failed_capture.close(
                        capture_status="FAILED", failure_reason=str(exc)
                    )
                except Exception:
                    pass
            self.page_capture = None
            self.page_capture_target_app = ""
            self.page_capture_id = ""
            self.page_capture_start_counters = {}
            self._refresh_page_capture_tags()
            self._send_status("PAGE_CAPTURE_START_FAILED", str(exc))

    def _page_capture_counters(self) -> dict[str, int]:
        """返回 helper 累计计数器快照，供 manifest 记录 capture 期间增量。"""
        return {
            "delivery_drops": int(self.delivery_drops),
            "perf_lost": int(self.perf_lost),
            "file_perf_lost": int(self.file_perf_lost),
            "cache_perf_lost": int(self.cache_perf_lost),
            "workload_perf_lost": int(self.workload_perf_lost),
        }

    def _clear_page_capture_maps(self) -> None:
        assert self.bpf is not None
        self.bpf["page_window_enabled"][ctypes.c_int(0)] = ctypes.c_uint32(0)
        self.bpf["page_window_epoch"][ctypes.c_int(0)] = ctypes.c_uint32(0)
        for name in (
            "page_window_epoch0", "page_window_epoch1",
            "page_window_ranges_epoch0", "page_window_ranges_epoch1",
            "page_mapping_dirty",
        ):
            table = self.bpf[name]
            try:
                table.clear()
            except AttributeError:
                for key in list(table.keys()):
                    del table[key]
        overflows = self.bpf["page_window_overflows"]
        overflows[ctypes.c_int(0)] = ctypes.c_uint64(0)
        overflows[ctypes.c_int(1)] = ctypes.c_uint64(0)

    def _stop_page_access_capture(self, *, expected_capture_id: str = "") -> None:
        if self.page_capture is None or self.bpf is None:
            self._send_status("PAGE_CAPTURE_STOPPED", "no active capture")
            return
        if expected_capture_id and expected_capture_id != self.page_capture_id:
            self._send_status("PAGE_CAPTURE_STOP_FAILED", "capture_id mismatch")
            return
        try:
            self.bpf["page_window_enabled"][ctypes.c_int(0)] = ctypes.c_uint32(0)
            rows, overflows, dirty = self._rotate_and_drain_page_maps(reenable=False)
            # 即使最后一个不足 1 秒的窗口为空也保留，并标记
            # INCOMPLETE_FINAL_WINDOW；这能区分“确实没有访问”和“未落盘”。
            self.page_capture.rotate(
                rows,
                bpf_map_overflows=overflows,
                dirty_pids=dirty,
                final=True,
            )
            end_counters = self._page_capture_counters()
            start_counters = dict(self.page_capture_start_counters)
            self.page_capture.metadata["source_integrity"] = {
                "helper_instance_id": self.instance_id,
                "helper_pid": os.getpid(),
                "counter_start": start_counters,
                "counter_end": end_counters,
                "counter_delta": {
                    name: max(0, value - int(start_counters.get(name, 0)))
                    for name, value in end_counters.items()
                },
            }
            self.page_capture_last_manifest = self.page_capture.close()
            self._send_status("PAGE_CAPTURE_STOPPED", "capture flushed and closed")
        except Exception as exc:
            try:
                self.page_capture.close(
                    capture_status="FAILED", failure_reason=str(exc)
                )
            except Exception:
                pass
            self._send_status("PAGE_CAPTURE_STOP_FAILED", str(exc))
        finally:
            self.page_capture = None
            self.page_capture_target_app = ""
            self.page_capture_id = ""
            self.page_capture_start_counters = {}
            self._refresh_page_capture_tags()

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
        if self.event_profile in {"page-hotset", "page-access-window"}:
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
            if self.event_profile != "page-access-window":
                self._aggregate_page_access(event)
            return
        if self.event_profile == "page-hotset":
            return
        if self.event_profile == "page-access-window":
            capture = self.page_capture
            if capture is None:
                return
            owner = self._owner_for_tag(int(event.app_tag))
            if owner is None or owner[0] != self.page_capture_target_app:
                return
            device = self._device_fields(int(event.device))
            capture.record_lifecycle({
                "event_type": "EVICT" if event_type == "eviction" else event_type,
                "boot_timestamp_ns": int(event.boot_timestamp_ns),
                "device_major": device["device_major"],
                "device_minor": device["device_minor"],
                "inode": int(event.inode),
                "page_index": int(event.offset) // self.page_size,
                "pfn": int(event.pfn),
                "page_order": int(event.page_order),
                "tgid": int(event.tgid),
                "tid": int(event.tid),
            })
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
        if self.event_profile in {"page-hotset", "page-access-window"}:
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
            if self.page_capture is not None:
                self.page_capture.note_quality_error(
                    f"CACHE_PERF_LOST:{count}"
                )
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

    @staticmethod
    def _ctypes_int(value: Any) -> int:
        return int(getattr(value, "value", value))

    def _rotate_and_drain_page_maps(
        self, *, reenable: bool = True
    ) -> tuple[list[dict[str, int]], int, set[int]]:
        """先翻转 active epoch，再排空关闭 epoch，避免边界事件丢失。"""
        assert self.bpf is not None
        epoch_table = self.bpf["page_window_epoch"]
        old_epoch = self._ctypes_int(epoch_table[ctypes.c_int(0)]) & 1
        new_epoch = 1 - old_epoch
        epoch_table[ctypes.c_int(0)] = ctypes.c_uint32(new_epoch)
        if reenable:
            self.bpf["page_window_enabled"][ctypes.c_int(0)] = ctypes.c_uint32(1)
        time.sleep(PAGE_EPOCH_FLIP_GRACE_S)
        table = self.bpf[
            "page_window_epoch1" if old_epoch else "page_window_epoch0"
        ]
        rows: list[dict[str, int]] = []
        for key, value in list(table.items()):
            owner = self._owner_for_tag(self._ctypes_int(value.app_tag))
            if owner is not None and owner[0] == self.page_capture_target_app:
                device = self._device_fields(self._ctypes_int(key.device))
                rows.append({
                    **device,
                    "inode": self._ctypes_int(key.inode),
                    "page_index": self._ctypes_int(key.page_index),
                    "pfn": self._ctypes_int(value.pfn),
                    "source_mask": self._ctypes_int(value.source_mask),
                    "first_boot_ns": self._ctypes_int(value.first_boot_ns),
                    "last_boot_ns": self._ctypes_int(value.last_boot_ns),
                    "tgid": self._ctypes_int(value.tgid),
                    "tid": self._ctypes_int(value.tid),
                })
            try:
                del table[key]
            except KeyError:
                pass
        range_table = self.bpf[
            "page_window_ranges_epoch1"
            if old_epoch else "page_window_ranges_epoch0"
        ]
        identity_table = self.bpf["page_identity_pfns"]
        for key, value in list(range_table.items()):
            owner = self._owner_for_tag(self._ctypes_int(key.app_tag))
            first_index = self._ctypes_int(key.first_index)
            last_index = self._ctypes_int(key.last_index)
            page_count = (
                last_index - first_index + 1
                if last_index >= first_index else 0
            )
            if (
                owner is not None
                and owner[0] == self.page_capture_target_app
                and page_count > 0
            ):
                # 超大异常请求不能让 root helper 在一个边界无限扩张。
                # 数据仍保留前 65536 页，同时明确把窗口标为无效。
                if page_count > 65536 and self.page_capture is not None:
                    self.page_capture.note_quality_error(
                        f"DIRECT_RANGE_TOO_LARGE:{page_count}"
                    )
                device = self._device_fields(self._ctypes_int(key.device))
                first_pfn = self._ctypes_int(value.first_pfn)
                for page_index in range(
                    first_index,
                    first_index + min(page_count, 65536),
                ):
                    resolved_pfn = (
                        first_pfn + page_index - first_index
                        if first_pfn > 0 else 0
                    )
                    if resolved_pfn <= 0:
                        identity_key = identity_table.Key(
                            self._ctypes_int(key.device),
                            self._ctypes_int(key.inode),
                            page_index,
                        )
                        try:
                            resolved_pfn = self._ctypes_int(
                                identity_table[identity_key]
                            )
                        except KeyError:
                            resolved_pfn = 0
                    rows.append({
                        **device,
                        "inode": self._ctypes_int(key.inode),
                        "page_index": page_index,
                        "pfn": resolved_pfn,
                        "source_mask": self._ctypes_int(value.source_mask),
                        "first_boot_ns": self._ctypes_int(value.first_boot_ns),
                        "last_boot_ns": self._ctypes_int(value.last_boot_ns),
                        "tgid": self._ctypes_int(key.tgid),
                        "tid": self._ctypes_int(key.tid),
                    })
            try:
                del range_table[key]
            except KeyError:
                pass
        overflows_table = self.bpf["page_window_overflows"]
        overflow_key = ctypes.c_int(old_epoch)
        overflows = self._ctypes_int(overflows_table[overflow_key])
        overflows_table[overflow_key] = ctypes.c_uint64(0)
        dirty_table = self.bpf["page_mapping_dirty"]
        dirty: set[int] = set()
        for key in list(dirty_table.keys()):
            dirty.add(self._ctypes_int(key))
            try:
                del dirty_table[key]
            except KeyError:
                pass
        return rows, overflows, dirty

    def _tick_page_access_capture(self) -> None:
        capture = self.page_capture
        if capture is None or self.bpf is None or not capture.due():
            return
        try:
            rows, overflows, dirty = self._rotate_and_drain_page_maps()
            record = capture.rotate(
                rows,
                bpf_map_overflows=overflows,
                dirty_pids=dirty,
            )
            if not record.get("quality", {}).get("valid", False):
                self._send_status(
                    "PAGE_CAPTURE_WINDOW_INVALID",
                    ",".join(record.get("quality", {}).get("invalid_reasons", [])),
                )
        except Exception as exc:
            self._send_status("PAGE_CAPTURE_FAILED", str(exc))
            self._stop_page_access_capture(expected_capture_id=self.page_capture_id)

    def run(self) -> int:
        self._bind_control()
        try:
            self._load_bpf()
            self._send_status("READY", "eBPF file/cache/workload tracepoints attached")
            assert self.bpf is not None and self.control is not None
            self._next_heartbeat_monotonic = time.monotonic() + HEARTBEAT_INTERVAL_S
            while True:
                # 先处理已到期边界，再进入最长 100 ms 的 perf poll。正常负载下
                # 这把边界偏差限制在一次 poll 内；生命周期批量落盘则避免 WPS
                # 启动事件洪峰在回调中制造额外的秒级延迟。
                self._tick_page_access_capture()
                self.bpf.perf_buffer_poll(timeout=100)
                self._flush_events()
                self._flush_page_windows()
                self._tick_page_access_capture()
                readable, _, _ = select.select([self.control], [], [], 0)
                if readable:
                    self._drain_control()
                if time.monotonic() >= self._next_heartbeat_monotonic:
                    self._flush_events()
                    self._flush_page_windows()
                    self._tick_page_access_capture()
                    self._send_status()
                    self._next_heartbeat_monotonic = time.monotonic() + HEARTBEAT_INTERVAL_S
        finally:
            if self.page_capture is not None:
                self._stop_page_access_capture(
                    expected_capture_id=self.page_capture_id
                )
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
