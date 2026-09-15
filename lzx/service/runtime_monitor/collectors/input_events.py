"""认证 root 输入 helper 的消息；复用进程事件通路的 Unix 凭据校验。"""
from __future__ import annotations

from .process_events import GlobalProcessEventCollector


INPUT_HANDLERS = {
    "MOUSE_CLICK": "mouseClick",
    "MOUSE_SCROLL": "mouseScroll",
    "MOUSE_MOVE": "mouseMove",
    "KEY_PRESS": "keyPress",
}


class InputEventCollector(GlobalProcessEventCollector):
    """只接收 source=libinput，仍逐包验证 SCM_CREDENTIALS 的 uid=0。

    监听线程仅向主线程交付最多 16 个事件的消息，不执行 hook、App 映射、
    LSTM 或日志写入。继承的是传输协议，不会订阅 proc connector。
    """

    SOURCE = "libinput"
    EVENT_KIND = "INPUT_EVENTS"
    THREAD_NAME = "runtime-monitor-input-events"
