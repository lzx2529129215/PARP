"""App Bind 表的用户态参考实现，用于离线回放和内核语义测试。

该模块不访问 debugfs，也不驱动内核。它严格镜像 Bindfix 内核的固定槽位
upsert 顺序，便于在安装前验证真实写入日志不会触发 ENOSPC。
"""

from __future__ import annotations

from dataclasses import dataclass


JIFFIES_MASK = (1 << 32) - 1
JIFFIES_HALF_RANGE = 1 << 31


@dataclass
class AppBinding:
    app_id: int
    cgroup_id: int
    ttl_ms: int
    updated_at_ms: int


@dataclass
class BindStats:
    write_calls: int = 0
    insert: int = 0
    refresh: int = 0
    replace_cgroup: int = 0
    replace_app: int = 0
    expired_reuse: int = 0
    enospc: int = 0
    invalid: int = 0
    high_watermark: int = 0


def jiffies_after(now_ms: int, expires_ms: int) -> bool:
    """模拟内核 ``time_after(now, expires)`` 的 32 位 wrap-safe 语义。"""
    delta = (int(now_ms) - int(expires_ms)) & JIFFIES_MASK
    return delta != 0 and delta < JIFFIES_HALF_RANGE


def binding_expired(binding: AppBinding, now_ms: int) -> bool:
    if binding.ttl_ms <= 0:
        return True
    expires_ms = (binding.updated_at_ms + binding.ttl_ms) & JIFFIES_MASK
    return jiffies_after(now_ms & JIFFIES_MASK, expires_ms)


class AppBindTable:
    """固定容量、无动态分配的 App/cgroup 绑定表参考模型。"""

    def __init__(self, capacity: int = 32) -> None:
        if capacity <= 0:
            raise ValueError("capacity 必须大于 0")
        self.capacity = int(capacity)
        self.slots: list[AppBinding | None] = [None] * self.capacity
        self.stats = BindStats()

    def active_entries(self, now_ms: int) -> int:
        return sum(
            binding is not None and not binding_expired(binding, now_ms)
            for binding in self.slots
        )

    def expired_entries(self, now_ms: int) -> int:
        return sum(
            binding is not None and binding_expired(binding, now_ms)
            for binding in self.slots
        )

    def clear(self) -> None:
        self.slots = [None] * self.capacity

    def upsert(self, app_id: int, cgroup_id: int, ttl_ms: int, now_ms: int) -> str:
        """写入一个绑定，返回与内核统计一致的操作类别。"""
        if int(app_id) <= 0 or int(cgroup_id) <= 0 or int(ttl_ms) <= 0:
            self.stats.invalid += 1
            return "invalid"

        app_id, cgroup_id, ttl_ms = int(app_id), int(cgroup_id), int(ttl_ms)
        now_ms = int(now_ms) & JIFFIES_MASK
        self.stats.write_calls += 1
        free_slot: int | None = None
        expired_slot: int | None = None
        same_cgroup_slot: int | None = None
        same_app_slot: int | None = None

        for index, binding in enumerate(self.slots):
            if binding is None:
                free_slot = index if free_slot is None else free_slot
                continue
            if binding.app_id == app_id and binding.cgroup_id == cgroup_id:
                self.slots[index] = AppBinding(app_id, cgroup_id, ttl_ms, now_ms)
                self.stats.refresh += 1
                return "refresh"
            if binding.cgroup_id == cgroup_id and same_cgroup_slot is None:
                same_cgroup_slot = index
            if binding.app_id == app_id and same_app_slot is None:
                same_app_slot = index
            if binding_expired(binding, now_ms) and expired_slot is None:
                expired_slot = index

        if same_cgroup_slot is not None:
            slot, action = same_cgroup_slot, "replace_cgroup"
            self.stats.replace_cgroup += 1
        elif same_app_slot is not None:
            slot, action = same_app_slot, "replace_app"
            self.stats.replace_app += 1
        elif expired_slot is not None:
            slot, action = expired_slot, "expired_reuse"
            self.stats.expired_reuse += 1
        elif free_slot is not None:
            slot, action = free_slot, "insert"
            self.stats.insert += 1
        else:
            self.stats.enospc += 1
            return "enospc"

        self.slots[slot] = AppBinding(app_id, cgroup_id, ttl_ms, now_ms)
        self.stats.high_watermark = max(self.stats.high_watermark, self.active_entries(now_ms))
        return action
