from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

from .models import IdentityConfidence, MappingType, VMARecord


@dataclass(frozen=True)
class StableRegion:
    stable_key: str
    canonical_key: str
    region_type: str
    identity_confidence: str
    path_metadata: dict[str, str]
    process_role: str


def vma_size_bucket(size_bytes: int) -> str:
    if size_bytes <= 0:
        return "0"
    return f"2^{int(math.floor(math.log2(size_bytes)))}"


def stable_region_for_address(app_id: str, vma: VMARecord, address: int, bucket_bytes: int) -> StableRegion:
    mapping_type = MappingType(vma.mapping_type)
    is_file = mapping_type in {
        MappingType.FILE,
        MappingType.SHARED_LIBRARY,
        MappingType.DOCUMENT_FILE,
        MappingType.GRAPHICS_DEVICE,
        MappingType.OTHER_DEVICE,
    } and vma.inode != 0
    if is_file:
        file_offset = vma.file_offset + max(0, address - vma.start_addr)
        bucket = file_offset // bucket_bytes
        role_key = {
            "kind": "file_role",
            "app_id": str(app_id),
            "process_role": vma.process_role,
            "dev_major": vma.dev_major,
            "dev_minor": vma.dev_minor,
            "inode": vma.inode,
            "file_offset_bucket": bucket,
            "permissions": vma.permissions,
        }
        canonical_key = _stable_json({
            "kind": "file_canonical",
            "dev_major": vma.dev_major,
            "dev_minor": vma.dev_minor,
            "inode": vma.inode,
            "file_offset_bucket": bucket,
        })
        confidence = IdentityConfidence.HIGH.value
    else:
        relative_offset = max(0, address - vma.start_addr)
        relative_bucket = relative_offset // bucket_bytes
        anon_type = mapping_type.value
        role_key = {
            "kind": "anon_role",
            "app_id": str(app_id),
            "process_role": vma.process_role,
            "anon_type": anon_type,
            "anon_name": vma.anon_name,
            "permissions": vma.permissions,
            "vma_size_bucket": vma_size_bucket(vma.size_bytes),
            "relative_offset_bucket": relative_bucket,
        }
        canonical_key = _stable_json(role_key)
        if mapping_type == MappingType.NAMED_ANON and vma.anon_name:
            confidence = IdentityConfidence.HIGH.value
        elif mapping_type in {MappingType.ANON_HEAP, MappingType.ANON_STACK, MappingType.GRAPHICS_DEVICE}:
            confidence = IdentityConfidence.MEDIUM.value
        else:
            confidence = IdentityConfidence.LOW.value
    stable_key = _stable_json(role_key)
    return StableRegion(
        stable_key=stable_key,
        canonical_key=canonical_key,
        region_type=mapping_type.value,
        identity_confidence=confidence,
        path_metadata={
            "pathname": vma.pathname,
            "basename": vma.pathname.rsplit("/", 1)[-1] if vma.pathname else "",
            "anon_name": vma.anon_name,
        },
        process_role=vma.process_role,
    )


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _stable_json(data: dict[str, object]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

