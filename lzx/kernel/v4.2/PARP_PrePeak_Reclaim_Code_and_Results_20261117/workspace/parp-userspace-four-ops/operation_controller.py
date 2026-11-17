#!/usr/bin/env python3
"""Operation-table lab runner; independent of the fixed-five-second kernel ABI.

Uses the supplied GIMP table or a get_wss_predictor library. Forecasts are future operation stages,
not time windows. Only explicit harness `enter` events advance a stage.
Default is read/print only. --apply controls native cgroup v2 high/reclaim with
explicit experimental thresholds; it does not install a calibrated v5 profile.
"""
import argparse
import errno
import json
import math
import os
from pathlib import Path
import queue
import signal
import sys
import threading
import time

from operation_lookup import OperationLookup, TableOperationLookup, UNITS, integer

BATCH = 32 * 1024**2


def stable(values):
    return bool(values) and min(values) > 0 and max(values) - min(values) <= min(values) // 10


def high_target(value, page_size):
    integer(value, 1)
    target = integer(value + (value + 9) // 10)
    return integer(((target + page_size - 1) // page_size) * page_size)


def classify(result, rss, reuse_fault_max=None, turnover_burst_min=None):
    integer(rss, 1)
    w, f = result.wss_bytes, result.pagefault
    total = sum(f)
    burst = max(0, max(f[:2], default=0) - max(f[2:], default=0))
    decision = dict(mode="observe", reason="insufficient_stages", valid_count=len(w),
                    fault_total=total, fault_burst=burst)
    # One phase cannot establish stability; absent slots are never invented.
    if len(w) < 2:
        return decision
    if not min(w) or max(w) >= rss:
        decision["reason"] = "not_positive_or_not_below_rss"
    elif stable(w) and reuse_fault_max is not None and total <= reuse_fault_max:
        decision.update(mode="high", reason="experimental_reuse_candidate")
    elif (len(w) >= 3 and w[1] > w[0] and w[1] - w[0] > w[0] // 10
          and stable(w[1:]) and turnover_burst_min is not None
          and turnover_burst_min > 0 and burst >= turnover_burst_min):
        decision.update(mode="reclaim", reason="experimental_turnover_candidate")
    else:
        decision["reason"] = "ambiguous_or_threshold_missing"
    return decision


class NativeCgroup:
    """Real file I/O only. Tests inject a separate in-memory backend."""
    def __init__(self, directory):
        if sys.platform != "linux":
            raise ValueError("native cgroup writes require Linux")
        self.path = Path(directory).resolve(strict=True)
        for name in ("cgroup.controllers", "memory.current", "memory.high", "memory.reclaim",
                     "memory.max", "memory.min", "memory.low", "memory.stat"):
            if not (self.path / name).is_file():
                raise ValueError(f"missing cgroup v2 interface: {name}")
        # A real cgroup root may omit memory.current. Its controllers file is
        # present, so direct children are valid non-root targets as well.
        if not (self.path.parent / "cgroup.controllers").is_file():
            raise ValueError("a non-root cgroup is required")
        self.check_exclusive()

    def check_exclusive(self):
        enabled = self.path / "memory.tier2_enabled"
        if enabled.exists() and enabled.read_text().strip() != "0":
            raise RuntimeError("disable memory.tier2_enabled before using the operation runner")
        if any(p.is_dir() and (p / "memory.current").exists() for p in self.path.iterdir()):
            raise RuntimeError("the operation runner requires a leaf cgroup")

    def read(self, name):
        return (self.path / name).read_text(encoding="ascii").strip()

    def write(self, name, value):
        # Native high writes may perform synchronous reclaim. One complete write.
        data = (str(value) + "\n").encode("ascii")
        fd = os.open(self.path / name, os.O_WRONLY)
        try:
            if os.write(fd, data) != len(data):
                raise OSError("short cgroup write")
        finally:
            os.close(fd)

    def usage(self):
        return integer(int(self.read("memory.current")))

    def limit(self, name):
        value = self.read(name)
        return (1 << 64) - 1 if value == "max" else integer(int(value))

    def refaults(self):
        stats = dict(line.split() for line in self.read("memory.stat").splitlines())
        return sum(integer(int(stats.get(key, 0))) for key in
                   ("workingset_refault_anon", "workingset_refault_file"))


class OperationController:
    def __init__(self, lookup, backend=None, apply=False, reuse_fault_max=None,
                 turnover_burst_min=None, lease_seconds=30, batch_bytes=BATCH,
                 page_size=4096):
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("positive finite safety lease required")
        integer(page_size, 1)
        integer(batch_bytes, page_size, BATCH)
        self.lookup, self.backend, self.apply = lookup, backend, apply
        self.reuse_fault_max = (None if reuse_fault_max is None else integer(reuse_fault_max))
        self.turnover_burst_min = (None if turnover_burst_min is None
                                   else integer(turnover_burst_min, 1))
        if apply and (backend is None or reuse_fault_max is None or turnover_burst_min is None):
            raise ValueError("--apply needs a real cgroup and both explicit trial thresholds")
        self.page_size, self.batch_bytes = page_size, batch_bytes // page_size * page_size
        self.lease_seconds = lease_seconds
        self.generation = 0
        self.seen = set()
        self.saved_high = self.applied_high = None
        self.result = None
        self.active_step = None
        self.decision = dict(mode="observe", reason="no_forecast")
        self.reserved_bytes = self.low_budget = self.budget = 0
        self.no_progress = 0
        self.preview_reported = False
        self.refault_baseline = self.refault_time = None
        self.observed_drop = 0

    def _restore_high(self):
        if self.applied_high is None:
            return
        if self.backend.read("memory.high") == self.applied_high:
            self.backend.write("memory.high", self.saved_high)
        self.saved_high = self.applied_high = None

    def stop(self, reason="stopped"):
        self._restore_high()
        self.result = None
        self.active_step = None
        self.decision = dict(mode="observe", reason=reason)
        return dict(event="stopped", reason=reason, executed=self.apply)

    def _check_high_owner(self):
        if (self.applied_high is not None
                and self.backend.read("memory.high") != self.applied_high):
            # A changed admin value wins. Userspace cannot detect an ABA write.
            self.saved_high = self.applied_high = None
            raise RuntimeError("administrator changed memory.high; trial stopped")

    def handle(self, event, now=None):
        now = time.monotonic() if now is None else now
        kind = event.get("event")
        if kind == "stop":
            return self.stop()
        if kind == "forecast":
            if self.result is not None:
                raise ValueError("stop the previous plan before replacing its immutable budget")
            keys = ("app_name", "sequence_num", "current_op_id", "pressure_gib")
            anchor = tuple(event[key] for key in keys)
            if anchor in self.seen:
                raise ValueError("repeated lookup anchor cannot refill this session's budget")
            rss = integer(event["rss_bytes"], 1)
            result = self.lookup.predict(*anchor)
            decision = classify(result, rss, self.reuse_fault_max, self.turnover_burst_min)
            self.seen.add(anchor)
            self.generation += 1
            self.result, self.rss, self.decision = result, rss, decision
            self.deadline = now + self.lease_seconds
            self.reserved_bytes = self.low_budget = self.budget = 0
            self.no_progress = self.observed_drop = 0
            self.refault_baseline = self.refault_time = None
            return dict(event="forecast", generation=self.generation, **decision,
                        source_kind=result.source_kind,
                        raw_wss_unit=result.wss_unit,
                        raw_wss=list(result.raw_wss), raw_pagefault=list(result.raw_pagefault),
                        wss_bytes=list(result.wss_bytes), pagefault=list(result.pagefault),
                        awaiting="explicit_enter_event", executed=False)
        if kind != "enter":
            raise ValueError("event must be forecast, enter or stop")
        if self.result is None or event.get("generation") != self.generation:
            raise ValueError("enter requires the current forecast generation")
        if now >= self.deadline:
            return self.stop("lease_expired")
        step = integer(event["step"], 0, self.result.valid_count)
        expected = 0 if self.active_step is None else self.active_step + 1
        if step != expected:
            raise ValueError("enter events must cover stages consecutively, starting at zero")
        if step == self.result.valid_count:
            return self.stop("forecast_horizon_complete")
        self.active_step = step
        self.preview_reported = False
        if step == 0:
            usage = self.backend.usage() if self.backend else self.rss
            low_floor = high_target(self.result.wss_bytes[0], self.page_size) if self.result.wss_bytes[0] else 0
            self.budget = min(self.rss, usage)
            self.low_budget = min(self.budget, max(0, usage - low_floor))
        return dict(event="entered", generation=self.generation, step=step, executed=False)

    def _set_high(self, target):
        self._check_high_owner()
        current = self.backend.read("memory.high")
        admin = self.saved_high if self.saved_high is not None else current
        ceiling = (1 << 64) - 1 if admin == "max" else int(admin)
        floor = max(self.backend.limit("memory.min"), self.backend.limit("memory.low"))
        if target > min(ceiling, self.backend.limit("memory.max")) or target < floor:
            raise RuntimeError("predicted high conflicts with administrator limits/protection")
        if self.saved_high is None:
            self.saved_high = current
        value = str(target)
        if current != value:
            # Mark before a potentially blocking write so finally can restore it.
            self.applied_high = value
            self.backend.write("memory.high", value)
        self.applied_high = value

    def _refault_stop(self, now):
        if self.refault_time is None:
            self.refault_time, self.refault_baseline = now, self.backend.refaults()
            return False
        if now - self.refault_time < 1:
            return False
        value = self.backend.refaults()
        delta = max(0, value - self.refault_baseline)
        reference = self.observed_drop or self.backend.usage()
        stop = delta > max(1, reference // self.page_size // 10)
        self.refault_time, self.refault_baseline = now, value
        self.observed_drop = 0
        return stop

    def tick(self, now=None):
        now = time.monotonic() if now is None else now
        if self.result is None:
            return None
        if now >= self.deadline:
            return self.stop("lease_expired")
        if self.active_step is None:
            return None  # A future low valley has not started merely because time passed.
        mode = self.decision["mode"]
        remaining = self.result.wss_bytes[self.active_step:]
        if not self.apply:
            if self.preview_reported:
                return None
            self.preview_reported = True
            return dict(event="candidate", generation=self.generation, step=self.active_step,
                        mode=mode, executed=False, **self._targets(mode, remaining))
        self.backend.check_exclusive()
        self._check_high_owner()
        if mode == "observe":
            return None
        if self._refault_stop(now):
            return self.stop("refault_guard")
        if mode == "high":
            target = high_target(max(remaining), self.page_size)
            changed = self.applied_high != str(target)
            self._set_high(target)
            if changed:
                return dict(event="high", target_bytes=target, executed=True)
            return None
        self._restore_high()
        usage = self.backend.usage()
        low_stage = self.active_step == 0
        floor = (high_target(self.result.wss_bytes[0], self.page_size) if low_stage
                 else max(remaining))
        floor = max(floor, self.backend.limit("memory.min"), self.backend.limit("memory.low"))
        if max(remaining) > self.backend.limit("memory.max"):
            return self.stop("limit_conflict")
        budget = self.low_budget if low_stage else self.budget
        request = min(self.batch_bytes, max(0, budget - self.reserved_bytes), max(0, usage - floor))
        request = request // self.page_size * self.page_size
        if not request:
            return None
        # memory.reclaim has no returned actual-page count. Reserve the entire
        # request even on partial reclaim/EAGAIN; never invent an actual count.
        self.reserved_bytes += request
        partial = False
        try:
            self.backend.write("memory.reclaim", request)
        except OSError as error:
            if error.errno != errno.EAGAIN:
                raise
            partial = True
        after = self.backend.usage()
        drop = max(0, usage - after)
        self.observed_drop += drop
        self.no_progress = 0 if drop else self.no_progress + 1
        record = dict(event="reclaim", stage="low" if low_stage else "rest", executed=True,
                      requested_bytes=request, reserved_bytes=self.reserved_bytes,
                      budget_bytes=self.budget, low_budget_bytes=self.low_budget,
                      observed_usage_drop_bytes=drop, partial=partial, floor_bytes=floor)
        if self.no_progress >= 3:
            record["stopped"] = self.stop("no_observed_progress")["reason"]
        return record

    def _targets(self, mode, remaining):
        if mode == "high":
            return dict(target_bytes=high_target(max(remaining), self.page_size))
        if mode == "reclaim":
            floor = (high_target(self.result.wss_bytes[0], self.page_size)
                     if self.active_step == 0 else max(remaining))
            return dict(floor_bytes=floor, budget_bytes=self.budget, low_budget_bytes=self.low_budget)
        return {}


def emit(record):
    if record is not None:
        print(json.dumps(record, ensure_ascii=False), flush=True)


def read_events(destination):
    try:
        for line in sys.stdin:
            destination.put(line)
    finally:
        destination.put(None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    provider = parser.add_mutually_exclusive_group()
    provider.add_argument("--library", type=Path)
    provider.add_argument("--table", type=Path, help="default: bundled workbook-derived GIMP table")
    parser.add_argument("--wss-unit", choices=UNITS,
                        help="required for an external library; the bundled API v3 table uses bytes")
    parser.add_argument("--success-code", type=int, default=0)
    parser.add_argument("--cgroup", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reuse-fault-max", type=int)
    parser.add_argument("--turnover-burst-min", type=int)
    parser.add_argument("--lease-seconds", type=float, default=30,
                        help="safety expiry only; not a prediction or operation duration")
    parser.add_argument("--batch-mib", type=int, default=32)
    parser.add_argument("--query", nargs=4, metavar=("APP", "SEQUENCE", "CURRENT_OP", "PRESSURE"))
    parser.add_argument("--rss-bytes", type=int, help="same-scope RSS for --query")
    args = parser.parse_args()
    if args.apply and (args.cgroup is None or args.query is not None):
        parser.error("--apply requires --cgroup and explicit phase events on stdin")
    if args.library:
        if args.wss_unit is None:
            parser.error("an external --library requires explicit --wss-unit")
        lookup = OperationLookup(args.library, args.wss_unit, args.success_code)
    else:
        if args.wss_unit not in (None, "bytes") or args.success_code != 0:
            parser.error("the bundled API v3 table uses exact bytes and success code 0")
        lookup = TableOperationLookup(args.table or Path(__file__).parent/"data/operation_table.json")
    backend = NativeCgroup(args.cgroup) if args.apply else None
    page = os.sysconf("SC_PAGESIZE") if hasattr(os, "sysconf") else 4096
    controller = OperationController(lookup, backend, args.apply, args.reuse_fault_max,
                                     args.turnover_burst_min, args.lease_seconds,
                                     args.batch_mib * 1024**2, page)
    try:
        if args.query is not None:
            if args.rss_bytes is None:
                parser.error("--query requires --rss-bytes")
            app, sequence, current, pressure = args.query
            emit(controller.handle(dict(event="forecast", app_name=app, sequence_num=int(sequence),
                                        current_op_id=int(current), pressure_gib=int(pressure),
                                        rss_bytes=args.rss_bytes)))
            return
        events = queue.Queue(maxsize=128)
        threading.Thread(target=read_events, args=(events,), daemon=True).start()
        last_tick = -math.inf
        while True:
            try:
                line = events.get(timeout=0.1)
            except queue.Empty:
                line = ""
            if line is None:
                break
            if line.strip():
                emit(controller.handle(json.loads(line)))
            # Events may arrive faster than the worker rate. Never use them to
            # request multiple batches in the same 100ms execution interval.
            now = time.monotonic()
            if now - last_tick >= 0.1:
                emit(controller.tick(now))
                last_tick = now
    finally:
        controller.stop("exit")


if __name__ == "__main__":
    def terminate(signum, frame):
        raise KeyboardInterrupt
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, terminate)
    try:
        main()
    except (ValueError, RuntimeError, KeyError, OSError) as error:
        print(f"operation runner: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
