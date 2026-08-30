"""Credential-checked client for the privileged proc-connector helper."""

from __future__ import annotations

import json
import os
import select
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 1
MAX_DATAGRAM_BYTES = 16 * 1024
UCRED = struct.Struct("=iii")


class GlobalProcessEventCollector:
    """Receive system-wide process events without granting monitor root access.

    The socket lives in the user's 0700 runtime directory.  Filesystem
    ownership alone is not trusted: every datagram must also carry
    ``SCM_CREDENTIALS`` with uid 0, proving that it came from the root helper.
    The callback only enqueues data; Runtime Monitor processes rows in its main
    thread so the shared CSV writer remains single-threaded.
    """

    def __init__(
        self,
        callback: Callable[[dict[str, Any]], None],
        *,
        socket_path: str | Path,
        expected_uid: int = 0,
    ) -> None:
        self.callback = callback
        self.socket_path = Path(socket_path)
        self.expected_uid = int(expected_uid)
        self._socket: socket.socket | None = None
        self._socket_inode = 0
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self.last_message_monotonic = 0.0
        self.rejected_datagrams = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._bind_socket()
        self._thread = threading.Thread(
            target=self._run,
            name="runtime-monitor-process-events",
            daemon=True,
        )
        self._thread.start()

    def wait_ready(self, timeout_s: float) -> bool:
        """Wait until one authenticated helper status/event has been received."""
        return self._ready.wait(max(0.0, float(timeout_s)))

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

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

    def _bind_socket(self) -> None:
        parent = self.socket_path.parent
        parent.mkdir(parents=False, exist_ok=True)
        parent_stat = parent.stat()
        if parent_stat.st_uid != os.getuid() or not stat.S_ISDIR(parent_stat.st_mode):
            raise PermissionError(f"unsafe process event socket parent: {parent}")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            existing = os.lstat(self.socket_path)
            if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                raise PermissionError(
                    f"refusing to replace unsafe process event socket: {self.socket_path}"
                )
            os.unlink(self.socket_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        listener.bind(str(self.socket_path))
        # socket 位于用户独占的 0700 /run/user/<uid> 中。末位 write 只用于让
        # 已剥离 CAP_DAC_OVERRIDE、仅保留 DAC_READ_SEARCH 的 root helper 发送；
        # 同 uid 进程即使能写也会因下游 SCM_CREDENTIALS uid != 0 被拒绝。
        os.chmod(self.socket_path, 0o602)
        self._socket_inode = os.lstat(self.socket_path).st_ino
        self._socket = listener

    def _unlink_own_socket(self) -> None:
        try:
            current = os.lstat(self.socket_path)
        except OSError:
            return
        if (
            stat.S_ISSOCK(current.st_mode)
            and current.st_uid == os.getuid()
            and current.st_ino == self._socket_inode
        ):
            os.unlink(self.socket_path)

    @staticmethod
    def _credentials(ancillary: list[tuple[int, int, bytes]]) -> tuple[int, int, int] | None:
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
            try:
                body, ancillary, flags, _address = self._socket.recvmsg(
                    MAX_DATAGRAM_BYTES,
                    socket.CMSG_SPACE(UCRED.size),
                )
            except OSError:
                if self._stop.is_set():
                    return
                continue
            credentials = self._credentials(ancillary)
            if credentials is None or credentials[1] != self.expected_uid:
                self.rejected_datagrams += 1
                continue
            if flags & socket.MSG_TRUNC:
                self.rejected_datagrams += 1
                continue
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.rejected_datagrams += 1
                continue
            if (
                not isinstance(payload, dict)
                or int(payload.get("protocol_version", 0) or 0) != PROTOCOL_VERSION
                or payload.get("source") != "proc-connector"
            ):
                self.rejected_datagrams += 1
                continue
            self.last_message_monotonic = time.monotonic()
            # STARTING 只证明 helper 进程存在，不能证明内核已经接受 proc
            # connector 订阅。只有内核 ACK 对应的 READY（或已经到达的真实进程
            # 事件）才能解除 monitor 的启动等待，避免把“有 helper、无事件源”
            # 误报为全系统覆盖已经就绪。
            if payload.get("kind") == "PROCESS_EVENT" or (
                payload.get("kind") == "SOURCE_STATUS"
                and payload.get("status") == "READY"
            ):
                self._ready.set()
            try:
                self.callback(payload)
            except Exception:
                # A malformed event must not kill the credential-checking loop.
                continue
