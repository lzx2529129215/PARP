"""Native ideal-output end-to-end check. No learned models or real GUI apps."""
from dataclasses import asdict
import errno
import hashlib
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid

from oracle_scenarios import APPS, MIB, Scenario
from study_pipeline import ROOT, Pipeline
from native import NativeCgroup
from executor import Guard
from prepeak_timing import release_profile


def dump(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def stats(path):
    return {k: int(v) for k, v in (line.split() for line in path.read_text().splitlines())}


class Worker:
    def __init__(self, leaf, directory, log_path):
        self.directory = directory
        self.log = log_path.open("wb")
        self.p = subprocess.Popen([sys.executable, "-B", "-u", str(ROOT / "memory_worker.py"),
                                   "--leaf", str(leaf), "--directory", str(directory)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                                  text=True, bufsize=1)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.p.stdout, selectors.EVENT_READ)
        try:
            self.ready = self.receive()
        except BaseException:
            if self.p.poll() is None:
                self.p.terminate()
                self.p.wait(timeout=5)
            self.selector.close()
            self.log.close()
            raise

    def receive(self, timeout=25):
        if not self.selector.select(timeout):
            raise TimeoutError("owned memory worker did not respond; inspect worker.log")
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("owned memory worker exited; inspect worker.log")
        return json.loads(line)

    def command(self, action, size=0):
        self.p.stdin.write(json.dumps({"action": action, "bytes": size}) + "\n")
        self.p.stdin.flush()
        return self.receive()

    def close(self):
        if self.p.poll() is None:
            self.p.stdin.write('{"action":"stop"}\n')
            self.p.stdin.flush()
            self.p.stdin.close()
            self.p.wait(timeout=10)
        self.selector.close()
        self.log.close()
        if self.p.returncode:
            raise RuntimeError("owned memory worker failed: " + str(self.p.returncode))


class MeasuredCgroup(NativeCgroup):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reclaim_calls = []

    def reclaim(self, amount):
        begin, before, error = time.monotonic_ns(), self.usage(), None
        try:
            return super().reclaim(amount)
        except OSError as exc:
            error = repr(exc)
            raise
        finally:
            self.reclaim_calls.append({"requested_bytes": amount, "begin_ns": begin,
                                       "end_ns": time.monotonic_ns(), "before_bytes": before,
                                       "after_bytes": self.usage(), "error": error})


class Case:
    def __init__(self, base, folder, scenario):
        self.path = base / ("case-" + uuid.uuid4().hex[:10])
        self.path.mkdir()
        (self.path / "cgroup.subtree_control").write_text("+memory")
        (self.path / "memory.swap.max").write_text("0")
        self.workers, self.backends, self.empty = {}, {}, {}
        self.local = tempfile.TemporaryDirectory(prefix="parp-ideal-data-")
        try:
            self._load(folder, scenario)
        except BaseException:
            self.close()
            raise

    def _load(self, folder, scenario):
        for app in APPS:
            leaf = self.path / app
            leaf.mkdir()
            directory = folder / app
            directory.mkdir()
            # Workload storage is on guest-local /tmp, not the shared filesystem.
            data_dir = Path(self.local.name) / app
            data_dir.mkdir()
            worker = Worker(leaf, data_dir, directory / "worker.log")
            self.workers[app] = worker
            self.backends[app] = MeasuredCgroup(leaf, background=True)
            self.empty[app] = {"rss": self.backends[app].rss(), "usage": self.backends[app].usage()}
        self.loads = {"cold": self.workers["cold"].command("cold", scenario.cold_bytes),
                      "hot": self.workers["hot"].command("hot", scenario.hot_bytes),
                      "incoming": self.workers["incoming"].command("hot", scenario.incoming_base_bytes)}
        time.sleep(.5)
        self.initial = {a: {"rss": b.rss(), "usage": b.usage()} for a, b in self.backends.items()}
        cold = self.backends["cold"]
        cold_stats = stats(cold.path / "memory.stat")
        if (self.initial["cold"]["rss"] - self.empty["cold"]["rss"] < .95 * scenario.cold_bytes
                or cold_stats.get("file", 0) < .95 * scenario.cold_bytes):
            raise RuntimeError("cold memory is not actually resident/charged; no formal trial admitted")
        if cold_stats.get("file_dirty", 0) > MIB or cold_stats.get("file_writeback", 0) > MIB:
            raise RuntimeError("known-cold clean-file fixture is not clean")

    def usage(self):
        return int((self.path / "memory.current").read_text())

    def close(self):
        errors = []
        for worker in self.workers.values():
            try:
                worker.close()
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                errors.append(str(error))
        for app in APPS:
            leaf = self.path / app
            if leaf.exists():
                if (leaf / "cgroup.procs").read_text().strip():
                    raise RuntimeError("owned process remains; preserved experiment subtree " + str(self.path))
                leaf.rmdir()
        self.path.rmdir()
        self.local.cleanup()
        if errors:
            raise RuntimeError("; ".join(errors))


def calibrate(base, output, scenario):
    directory = output / "physical_calibration"
    directory.mkdir()
    case = Case(base, directory, scenario)
    try:
        backend = case.backends["cold"]
        begin_stats = stats(backend.path / "memory.stat")
        before = backend.usage()
        requested, writes, batch = 0, [], 8 * MIB
        while requested < 384 * MIB:
            amount = min(batch, 384 * MIB - requested)
            usage = backend.usage()
            start = time.monotonic_ns()
            error = None
            try:
                backend.reclaim(amount)
            except OSError as exc:
                if exc.errno != errno.EAGAIN:
                    raise
                error = str(exc)
            end = time.monotonic_ns()
            after = backend.usage()
            writes.append({"name": "memory.reclaim", "value": amount, "start_ns": start,
                           "end_ns": end, "before_bytes": usage, "after_bytes": after, "error": error})
            requested += amount
            batch = min(32 * MIB, batch * 2)
            time.sleep(.1)
        time.sleep(.5)
        after = backend.usage()
        faults = max(0, stats(backend.path / "memory.stat")["pgfault"] - begin_stats["pgfault"])
        approved = min(requested, max(0, before - after))
        profile = release_profile(SimpleNamespace(observed_net_release_bytes=approved, requested_bytes=requested),
                                  {"calibration_writes": writes}, Guard(), .6)
        result = {"measurement_id": "native-clean-cold-" + uuid.uuid4().hex,
                  "approved_bytes": approved, "safe_rss": backend.rss(), "safe_usage": after,
                  "idle_faults": faults, "profile": asdict(profile), "writes": writes,
                  "before_bytes": before, "after_bytes": after, "requested_bytes": requested,
                  "fixture_initial": case.initial, "stage": "physical_calibration_not_scored"}
        dump(directory / "result.json", result)
        if approved < 320 * MIB or faults > 64 or profile.bytes_per_s <= 0:
            raise RuntimeError("physical reclaim capability not qualified; formal trials not started")
        bound = (scenario.incoming_growth_bytes + scenario.reserve_bytes - scenario.initial_headroom_bytes) / profile.bytes_per_s
        if bound + profile.batch_bound_s + 1. > scenario.peak_s - .5:
            raise RuntimeError("measured whole-gap time does not fit the preset lead; formal trials not started")
        return result, profile
    finally:
        case.close()


