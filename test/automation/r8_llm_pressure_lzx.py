#!/usr/bin/env python3
"""Run a pinned llama.cpp server as R8's real LLM weight-load aggressor."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import mmap
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_int(path: Path) -> int:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        return 0


def read_kv(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            key, value = line.split(maxsplit=1)
            values[key] = int(value)
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass
    return values


def own_cgroup() -> Path:
    for line in Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0":
            return Path("/sys/fs/cgroup") / fields[2].lstrip("/")
    raise RuntimeError("cannot resolve unified cgroup for LLM aggressor")


def process_cgroup(pid: int) -> str:
    for line in (Path("/proc") / str(pid) / "cgroup").read_text(encoding="ascii").splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0] == "0":
            return fields[2]
    return ""


def oom_score_adj(pid: int) -> int:
    return int((Path("/proc") / str(pid) / "oom_score_adj").read_text(encoding="ascii").strip())


def resident_file_bytes(path: Path) -> int:
    """Measure page-cache residency without faulting the file contents in."""
    size = path.stat().st_size
    page_size = int(os.sysconf("SC_PAGE_SIZE"))
    page_count = (size + page_size - 1) // page_size
    vector = (ctypes.c_ubyte * page_count)()
    libc = ctypes.CDLL(None, use_errno=True)
    with path.open("rb") as stream, mmap.mmap(
        stream.fileno(), 0, access=mmap.ACCESS_COPY,
    ) as mapping:
        probe = ctypes.c_char.from_buffer(mapping)
        address = ctypes.addressof(probe)
        result = libc.mincore(
            ctypes.c_void_p(address), ctypes.c_size_t(size), ctypes.byref(vector),
        )
        del probe
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
    resident_pages = sum(1 for value in vector if value & 1)
    return min(size, resident_pages * page_size)


def smaps_rollup(pids: list[int]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for pid in pids:
        try:
            lines = (Path("/proc") / str(pid) / "smaps_rollup").read_text(
                encoding="ascii", errors="replace",
            ).splitlines()
        except (FileNotFoundError, PermissionError, OSError):
            continue
        for line in lines:
            fields = line.split()
            if len(fields) >= 2 and fields[0].endswith(":") and fields[1].isdigit():
                key = fields[0][:-1]
                totals[key] = totals.get(key, 0) + int(fields[1]) * 1024
    return totals


def sample_scope(cgroup: Path) -> dict[str, Any]:
    try:
        pids = [int(value) for value in (cgroup / "cgroup.procs").read_text(encoding="ascii").split()]
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pids = []
    return {
        "timestamp_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
        "memory_current_bytes": read_int(cgroup / "memory.current"),
        "memory_peak_bytes": read_int(cgroup / "memory.peak"),
        "memory_events": read_kv(cgroup / "memory.events.local"),
        "pids": pids,
        "smaps_rollup_bytes": smaps_rollup(pids),
    }


def request_json(url: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="GET" if data is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        return json.loads(body.decode("utf-8")) if body else {}


def completion(port: int, prompt: str, seed: int, timeout: float) -> tuple[str, str]:
    candidates = [
        (
            f"http://127.0.0.1:{port}/completion",
            {"prompt": prompt, "n_predict": 1, "temperature": 0, "seed": seed, "stream": False},
        ),
        (
            f"http://127.0.0.1:{port}/v1/completions",
            {"prompt": prompt, "max_tokens": 1, "temperature": 0, "seed": seed, "stream": False},
        ),
    ]
    failures: list[str] = []
    for url, payload in candidates:
        try:
            result = request_json(url, payload, timeout)
            text = str(result.get("content", ""))
            if not text:
                choices = result.get("choices", [])
                if choices and isinstance(choices[0], dict):
                    text = str(choices[0].get("text", ""))
            if text:
                return url, text
            failures.append(f"{url}: response contains no generated token")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
    raise RuntimeError("; ".join(failures))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--runtime", type=Path, required=True)
    root.add_argument("--model", type=Path, required=True)
    root.add_argument("--runtime-sha256", required=True)
    root.add_argument("--gguf-sha256", required=True)
    root.add_argument("--model-size-bytes", type=int, required=True)
    root.add_argument("--prompt", required=True)
    root.add_argument("--context-length", type=int, required=True)
    root.add_argument("--threads", type=int, required=True)
    root.add_argument("--seed", type=int, required=True)
    root.add_argument("--port", type=int, required=True)
    root.add_argument("--load-timeout", type=float, required=True)
    root.add_argument("--completion-timeout", type=float, required=True)
    root.add_argument("--sample-interval", type=float, default=0.1)
    root.add_argument("--maximum-cached-ratio", type=float, required=True)
    root.add_argument("--state", type=Path, required=True)
    root.add_argument("--samples", type=Path, required=True)
    root.add_argument("--server-log", type=Path, required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    state: dict[str, Any] = {
        "schema_version": 1,
        "status": "VALIDATING",
        "runtime": str(args.runtime.resolve()),
        "model": str(args.model.resolve()),
        "runtime_sha256": "",
        "gguf_sha256": "",
        "model_size_bytes": 0,
        "model_cache_state": "cold_fadvise_dontneed",
        "no_mmap": True,
        "context_length": args.context_length,
        "threads": args.threads,
        "seed": args.seed,
        "prompt_sha256": hashlib.sha256(args.prompt.encode("utf-8")).hexdigest(),
        "wrapper_pid": os.getpid(),
    }
    atomic_json(args.state, state)
    server: subprocess.Popen[Any] | None = None
    log_stream: Any = None
    stop = threading.Event()
    peak = {"memory_current_bytes": 0, "memory_peak_bytes": 0}
    latest: dict[str, Any] = {}
    sample_lock = threading.Lock()
    sampler: threading.Thread | None = None

    def stop_handler(_signum: int, _frame: Any) -> None:
        stop.set()
        if server is not None and server.poll() is None:
            server.terminate()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    try:
        runtime = args.runtime.resolve(strict=True)
        model = args.model.resolve(strict=True)
        if not runtime.is_file() or not os.access(runtime, os.X_OK):
            raise RuntimeError("llama.cpp server runtime is not executable")
        if not model.is_file():
            raise RuntimeError("GGUF model is not a regular file")
        size = model.stat().st_size
        if size != args.model_size_bytes:
            raise RuntimeError(f"model size mismatch: expected={args.model_size_bytes} observed={size}")
        with model.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise RuntimeError("model does not have GGUF magic")
        runtime_hash = sha256_file(runtime)
        model_hash = sha256_file(model)
        if runtime_hash != args.runtime_sha256:
            raise RuntimeError("runtime SHA-256 mismatch")
        if model_hash != args.gguf_sha256:
            raise RuntimeError("GGUF SHA-256 mismatch")
        with model.open("rb") as stream:
            if not hasattr(os, "posix_fadvise"):
                raise RuntimeError("posix_fadvise is unavailable")
            os.posix_fadvise(stream.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        cached_bytes = resident_file_bytes(model)
        cached_ratio = cached_bytes / size
        if cached_ratio > args.maximum_cached_ratio:
            raise RuntimeError(
                "GGUF cold-cache gate failed: "
                f"resident={cached_bytes} size={size} ratio={cached_ratio:.6f} "
                f"maximum={args.maximum_cached_ratio:.6f}"
            )
        cgroup = own_cgroup()
        wrapper_score = oom_score_adj(os.getpid())
        if wrapper_score != 0:
            raise RuntimeError(f"LLM wrapper oom_score_adj is {wrapper_score}, expected 0")
        initial = sample_scope(cgroup)
        state.update({
            "status": "MODEL_LOAD_STARTED",
            "runtime_sha256": runtime_hash,
            "gguf_sha256": model_hash,
            "model_size_bytes": size,
            "cgroup": str(cgroup),
            "load_started_realtime_ns": time.time_ns(),
            "load_started_monotonic_ns": time.monotonic_ns(),
            "initial": initial,
            "fadvise_dontneed_applied": True,
            "cold_cache_gate": {
                "valid": True,
                "resident_bytes": cached_bytes,
                "model_size_bytes": size,
                "resident_ratio": cached_ratio,
                "maximum_ratio": args.maximum_cached_ratio,
            },
        })
        atomic_json(args.state, state)
        args.samples.parent.mkdir(parents=True, exist_ok=True)
        samples_stream = args.samples.open("w", encoding="utf-8")

        def sample_loop() -> None:
            while not stop.is_set():
                row = sample_scope(cgroup)
                with sample_lock:
                    latest.clear()
                    latest.update(row)
                    peak["memory_current_bytes"] = max(peak["memory_current_bytes"], int(row["memory_current_bytes"]))
                    peak["memory_peak_bytes"] = max(peak["memory_peak_bytes"], int(row["memory_peak_bytes"]))
                samples_stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                samples_stream.flush()
                stop.wait(max(0.02, args.sample_interval))

        sampler = threading.Thread(target=sample_loop, name="r8-llm-sampler", daemon=True)
        sampler.start()
        args.server_log.parent.mkdir(parents=True, exist_ok=True)
        log_stream = args.server_log.open("w", encoding="utf-8")
        command = [
            str(runtime), "--model", str(model), "--no-mmap",
            "--ctx-size", str(args.context_length), "--threads", str(args.threads),
            "--host", "127.0.0.1", "--port", str(args.port),
        ]
        server = subprocess.Popen(command, stdout=log_stream, stderr=subprocess.STDOUT, text=True)
        server_score = oom_score_adj(server.pid)
        server_cgroup = process_cgroup(server.pid)
        own_relative_cgroup = str(cgroup).removeprefix("/sys/fs/cgroup")
        if server_score != 0:
            raise RuntimeError(f"llama.cpp server oom_score_adj is {server_score}, expected 0")
        if server_cgroup != own_relative_cgroup:
            raise RuntimeError(
                f"llama.cpp server cgroup differs: expected={own_relative_cgroup} observed={server_cgroup}"
            )
        state["server_pid"] = server.pid
        state["runtime_argv"] = command
        state["oom_score_gate"] = {
            "valid": True,
            "expected": 0,
            "wrapper_pid": os.getpid(),
            "wrapper_oom_score_adj": wrapper_score,
            "server_pid": server.pid,
            "server_oom_score_adj": server_score,
            "cgroup": own_relative_cgroup,
            "server_cgroup": server_cgroup,
        }
        atomic_json(args.state, state)
        deadline = time.monotonic() + args.load_timeout
        health: dict[str, Any] = {}
        while time.monotonic() <= deadline and not stop.is_set():
            if server.poll() is not None:
                raise RuntimeError(f"llama.cpp server exited during model load: rc={server.returncode}")
            try:
                health = request_json(f"http://127.0.0.1:{args.port}/health", None, 1.0)
                break
            except (OSError, ValueError, urllib.error.URLError):
                time.sleep(0.1)
        else:
            raise RuntimeError("llama.cpp server model-load timeout")
        state.update({
            "status": "MODEL_LOAD_COMPLETE",
            "load_completed_realtime_ns": time.time_ns(),
            "load_completed_monotonic_ns": time.monotonic_ns(),
            "health": health,
        })
        atomic_json(args.state, state)
        endpoint, token = completion(args.port, args.prompt, args.seed, args.completion_timeout)
        final_sample = sample_scope(cgroup)
        with sample_lock:
            peak_current = max(peak["memory_current_bytes"], int(final_sample["memory_current_bytes"]))
            peak_memory = max(peak["memory_peak_bytes"], int(final_sample["memory_peak_bytes"]))
        initial_current = int(initial["memory_current_bytes"])
        state.update({
            "status": "FIRST_TOKEN_COMPLETE",
            "first_token_completed_realtime_ns": time.time_ns(),
            "first_token_completed_monotonic_ns": time.monotonic_ns(),
            "completion_endpoint": endpoint,
            "generated_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "generated_token_bytes": len(token.encode("utf-8")),
            "resident_after_load_bytes": max(0, int(final_sample["memory_current_bytes"]) - initial_current),
            "peak_memory_current_bytes": peak_current,
            "peak_memory_delta_bytes": max(0, peak_current - initial_current),
            "memory_peak_bytes": peak_memory,
            "final_sample": final_sample,
        })
        atomic_json(args.state, state)
        while not stop.wait(0.5):
            if server.poll() is not None:
                raise RuntimeError(f"llama.cpp server exited while holding weights: rc={server.returncode}")
        return 0
    except Exception as exc:
        state.update({
            "status": "MEMORY_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "failed_realtime_ns": time.time_ns(),
        })
        atomic_json(args.state, state)
        return 2
    finally:
        stop.set()
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        if sampler is not None:
            sampler.join(timeout=2)
        try:
            samples_stream.close()  # type: ignore[possibly-undefined]
        except (NameError, OSError):
            pass
        if log_stream is not None:
            log_stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
