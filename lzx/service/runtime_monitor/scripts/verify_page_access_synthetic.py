#!/usr/bin/env python3
"""用真实 eBPF + Page Idle 验证 mmap/pread 文件页集合的召回率和误报。"""

from __future__ import annotations

import argparse
import ctypes
import json
import mmap
import os
import sys
import tempfile
import time
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from runtime_monitor.core.page_access_window import (
    FilePageKey,
    PageAccessWindowCapture,
)
from runtime_monitor.helpers.ebpf_file_event_helper import EBPFFileEventHelper


def expand_file_pages(record: dict, catalog_id: int) -> set[int]:
    result: set[int] = set()
    for item in record.get("page_ranges", []):
        if int(item.get("file_catalog_id", 0)) != catalog_id:
            continue
        start = int(item["page_index_start"])
        result.update(range(start, start + int(item["page_count"])))
    return result


def expand_pages_with_source(
    record: dict, catalog_id: int, source_bit: int
) -> set[int]:
    result: set[int] = set()
    for item in record.get("page_ranges", []):
        if (
            int(item.get("file_catalog_id", 0)) != catalog_id
            or not int(item.get("source_mask_raw", 0)) & source_bit
        ):
            continue
        start = int(item["page_index_start"])
        result.update(range(start, start + int(item["page_count"])))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=512)
    parser.add_argument("--selected-pages", type=int, default=128)
    parser.add_argument("--minimum-recall", type=float, default=0.99)
    args = parser.parse_args()
    if os.geteuid() != 0:
        print("run with sudo: Page Idle and pagemap require root", file=sys.stderr)
        return 2
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    total_pages = max(32, int(args.pages))
    selected_count = min(total_pages // 2, max(2, int(args.selected_pages)))

    helper = object.__new__(EBPFFileEventHelper)
    helper.bpf_source = SERVICE_ROOT / "runtime_monitor/ebpf/file_events.bpf.c"
    helper.event_profile = "page-access-window"
    helper.page_capture = None
    helper.page_capture_target_app = "WPS"
    helper.tag_owners = {1: ("WPS", "gui")}
    helper.page_size = page_size
    helper.perf_lost = 0
    helper.file_perf_lost = 0
    helper.cache_perf_lost = 0
    helper.workload_perf_lost = 0
    helper.bpf = None

    with tempfile.TemporaryDirectory(prefix="parp-page-access-") as tmp:
        root = Path(tmp)
        data_path = root / "synthetic.bin"
        fd = os.open(data_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.ftruncate(fd, total_pages * page_size)
        mapping = mmap.mmap(fd, total_pages * page_size, access=mmap.ACCESS_WRITE)
        # 先让全部文件页驻留，再开始窗口；这不会把初始 fault 混进测试窗口。
        for page in range(total_pages):
            mapping[page * page_size] = page & 0xFF
        mapping.flush()

        names = ["windows.bin", "lifecycle.bin", "catalog.jsonl", "summary.csv", "manifest.json"]
        output_fds = [
            os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            for name in names
        ]
        capture = PageAccessWindowCapture(
            window_fd=output_fds[0], lifecycle_fd=output_fds[1],
            catalog_fd=output_fds[2], summary_fd=output_fds[3],
            manifest_fd=output_fds[4], session_id="synthetic",
            target_app="WPS", app_id=1, window_ms=1000,
            lock_path=root / "page-idle.lock",
        )
        helper.page_capture = capture
        try:
            helper._load_bpf()
            assert helper.bpf is not None
            helper.bpf["page_hotset_only"][ctypes.c_int(0)] = ctypes.c_uint32(2)
            helper.bpf["target_tgids"][ctypes.c_uint32(os.getpid())] = ctypes.c_uint32(1)
            helper.bpf["target_tids"][ctypes.c_uint32(os.getpid())] = ctypes.c_uint32(1)
            helper.bpf["page_capture_tags"][ctypes.c_uint32(1)] = ctypes.c_ubyte(1)
            capture.update_processes({os.getpid(): ("WPS", "gui", "synthetic")})
            helper._clear_page_capture_maps()
            capture.arm_initial_window()
            helper.bpf["page_window_enabled"][ctypes.c_int(0)] = ctypes.c_uint32(1)

            mmap_pages = set(range(0, selected_count, 2))
            pread_pages = set(range(1, selected_count, 2))
            expected = mmap_pages | pread_pages
            for page in mmap_pages:
                _ = mapping[page * page_size]
            for page in pread_pages:
                os.pread(fd, 1, page * page_size)
            # 同一秒重复访问不能生成第二条训练记录。
            for page in sorted(expected)[:16]:
                _ = mapping[page * page_size]
                os.pread(fd, 1, page * page_size)
            deadline = capture.window_start_boot_ns + capture.window_ns
            while time.monotonic_ns() < deadline:
                helper.bpf.perf_buffer_poll(timeout=50)
            rows, overflows, dirty = helper._rotate_and_drain_page_maps(reenable=False)
            helper.bpf.perf_buffer_poll(timeout=50)
            record = capture.rotate(
                rows, bpf_map_overflows=overflows, dirty_pids=dirty
            )
            values = data_path.stat()
            key = FilePageKey(
                os.major(values.st_dev), os.minor(values.st_dev), values.st_ino, 0
            )
            catalog_id = capture.catalog.resolve(key, str(data_path))
            observed = expand_file_pages(record, catalog_id)
            directly_observed = expand_pages_with_source(record, catalog_id, 1 << 1)
            true_positive = len(expected & observed)
            false_positive = len(observed - expected)
            recall = true_positive / len(expected)
            result = {
                "expected_pages": len(expected),
                "observed_pages": len(observed),
                "true_positive_pages": true_positive,
                "false_positive_pages": false_positive,
                "direct_pread_pages": len(pread_pages & directly_observed),
                "expected_direct_pread_pages": len(pread_pages),
                "recall": recall,
                "minimum_recall": args.minimum_recall,
                "bpf_map_overflows": overflows,
                "window_valid": bool(record.get("quality", {}).get("valid")),
                "invalid_reasons": record.get("quality", {}).get("invalid_reasons", []),
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            passed = (
                recall >= args.minimum_recall
                and false_positive == 0
                and pread_pages <= directly_observed
                and overflows == 0
                and result["window_valid"]
            )
            return 0 if passed else 1
        finally:
            capture.close()
            if helper.bpf is not None:
                helper.bpf.cleanup()
            mapping.close()
            os.close(fd)


if __name__ == "__main__":
    raise SystemExit(main())
