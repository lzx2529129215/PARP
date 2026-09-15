#!/usr/bin/env python3
"""读取 libinput 的输入事件，批量投递给普通用户常驻服务（不保存输入文件）。

libinput 直接接收内核 evdev 和 udev 热插拔通知，适用于 X11 / Wayland。
只绑定 libinput >= 1.19 的稳定 C API；不运行 libinput debug-events 子进程。
参考：https://wayland.freedesktop.org/libinput/doc/latest/api/group__base.html
"""
from __future__ import annotations

import argparse
import ctypes as C
import errno
import json
import os
import select
import signal
import socket
import stat
import time
import uuid
from pathlib import Path
from typing import Any


OPEN = C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_int, C.c_void_p)
CLOSE = C.CFUNCTYPE(None, C.c_int, C.c_void_p)


class Interface(C.Structure):
    _fields_ = [("open_restricted", OPEN), ("close_restricted", CLOSE)]


def open_device(path: bytes, flags: int, _data: Any) -> int:
    """只允许打开 /dev/input/eventN；保持共享读取，不抓取或阻断用户输入。"""
    try:
        target = Path(os.fsdecode(path))
        if target.parent != Path("/dev/input") or not target.name.startswith("event"):
            return -errno.EACCES
        # libinput 默认要求 O_RDWR（供 compositor 更新键盘 LED 等）。本 helper
        # 只观察，不调用 LED/写输入接口，实际 fd 限定 O_RDONLY，才能与 systemd
        # DeviceAllow=char-input r 一致；不能为了监听放开设备注入权限。
        read_flags = (flags & ~os.O_ACCMODE) | os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
        return os.open(target, read_flags)
    except OSError as exc:
        return -int(exc.errno or errno.EIO)


