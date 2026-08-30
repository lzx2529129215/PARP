"""Bounded, per-cgroup workload profiles for PARP reclaim.  lzx-note"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


WORKLOAD_NONE = 0
WORKLOAD_ANON_HEAVY = 1
WORKLOAD_FILE_CLEAN = 2
WORKLOAD_FILE_DIRTY = 3
WORKLOAD_MIXED = 4

WORKLOAD_NAMES = {  # lzx-note
    WORKLOAD_NONE: "NONE",
    WORKLOAD_ANON_HEAVY: "ANON_HEAVY",
    WORKLOAD_FILE_CLEAN: "FILE_CLEAN",
    WORKLOAD_FILE_DIRTY: "FILE_DIRTY",
    WORKLOAD_MIXED: "MIXED",
}

MIN_MANAGED_BYTES = 8 * 1024 * 1024
MIN_DIRTY_BYTES = 8 * 1024 * 1024
MIN_CONFIDENCE_Q8 = 128  # A true 50/50 mixed cgroup remains a valid native-balance hint. lzx-note


@dataclass(frozen=True)
class ReclaimWorkloadProfile:
    """A cgroup-local composition decision carried in the next myfs batch.  lzx-note"""

    domain_id: int = 0
    workload_class: int = WORKLOAD_NONE
    swappiness: int = 0
    confidence_q8: int = 0
    allow_writepage: bool = False
    anon_bytes: int = 0
    file_bytes: int = 0
    file_dirty_bytes: int = 0
    reason: str = "unavailable"

    @property
    def valid(self) -> bool:
        return (
            self.domain_id > 0
            and self.workload_class in {
                WORKLOAD_ANON_HEAVY,
                WORKLOAD_FILE_CLEAN,
                WORKLOAD_FILE_DIRTY,
                WORKLOAD_MIXED,
            }
            and 0 <= self.swappiness <= 200
            and self.confidence_q8 >= MIN_CONFIDENCE_Q8
        )

    def workload_hint(self) -> int:
        """Match include/uapi/linux/parp_predict.h exactly.  lzx-note"""
        if not self.valid:
            return 0
        value = int(self.workload_class & 0x0F)
        value |= int(self.swappiness & 0xFF) << 8
        value |= int(self.confidence_q8 & 0xFF) << 16
        if self.allow_writepage:
            value |= 1 << 24
        return value


def _read_memory_stat(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in (path / "memory.stat").read_text(encoding="utf-8").splitlines():
            name, value = line.split(maxsplit=1)
            values[name] = int(value)
    except (OSError, ValueError):
        return {}
    return values


def classify_memory_stat(domain_id: int, values: dict[str, int]) -> ReclaimWorkloadProfile:
    """按驻留内存组成分类，而不是按瞬时 fault 活动分类。

    分类结果编码进 v3 binding 的 workload_hint：类别决定建议 swappiness，只有明确
    的脏文件负载才允许 writepage。小于 8 MiB 或置信度不足的 cgroup 返回无效
    profile，bridge 仍提交 active binding，但不会设置 WORKLOAD_VALID。
    """
    anon = max(0, int(values.get("anon", 0)))
    file_bytes = max(0, int(values.get("file", 0)))
    dirty = min(file_bytes, max(0, int(values.get("file_dirty", 0))))
    total = anon + file_bytes
    if domain_id <= 0 or total < MIN_MANAGED_BYTES:
        return ReclaimWorkloadProfile(
            domain_id=domain_id, anon_bytes=anon, file_bytes=file_bytes,
            file_dirty_bytes=dirty, reason="insufficient_managed_memory",
        )

    anon_ratio = anon / total
    file_ratio = file_bytes / total
    dirty_ratio = dirty / file_bytes if file_bytes else 0.0
    # FILE_DIRTY 优先判断，因为“文件占比不高但脏页已经很多”的混合 cgroup 仍需
    # 更保守的 swappiness 和受控 writepage；之后才按 65% 阈值区分 anon/file。
    if dirty >= MIN_DIRTY_BYTES and dirty_ratio >= 0.25:
        # Crossing the explicit dirty-byte and dirty-ratio gates is already a
        # decisive classification.  A cgroup whose anon/file composition is
        # almost exactly balanced must not encode FILE_DIRTY with confidence
        # 127 and then invalidate its own hint at the generic 128 boundary.
        # lzx-note
        confidence = max(0.5, dirty_ratio, file_ratio)
        workload_class, swappiness, allow_writepage, reason = (
            WORKLOAD_FILE_DIRTY, 20, True,
            f"file_dirty={dirty};file={file_bytes};dirty_ratio={dirty_ratio:.3f}",
        )
    elif anon_ratio >= 0.65:
        confidence = anon_ratio
        workload_class, swappiness, allow_writepage, reason = (
            WORKLOAD_ANON_HEAVY, 140, False,
            f"anon={anon};file={file_bytes};anon_ratio={anon_ratio:.3f}",
        )
    elif file_ratio >= 0.65:
        confidence = file_ratio
        workload_class, swappiness, allow_writepage, reason = (
            WORKLOAD_FILE_CLEAN, 40, False,
            f"anon={anon};file={file_bytes};file_ratio={file_ratio:.3f}",
        )
    else:
        confidence = max(anon_ratio, file_ratio)
        workload_class, swappiness, allow_writepage, reason = (
            WORKLOAD_MIXED, 60, False,
            f"anon={anon};file={file_bytes};mixed",)
    return ReclaimWorkloadProfile(
        domain_id=domain_id,
        workload_class=workload_class,
        swappiness=swappiness,
        confidence_q8=max(1, min(255, round(confidence * 255))),
        allow_writepage=allow_writepage,
        anon_bytes=anon,
        file_bytes=file_bytes,
        file_dirty_bytes=dirty,
        reason=reason,
    )


class CgroupReclaimWorkloadProfiler:
    """在 LSTM 事件时刻只读每个实时 binding 的 memory.stat 并分类。"""

    def sample(
        self, binding_paths: dict[int, tuple[int, str, Path]],
    ) -> dict[int, ReclaimWorkloadProfile]:
        # 不缓存 profile：anon/file/dirty 组成可能快速变化，必须与本次预测/binding
        # 使用同一事件时刻的视图；读取失败会自然得到 invalid profile。
        profiles: dict[int, ReclaimWorkloadProfile] = {}
        for domain_id, _binding in binding_paths.items():
            path = _binding[2]
            profiles[domain_id] = classify_memory_stat(domain_id, _read_memory_stat(path))
        return profiles


def profile_summary(profiles: dict[int, ReclaimWorkloadProfile]) -> dict[str, Any]:
    """Small audit payload; it intentionally contains no per-page data.  lzx-note"""
    result: dict[str, Any] = {"total": len(profiles), "valid": 0, "classes": {}}
    for profile in profiles.values():
        name = WORKLOAD_NAMES.get(profile.workload_class, "UNKNOWN")
        result["classes"][name] = int(result["classes"].get(name, 0)) + 1
        if profile.valid:
            result["valid"] += 1
    return result
