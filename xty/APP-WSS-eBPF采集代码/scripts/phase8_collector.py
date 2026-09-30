#!/usr/bin/env python3
"""Phase-8 aligned collector with PageFault Total and handler-path counts.

The unchanged exceptions/page_fault_user program remains the authority for
Total. kprobes add handler-path observations; they are not unique page types.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import select
import signal
import statistics
import subprocess
import threading
import time
from pathlib import Path


SMAPS_HEADER = re.compile(
    r"^[0-9a-fA-F]+-[0-9a-fA-F]+\s+[rwxps-]{4}\s+[0-9a-fA-F]+\s+"
    r"[0-9a-fA-F]+:[0-9a-fA-F]+\s+\d+\s*(.*)$")


def cgroup_pids(root: Path) -> list[int]:
    root = root.resolve()
    values: set[int] = set()
    paths = [root]
    try:
        paths.extend(sorted(item for item in root.rglob("*") if item.is_dir()))
    except (FileNotFoundError, PermissionError, OSError):
        pass
    for path in paths:
        try:
            text = (path / "cgroup.procs").read_text(encoding="ascii")
        except (FileNotFoundError, PermissionError, OSError):
            continue
        values.update(int(value) for value in text.split() if value.isdigit())
    return sorted(values)


def read_number(path: Path):
    try:
        text = path.read_text(encoding="ascii").strip()
        return int(text) if text != "max" else None
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return None


def read_pairs(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            fields = line.split()
            if len(fields) >= 2:
                try:
                    result[fields[0]] = int(fields[1])
                except ValueError:
                    pass
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return result


def read_meminfo() -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return result
    for line in lines:
        fields = line.replace(":", " ", 1).split()
        if len(fields) >= 2:
            try:
                result[fields[0]] = int(fields[1]) * 1024
            except ValueError:
                pass
    return result


def read_psi(path: Path) -> dict[str, float | int]:
    result: dict[str, float | int] = {}
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return result
    for line in lines:
        fields = line.split()
        if not fields:
            continue
        kind = fields[0]
        for field in fields[1:]:
            if "=" not in field:
                continue
            key, raw = field.split("=", 1)
            try:
                result[f"{kind}_{key}"] = int(raw) if key == "total" else float(raw)
            except ValueError:
                pass
    return result


def read_pid_rss(pid: int, page_size: int) -> dict[str, int] | None:
    status: dict[str, int] = {}
    try:
        for line in Path(f"/proc/{pid}/status").read_text(
                encoding="ascii", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            if key in {"VmRSS", "RssAnon", "RssFile", "RssShmem", "VmSwap"}:
                fields = rest.split()
                if fields:
                    status[key] = int(fields[0]) * 1024
        statm = Path(f"/proc/{pid}/statm").read_text(encoding="ascii").split()
        status["StatmRSS"] = int(statm[1]) * page_size
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError,
            ValueError, IndexError):
        return None
    if not {"VmRSS", "RssAnon", "RssFile", "RssShmem"}.issubset(status):
        return None
    return status


def read_proc_cpu(pid: int):
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = text[text.rfind(")") + 2:].split()
        return int(fields[11]) + int(fields[12])
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError,
            ValueError, IndexError):
        return None


def proc_fault_snapshot(cgroup: Path) -> dict[str, int]:
    minor = major = successes = failures = 0
    for pid in cgroup_pids(cgroup):
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = text[text.rfind(")") + 2:].split()
            minor += int(fields[7])
            major += int(fields[9])
            successes += 1
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError,
                ValueError, IndexError):
            failures += 1
    return {"proc_minor_fault_cumulative": minor,
            "proc_major_fault_cumulative": major,
            "proc_fault_process_count": successes,
            "proc_fault_pid_read_failures": failures}


def rss_snapshot(cgroup: Path, page_size: int) -> dict[str, int]:
    started = time.monotonic_ns()
    pids = cgroup_pids(cgroup)
    totals = {"rss_statm_bytes": 0, "rss_total_bytes": 0,
              "rss_anon_bytes": 0, "rss_file_bytes": 0,
              "rss_shmem_bytes": 0, "rss_swap_bytes": 0}
    successes = 0
    failures = 0
    for pid in pids:
        values = read_pid_rss(pid, page_size)
        if values is None:
            failures += 1
            continue
        successes += 1
        totals["rss_statm_bytes"] += values.get("StatmRSS", 0)
        totals["rss_total_bytes"] += values.get("VmRSS", 0)
        totals["rss_anon_bytes"] += values.get("RssAnon", 0)
        totals["rss_file_bytes"] += values.get("RssFile", 0)
        totals["rss_shmem_bytes"] += values.get("RssShmem", 0)
        totals["rss_swap_bytes"] += values.get("VmSwap", 0)
    totals.update({
        "rss_reconciliation_error_bytes":
            totals["rss_total_bytes"] - totals["rss_anon_bytes"]
            - totals["rss_file_bytes"] - totals["rss_shmem_bytes"],
        "rss_statm_vs_status_error_bytes":
            totals["rss_statm_bytes"] - totals["rss_total_bytes"],
        "rss_process_count": successes,
        "rss_pid_read_failures": failures,
        "rss_collection_cost_us": (time.monotonic_ns() - started) // 1000,
        "cgroup_pid_count": len(pids),
    })
    return totals


def mapping_kind(pathname: str) -> str:
    value = pathname.strip()
    if not value or value.startswith("[heap]") or value.startswith("[stack") \
            or value.startswith("[anon:"):
        return "anon"
    lowered = value.lower()
    if lowered.startswith("/dev/shm/") or "memfd:" in lowered \
            or lowered.startswith("/sysv") or value.startswith("[shmem"):
        return "shmem"
    if value.startswith("/"):
        return "file"
    if value.startswith("["):
        return "other"
    return "unknown"


def parse_smaps(pid: int) -> dict[str, int]:
    totals = {
        "size_kib": 0, "rss_kib": 0, "pss_kib": 0, "swap_kib": 0,
        "referenced_total_kib": 0, "referenced_anon_kib": 0,
        "referenced_file_kib": 0, "referenced_shmem_kib": 0,
        "referenced_other_kib": 0, "referenced_unknown_kib": 0,
        "mixed_file_vma_count": 0,
    }
    current = None

    def flush(vma):
        if not vma:
            return
        referenced = vma.get("Referenced", 0)
        kind = vma["kind"]
        if kind == "file":
            rss = vma.get("Rss", 0)
            anonymous = vma.get("Anonymous", 0)
            if anonymous == 0:
                kind = "file"
            elif rss > 0 and anonymous >= rss:
                kind = "anon"
            else:
                kind = "unknown"
                totals["mixed_file_vma_count"] += 1
        totals[f"referenced_{kind}_kib"] += referenced

    with Path(f"/proc/{pid}/smaps").open(
            encoding="utf-8", errors="replace") as stream:
        for line in stream:
            header = SMAPS_HEADER.match(line)
            if header:
                flush(current)
                current = {"kind": mapping_kind(header.group(1))}
                continue
            if current is None or ":" not in line:
                continue
            key, rest = line.split(":", 1)
            if key not in {"Size", "Rss", "Pss", "Swap", "Referenced", "Anonymous"}:
                continue
            try:
                value = int(rest.split()[0])
            except (ValueError, IndexError):
                continue
            current[key] = value
            if key == "Size":
                totals["size_kib"] += value
            elif key == "Rss":
                totals["rss_kib"] += value
            elif key == "Pss":
                totals["pss_kib"] += value
            elif key == "Swap":
                totals["swap_kib"] += value
            elif key == "Referenced":
                totals["referenced_total_kib"] += value
        flush(current)
    return totals


def referenced_snapshot(pids: list[int]) -> tuple[dict[str, int], list[str], int]:
    started = time.monotonic_ns()
    totals = {
        "size_kib": 0, "rss_kib": 0, "pss_kib": 0, "swap_kib": 0,
        "referenced_total_kib": 0, "referenced_anon_kib": 0,
        "referenced_file_kib": 0, "referenced_shmem_kib": 0,
        "referenced_other_kib": 0, "referenced_unknown_kib": 0,
        "mixed_file_vma_count": 0,
    }
    errors = []
    for pid in pids:
        try:
            values = parse_smaps(pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError) as exc:
            errors.append(f"pid={pid}:{type(exc).__name__}:{exc}")
            continue
        for key in totals:
            totals[key] += values[key]
    cost_us = (time.monotonic_ns() - started) // 1000
    return totals, errors, cost_us


def clear_refs(pids: list[int]) -> list[str]:
    errors = []
    for pid in pids:
        result = subprocess.run(
            ["sudo", "-n", "tee", f"/proc/{pid}/clear_refs"], input="1\n",
            text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if result.returncode:
            errors.append(f"pid={pid}:{result.stderr.strip()}")
    return errors


def cgroup_snapshot(prefix: str, cgroup: Path) -> dict[str, int | None]:
    stat = read_pairs(cgroup / "memory.stat")
    events = read_pairs(cgroup / "memory.events")
    result: dict[str, int | None] = {
        f"{prefix}_memory_current_bytes": read_number(cgroup / "memory.current"),
        f"{prefix}_memory_swap_bytes": read_number(cgroup / "memory.swap.current"),
    }
    for name in ("anon", "file", "shmem", "active_anon", "inactive_anon",
                 "active_file", "inactive_file", "pgfault", "pgmajfault",
                 "workingset_refault_anon", "workingset_refault_file"):
        result[f"{prefix}_stat_{name}"] = stat.get(name)
    for name in ("low", "high", "max", "oom", "oom_kill"):
        result[f"{prefix}_event_{name}"] = events.get(name)
    return result


def process_alive(pid: int | None) -> bool | None:
    if not pid:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def sample_once(args, page_size: int) -> dict:
    started = time.monotonic_ns()
    row = {
        "timestamp_ns": time.time_ns(),
        "timestamp_monotonic_ns": started,
        "run_id": args.run_id,
        "test_type": args.test_type,
        "application": args.application,
        "operation_id": args.operation_id,
        "operation_name": args.operation_name,
        "pressure_target_gib": args.pressure_target_gib,
        "page_size_bytes": page_size,
        "cgroup_path": str(args.cgroup),
    }
    row.update(rss_snapshot(args.cgroup, page_size))
    row.update(proc_fault_snapshot(args.cgroup))
    row.update(cgroup_snapshot("target", args.cgroup))
    cgroup_psi = read_psi(args.cgroup / "memory.pressure")
    system_psi = read_psi(Path("/proc/pressure/memory"))
    meminfo = read_meminfo()
    for key, value in cgroup_psi.items():
        row[f"target_psi_{key}"] = value
    for key, value in system_psi.items():
        row[f"system_psi_{key}"] = value
    row["system_mem_available_bytes"] = meminfo.get("MemAvailable")
    row["system_swap_free_bytes"] = meminfo.get("SwapFree")
    if args.pressure_cgroup:
        row.update(cgroup_snapshot("pressure", args.pressure_cgroup))
        row["pressure_process_alive"] = process_alive(args.pressure_pid)
    row["sample_collection_cost_us"] = (time.monotonic_ns() - started) // 1000
    return row


class Sampler(threading.Thread):
    def __init__(self, args, page_size: int, deadline_ns: int):
        super().__init__(daemon=True)
        self.args = args
        self.page_size = page_size
        self.deadline_ns = deadline_ns
        self.rows: list[dict] = []
        self.error = ""

    def run(self):
        try:
            interval_ns = self.args.interval_ms * 1_000_000
            next_ns = time.monotonic_ns()
            sequence = 0
            while next_ns <= self.deadline_ns:
                delay = next_ns - time.monotonic_ns()
                if delay > 0:
                    time.sleep(delay / 1e9)
                sequence += 1
                row = sample_once(self.args, self.page_size)
                row["sample_seq"] = sequence
                self.rows.append(row)
                next_ns += interval_ns
        except Exception as exc:  # evidence is preserved in metadata
            self.error = f"{type(exc).__name__}: {exc}"


def wait_collector(process, timeout=15) -> str:
    deadline = time.monotonic() + timeout
    lines = []
    while time.monotonic() < deadline:
        readable, _, _ = select.select([process.stdout], [], [], 0.25)
        if readable:
            line = process.stdout.readline()
            if line:
                lines.append(line)
                if line.startswith("collector_ready "):
                    return line.strip()
        if process.poll() is not None:
            break
    stderr = process.stderr.read() if process.poll() is not None else ""
    raise RuntimeError(f"collector not ready: {''.join(lines)} {stderr}")


def stop_process(process) -> None:
    if process.poll() is not None:
        return
    subprocess.run(["sudo", "-n", "kill", "-TERM", str(process.pid)],
                   check=False, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        subprocess.run(["sudo", "-n", "kill", "-KILL", str(process.pid)],
                       check=False, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        process.wait(timeout=2)


def write_csv(path: Path, rows: list[dict]) -> None:
    keys = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def values(rows: list[dict], key: str) -> list[float]:
    return [float(row[key]) for row in rows if row.get(key) not in (None, "")]


def mean_or_none(rows: list[dict], key: str):
    found = values(rows, key)
    return statistics.fmean(found) if found else None


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--test-type", required=True)
    parser.add_argument("--application", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--operation-name", required=True)
    parser.add_argument("--pressure-target-gib", type=int, default=0)
    parser.add_argument("--pressure-cgroup", type=Path)
    parser.add_argument("--pressure-pid", type=int)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--window-ms", type=int, default=5000)
    parser.add_argument("--interval-ms", type=int, default=500)
    parser.add_argument("trigger", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.project = args.project.resolve()
    args.cgroup = args.cgroup.resolve()
    if args.pressure_cgroup:
        args.pressure_cgroup = args.pressure_cgroup.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    raw_path = args.output_dir / "lightweight_features_v2.csv"
    stdout_path = args.output_dir / "collector_stdout.log"
    stderr_path = args.output_dir / "collector_stderr.log"
    page_size = os.sysconf("SC_PAGE_SIZE")
    duration_seconds = (args.window_ms + 999) // 1000 + 2
    collector_cmd = [
        "sudo", "-n", str(args.project / "build/app_fault_collector"),
        "--bpf-object", str(args.project / "build/app_fault.bpf.o"),
        "--cgroup", str(args.cgroup), "--app-id", args.application.lower(),
        "--app-name", args.application, "--run-id", args.run_id,
        "--operation-id", args.operation_id,
        "--interval-ms", str(args.interval_ms),
        "--duration-seconds", str(duration_seconds), "--output", str(raw_path),
    ]
    collector = subprocess.Popen(collector_cmd, text=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, bufsize=1)
    metadata = {"run_id": args.run_id, "collector_command": collector_cmd,
                "kernel_version": os.uname().release,
                "page_size_bytes": page_size, "status": "FAIL"}
    sampler = None
    try:
        ready = wait_collector(collector)
        metadata["collector_ready"] = ready
        time.sleep(args.interval_ms / 1000 + 0.1)
        collector_cpu_start = read_proc_cpu(collector.pid)
        t0_pids = cgroup_pids(args.cgroup)
        if not t0_pids:
            raise RuntimeError("target cgroup has no processes at T0")
        clear_errors = clear_refs(t0_pids)
        window_start_ns = time.monotonic_ns()
        window_start_wall_ns = time.time_ns()
        deadline_ns = window_start_ns + args.window_ms * 1_000_000
        sampler = Sampler(args, page_size, deadline_ns)
        sampler.start()
        trigger = args.trigger[1:] if args.trigger and args.trigger[0] == "--" else args.trigger
        trigger_result = subprocess.run(trigger, text=True, capture_output=True,
                                        check=False) if trigger else None
        remaining = deadline_ns - time.monotonic_ns()
        if remaining > 0:
            time.sleep(remaining / 1e9)
        window_end_ns = time.monotonic_ns()
        window_end_wall_ns = time.time_ns()
        sampler.join(timeout=3)
        t1_pids = cgroup_pids(args.cgroup)
        refs, smaps_errors, wss_cost_us = referenced_snapshot(
            sorted(set(t0_pids) | set(t1_pids)))
        collector_cpu_end = read_proc_cpu(collector.pid)
        stop_process(collector)
        stdout_path.write_text(ready + "\n" + collector.stdout.read(), encoding="utf-8")
        stderr_path.write_text(collector.stderr.read(), encoding="utf-8")
        write_csv(args.output_dir / "phase8_samples.csv", sampler.rows)

        with raw_path.open(newline="", encoding="utf-8") as stream:
            raw_rows = list(csv.DictReader(stream))
        raw_window = [row for row in raw_rows
                      if window_start_ns < int(row["timestamp_monotonic_ns"])
                      <= window_end_ns + args.interval_ms * 200_000]
        fault_total = sum(int(row["fault_total_delta"]) for row in raw_window)
        fault_anon_handler = sum(int(row["fault_anon_handler_delta"])
                                 for row in raw_window)
        fault_file_handler = sum(int(row["fault_file_handler_delta"])
                                 for row in raw_window)
        fault_wp_handler = sum(int(row["fault_wp_handler_delta"])
                               for row in raw_window)
        fault_shmem_handler = sum(int(row["fault_shmem_handler_delta"])
                                  for row in raw_window)
        anon_file_handler_sum = fault_anon_handler + fault_file_handler
        ref_component = sum(refs[f"referenced_{kind}_kib"]
                            for kind in ("anon", "file", "shmem", "other", "unknown"))
        ref_error_kib = refs["referenced_total_kib"] - ref_component
        rss_recon = values(sampler.rows, "rss_reconciliation_error_bytes")
        memory_pgfault = values(sampler.rows, "target_stat_pgfault")
        proc_minor = values(sampler.rows, "proc_minor_fault_cumulative")
        proc_major = values(sampler.rows, "proc_major_fault_cumulative")
        collector_costs = [float(row["collector_read_cost_us"])
                           for row in raw_window
                           if row.get("collector_read_cost_us") not in (None, "")]
        sample_costs = values(sampler.rows, "sample_collection_cost_us")
        clock_ticks = os.sysconf("SC_CLK_TCK")
        collector_cpu_ms = None
        if collector_cpu_start is not None and collector_cpu_end is not None:
            collector_cpu_ms = max(collector_cpu_end - collector_cpu_start, 0) \
                               * 1000 / clock_ticks
        monotonic_values = [int(row["timestamp_monotonic_ns"]) for row in raw_window]
        monotonic_ok = all(left < right for left, right in
                           zip(monotonic_values, monotonic_values[1:]))
        pressure_values = values(sampler.rows, "pressure_memory_current_bytes")
        pressure_actual = statistics.fmean(pressure_values) if pressure_values else None
        pressure_target = args.pressure_target_gib * 1024 ** 3
        pressure_stable = None
        if pressure_target:
            pressure_stable = bool(pressure_values and
                                   min(pressure_values) >= pressure_target * 0.90 and
                                   process_alive(args.pressure_pid))
        trigger_status = trigger_result.returncode if trigger_result else 0
        base_failures = bool(clear_errors or smaps_errors or sampler.error or
                             trigger_status != 0 or not raw_window or not monotonic_ok)
        status = "FAIL" if base_failures else "PASS"
        if status == "PASS" and pressure_stable is False:
            status = "WARN"
        unknown_ratio = (refs["referenced_unknown_kib"] /
                         refs["referenced_total_kib"]
                         if refs["referenced_total_kib"] else 0.0)
        summary = {
            "timestamp": window_end_wall_ns,
            "run_id": args.run_id, "test_type": args.test_type,
            "application": args.application, "operation_id": args.operation_id,
            "operation_name": args.operation_name,
            "pressure_target_gib": args.pressure_target_gib,
            "pressure_actual_bytes": pressure_actual,
            "pressure_actual_anon_bytes": mean_or_none(sampler.rows, "pressure_stat_anon"),
            "pressure_actual_file_bytes": mean_or_none(sampler.rows, "pressure_stat_file"),
            "pressure_swap_bytes": mean_or_none(sampler.rows, "pressure_memory_swap_bytes"),
            "pressure_stable": pressure_stable,
            "page_size_bytes": page_size, "cgroup_path": str(args.cgroup),
            "pressure_cgroup_path": str(args.pressure_cgroup) if args.pressure_cgroup else "",
            "kernel_version": os.uname().release,
            "collector_version": "phase8-handler-path-v1",
            "window_start": window_start_ns, "window_end": window_end_ns,
            "actual_window_ms": (window_end_ns - window_start_ns) / 1e6,
            "sample_count": len(sampler.rows), "ebpf_sample_count": len(raw_window),
            "operation_status": "PASS" if trigger_status == 0 else "FAIL",
            "data_quality_status": status,
            "wss_referenced_total_bytes": refs["referenced_total_kib"] * 1024,
            "wss_referenced_anon_bytes": refs["referenced_anon_kib"] * 1024,
            "wss_referenced_file_bytes": refs["referenced_file_kib"] * 1024,
            "wss_referenced_shmem_bytes": refs["referenced_shmem_kib"] * 1024,
            "wss_referenced_other_bytes": refs["referenced_other_kib"] * 1024,
            "wss_referenced_unknown_bytes": refs["referenced_unknown_kib"] * 1024,
            "wss_reconciliation_error_bytes": ref_error_kib * 1024,
            "wss_unknown_ratio": unknown_ratio,
            "wss_collection_cost_us": wss_cost_us,
            "wss_pid_read_failures": len(smaps_errors),
            "wss_split_method": "VMA_AND_RESIDENCY_ASSISTED_APPROXIMATION",
            "rss_total_bytes": mean_or_none(sampler.rows, "rss_total_bytes"),
            "rss_anon_bytes": mean_or_none(sampler.rows, "rss_anon_bytes"),
            "rss_file_bytes": mean_or_none(sampler.rows, "rss_file_bytes"),
            "rss_shmem_bytes": mean_or_none(sampler.rows, "rss_shmem_bytes"),
            "rss_statm_bytes": mean_or_none(sampler.rows, "rss_statm_bytes"),
            "rss_reconciliation_error_bytes": statistics.fmean(rss_recon) if rss_recon else None,
            "rss_process_count": mean_or_none(sampler.rows, "rss_process_count"),
            "rss_pid_read_failures": sum(values(sampler.rows, "rss_pid_read_failures")),
            "rss_collection_cost_us": mean_or_none(sampler.rows, "rss_collection_cost_us"),
            "cgroup_memory_current_bytes": mean_or_none(sampler.rows, "target_memory_current_bytes"),
            "cgroup_memory_anon_bytes": mean_or_none(sampler.rows, "target_stat_anon"),
            "cgroup_memory_file_bytes": mean_or_none(sampler.rows, "target_stat_file"),
            "cgroup_memory_shmem_bytes": mean_or_none(sampler.rows, "target_stat_shmem"),
            "page_fault_total_count": fault_total,
            "page_fault_anon_handler_count": fault_anon_handler,
            "page_fault_file_handler_count": fault_file_handler,
            "page_fault_wp_handler_count": fault_wp_handler,
            "page_fault_shmem_handler_count": fault_shmem_handler,
            "anon_file_handler_sum": anon_file_handler_sum,
            "anon_file_vs_total_ratio": (anon_file_handler_sum / fault_total
                                         if fault_total else None),
            "anon_handler_vs_total_ratio": (fault_anon_handler / fault_total
                                             if fault_total else None),
            "file_handler_vs_total_ratio": (fault_file_handler / fault_total
                                             if fault_total else None),
            "page_fault_anon_count": None, "page_fault_file_count": None,
            "page_fault_shmem_count": None, "page_fault_cow_count": None,
            "page_fault_swap_count": None, "page_fault_other_count": None,
            "page_fault_unknown_count": None,
            "page_fault_retry_or_duplicate_count": None,
            "page_fault_lost_event_count": 0,
            "page_fault_lost_event_basis": "PERCPU_ARRAY_NO_EVENT_STREAM",
            "page_fault_classification_status": "HANDLER_PATH_OBSERVATION_NOT_EXACT_TYPE",
            "page_fault_basepage_equivalent_bytes": fault_total * page_size,
            "page_fault_total_4k_equivalent_bytes": (fault_total * page_size
                                                      if page_size == 4096 else None),
            "page_fault_anon_handler_4k_equivalent_bytes":
                (fault_anon_handler * page_size if page_size == 4096 else None),
            "page_fault_file_handler_4k_equivalent_bytes":
                (fault_file_handler * page_size if page_size == 4096 else None),
            "memory_stat_pgfault_delta": (max(memory_pgfault[-1] - memory_pgfault[0], 0)
                                          if len(memory_pgfault) >= 2 else None),
            "proc_minor_fault_delta": (max(proc_minor[-1] - proc_minor[0], 0)
                                       if len(proc_minor) >= 2 else None),
            "proc_major_fault_delta": (max(proc_major[-1] - proc_major[0], 0)
                                       if len(proc_major) >= 2 else None),
            "collector_cpu_ms": collector_cpu_ms,
            "collector_read_cost_us_mean": (statistics.fmean(collector_costs)
                                             if collector_costs else None),
            "collector_read_cost_us_max": (max(collector_costs)
                                            if collector_costs else None),
            "sample_collection_cost_us_mean": (statistics.fmean(sample_costs)
                                                if sample_costs else None),
            "sample_collection_cost_us_max": (max(sample_costs)
                                               if sample_costs else None),
            "system_mem_available_bytes": mean_or_none(sampler.rows, "system_mem_available_bytes"),
            "system_swap_free_bytes": mean_or_none(sampler.rows, "system_swap_free_bytes"),
            "system_psi_some_avg10": mean_or_none(sampler.rows, "system_psi_some_avg10"),
            "system_psi_full_avg10": mean_or_none(sampler.rows, "system_psi_full_avg10"),
            "clear_refs_error_count": len(clear_errors),
            "smaps_error_count": len(smaps_errors),
            "bpf_timestamps_monotonic": monotonic_ok,
            "collector_returncode": collector.returncode,
            "raw_source_path": str(raw_path.relative_to(args.project)),
            "sample_source_path": str((args.output_dir / "phase8_samples.csv").relative_to(args.project)),
            "notes": "Anon/File are handler-path counts, not an exact partition of Total.",
        }
        write_csv(args.output_dir / "phase8_summary.csv", [summary])
        (args.output_dir / "phase8_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        metadata.update({
            "status": status, "t0_pids": t0_pids, "t1_pids": t1_pids,
            "clear_errors": clear_errors, "smaps_errors": smaps_errors,
            "sampler_error": sampler.error, "trigger": trigger,
            "trigger_returncode": trigger_status,
            "trigger_stdout": trigger_result.stdout if trigger_result else "",
            "trigger_stderr": trigger_result.stderr if trigger_result else "",
            "window_start_ns": window_start_ns, "window_end_ns": window_end_ns,
            "window_start_wall_ns": window_start_wall_ns,
            "window_end_wall_ns": window_end_wall_ns,
        })
        (args.output_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        raise SystemExit(0 if status in {"PASS", "WARN"} else 1)
    except Exception as exc:
        stop_process(collector)
        metadata.update({"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"})
        (args.output_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise


if __name__ == "__main__":
    main()