class LibinputSource:
    """持久 C 上下文；每个 libinput 事件解码一次，设备热插拔由 udev 维护。

    相对位移是本上下文的 libinput 位移；绝对位置归一化至 [0,1]，不是屏幕
    像素。独立上下文不一定和 GNOME 的加速、自然滚动等用户设置一致。
    键盘只输出 Linux keycode 和 pressed/released，libinput 不产生桌面自动
    连发，也不转换文字、组合键文本、输入法内容或密码。
    """

    def __init__(self, seat: str = "seat0", *, device_path: str = "") -> None:
        self.li = C.CDLL("libinput.so.10", use_errno=True)
        self.udev = C.CDLL("libudev.so.1", use_errno=True)
        self._bind_api()
        # ctypes 回调必须保留强引用，否则 C 稍后调用时可能跳入已释放内存。
        self._open = OPEN(open_device)
        self._close = CLOSE(lambda fd, _data: os.close(fd))
        self.interface = Interface(self._open, self._close)
        self.context = None
        self.udev_context = None
        self.devices: dict[str, str] = {}
        self.buttons: set[tuple[str, int]] = set()
        self.keys: dict[tuple[str, int], int] = {}
        try:
            if device_path:  # 仅供隔离合成测试；生产使用整个 seat 的 udev 通知。
                self.context = self.li.libinput_path_create_context(C.byref(self.interface), None)
                if not self.context or not self.li.libinput_path_add_device(self.context, os.fsencode(device_path)):
                    raise RuntimeError(f"cannot attach libinput test device: {device_path}")
            else:
                self.udev_context = self.udev.udev_new()
                if not self.udev_context:
                    raise RuntimeError("udev_new failed")
                self.context = self.li.libinput_udev_create_context(C.byref(self.interface), None, self.udev_context)
                if not self.context or self.li.libinput_udev_assign_seat(self.context, seat.encode()) != 0:
                    raise RuntimeError(f"cannot attach libinput seat: {seat}")
        except Exception:
            self.close()
            raise

    def _bind_api(self) -> None:
        """显式声明指针/64 位时间/浮点返回类型，避免 ctypes 默认 int 截断。"""
        p, i, u, d = C.c_void_p, C.c_int, C.c_uint32, C.c_double
        signatures = {
            "udev_create_context": (p, [C.POINTER(Interface), p, p]),
            "udev_assign_seat": (i, [p, C.c_char_p]),
            "path_create_context": (p, [C.POINTER(Interface), p]),
            "path_add_device": (p, [p, C.c_char_p]),
            "unref": (p, [p]), "get_fd": (i, [p]), "dispatch": (i, [p]),
            "get_event": (p, [p]), "event_destroy": (None, [p]),
            "event_get_type": (i, [p]), "event_get_device": (p, [p]),
            "device_get_sysname": (C.c_char_p, [p]),
            "device_get_name": (C.c_char_p, [p]),
            "event_get_keyboard_event": (p, [p]),
            "event_get_pointer_event": (p, [p]),
            "event_get_gesture_event": (p, [p]),
            "event_keyboard_get_key": (u, [p]),
            "event_keyboard_get_key_state": (i, [p]),
            "event_pointer_get_button": (u, [p]),
            "event_pointer_get_button_state": (i, [p]),
            "event_pointer_has_axis": (i, [p, i]),
            "event_pointer_get_scroll_value": (d, [p, i]),
            "event_pointer_get_scroll_value_v120": (d, [p, i]),
            "event_gesture_get_finger_count": (i, [p]),
            "event_gesture_get_cancelled": (i, [p]),
        }
        for family in ("keyboard", "pointer", "gesture"):
            signatures[f"event_{family}_get_time_usec"] = (C.c_uint64, [p])
        for field in ("dx", "dy", "dx_unaccelerated", "dy_unaccelerated"):
            signatures[f"event_pointer_get_{field}"] = (d, [p])
        for axis in ("x", "y"):
            signatures[f"event_pointer_get_absolute_{axis}_transformed"] = (d, [p, u])
        for axis in ("dx", "dy"):
            signatures[f"event_gesture_get_{axis}"] = (d, [p])
        for name, (restype, argtypes) in signatures.items():
            fn = getattr(self.li, "libinput_" + name)
            fn.restype, fn.argtypes = restype, argtypes
        self.udev.udev_new.restype = p
        self.udev.udev_new.argtypes = []
        self.udev.udev_unref.restype = p
        self.udev.udev_unref.argtypes = [p]

    def fileno(self) -> int:
        return self.li.libinput_get_fd(self.context)

    def close(self) -> None:
        if self.context:
            self.li.libinput_unref(self.context)
            self.context = None
        if self.udev_context:
            self.udev.udev_unref(self.udev_context)
            self.udev_context = None

    def events(self):
        if self.li.libinput_dispatch(self.context) != 0:
            raise RuntimeError("libinput_dispatch failed")
        while event := self.li.libinput_get_event(self.context):
            try:
                decoded = self.decode(event)
                if decoded:
                    yield decoded
            finally:
                self.li.libinput_event_destroy(event)

    def decode(self, event: Any) -> dict[str, Any] | None:
        li = self.li
        kind = li.libinput_event_get_type(event)
        device = li.libinput_event_get_device(event)
        name = li.libinput_device_get_sysname(device).decode(errors="replace")
        if kind in (1, 2):
            if kind == 1:
                self.devices[name] = li.libinput_device_get_name(device).decode(errors="replace")
            else:
                self.devices.pop(name, None)
                self.buttons = {item for item in self.buttons if item[0] != name}
                self.keys = {key: value for key, value in self.keys.items() if key[0] != name}
            return None
        if kind == 300:
            native = li.libinput_event_get_keyboard_event(event)
            mono = li.libinput_event_keyboard_get_time_usec(native) * 1000
            code = li.libinput_event_keyboard_get_key(native)
            pressed = li.libinput_event_keyboard_get_key_state(native) == 1
            key = (name, code)
            fields = {"event_type": "KEY_PRESS", "key_code": code,
                      "state": "pressed" if pressed else "released"}
            if pressed:
                self.keys[key] = mono
            else:
                began = self.keys.pop(key, None)
                fields["held_ns"] = max(0, mono - began) if began is not None else None
        elif kind in (400, 401, 402, 404, 405, 406):
            native = li.libinput_event_get_pointer_event(event)
            mono = li.libinput_event_pointer_get_time_usec(native) * 1000
            if kind == 402:
                code = li.libinput_event_pointer_get_button(native)
                pressed = li.libinput_event_pointer_get_button_state(native) == 1
                if pressed:
                    self.buttons.add((name, code))
                else:
                    self.buttons.discard((name, code))
                fields = {"event_type": "MOUSE_CLICK", "button_code": code,
                          "button": {272: "left", 273: "right", 274: "middle", 275: "side", 276: "extra"}.get(code, str(code)),
                          "state": "pressed" if pressed else "released"}
            elif kind in (400, 401):
                fields = {"event_type": "MOUSE_MOVE", "motion_kind": "relative" if kind == 400 else "absolute"}
                if kind == 400:
                    for axis in ("dx", "dy", "dx_unaccelerated", "dy_unaccelerated"):
                        fields[axis] = getattr(li, "libinput_event_pointer_get_" + axis)(native)
                else:
                    fields["x_normalized"] = li.libinput_event_pointer_get_absolute_x_transformed(native, 1)
                    fields["y_normalized"] = li.libinput_event_pointer_get_absolute_y_transformed(native, 1)
                fields["buttons_down"] = sorted({code for _, code in self.buttons})
                fields["dragging"] = bool(self.buttons)
            else:
                fields = {"event_type": "MOUSE_SCROLL", "scroll_source": {404: "wheel", 405: "finger", 406: "continuous"}[kind]}
                for axis, axis_name in ((0, "vertical"), (1, "horizontal")):
                    if li.libinput_event_pointer_has_axis(native, axis):
                        fields[axis_name] = li.libinput_event_pointer_get_scroll_value(native, axis)
                        if kind == 404:
                            fields[axis_name + "_v120"] = li.libinput_event_pointer_get_scroll_value_v120(native, axis)
        elif kind in (800, 801, 802):
            native = li.libinput_event_get_gesture_event(event)
            mono = li.libinput_event_gesture_get_time_usec(native) * 1000
            fields = {"event_type": "MOUSE_MOVE", "motion_kind": "swipe",
                      "phase": {800: "begin", 801: "update", 802: "end"}[kind],
                      "fingers": li.libinput_event_gesture_get_finger_count(native)}
            if kind == 801:
                fields.update(dx=li.libinput_event_gesture_get_dx(native), dy=li.libinput_event_gesture_get_dy(native))
            if kind == 802:
                fields["cancelled"] = bool(li.libinput_event_gesture_get_cancelled(native))
        else:
            # 403 是旧 POINTER_AXIS；libinput >=1.19 同时发送新旧滚动通知，
            # 只消费 404/405/406，避免一格滚轮触发两次 mouseScroll。
            return None
        return {"source": "libinput", "device": name, "monotonic_ns": mono,
                "timestamp_ns": mono + (time.time_ns() - time.monotonic_ns()), **fields}


