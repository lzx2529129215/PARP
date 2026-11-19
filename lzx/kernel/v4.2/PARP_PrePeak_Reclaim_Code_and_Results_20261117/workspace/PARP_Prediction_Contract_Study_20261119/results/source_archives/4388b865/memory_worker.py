"""Small controlled applications: wholly cold clean data or wholly hot data."""
import argparse
import json
import mmap
import os
from pathlib import Path
import signal
import sys
import threading
import time


def counters():
    raw = Path("/proc/self/stat").read_text()
    fields = raw[raw.rfind(")") + 2:].split()
    return {"rss": int(fields[21]) * os.sysconf("SC_PAGE_SIZE"),
            "minor": int(fields[7]), "major": int(fields[9]),
            "process_cpu_ms": (int(fields[11])+int(fields[12]))*1000/os.sysconf("SC_CLK_TCK")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--leaf", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    (args.leaf / "cgroup.procs").write_text(str(os.getpid()))
    regions, files, stopping = [], [], threading.Event()
    lock = threading.RLock()
    sweep_limit = [2**63-1]
    checksum = [0]
    def sweep():
        while not stopping.is_set():
            with lock:
                left = sweep_limit[0]
                for memory, hot in regions:
                    if hot and left > 0:
                        for offset in range(0, min(len(memory), left), 4096):
                            checksum[0] ^= memory[offset]
                        left -= len(memory)
            stopping.wait(.05)
    thread = threading.Thread(target=sweep, daemon=True)
    thread.start()
    print(json.dumps({"event": "ready", "pid": os.getpid(), **counters()}), flush=True)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            if request["action"] == "stop":
                break
            if request["action"] == "snapshot":
                print(json.dumps({"event": "snapshot", **counters()}), flush=True)
                continue
            if request["action"] == "sweep_limit":
                sweep_limit[0] = request["bytes"]
                print(json.dumps({"event": "sweep_limit_set"}), flush=True)
                continue
            begin = time.monotonic_ns()
            begin_cpu = time.process_time_ns()
            size = request["bytes"]
            if request["action"] == "cold":
                filename = args.directory / ("cold-" + str(len(files)) + ".bin")
                stream = filename.open("w+b")
                stream.truncate(size)
                memory = mmap.mmap(stream.fileno(), size, access=mmap.ACCESS_READ)
                for offset in range(0, size, 4096):
                    checksum[0] ^= memory[offset]
                if hasattr(mmap, "MADV_COLD"):
                    memory.madvise(mmap.MADV_COLD)
                files.append((stream, filename))
                regions.append((memory, False))
            elif request["action"] == "hot":
                # Small independent allocations permit real application frees
                # for V/oscillating fixtures without using reclaim as a substitute.
                if size % (8*1024**2):
                    raise ValueError("fixture allocations must be 8 MiB aligned")
                for _ in range(size//(8*1024**2)):
                    memory = mmap.mmap(-1, 8*1024**2, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
                    memory.madvise(mmap.MADV_NOHUGEPAGE)
                    for offset in range(0, len(memory), 4096):
                        memory[offset] = 37
                    with lock:
                        regions.append((memory, True))
            elif request["action"] == "release_hot":
                with lock:
                    left = size
                    for index in range(len(regions)-1, -1, -1):
                        memory, hot = regions[index]
                        if hot and len(memory) <= left:
                            left -= len(memory)
                            regions.pop(index)
                            memory.close()
                        if not left:
                            break
                    if left:
                        raise ValueError("requested self-free exceeds owned hot blocks")
            elif request["action"] == "revisit":
                with lock:
                    for memory, hot in regions:
                        if not hot:
                            for offset in range(0, len(memory), 4096):
                                checksum[0] ^= memory[offset]
            else:
                raise ValueError("unknown workload action")
            print(json.dumps({"event": "complete", "action": request["action"], "bytes": size,
                              "elapsed_ms": (time.monotonic_ns() - begin) / 1e6,
                              "cpu_elapsed_ms": (time.process_time_ns()-begin_cpu)/1e6, **counters()}), flush=True)
    finally:
        stopping.set()
        thread.join(timeout=3)
        for memory, _ in regions:
            memory.close()
        for stream, filename in files:
            stream.close()
            filename.unlink()


if __name__ == "__main__":
    main()
