"""WPS 文件页访问窗口的 Page Idle 采集与无损落盘。

本模块由 root eBPF helper 调用。它只观测目标 App 已知的文件页：通过
``/proc/<pid>/maps`` + ``pagemap`` 建立文件页到 PFN 的映射，通过
``/sys/kernel/mm/page_idle/bitmap`` 在固定窗口边界读取并重新设置 Idle 位。
它不写 ``clear_refs``，不执行回收、预取、训练或内存压力控制。

高频直接访问先在 eBPF 双缓冲 map 中去重；helper 每个窗口只把去重后的集合
传给本模块一次。Page Idle 负责补上 mmap 后不再经过 syscall 的 CPU 访问。
"""

from __future__ import annotations

import csv
import ctypes
import fcntl
import hashlib
import json
import mmap
import os
import re
import struct
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator, TextIO


SCHEMA_VERSION = 1
WINDOW_MAGIC = b"PARPPGW1"
LIFECYCLE_MAGIC = b"PARPPGL1"
FILE_HEADER = struct.Struct("<8sHHII")
FRAME_HEADER = struct.Struct("<II")
PAGEMAP_ENTRY = struct.Struct("<Q")
U64 = struct.Struct("=Q")
PAGEMAP_PRESENT = 1 << 63
PAGEMAP_PFN_MASK = (1 << 55) - 1
KPF_LRU = 1 << 5
KPF_ANON = 1 << 12
LIFECYCLE_BATCH_MAX = 4096
VMA_REFRESH_BUDGET_NS = 25_000_000

# source_mask：一页可以同时由多个来源确认，所以使用位集合而不是单值。
SOURCE_PAGE_IDLE_CLEARED = 1 << 0
SOURCE_DIRECT_WPS_ACCESS = 1 << 1
SOURCE_FILE_FAULT = 1 << 2
SOURCE_NEW_RESIDENT = 1 << 3
SOURCE_EVICT_BEFORE_SAMPLE = 1 << 4
SOURCE_PFN_CHANGED = 1 << 5

# attribution_mask：共享页仍纳入数据，但不能伪装成 WPS 独占访问。
ATTR_DIRECT_WPS = 1 << 0
ATTR_WPS_MAPPED_UNIQUE = 1 << 1
ATTR_WPS_MAPPED_SHARED = 1 << 2
ATTR_MEMCG_CHARGED = 1 << 3
ATTR_UNRESOLVED = 1 << 4

SOURCE_NAMES = {
    SOURCE_PAGE_IDLE_CLEARED: "PAGE_IDLE_CLEARED",
    SOURCE_DIRECT_WPS_ACCESS: "DIRECT_WPS_ACCESS",
    SOURCE_FILE_FAULT: "FILE_FAULT",
    SOURCE_NEW_RESIDENT: "NEW_RESIDENT",
    SOURCE_EVICT_BEFORE_SAMPLE: "EVICT_BEFORE_SAMPLE",
    SOURCE_PFN_CHANGED: "PFN_CHANGED",
}
ATTRIBUTION_NAMES = {
    ATTR_DIRECT_WPS: "DIRECT_WPS",
    ATTR_WPS_MAPPED_UNIQUE: "WPS_MAPPED_UNIQUE",
    ATTR_WPS_MAPPED_SHARED: "WPS_MAPPED_SHARED",
    ATTR_MEMCG_CHARGED: "MEMCG_CHARGED",
    ATTR_UNRESOLVED: "UNRESOLVED",
}

_MAPS_RE = re.compile(
    r"^(?P<start>[0-9a-fA-F]+)-(?P<end>[0-9a-fA-F]+)\s+"
    r"(?P<perms>\S+)\s+(?P<offset>[0-9a-fA-F]+)\s+"
    r"(?P<major>[0-9a-fA-F]+):(?P<minor>[0-9a-fA-F]+)\s+"
    r"(?P<inode>\d+)(?:\s+(?P<path>.*))?$"
)


def _mask_names(value: int, names: dict[int, str]) -> list[str]:
    return [name for bit, name in names.items() if int(value) & bit]


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _kernel_release() -> str:
    try:
        return os.uname().release
    except OSError:
        return ""


def _normalize_path(path: str) -> str:
    value = str(path or "").removesuffix(" (deleted)")
    if not value.startswith("/"):
        return ""
    try:
        return os.path.realpath(value)
    except OSError:
        return os.path.normpath(value)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()


@dataclass(frozen=True, order=True)
class FilePageKey:
    device_major: int
    device_minor: int
    inode: int
    page_index: int


@dataclass
class RegisteredPage:
    key: FilePageKey
    pfn: int
    last_known_pfn: int = 0
    path: str = ""
    mapped_pids: set[int] = field(default_factory=set)
    first_seen_boot_ns: int = 0
    last_seen_boot_ns: int = 0


@dataclass
class AccessObservation:
    key: FilePageKey
    pfn: int
    source_mask: int
    attribution_mask: int
    first_boot_ns: int
    last_boot_ns: int
    tgid: int = 0
    tid: int = 0


class FramedBinaryWriter:
    """长度分帧、CRC32 校验、zlib 无损压缩的版本化二进制写入器。"""

    def __init__(self, fd: int, *, magic: bytes, metadata: dict[str, Any]) -> None:
        if len(magic) != 8:
            raise ValueError("binary magic must be exactly eight bytes")
        self.file: BinaryIO = os.fdopen(fd, "wb", buffering=0)
        header = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.file.write(FILE_HEADER.pack(
            magic, SCHEMA_VERSION, 1, len(header), zlib.crc32(header) & 0xFFFFFFFF
        ))
        self.file.write(header)
        self.frames = 0

    def write(self, record: dict[str, Any]) -> None:
        self._write_payload(record)

    def write_many(self, records: list[dict[str, Any]]) -> None:
        """把多条逻辑记录压入同一个 CRC frame，降低事件洪峰开销。"""
        if records:
            self._write_payload(records)

    def _write_payload(self, record: Any) -> None:
        raw = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        payload = zlib.compress(raw, level=1)
        self.file.write(FRAME_HEADER.pack(
            len(payload), zlib.crc32(payload) & 0xFFFFFFFF
        ))
        self.file.write(payload)
        self.frames += 1

    def close(self) -> None:
        if self.file.closed:
            return
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()


