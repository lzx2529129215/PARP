from __future__ import annotations

from pathlib import Path

from .models import MappingType, VMARecord


def parse_maps_line(line: str, *, pid: int, process_starttime: int, process_role: str) -> VMARecord:
    parts = line.rstrip("\n").split(None, 5)
    if len(parts) < 5:
        raise ValueError(f"invalid maps line: {line!r}")
    addr, perms, offset_hex, dev, inode_text = parts[:5]
    pathname = parts[5] if len(parts) > 5 else ""
    start_text, end_text = addr.split("-", 1)
    dev_major_text, dev_minor_text = dev.split(":", 1)
    start = int(start_text, 16)
    end = int(end_text, 16)
    inode = int(inode_text)
    anon_name = _anon_name(pathname)
    mapping_type = classify_mapping(pathname, perms, inode).value
    return VMARecord(
        pid=pid,
        process_starttime=process_starttime,
        process_role=process_role,
        start_addr=start,
        end_addr=end,
        size_bytes=max(0, end - start),
        permissions=perms,
        file_offset=int(offset_hex, 16),
        dev_major=int(dev_major_text, 16),
        dev_minor=int(dev_minor_text, 16),
        inode=inode,
        pathname=pathname,
        anon_name=anon_name,
        mapping_type=mapping_type,
    )


def parse_maps_text(text: str, *, pid: int, process_starttime: int, process_role: str) -> list[VMARecord]:
    records: list[VMARecord] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        records.append(parse_maps_line(line, pid=pid, process_starttime=process_starttime, process_role=process_role))
    return records


def read_proc_maps(pid: int, process_starttime: int, process_role: str) -> tuple[list[VMARecord], str]:
    path = Path("/proc") / str(pid) / "maps"
    try:
        return parse_maps_text(path.read_text(encoding="utf-8", errors="replace"), pid=pid, process_starttime=process_starttime, process_role=process_role), ""
    except (OSError, ValueError) as exc:
        return [], str(exc)


def classify_mapping(pathname: str, permissions: str, inode: int) -> MappingType:
    path = pathname.removesuffix(" (deleted)")
    lower = path.lower()
    if path == "[heap]":
        return MappingType.ANON_HEAP
    if path.startswith("[stack"):
        return MappingType.ANON_STACK
    if path.startswith("[anon:") or path.startswith("[anon_shmem:"):
        return MappingType.NAMED_ANON
    if not path:
        return MappingType.UNKNOWN_ANON if inode == 0 else MappingType.FILE
    if path.startswith("["):
        return MappingType.NAMED_ANON
    if "/dev/dri" in lower or "gpu" in lower or "nvidia" in lower:
        return MappingType.GRAPHICS_DEVICE
    if path.startswith("/dev/"):
        return MappingType.OTHER_DEVICE
    suffixes = (".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".pdf", ".txt", ".jpg", ".jpeg", ".png")
    if lower.endswith(suffixes):
        return MappingType.DOCUMENT_FILE
    if ".so" in lower or "/lib" in lower:
        return MappingType.SHARED_LIBRARY
    if inode != 0:
        return MappingType.FILE
    return MappingType.UNKNOWN_ANON


def _anon_name(pathname: str) -> str:
    if pathname.startswith("[") and pathname.endswith("]"):
        return pathname[1:-1]
    return ""

