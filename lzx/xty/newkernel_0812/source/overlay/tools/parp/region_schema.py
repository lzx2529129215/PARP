#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Neutral Phase-2 region schema and synthetic VMA alignment reference."""

from dataclasses import dataclass, asdict
import hashlib
from typing import Dict, Iterable, List, Optional, Tuple

PAGE_SIZE = 4096
MAX_SPLITS = 64
Q15_ONE = 32767


@dataclass(frozen=True)
class VMA:
    start: int
    end: int
    kind: str
    flags: int = 0
    vm_pgoff: int = 0
    dev_major: int = 0
    dev_minor: int = 0
    inode: int = 0
    file_version: int = 0
    backing_class: str = "UNKNOWN"
    vma_generation: int = 1
    name_hash: int = 0


def align_interval(start: int, end: int,
                   page_size: int = PAGE_SIZE) -> Tuple[int, int]:
    if page_size <= 0 or page_size & (page_size - 1) or start < 0 or start >= end:
        raise ValueError("invalid interval")
    aligned_start = start & ~(page_size - 1)
    aligned_end = (end + page_size - 1) & ~(page_size - 1)
    if aligned_end <= aligned_start:
        raise ValueError("interval overflow")
    return aligned_start, aligned_end


def file_page_range(vma: VMA, start: int, end: int) -> Tuple[int, int]:
    if start < vma.start or start >= end or start % PAGE_SIZE or end % PAGE_SIZE:
        raise ValueError("invalid file segment")
    first = vma.vm_pgoff + (start - vma.start) // PAGE_SIZE
    pages = (end - start) // PAGE_SIZE
    if pages <= 0:
        raise ValueError("empty file segment")
    return first, pages


def anon_page_range(vma: VMA, start: int, end: int) -> Tuple[int, int]:
    if start < vma.start or start >= end or start % PAGE_SIZE or end % PAGE_SIZE:
        raise ValueError("invalid anonymous segment")
    return (start - vma.start) // PAGE_SIZE, (end - start) // PAGE_SIZE


def vma_signature(vma: VMA, process_role: int = 0) -> int:
    length_bin = max(1, (vma.end - vma.start) // PAGE_SIZE).bit_length() - 1
    payload = f"{vma.kind}:{vma.flags}:{length_bin}:{process_role}:{vma.name_hash}"
    return int.from_bytes(hashlib.blake2b(payload.encode(), digest_size=8).digest(),
                          "little") or 1


def split_region(start: int, end: int, vmas: Iterable[VMA],
                 max_splits: int = MAX_SPLITS) -> List[Dict]:
    """Split a page-aligned range at every VMA boundary and preserve length."""
    start, end = align_interval(start, end)
    ordered = sorted(vmas, key=lambda item: item.start)
    cursor = start
    output: List[Dict] = []
    while cursor < end and len(output) < max_splits:
        vma: Optional[VMA] = next((item for item in ordered if item.end > cursor), None)
        if vma is None:
            output.append({"start": cursor, "end": end,
                           "region_type": "UNRESOLVED", "reason": "VMA_HOLE"})
            cursor = end
            break
        if cursor < vma.start:
            segment_end = min(end, vma.start)
            output.append({"start": cursor, "end": segment_end,
                           "region_type": "UNRESOLVED", "reason": "VMA_HOLE"})
            cursor = segment_end
            continue
        segment_end = min(end, vma.end)
        output.append(classify_segment(vma, cursor, segment_end))
        cursor = segment_end
    if cursor < end:
        output.append({"start": cursor, "end": end,
                       "region_type": "UNRESOLVED", "reason": "SPLIT_LIMIT",
                       "truncated": True})
    if sum(item["end"] - item["start"] for item in output) != end - start:
        raise AssertionError("split length conservation violated")
    for previous, current in zip(output, output[1:]):
        if previous["end"] != current["start"]:
            raise AssertionError("split overlap or gap accounting bug")
    return output


def classify_segment(vma: VMA, start: int, end: int) -> Dict:
    base = {"start": start, "end": end,
            "alignment_status": "EXACT", "alignment_confidence": Q15_ONE}
    if vma.kind == "file":
        first, pages = file_page_range(vma, start, end)
        persistence_safe = vma.backing_class not in {
            "SHMEM", "TMPFS", "DELETED_FILE", "SPECIAL", "UNKNOWN"
        } and vma.file_version != 0
        base.update({
            "region_type": "FILE", "start_index": first, "nr_pages": pages,
            "dev_major": vma.dev_major, "dev_minor": vma.dev_minor,
            "inode": vma.inode, "file_version": vma.file_version,
            "file_version_source": "IVERSION" if vma.file_version else "UNKNOWN",
            "backing_class": vma.backing_class,
            "persistence_safe": persistence_safe,
        })
    elif vma.kind in {"anon", "heap", "stack", "anon_shared"}:
        relative, pages = anon_page_range(vma, start, end)
        classes = {"anon": "ANON_PRIVATE", "heap": "HEAP", "stack": "STACK",
                   "anon_shared": "ANON_SHARED"}
        base.update({
            "region_type": "ANON", "relative_start_pages": relative,
            "nr_pages": pages, "anon_class": classes[vma.kind],
            "vma_signature": vma_signature(vma), "session_identity_only": True,
            "vma_generation": vma.vma_generation,
        })
    elif vma.kind == "special":
        base.update({"region_type": "SPECIAL", "reason": "SPECIAL_MAPPING"})
    else:
        base.update({"region_type": "UNRESOLVED", "reason": "UNKNOWN_MAPPING"})
    return base


def public_record(record: Dict, synthetic: bool = False) -> Dict:
    """Remove raw virtual addresses from non-synthetic exported datasets."""
    result = dict(record)
    if not synthetic and "region_start" in result and "region_end" in result:
        length = result["region_end"] - result["region_start"]
        result["region_start"] = 0
        result["region_end"] = length
        result["address_redacted"] = True
    result.pop("path", None)
    return result


def align_app_context(tasks: Iterable[Dict], mm_cookie: int,
                      timestamp_ns: int) -> Dict:
    """Reject stale or conflicting observation owners; never guess."""
    owners = [task for task in tasks if task.get("mm_cookie") == mm_cookie and
              task.get("alive", True)]
    if not owners:
        return {"alignment_status": "STALE", "reason": "TARGET_OR_MM_GONE"}
    valid = [task for task in owners
             if task.get("bind_expiry_ns", 0) > timestamp_ns and
             task.get("app_prior_expiry_ns", 0) > timestamp_ns and
             task.get("domain_online", True)]
    if len(valid) != len(owners):
        return {"alignment_status": "UNRESOLVED", "reason": "EXPIRED_OR_OFFLINE"}
    identities = {(task["domain_id"], task["app_id"], task["bind_generation"],
                   task["foreground_epoch_id"]) for task in valid}
    if len(identities) != 1:
        return {"alignment_status": "AMBIGUOUS",
                "reason": "SHARED_MM_APPBIND_CONFLICT"}
    domain_id, app_id, generation, epoch = identities.pop()
    return {"alignment_status": "EXACT", "domain_id": domain_id,
            "app_id": app_id, "bind_generation": generation,
            "foreground_epoch_id": epoch,
            "owner_source": "OBSERVATION_OWNER_TASK_MEMCG",
            "owner_confidence": Q15_ONE}


def as_json_dict(value) -> Dict:
    return asdict(value)