def read_framed_binary(
    path: str | Path, *, recover_truncated: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """只读解码器。

    默认 CRC 或截断会显式失败。``recover_truncated`` 只允许忽略末尾未写完的
    header/payload，之前完整且 CRC 正确的帧仍返回；中间 CRC 损坏绝不跳过。
    """
    with Path(path).open("rb") as handle:
        fixed = handle.read(FILE_HEADER.size)
        if len(fixed) != FILE_HEADER.size:
            raise ValueError("truncated binary header")
        magic, version, flags, header_len, header_crc = FILE_HEADER.unpack(fixed)
        if version != SCHEMA_VERSION or flags != 1:
            raise ValueError(f"unsupported binary schema version={version} flags={flags}")
        header = handle.read(header_len)
        if len(header) != header_len or (zlib.crc32(header) & 0xFFFFFFFF) != header_crc:
            raise ValueError("invalid binary metadata CRC")
        metadata = json.loads(header.decode("utf-8"))
        metadata["magic"] = magic.decode("ascii", errors="replace")
        metadata["truncated_tail_recovered"] = False
        rows: list[dict[str, Any]] = []
        while True:
            frame_header = handle.read(FRAME_HEADER.size)
            if not frame_header:
                break
            if len(frame_header) != FRAME_HEADER.size:
                if recover_truncated:
                    metadata["truncated_tail_recovered"] = True
                    break
                raise ValueError("truncated frame header")
            length, checksum = FRAME_HEADER.unpack(frame_header)
            payload = handle.read(length)
            if len(payload) != length:
                if recover_truncated:
                    metadata["truncated_tail_recovered"] = True
                    break
                raise ValueError("truncated frame payload")
            if (zlib.crc32(payload) & 0xFFFFFFFF) != checksum:
                raise ValueError("invalid frame CRC")
            decoded = json.loads(zlib.decompress(payload).decode("utf-8"))
            # lifecycle 文件允许把事件批量装入一个物理 frame；对解码方仍
            # 展开为逐事件记录，保持只读工具和旧调用者的逻辑接口不变。
            if isinstance(decoded, list):
                rows.extend(decoded)
            else:
                rows.append(decoded)
        return metadata, rows


class FileCatalog:
    """把运行期 inode 身份变成 session 内整数 ID 和跨轮稳定哈希。"""

    def __init__(
        self,
        fd: int,
        *,
        session_id: str,
        boot_id: str,
        fixture_catalog: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.file: TextIO = os.fdopen(fd, "w", encoding="utf-8", newline="")
        self.session_id = session_id
        self.boot_id = boot_id
        self.fixture_catalog = {
            _normalize_path(path): dict(values)
            for path, values in (fixture_catalog or {}).items()
            if _normalize_path(path)
        }
        self.ids: dict[tuple[int, int, int], int] = {}
        self.id_owners: dict[int, tuple[int, int, int]] = {}
        self.paths: dict[tuple[int, int, int], str] = {}
        self.fixture_identities: dict[tuple[int, int, int], str] = {}
        for path in self.fixture_catalog:
            try:
                values = os.stat(path)
            except OSError:
                continue
            identity = (
                os.major(values.st_dev), os.minor(values.st_dev), int(values.st_ino)
            )
            self.fixture_identities[identity] = path

    def _catalog_id(
        self, file_key: tuple[int, int, int], normalized_path: str
    ) -> int:
        fixture_path = self.fixture_identities.get(file_key, "")
        fixture = self.fixture_catalog.get(fixture_path or normalized_path, {})
        logical_id = str(fixture.get("logical_id", ""))
        if logical_id:
            stable_material = f"fixture:{logical_id}"
        elif normalized_path:
            stable_material = f"path:{_sha256_text(normalized_path)}"
        else:
            # 对未取得路径的系统库/临时文件，device+inode 在不重建该文件的
            # 多轮实验中仍稳定；catalog 会把 identity_quality 标成 SESSION_ONLY。
            stable_material = "inode:" + ":".join(str(value) for value in file_key)
        salt = 0
        while True:
            material = stable_material if salt == 0 else f"{stable_material}#{salt}"
            value = int.from_bytes(
                hashlib.sha256(material.encode("utf-8")).digest()[:8], "little"
            ) & ((1 << 63) - 1)
            value = value or 1
            owner = self.id_owners.get(value)
            if owner is None or owner == file_key:
                self.id_owners[value] = file_key
                return value
            salt += 1

    def resolve(self, key: FilePageKey, path: str = "") -> int:
        file_key = (key.device_major, key.device_minor, key.inode)
        catalog_id = self.ids.get(file_key)
        # 同一文件可能有数万页。已有 ID 且路径已经登记（或本次仍无路径）时
        # 直接返回，不能为每个物理页重复执行 realpath/stat。
        if catalog_id is not None and (
            not path or bool(self.paths.get(file_key))
        ):
            return catalog_id
        normalized = _normalize_path(path) or self.fixture_identities.get(file_key, "")
        if catalog_id is None:
            catalog_id = self._catalog_id(file_key, normalized)
            self.ids[file_key] = catalog_id
            self._write(catalog_id, file_key, normalized, revision=1)
            if normalized:
                self.paths[file_key] = normalized
        elif normalized and not self.paths.get(file_key):
            # 首次事件可能只有 inode；稍后从 maps/fd 得到路径时追加修订记录。
            self.paths[file_key] = normalized
            self._write(catalog_id, file_key, normalized, revision=2)
        return catalog_id

    def _write(
        self,
        catalog_id: int,
        file_key: tuple[int, int, int],
        path: str,
        *,
        revision: int,
    ) -> None:
        major, minor, inode = file_key
        fixture = self.fixture_catalog.get(path, {})
        logical_id = str(fixture.get("logical_id", ""))
        content_hash = str(fixture.get("content_sha256", ""))
        path_hash = _sha256_text(path) if path else ""
        stable_key = (
            f"fixture:{logical_id}" if logical_id
            else f"path-sha256:{path_hash}" if path_hash
            else f"boot-inode:{self.boot_id}:{major}:{minor}:{inode}"
        )
        size = 0
        mtime_ns = 0
        if path:
            try:
                values = os.stat(path)
                if (
                    os.major(values.st_dev) == major
                    and os.minor(values.st_dev) == minor
                    and int(values.st_ino) == inode
                ):
                    size = int(values.st_size)
                    mtime_ns = int(values.st_mtime_ns)
            except OSError:
                pass
        row = {
            "record_type": "FILE_CATALOG" if revision == 1 else "FILE_CATALOG_UPDATE",
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "file_catalog_id": catalog_id,
            "revision": revision,
            "fixture_logical_id": logical_id,
            "stable_file_key": stable_key,
            "normalized_path_sha256": path_hash,
            "content_sha256": content_hash,
            "basename_hash": _sha256_text(os.path.basename(path)) if path else "",
            "device_major": major,
            "device_minor": minor,
            "inode": inode,
            "size_bytes": size,
            "mtime_ns": mtime_ns,
            "path_stored": False,
            "identity_quality": (
                "FIXTURE_LOGICAL_ID" if logical_id
                else "STABLE_PATH" if path
                else "SESSION_ONLY_INODE"
            ),
        }
        self.file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        self.file.flush()

    def close(self) -> None:
        if self.file.closed:
            return
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()


class PagemapFileScanner:
    """仅读取目标 PID 的文件 VMA，不触碰或 fault-in 页面。"""

    def __init__(self, page_size: int | None = None) -> None:
        self.page_size = int(page_size or os.sysconf("SC_PAGE_SIZE"))

    def scan_pid(self, pid: int) -> Iterator[RegisteredPage]:
        maps_path = Path(f"/proc/{int(pid)}/maps")
        pagemap_path = Path(f"/proc/{int(pid)}/pagemap")
        try:
            lines = maps_path.read_text(encoding="utf-8", errors="replace").splitlines()
            pagemap_fd = os.open(pagemap_path, os.O_RDONLY | os.O_CLOEXEC)
        except OSError:
            return
        try:
            for line in lines:
                match = _MAPS_RE.match(line)
                if match is None:
                    continue
                inode = int(match.group("inode"))
                path = str(match.group("path") or "")
                if inode <= 0 or not path.startswith("/"):
                    continue
                start = int(match.group("start"), 16)
                end = int(match.group("end"), 16)
                file_offset = int(match.group("offset"), 16)
                major = int(match.group("major"), 16)
                minor = int(match.group("minor"), 16)
                first_vpn = start // self.page_size
                page_count = max(0, (end - start) // self.page_size)
                base_file_page = file_offset // self.page_size
                # 每批最多读 8192 个 pagemap entry，避免大 VMA 分配巨型缓冲。
                for batch_start in range(0, page_count, 8192):
                    batch_count = min(8192, page_count - batch_start)
                    raw = os.pread(
                        pagemap_fd,
                        batch_count * PAGEMAP_ENTRY.size,
                        (first_vpn + batch_start) * PAGEMAP_ENTRY.size,
                    )
                    entries = len(raw) // PAGEMAP_ENTRY.size
                    for index in range(entries):
                        value = PAGEMAP_ENTRY.unpack_from(raw, index * PAGEMAP_ENTRY.size)[0]
                        if not value & PAGEMAP_PRESENT:
                            continue
                        pfn = int(value & PAGEMAP_PFN_MASK)
                        if pfn <= 0:
                            continue
                        yield RegisteredPage(
                            key=FilePageKey(
                                major,
                                minor,
                                inode,
                                base_file_page + batch_start + index,
                            ),
                            pfn=pfn,
                            path=path,
                            mapped_pids={int(pid)},
                        )
        finally:
            os.close(pagemap_fd)

    def resolve_resident_file_pages(
        self,
        pids: Iterable[int],
        keys: Iterable[FilePageKey],
        *,
        path_hints: dict[tuple[int, int, int], str] | None = None,
    ) -> dict[FilePageKey, tuple[int, str]]:
        """在窗口已经采样后，为 direct I/O 证据补齐常驻页 PFN。

        ``mm_filemap_get_pages`` 能给出精确文件页身份，但对 capture 启动
        前已经在 page cache 中的 buffered-I/O 页没有 PFN。本方法只处理
        这些已确认被 WPS 直接访问的页：先用 ``mincore`` 确认页仍在
        page cache，然后才建立临时映射并从 helper 自身 pagemap 取 PFN。

        调用点位于当前窗口 Idle 位读取之后、下个窗口重新 arm
        之前，所以 helper 的身份解析访问不会被误计入训练集。
        不在缓存中的页不会被 fault-in，仍保留为 ``PFN_UNRESOLVED``。
        """
        wanted = set(keys)
        if not wanted:
            return {}
        by_file: dict[tuple[int, int, int], list[FilePageKey]] = {}
        for key in wanted:
            by_file.setdefault(
                (key.device_major, key.device_minor, key.inode), []
            ).append(key)
        candidates: dict[tuple[int, int, int], tuple[int, str]] = {}
        hints = path_hints or {}
        for identity, path in hints.items():
            if identity not in by_file or not path:
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                values = os.fstat(fd)
            except OSError:
                continue
            actual = (
                os.major(values.st_dev), os.minor(values.st_dev), int(values.st_ino)
            )
            if actual == identity:
                candidates[identity] = (fd, path)
            else:
                os.close(fd)
        missing = set(by_file) - set(candidates)
        for pid in sorted(set(int(value) for value in pids if int(value) > 0)):
            if not missing:
                break
            try:
                names = os.listdir(f"/proc/{pid}/fd")
            except OSError:
                continue
            for name in names:
                if not missing:
                    break
                fd_path = f"/proc/{pid}/fd/{name}"
                try:
                    fd = os.open(fd_path, os.O_RDONLY | os.O_CLOEXEC)
                    values = os.fstat(fd)
                except OSError:
                    continue
                identity = (
                    os.major(values.st_dev), os.minor(values.st_dev),
                    int(values.st_ino),
                )
                if identity not in missing or not os.path.isfile(fd_path):
                    os.close(fd)
                    continue
                try:
                    path = os.readlink(fd_path)
                except OSError:
                    path = ""
                candidates[identity] = (fd, path)
                missing.discard(identity)

        resolved: dict[FilePageKey, tuple[int, str]] = {}
        try:
            pagemap_fd = os.open(
                f"/proc/{os.getpid()}/pagemap", os.O_RDONLY | os.O_CLOEXEC
            )
        except OSError:
            for fd, _path in candidates.values():
                os.close(fd)
            return resolved
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            for identity, file_keys in by_file.items():
                candidate = candidates.get(identity)
                if candidate is None:
                    continue
                fd, path = candidate
                try:
                    size = int(os.fstat(fd).st_size)
                except OSError:
                    continue
                valid_indices = sorted(
                    key.page_index for key in file_keys
                    if key.page_index >= 0 and key.page_index * self.page_size < size
                )
                chunks: list[list[int]] = []
                for page_index in valid_indices:
                    if (
                        not chunks
                        or page_index != chunks[-1][-1] + 1
                        or len(chunks[-1]) >= 8192
                    ):
                        chunks.append([page_index])
                    else:
                        chunks[-1].append(page_index)
                for chunk in chunks:
                    first = chunk[0]
                    length = min(
                        len(chunk) * self.page_size,
                        size - first * self.page_size,
                    )
                    if length <= 0:
                        continue
                    try:
                        mapping = mmap.mmap(
                            fd,
                            length,
                            flags=mmap.MAP_PRIVATE,
                            prot=mmap.PROT_READ | mmap.PROT_WRITE,
                            offset=first * self.page_size,
                        )
                    except (OSError, ValueError):
                        continue
                    address_holder: Any | None = None
                    try:
                        address_holder = ctypes.c_char.from_buffer(mapping)
                        base = ctypes.addressof(address_holder)
                        page_count = (length + self.page_size - 1) // self.page_size
                        residency = (ctypes.c_ubyte * page_count)()
                        if libc.mincore(
                            ctypes.c_void_p(base), ctypes.c_size_t(length), residency
                        ) != 0:
                            continue
                        for page_index in chunk:
                            relative_page = page_index - first
                            relative_offset = relative_page * self.page_size
                            if not residency[relative_page] & 1:
                                continue
                            # 只触碰 mincore 确认已驻留的页，不引入新 cache 页。
                            _ = mapping[relative_offset]
                            raw = os.pread(
                                pagemap_fd,
                                PAGEMAP_ENTRY.size,
                                ((base + relative_offset) // self.page_size)
                                * PAGEMAP_ENTRY.size,
                            )
                            if len(raw) != PAGEMAP_ENTRY.size:
                                continue
                            value = PAGEMAP_ENTRY.unpack(raw)[0]
                            pfn = int(value & PAGEMAP_PFN_MASK)
                            if value & PAGEMAP_PRESENT and pfn > 0:
                                key = FilePageKey(*identity, page_index)
                                resolved[key] = (pfn, path)
                    finally:
                        if address_holder is not None:
                            del address_holder
                        mapping.close()
        finally:
            os.close(pagemap_fd)
            for fd, _path in candidates.values():
                os.close(fd)
        return resolved


class PageIdleBackend:
    """对明确的 PFN 集合执行 Idle 位读取/重置，不扫描整机 PFN 空间。"""

    def __init__(
        self,
        *,
        bitmap_path: str | Path = "/sys/kernel/mm/page_idle/bitmap",
        kpageflags_path: str | Path = "/proc/kpageflags",
        kpagecount_path: str | Path = "/proc/kpagecount",
    ) -> None:
        self.bitmap_fd = os.open(bitmap_path, os.O_RDWR | os.O_CLOEXEC)
        self.kpageflags_fd = os.open(kpageflags_path, os.O_RDONLY | os.O_CLOEXEC)
        self.kpagecount_fd = os.open(kpagecount_path, os.O_RDONLY | os.O_CLOEXEC)

    @staticmethod
    def _word_masks(pfns: Iterable[int]) -> dict[int, int]:
        words: dict[int, int] = {}
        for pfn in set(int(value) for value in pfns if int(value) >= 0):
            word = pfn // 64
            words[word] = words.get(word, 0) | (1 << (pfn % 64))
        return words

    @staticmethod
    def _word_runs(
        masks: dict[int, int], *, max_words: int = 8192
    ) -> Iterator[list[tuple[int, int]]]:
        """把连续 bitmap 字分组，减少 page_idle 上的 pread/pwrite 次数。

        每个 run 只包含实际注册页所在的连续 64-PFN 字；不会跨过没有目标页
        的 bitmap 字，因此不会为了批量 I/O 扫描整机物理内存。写入字中的 0 位
        对其他页面没有影响，1 位才会把对应目标 PFN 标记为 Idle。
        """
        run: list[tuple[int, int]] = []
        for item in sorted(masks.items()):
            if run and (
                item[0] != run[-1][0] + 1 or len(run) >= max_words
            ):
                yield run
                run = []
            run.append(item)
        if run:
            yield run

    @staticmethod
    def _read_u64_runs(
        fd: int, pfns: Iterable[int], *, label: str
    ) -> tuple[dict[int, int], list[str]]:
        """只合并严格连续的 PFN 元数据读取，不读取未注册的相邻 PFN。"""
        ordered = sorted(set(int(value) for value in pfns if int(value) > 0))
        values: dict[int, int] = {}
        errors: list[str] = []
        runs: list[list[int]] = []
        for pfn in ordered:
            if (
                not runs or pfn != runs[-1][-1] + 1
                or len(runs[-1]) >= 8192
            ):
                runs.append([pfn])
            else:
                runs[-1].append(pfn)
        for run in runs:
            first = run[0]
            wanted_bytes = len(run) * U64.size
            try:
                raw = os.pread(fd, wanted_bytes, first * U64.size)
            except OSError as exc:
                errors.append(
                    f"{label}:pfn={first}:count={len(run)}:{exc.errno}"
                )
                continue
            complete = len(raw) // U64.size
            if complete != len(run):
                errors.append(
                    f"short_{label}:pfn={first}:count={len(run)}:bytes={len(raw)}"
                )
            for index in range(complete):
                values[first + index] = U64.unpack_from(
                    raw, index * U64.size
                )[0]
        return values, errors

    def arm(self, pfns: Iterable[int]) -> tuple[int, list[str]]:
        started = time.monotonic_ns()
        errors: list[str] = []
        # 注册表中的 PFN 已由目标文件 VMA/pagemap 或校准后的 folio 指针确认。
        # 写 Idle 位本身不会 fault-in 页面；真正把“cleared”归因给 WPS 前，
        # sample() 仍会复核当前 kpageflags，防止迁移/复用造成误报。逐页在
        # arm 和 sample 各读一次 kpageflags 会令大启动窗口产生数万次 syscall。
        for run in self._word_runs(self._word_masks(pfns)):
            first_word = run[0][0]
            payload = b"".join(U64.pack(mask) for _, mask in run)
            try:
                written = os.pwrite(
                    self.bitmap_fd, payload, first_word * U64.size
                )
                if written != len(payload):
                    errors.append(
                        "short_idle_write:"
                        f"word={first_word}:count={len(run)}:bytes={written}"
                    )
            except OSError as exc:
                errors.append(
                    f"idle_write:word={first_word}:count={len(run)}:{exc.errno}"
                )
        return time.monotonic_ns() - started, errors

    def sample(
        self, pfns: Iterable[int]
    ) -> tuple[set[int], dict[int, int], int, list[str]]:
        accessed: set[int] = set()
        map_counts: dict[int, int] = {}
        skipped_non_lru = 0
        errors: list[str] = []
        idle_cleared: set[int] = set()
        for run in self._word_runs(self._word_masks(pfns)):
            first_word = run[0][0]
            wanted_bytes = len(run) * U64.size
            try:
                raw = os.pread(
                    self.bitmap_fd, wanted_bytes, first_word * U64.size
                )
            except OSError as exc:
                errors.append(
                    f"idle_read:word={first_word}:count={len(run)}:{exc.errno}"
                )
                continue
            complete = len(raw) // U64.size
            if complete != len(run):
                errors.append(
                    "short_idle_read:"
                    f"word={first_word}:count={len(run)}:bytes={len(raw)}"
                )
            for index, (word, mask) in enumerate(run[:complete]):
                idle_mask = U64.unpack_from(raw, index * U64.size)[0]
                candidates = mask
                while candidates:
                    bit_mask = candidates & -candidates
                    bit = bit_mask.bit_length() - 1
                    pfn = word * 64 + bit
                    candidates ^= bit_mask
                    # Idle 未清除的页不进入数据集，无需为它读取 kpageflags。
                    if idle_mask & bit_mask:
                        continue
                    idle_cleared.add(pfn)
        flags_by_pfn, flag_errors = self._read_u64_runs(
            self.kpageflags_fd, idle_cleared, label="kpageflags"
        )
        errors.extend(flag_errors)
        for pfn in sorted(idle_cleared):
            flags = flags_by_pfn.get(pfn)
            if flags is None:
                continue
            if not flags & KPF_LRU or flags & KPF_ANON:
                skipped_non_lru += 1
                continue
            accessed.add(pfn)
        counts_by_pfn, count_errors = self._read_u64_runs(
            self.kpagecount_fd, accessed, label="kpagecount"
        )
        errors.extend(count_errors)
        map_counts.update(counts_by_pfn)
        return accessed, map_counts, skipped_non_lru, errors

    def close(self) -> None:
        for fd in (self.bitmap_fd, self.kpageflags_fd, self.kpagecount_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def compress_access_ranges(
    rows: Iterable[dict[str, int]],
) -> list[dict[str, int | list[str]]]:
    """仅在文件页、PFN、来源和归因全部连续/相同时做无损压缩。"""
    ordered = sorted(
        rows,
        key=lambda row: (
            row["file_catalog_id"], row["page_index"], row["pfn"],
            row["source_mask"], row["attribution_mask"],
        ),
    )
    ranges: list[dict[str, int | list[str]]] = []
    for row in ordered:
        previous = ranges[-1] if ranges else None
        if (
            previous is not None
            and int(previous["file_catalog_id"]) == row["file_catalog_id"]
            and int(previous["source_mask_raw"]) == row["source_mask"]
            and int(previous["attribution_mask_raw"]) == row["attribution_mask"]
            and int(previous["page_index_start"]) + int(previous["page_count"]) == row["page_index"]
            and int(previous["pfn_start"]) + int(previous["page_count"]) == row["pfn"]
        ):
            previous["page_count"] = int(previous["page_count"]) + 1
            continue
        ranges.append({
            "file_catalog_id": row["file_catalog_id"],
            "page_index_start": row["page_index"],
            "pfn_start": row["pfn"],
            "page_count": 1,
            "pfn_stride": 1,
            "source_mask_raw": row["source_mask"],
            "source_mask": _mask_names(row["source_mask"], SOURCE_NAMES),
            "attribution_mask_raw": row["attribution_mask"],
            "attribution_mask": _mask_names(
                row["attribution_mask"], ATTRIBUTION_NAMES
            ),
        })
    return ranges


class PageAccessWindowCapture:
    """一个 capture session 的文件目录、Page Idle 窗口与质量状态。"""

    SUMMARY_FIELDS = [
        "session_id", "window_id", "start_monotonic_ns", "end_monotonic_ns",
        "duration_ns", "accessed_pages", "direct_wps_pages",
        "unique_mapped_pages", "shared_pages", "new_pages", "evicted_pages",
        "unresolved_pfn_pages",
        "reset_latency_ms", "scan_latency_ms", "bpf_map_overflows",
        "skipped_non_lru", "valid", "invalid_reason",
    ]

    def __init__(
        self,
        *,
        window_fd: int,
        lifecycle_fd: int,
        catalog_fd: int,
        summary_fd: int,
        manifest_fd: int,
        session_id: str,
        target_app: str,
        app_id: int,
        window_ms: int = 1000,
        fixture_catalog: dict[str, dict[str, str]] | None = None,
        metadata: dict[str, Any] | None = None,
        bitmap_path: str | Path = "/sys/kernel/mm/page_idle/bitmap",
        kpageflags_path: str | Path = "/proc/kpageflags",
        kpagecount_path: str | Path = "/proc/kpagecount",
        lock_path: str | Path = "/run/parp-page-idle.lock",
        idle_backend: Any | None = None,
        scanner: PagemapFileScanner | None = None,
    ) -> None:
        self.session_id = str(session_id)
        self.target_app = str(target_app).strip().upper()
        self.app_id = int(app_id)
        self.window_ns = max(100_000_000, int(window_ms) * 1_000_000)
        self.page_size = int(os.sysconf("SC_PAGE_SIZE"))
        self.boot_id = _boot_id()
        self.metadata = dict(metadata or {})
        common = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "target_app": self.target_app,
            "app_id": self.app_id,
            "boot_id": self.boot_id,
            "kernel_release": _kernel_release(),
            "page_size": self.page_size,
            "window_ns": self.window_ns,
        }
        self.window_writer = FramedBinaryWriter(
            window_fd, magic=WINDOW_MAGIC, metadata={**common, "kind": "PAGE_ACCESS_WINDOW"}
        )
        self.lifecycle_writer = FramedBinaryWriter(
            lifecycle_fd, magic=LIFECYCLE_MAGIC, metadata={**common, "kind": "PAGE_LIFECYCLE"}
        )
        self.catalog = FileCatalog(
            catalog_fd,
            session_id=self.session_id,
            boot_id=self.boot_id,
            fixture_catalog=fixture_catalog,
        )
        self.summary_file: TextIO = os.fdopen(
            summary_fd, "w", encoding="utf-8", newline=""
        )
        self.summary = csv.DictWriter(self.summary_file, fieldnames=self.SUMMARY_FIELDS)
        self.summary.writeheader()
        self.summary_file.flush()
        self.manifest_file: TextIO = os.fdopen(manifest_fd, "w", encoding="utf-8")
        self.lock_file = Path(lock_path).open("a+")
        fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.idle = idle_backend or PageIdleBackend(
                bitmap_path=bitmap_path,
                kpageflags_path=kpageflags_path,
                kpagecount_path=kpagecount_path,
            )
        except Exception:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
            raise
        self.scanner = scanner or PagemapFileScanner(self.page_size)
        self.processes: dict[int, tuple[str, str, str]] = {}
        self.registry: dict[FilePageKey, RegisteredPage] = {}
        self.pid_pages: dict[int, set[FilePageKey]] = {}
        self.scan_tasks: dict[
            int, tuple[Iterator[RegisteredPage], set[FilePageKey]]
        ] = {}
        self.current_accesses: dict[FilePageKey, AccessObservation] = {}
        self.current_evictions: set[FilePageKey] = set()
        self.current_new_resident: set[FilePageKey] = set()
        self.current_pfn_changed: set[FilePageKey] = set()
        self.dirty_pids: set[int] = set()
        self.armed: dict[int, list[RegisteredPage]] = {}
        self.window_id = 0
        self.lifecycle_sequence = 0
        self.lifecycle_buffer: list[dict[str, Any]] = []
        self.started_boot_ns = time.monotonic_ns()
        self.started_realtime_ns = time.time_ns()
        self.clock_offset_ns = self.started_realtime_ns - self.started_boot_ns
        self.window_start_boot_ns = self.started_boot_ns
        self.next_deadline_boot_ns = self.window_start_boot_ns + self.window_ns
        self.last_reset_latency_ns = 0
        self.last_reset_begin_ns = 0
        self.last_reset_end_ns = 0
        self.last_reset_errors: list[str] = []
        self.pending_quality_errors: list[str] = []
        self.total_accessed_pages = 0
        self.total_invalid_windows = 0
        self.total_overflows = 0
        self.closed = False

    def update_processes(
        self, processes: dict[int, tuple[str, str, str]], *, refresh: bool = True
    ) -> None:
        wanted = {
            int(pid): values
            for pid, values in processes.items()
            if str(values[0]).strip().upper() == self.target_app
        }
        previous = self.processes
        removed = set(previous) - set(wanted)
        for pid in removed:
            self._cancel_scan_task(pid)
            self._drop_pid_mappings(pid)
        self.processes = wanted
        # SYNC_PROCESSES 携带完整 AppProcessIndex 快照，但一次 create/exit 不应
        # 让 helper 反复扫描 App 中所有老进程。这里只增量标记新增或身份变化的
        # PID；同 App 的 exec/mmap 地址空间变化另由内核 mmap/munmap/mremap
        # hook 写入 page_mapping_dirty，窗口边界再做一次定向扫描。
        self.dirty_pids.update(
            pid for pid, values in wanted.items()
            if previous.get(pid) != values
        )
        if refresh:
            self.refresh_dirty_processes()

    def mark_mapping_dirty(self, pids: Iterable[int]) -> None:
        self.dirty_pids.update(int(pid) for pid in pids if int(pid) in self.processes)

    def note_quality_error(self, reason: str) -> None:
        """把异步 helper/BPF 完整性故障归入当前窗口。"""
        value = str(reason).strip()
        if value:
            self.pending_quality_errors.append(value)

    def _set_registered_pfn(
        self,
        page: RegisteredPage,
        pfn: int,
        *,
        pid: int = 0,
        emit_change: bool = True,
    ) -> bool:
        """更新稳定文件页的当次 PFN，并保留回收前的最后 PFN。

        eviction 后 ``page.pfn`` 必须置零，否则后续 direct 证据可能
        带上已被复用的物理页。``last_known_pfn`` 仅用于在同一
        稳定文件页重新驻留时识别 PFN_CHANGE，不作为当前物理身份。
        """
        new_pfn = int(pfn)
        if new_pfn <= 0:
            return False
        previous = int(page.pfn or page.last_known_pfn)
        changed = previous > 0 and previous != new_pfn
        if changed:
            if emit_change:
                self._write_lifecycle(
                    "PFN_CHANGE", page.key, new_pfn,
                    old_pfn=previous, pid=pid,
                )
            self.current_pfn_changed.add(page.key)
        page.pfn = new_pfn
        page.last_known_pfn = new_pfn
        return changed

    def _drop_pid_mappings(self, pid: int) -> None:
        for key in self.pid_pages.pop(int(pid), set()):
            page = self.registry.get(key)
            if page is not None:
                page.mapped_pids.discard(int(pid))

    def _cancel_scan_task(self, pid: int) -> None:
        task = self.scan_tasks.pop(int(pid), None)
        if task is None:
            return
        iterator, discovered = task
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
        for key in discovered:
            page = self.registry.get(key)
            if page is not None:
                page.mapped_pids.discard(int(pid))

    def _register_scanned_page(
        self, pid: int, page: RegisteredPage, discovered: set[FilePageKey]
    ) -> None:
        discovered.add(page.key)
        current = self.registry.get(page.key)
        if current is None:
            page.last_known_pfn = page.pfn
            current = page
            self.registry[page.key] = current
        else:
            self._set_registered_pfn(current, page.pfn, pid=pid)
            current.path = current.path or page.path
        current.mapped_pids.add(pid)
        self.catalog.resolve(page.key, current.path)

    def refresh_dirty_processes(self, *, budget_ns: int | None = None) -> None:
        """增量推进 VMA/pagemap 注册；窗口热路径受固定时间预算约束。

        新进程的首个 mmap/read/fault 已由 eBPF 精确记录。全 VMA 扫描用于把
        其余常驻映射加入后续 Page Idle 窗口，不应让一个拥有数万文件页的 GUI
        在单个 1 秒边界中独占数百毫秒。无预算调用（capture 初始化/单测）仍
        会完整扫描；窗口调用则轮转推进每个 PID 的 generator。
        """
        for pid in sorted(set(self.dirty_pids) - set(self.scan_tasks)):
            self.dirty_pids.discard(pid)
            if pid not in self.processes:
                continue
            self._drop_pid_mappings(pid)
            self.scan_tasks[pid] = (iter(self.scanner.scan_pid(pid)), set())
        if not self.scan_tasks:
            return
        deadline = (
            time.monotonic_ns() + max(1, int(budget_ns))
            if budget_ns is not None else None
        )
        while self.scan_tasks:
            progressed = False
            for pid in sorted(list(self.scan_tasks)):
                task = self.scan_tasks.get(pid)
                if task is None:
                    continue
                iterator, discovered = task
                complete = False
                for _ in range(256):
                    try:
                        page = next(iterator)
                    except StopIteration:
                        complete = True
                        break
                    self._register_scanned_page(pid, page, discovered)
                    progressed = True
                if complete:
                    self.pid_pages[pid] = discovered
                    self.scan_tasks.pop(pid, None)
                if deadline is not None and time.monotonic_ns() >= deadline:
                    return
            if not progressed and not self.scan_tasks:
                return

    def arm_initial_window(self) -> None:
        # 初始窗口尚未开始计时，可以完整建立启动时已有映射的基线。
        self.refresh_dirty_processes()
        self._arm_current_registry()
        self.window_start_boot_ns = time.monotonic_ns()
        self.next_deadline_boot_ns = self.window_start_boot_ns + self.window_ns

    def _arm_current_registry(self) -> None:
        by_pfn: dict[int, list[RegisteredPage]] = {}
        for page in self.registry.values():
            if page.pfn > 0:
                by_pfn.setdefault(page.pfn, []).append(RegisteredPage(
                    key=page.key,
                    pfn=page.pfn,
                    last_known_pfn=page.last_known_pfn,
                    path=page.path,
                    mapped_pids=set(page.mapped_pids),
                    first_seen_boot_ns=page.first_seen_boot_ns,
                    last_seen_boot_ns=page.last_seen_boot_ns,
                ))
        self.armed = by_pfn
        self.last_reset_begin_ns = time.monotonic_ns()
        measured_latency, self.last_reset_errors = self.idle.arm(by_pfn)
        self.last_reset_end_ns = time.monotonic_ns()
        self.last_reset_latency_ns = max(
            int(measured_latency), self.last_reset_end_ns - self.last_reset_begin_ns
        )

    def ingest_accesses(self, rows: Iterable[dict[str, Any]]) -> None:
        for row in rows:
            key = FilePageKey(
                int(row.get("device_major", 0)),
                int(row.get("device_minor", 0)),
                int(row.get("inode", 0)),
                int(row.get("page_index", 0)),
            )
            if key.inode <= 0:
                continue
            pfn = int(row.get("pfn", 0) or 0)
            source = int(row.get("source_mask", SOURCE_DIRECT_WPS_ACCESS))
            if key in self.current_new_resident:
                source |= SOURCE_NEW_RESIDENT
            if key in self.current_pfn_changed:
                source |= SOURCE_PFN_CHANGED
            first_ns = int(row.get("first_boot_ns", 0) or time.monotonic_ns())
            last_ns = int(row.get("last_boot_ns", 0) or first_ns)
            page = self.registry.get(key)
            if page is None:
                page = RegisteredPage(
                    key=key,
                    pfn=pfn,
                    last_known_pfn=pfn,
                    first_seen_boot_ns=first_ns,
                    last_seen_boot_ns=last_ns,
                )
                self.registry[key] = page
            elif pfn > 0:
                if self._set_registered_pfn(
                    page, pfn, pid=int(row.get("tgid", 0) or 0)
                ):
                    source |= SOURCE_PFN_CHANGED
            page.last_seen_boot_ns = max(page.last_seen_boot_ns, last_ns)
            self.catalog.resolve(key, page.path)
            attribution = ATTR_DIRECT_WPS
            existing = self.current_accesses.get(key)
            if existing is None:
                self.current_accesses[key] = AccessObservation(
                    key=key,
                    pfn=(
                        pfn or page.pfn or (
                            page.last_known_pfn
                            if key in self.current_evictions else 0
                        )
                    ),
                    source_mask=(
                        source | SOURCE_EVICT_BEFORE_SAMPLE
                        if key in self.current_evictions else source
                    ),
                    attribution_mask=attribution,
                    first_boot_ns=first_ns,
                    last_boot_ns=last_ns,
                    tgid=int(row.get("tgid", 0) or 0),
                    tid=int(row.get("tid", 0) or 0),
                )
            else:
                existing.source_mask |= source
                existing.attribution_mask |= attribution
                existing.first_boot_ns = min(existing.first_boot_ns, first_ns)
                existing.last_boot_ns = max(existing.last_boot_ns, last_ns)
                if pfn:
                    existing.pfn = pfn

    def record_lifecycle(self, row: dict[str, Any]) -> None:
        event_type = str(row.get("event_type", "")).upper()
        page_index = int(row.get("page_index", 0) or 0)
        key = FilePageKey(
            int(row.get("device_major", 0)),
            int(row.get("device_minor", 0)),
            int(row.get("inode", 0)),
            page_index,
        )
        if key.inode <= 0:
            return
        pfn = int(row.get("pfn", 0) or 0)
        self._write_lifecycle(
            event_type, key, pfn,
            pid=int(row.get("tgid", 0) or 0),
            tid=int(row.get("tid", 0) or 0),
            boot_timestamp_ns=int(row.get("boot_timestamp_ns", 0) or 0),
            page_order=int(row.get("page_order", 0) or 0),
        )
        if event_type in {"FAULT", "FILE_FAULT"}:
            self.ingest_accesses([{
                **row,
                "source_mask": SOURCE_FILE_FAULT,
                "first_boot_ns": row.get("boot_timestamp_ns", 0),
                "last_boot_ns": row.get("boot_timestamp_ns", 0),
            }])
            self.mark_mapping_dirty([int(row.get("tgid", 0) or 0)])
        elif event_type == "CACHE_ADD" and pfn > 0:
            page_count = 1 << min(16, max(0, int(row.get("page_order", 0) or 0)))
            added_keys = [
                FilePageKey(
                    key.device_major, key.device_minor, key.inode,
                    key.page_index + index,
                )
                for index in range(page_count)
            ]
            self.current_new_resident.update(added_keys)
            for index, added_key in enumerate(added_keys):
                current = self.registry.get(added_key)
                if current is None:
                    self.registry[added_key] = RegisteredPage(
                        key=added_key,
                        pfn=pfn + index,
                        last_known_pfn=pfn + index,
                    )
                else:
                    self._set_registered_pfn(
                        current,
                        pfn + index,
                        pid=int(row.get("tgid", 0) or 0),
                    )
                existing_access = self.current_accesses.get(added_key)
                if existing_access is not None:
                    if not existing_access.pfn:
                        existing_access.pfn = pfn + index
                    existing_access.source_mask |= SOURCE_NEW_RESIDENT
                self.catalog.resolve(added_key)
        elif event_type == "EVICT":
            page_count = 1 << min(16, max(0, int(row.get("page_order", 0) or 0)))
            for index in range(page_count):
                evicted_key = FilePageKey(
                    key.device_major, key.device_minor, key.inode,
                    key.page_index + index,
                )
                expected_pfn = pfn + index if pfn > 0 else 0
                self.current_evictions.add(evicted_key)
                existing_access = self.current_accesses.get(evicted_key)
                if existing_access is not None:
                    existing_access.source_mask |= SOURCE_EVICT_BEFORE_SAMPLE
                    if not existing_access.pfn and expected_pfn > 0:
                        existing_access.pfn = expected_pfn
                current = self.registry.get(evicted_key)
                if current is not None and (
                    expected_pfn <= 0 or current.pfn == expected_pfn
                ):
                    if current.pfn > 0:
                        current.last_known_pfn = current.pfn
                    current.pfn = 0

    def _write_lifecycle(
        self,
        event_type: str,
        key: FilePageKey,
        pfn: int,
        *,
        old_pfn: int = 0,
        pid: int = 0,
        tid: int = 0,
        boot_timestamp_ns: int = 0,
        page_order: int = 0,
    ) -> None:
        self.lifecycle_sequence += 1
        boot_ns = int(boot_timestamp_ns or time.monotonic_ns())
        self.lifecycle_buffer.append({
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "sequence": self.lifecycle_sequence,
            "event_type": str(event_type),
            "monotonic_ns": boot_ns,
            "realtime_ns": self.clock_offset_ns + boot_ns,
            "app_key": self.target_app,
            "app_id": self.app_id,
            "pid": int(pid),
            "tid": int(tid),
            "file_catalog_id": self.catalog.resolve(key),
            "page_index": key.page_index,
            "pfn": int(pfn),
            "old_pfn": int(old_pfn),
            "folio_order": int(page_order),
            "window_id": self.window_id + 1,
        })
        if len(self.lifecycle_buffer) >= LIFECYCLE_BATCH_MAX:
            self._flush_lifecycle_buffer()

    def _flush_lifecycle_buffer(self) -> None:
        """批量压缩生命周期事件，避免 WPS 启动洪峰拖过窗口边界。"""
        if not self.lifecycle_buffer:
            return
        pending = self.lifecycle_buffer
        self.lifecycle_buffer = []
        self.lifecycle_writer.write_many(pending)

    def due(self, now_boot_ns: int | None = None) -> bool:
        return int(now_boot_ns or time.monotonic_ns()) >= self.next_deadline_boot_ns

    def _resolve_unresolved_direct_pfns(self) -> None:
        """在 Idle 采样完成后补齐已验证 direct 页的物理身份。"""
        unresolved = {
            key for key, access in self.current_accesses.items()
            if access.pfn <= 0 and access.attribution_mask & ATTR_DIRECT_WPS
        }
        resolver = getattr(self.scanner, "resolve_resident_file_pages", None)
        if not unresolved or not callable(resolver):
            return
        hints: dict[tuple[int, int, int], str] = dict(
            self.catalog.fixture_identities
        )
        for page in self.registry.values():
            if page.path:
                hints[
                    (page.key.device_major, page.key.device_minor, page.key.inode)
                ] = page.path
        resolved = resolver(self.processes, unresolved, path_hints=hints)
        for key, (pfn, path) in resolved.items():
            access = self.current_accesses.get(key)
            if access is None or access.pfn > 0:
                continue
            page = self.registry.get(key)
            if page is None:
                page = RegisteredPage(
                    key=key,
                    pfn=int(pfn),
                    last_known_pfn=int(pfn),
                    path=str(path),
                )
                self.registry[key] = page
            else:
                self._set_registered_pfn(page, int(pfn), pid=access.tgid)
                page.path = page.path or str(path)
            access.pfn = int(pfn)
            self.catalog.resolve(key, page.path)

    def rotate(
        self,
        direct_rows: Iterable[dict[str, Any]],
        *,
        bpf_map_overflows: int = 0,
        dirty_pids: Iterable[int] = (),
        final: bool = False,
    ) -> dict[str, Any]:
        self.ingest_accesses(direct_rows)
        self.mark_mapping_dirty(dirty_pids)
        scan_started = time.monotonic_ns()
        # 运行期新进程/映射采用限时增量注册，避免一次大 GUI VMA 扫描阻塞
        # 单个窗口数百毫秒；精确的 read/fault 证据仍由 eBPF 当窗补齐。
        self.refresh_dirty_processes(budget_ns=VMA_REFRESH_BUDGET_NS)
        # direct read/fault 已有高置信度身份与 PFN，不需要再为同一页执行
        # kpageflags/kpagecount 的 Page Idle 归因；这也避免大文件顺序读在窗口
        # 边界制造数万次 /proc 读取。Page Idle 只补 mmap/CPU 访问的页。
        direct_pfns = {
            int(access.pfn)
            for access in self.current_accesses.values()
            if access.pfn > 0 and access.attribution_mask & ATTR_DIRECT_WPS
        }
        idle_candidates = {
            pfn: pages for pfn, pages in self.armed.items()
            if pfn not in direct_pfns
        }
        idle_pfns, map_counts, skipped_non_lru, sample_errors = self.idle.sample(
            idle_candidates
        )
        scan_ended = time.monotonic_ns()
        for pfn in idle_pfns:
            for page in self.armed.get(pfn, []):
                # 若稳定文件页已经回收、迁移或 PFN 被复用，旧 PFN 的 Idle
                # 变化不再归因给它；窗口中的 fault/direct 证据仍会保留。
                current_page = self.registry.get(page.key)
                if (
                    page.key in self.current_evictions
                    or current_page is None
                    or current_page.pfn != pfn
                ):
                    continue
                existing = self.current_accesses.get(page.key)
                target_mapping_count = max(1, len(page.mapped_pids))
                attribution = (
                    ATTR_WPS_MAPPED_SHARED
                    if int(map_counts.get(pfn, 0)) > target_mapping_count
                    else ATTR_WPS_MAPPED_UNIQUE
                )
                if existing is None:
                    self.current_accesses[page.key] = AccessObservation(
                        key=page.key,
                        pfn=pfn,
                        source_mask=SOURCE_PAGE_IDLE_CLEARED,
                        attribution_mask=attribution,
                        first_boot_ns=self.window_start_boot_ns,
                        last_boot_ns=scan_started,
                    )
                else:
                    existing.source_mask |= SOURCE_PAGE_IDLE_CLEARED
                    existing.attribution_mask |= attribution
                    if not existing.pfn:
                        existing.pfn = pfn
        # 解析发生在当前 Idle 位读取之后。紧接着的
        # _arm_current_registry() 会在下一窗口前清理这次 helper 触碰。
        self._resolve_unresolved_direct_pfns()
        self.window_id += 1
        window_end = scan_started
        rows: list[dict[str, int]] = []
        unresolved_pfn_count = 0
        for access in self.current_accesses.values():
            registered = self.registry.get(access.key)
            if not access.pfn and registered is not None and registered.pfn > 0:
                access.pfn = registered.pfn
            if not access.pfn:
                access.attribution_mask |= ATTR_UNRESOLVED
                unresolved_pfn_count += 1
            if access.key in self.current_pfn_changed:
                access.source_mask |= SOURCE_PFN_CHANGED
            catalog_id = self.catalog.resolve(
                access.key, registered.path if registered is not None else ""
            )
            rows.append({
                "file_catalog_id": catalog_id,
                "page_index": access.key.page_index,
                "pfn": access.pfn,
                "source_mask": access.source_mask,
                "attribution_mask": access.attribution_mask,
            })
        ranges = compress_access_ranges(rows)
        invalid_reasons = [
            *self.last_reset_errors,
            *sample_errors,
            *self.pending_quality_errors,
        ]
        overflow_count = max(0, int(bpf_map_overflows))
        if overflow_count:
            invalid_reasons.append("BPF_MAP_OVERFLOW")
        if unresolved_pfn_count:
            invalid_reasons.append("PFN_UNRESOLVED")
        duration = max(0, window_end - self.window_start_boot_ns)
        if final and duration < self.window_ns:
            invalid_reasons.append("INCOMPLETE_FINAL_WINDOW")
        if duration > self.window_ns * 3 // 2:
            invalid_reasons.append("BOUNDARY_OVERRUN")
        valid = not invalid_reasons
        if not valid:
            self.total_invalid_windows += 1
        self.total_overflows += overflow_count
        self.total_accessed_pages += len(rows)
        direct_count = sum(
            1 for row in rows if row["attribution_mask"] & ATTR_DIRECT_WPS
        )
        shared_count = sum(
            1 for row in rows if row["attribution_mask"] & ATTR_WPS_MAPPED_SHARED
        )
        unique_count = sum(
            1 for row in rows if row["attribution_mask"] & ATTR_WPS_MAPPED_UNIQUE
        )
        new_count = sum(1 for row in rows if row["source_mask"] & SOURCE_NEW_RESIDENT)
        record = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "window_id": self.window_id,
            "app_key": self.target_app,
            "app_id": self.app_id,
            "window_start_monotonic_ns": self.window_start_boot_ns,
            "window_end_monotonic_ns": window_end,
            "window_start_realtime_ns": (
                self.started_realtime_ns
                + self.window_start_boot_ns - self.started_boot_ns
            ),
            "window_end_realtime_ns": (
                self.started_realtime_ns + window_end - self.started_boot_ns
            ),
            "window_duration_ns": duration,
            "reset_begin_ns": self.last_reset_begin_ns,
            "reset_end_ns": self.last_reset_end_ns,
            "reset_latency_ns": self.last_reset_latency_ns,
            "scan_begin_ns": scan_started,
            "scan_end_ns": scan_ended,
            "scan_latency_ns": scan_ended - scan_started,
            "target_pid_count": len(self.processes),
            "accessed_page_count": len(rows),
            "page_range_count": len(ranges),
            "page_ranges": ranges,
            "quality": {
                "valid": valid,
                "invalid_reasons": sorted(set(invalid_reasons)),
                "bpf_map_overflow": overflow_count,
                "unresolved_pfn_pages": unresolved_pfn_count,
                "skipped_non_lru": skipped_non_lru,
                "boundary_overrun": "BOUNDARY_OVERRUN" in invalid_reasons,
            },
        }
        self.window_writer.write(record)
        # 在每个窗口边界完成一批 lifecycle frame，使 partial 文件即使异常
        # 中止也最多只损失尚未跨过边界的一小批辅助审计事件。
        self._flush_lifecycle_buffer()
        self.summary.writerow({
            "session_id": self.session_id,
            "window_id": self.window_id,
            "start_monotonic_ns": self.window_start_boot_ns,
            "end_monotonic_ns": window_end,
            "duration_ns": duration,
            "accessed_pages": len(rows),
            "direct_wps_pages": direct_count,
            "unique_mapped_pages": unique_count,
            "shared_pages": shared_count,
            "new_pages": new_count,
            "evicted_pages": len(self.current_evictions),
            "unresolved_pfn_pages": unresolved_pfn_count,
            "reset_latency_ms": f"{self.last_reset_latency_ns / 1e6:.3f}",
            "scan_latency_ms": f"{(scan_ended - scan_started) / 1e6:.3f}",
            "bpf_map_overflows": overflow_count,
            "skipped_non_lru": skipped_non_lru,
            "valid": str(valid).lower(),
            "invalid_reason": ";".join(sorted(set(invalid_reasons))),
        })
        self.summary_file.flush()
        self.current_accesses.clear()
        self.current_evictions.clear()
        self.current_new_resident.clear()
        self.current_pfn_changed.clear()
        self.pending_quality_errors.clear()
        if not final:
            self._arm_current_registry()
            self.window_start_boot_ns = time.monotonic_ns()
            self.next_deadline_boot_ns = self.window_start_boot_ns + self.window_ns
        return record

    def close(
        self, *, capture_status: str = "COMPLETE", failure_reason: str = ""
    ) -> dict[str, Any]:
        if self.closed:
            return {}
        self.closed = True
        ended_boot_ns = time.monotonic_ns()
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "capture_status": str(capture_status),
            "failure_reason": str(failure_reason),
            "app": {"app_key": self.target_app, "app_id": self.app_id},
            "experiment": {
                **{
                    key: value for key, value in self.metadata.items()
                    if key != "source_integrity"
                },
                "window_ms": self.window_ns // 1_000_000,
            },
            "source_integrity": dict(
                self.metadata.get("source_integrity", {})
            ),
            "host": {
                "kernel": _kernel_release(),
                "boot_id": self.boot_id,
                "page_size": self.page_size,
            },
            "clock": {
                "start_monotonic_ns": self.started_boot_ns,
                "start_realtime_ns": self.started_realtime_ns,
                "end_monotonic_ns": ended_boot_ns,
                "end_realtime_ns": time.time_ns(),
            },
            "result": {
                "total_windows": self.window_id,
                "valid_windows": self.window_id - self.total_invalid_windows,
                "invalid_windows": self.total_invalid_windows,
                "distinct_files": len(self.catalog.ids),
                "distinct_file_pages": len(self.registry),
                "accessed_page_window_rows": self.total_accessed_pages,
                "lifecycle_events": self.lifecycle_sequence,
                "bpf_map_overflows": self.total_overflows,
            },
            "files": {
                "page_access_windows": "dataset/page_access_windows.bin",
                "page_lifecycle": "dataset/page_lifecycle.bin",
                "file_catalog": "dataset/file_catalog.jsonl",
                "window_summary": "dataset/window_summary.csv",
                "automation_trace": "automation_trace.csv",
            },
        }
        try:
            for pid in list(self.scan_tasks):
                self._cancel_scan_task(pid)
            self.window_writer.close()
            self._flush_lifecycle_buffer()
            self.lifecycle_writer.close()
            self.catalog.close()
            self.summary_file.flush()
            os.fsync(self.summary_file.fileno())
            self.summary_file.close()
            self.manifest_file.write(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
            self.manifest_file.flush()
            os.fsync(self.manifest_file.fileno())
            self.manifest_file.close()
        finally:
            self.idle.close()
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
        return manifest