class InputPublisher:
    """最多 16 条事件一包；非阻塞发送，积压时计数并在状态消息中报告。"""

    def __init__(self, uid: int, socket_path: Path) -> None:
        if uid <= 0 or socket_path.parent != Path(f"/run/user/{uid}"):
            raise ValueError("input socket must be directly inside target user's runtime directory")
        self.uid, self.path = uid, socket_path
        self.instance = uuid.uuid4().hex
        self.sequence = 0
        self.delivery_drops = 0
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

    def send(self, payload: dict[str, Any]) -> None:
        events = payload.get("events", [])
        for event in events:
            self.sequence += 1
            event["source_seq"] = self.sequence
        envelope = {"protocol_version": 1, "source": "libinput", "source_instance_id": self.instance,
                    "source_seq": self.sequence, "delivery_drops": self.delivery_drops,
                    "timestamp_ns": time.time_ns(), **payload}
        try:
            parent = self.path.parent.stat()
            target = self.path.lstat()
            if parent.st_uid != self.uid or parent.st_mode & 0o077 or not stat.S_ISDIR(parent.st_mode):
                raise PermissionError("unsafe input socket directory")
            if not stat.S_ISSOCK(target.st_mode) or target.st_uid != self.uid:
                raise PermissionError("unsafe input socket")
            body = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode()
            if len(body) > 16 * 1024:
                raise ValueError("input batch exceeds protocol limit")
            self.sock.sendto(body, str(self.path))
        except (OSError, ValueError):
            self.delivery_drops += len(events)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-uid", required=True, type=int)
    parser.add_argument("--seat", default="seat0")
    parser.add_argument("--socket-path", default="")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error("requires root to read seat input devices")
    output = InputPublisher(args.target_uid, Path(args.socket_path or f"/run/user/{args.target_uid}/parp-input-events.sock"))
    source = LibinputSource(args.seat)
    wake_r, wake_w = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    previous_wakeup = signal.set_wakeup_fd(wake_w)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    next_status = 0.0
    try:
        while not stopping:
            batch: list[dict[str, Any]] = []
            for event in source.events():
                batch.append(event)
                if len(batch) == 16:
                    output.send({"kind": "INPUT_EVENTS", "events": batch})
                    batch = []
            if batch:
                output.send({"kind": "INPUT_EVENTS", "events": batch})
            now = time.monotonic()
            if now >= next_status:
                output.send({"kind": "SOURCE_STATUS", "status": "READY" if source.devices else "NO_DEVICES",
                             "devices": source.devices, "seat": args.seat, "helper_pid": os.getpid()})
                next_status = now + 2.0
            # fd 就绪才 dispatch；两秒超时只发健康心跳，不查询鼠标/键盘状态。
            select.select([source.fileno(), wake_r], [], [], max(0.0, next_status - time.monotonic()))
    finally:
        signal.set_wakeup_fd(previous_wakeup)
        source.close()
        output.sock.close()
        os.close(wake_r)
        os.close(wake_w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
