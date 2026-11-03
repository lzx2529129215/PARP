#!/usr/bin/env python3
"""Contract and controller tests; every lookup/result here is synthetic."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from operation_lookup import OperationLookup, decode_result
from operation_controller import OperationController, classify, high_target

MIB = 1024**2
HERE = Path(__file__).resolve().parent


class FixtureLookup:
    def __init__(self, wss=(200, 200, 200, 200), faults=(1, 1, 1, 1)):
        self.result = decode_result(wss, faults, "mib")
        self.calls = []

    def predict(self, *args):
        self.calls.append(args)
        return self.result


class FakeCgroup:
    def __init__(self):
        self.values = {"memory.high": "max", "memory.max": "max",
                       "memory.min": "0", "memory.low": "0"}
        self.current = 1000 * MIB
        self.reclaimable = self.current
        self.writes = []
        self.refault = 0
        self.partial = False
        self.busy = False

    def check_exclusive(self):
        if self.busy:
            raise RuntimeError("another controller is active")

    def read(self, name):
        return self.values[name]

    def limit(self, name):
        return (1 << 64) - 1 if self.values[name] == "max" else int(self.values[name])

    def usage(self):
        return self.current

    def refaults(self):
        return self.refault

    def write(self, name, value):
        self.writes.append((name, str(value)))
        if name == "memory.reclaim":
            amount = min(int(value), self.reclaimable, self.current)
            self.current -= amount
            self.reclaimable -= amount
            if self.partial:
                raise OSError(errno.EAGAIN, "fixture partial reclaim")
        else:
            self.values[name] = str(value)


def anchor():
    return dict(event="forecast", app_name="fixture-only", sequence_num=123,
                current_op_id=1, pressure_gib=14, rss_bytes=1000 * MIB)


def make_controller(lookup=None, **options):
    group = FakeCgroup()
    controller = OperationController(lookup or FixtureLookup(), group, apply=True,
                                    reuse_fault_max=20, turnover_burst_min=100, **options)
    return controller, group


def enter(controller, step, now=1):
    return controller.handle(dict(event="enter", generation=controller.generation, step=step), now)


class OperationContractTest(unittest.TestCase):
    def test_suffix_and_signed_values(self):
        r = decode_result([200, 500, 500, -1], [1, 1000, 1, -1], "mib")
        self.assertEqual(r.valid_count, 3)
        self.assertEqual(r.raw_wss[-1], -1)
        self.assertEqual(r.wss_bytes, (200*MIB, 500*MIB, 500*MIB))
        self.assertEqual(decode_result([-1]*4, [-1]*4, "bytes").valid_count, 0)
        for w, f in (([1, -1, 1, -1], [0, -1, 0, -1]),
                     ([1, -1, -1, -1], [0, 0, -1, -1]),
                     ([1, -2, -1, -1], [0, -1, -1, -1])):
            with self.assertRaises(ValueError):
                decode_result(w, f, "bytes")
        with self.assertRaises(ValueError):
            decode_result([(1 << 63)-1]*4, [0]*4, "gib")

    def test_available_stages_and_ambiguous_faults(self):
        for count, expected in ((0, "observe"), (1, "observe"), (2, "high"), (4, "high")):
            r = decode_result([200]*count + [-1]*(4-count), [1]*count + [-1]*(4-count), "mib")
            self.assertEqual(classify(r, 1000*MIB, 20, 100)["mode"], expected)
        r = decode_result([200, 500, 500, -1], [1, 1000, 1, -1], "mib")
        self.assertEqual(classify(r, 1000*MIB, 20, 100)["mode"], "reclaim")
        self.assertEqual(classify(r, 1000*MIB)["mode"], "observe")
        r = decode_result([200, 500, -1, -1], [1, 1000, -1, -1], "mib")
        self.assertEqual(classify(r, 1000*MIB, 20, 100)["mode"], "observe")
        self.assertEqual(high_target(200*MIB, 4096), 220*MIB)


class OperationControllerTest(unittest.TestCase):
    def test_explicit_phase_progress_and_expiry(self):
        c, g = make_controller()
        c.handle(anchor(), 0)
        self.assertIsNone(c.tick(6))
        self.assertFalse(g.writes)  # Six seconds cannot mean an operation started.
        with self.assertRaises(ValueError):
            enter(c, 1, 7)
        enter(c, 0, 7)
        c.tick(7)
        self.assertEqual(g.values["memory.high"], str(220*MIB))
        c.tick(30)
        self.assertEqual(g.values["memory.high"], "max")
        self.assertIsNone(c.result)

    def test_high_admin_restore_and_limits(self):
        c, g = make_controller()
        c.handle(anchor(), 0); enter(c, 0); c.tick(1)
        c.tick(1.1)
        self.assertEqual(len(g.writes), 1)
        c.stop()
        self.assertEqual(g.values["memory.high"], "max")
        c, g = make_controller()
        c.handle(anchor(), 0); enter(c, 0); c.tick(1)
        g.values["memory.high"] = str(300*MIB)
        with self.assertRaises(RuntimeError):
            c.tick(1.1)
        c.stop()
        self.assertEqual(g.values["memory.high"], str(300*MIB))
        c, g = make_controller()
        g.values["memory.low"] = str(300*MIB)
        c.handle(anchor(), 0); enter(c, 0)
        with self.assertRaises(RuntimeError):
            c.tick(1)
        self.assertFalse(g.writes)

    def test_two_stages_immutable_budget_and_no_timer_transition(self):
        lookup = FixtureLookup((200, 500, 500, 500), (1, 1000, 1, 1))
        c, g = make_controller(lookup)
        result = c.handle(anchor(), 0)
        enter(c, 0)
        for i in range(30):
            c.tick(1 + i/10)
        self.assertEqual(g.current, 220*MIB)
        self.assertEqual(c.reserved_bytes, 780*MIB)
        g.current = 720*MIB
        g.reclaimable = g.current
        for i in range(10):
            c.tick(10+i/10)
        self.assertEqual(c.reserved_bytes, 780*MIB)
        self.assertEqual(g.current, 720*MIB)  # Exhausted low-stage budget.
        enter(c, 1, 12)
        for i in range(10):
            c.tick(12+i/10)
        self.assertEqual(c.reserved_bytes, 1000*MIB)
        self.assertEqual(g.current, 500*MIB)
        g.current = 900*MIB
        c.tick(14)
        self.assertEqual(c.reserved_bytes, 1000*MIB)
        with self.assertRaises(ValueError):
            c.handle(anchor(), 14)  # No replenishing while active.
        self.assertEqual(result["mode"], "reclaim")
        self.assertEqual(len(lookup.calls), 1)
        c.stop()
        with self.assertRaises(ValueError):
            c.handle(anchor(), 15)  # Same lookup anchor cannot rearm.

    def test_partial_reclaim_no_progress_and_refault(self):
        lookup = FixtureLookup((200, 500, 500, -1), (1, 1000, 1, -1))
        c, g = make_controller(lookup)
        c.handle(anchor(), 0); enter(c, 0)
        g.partial = True
        out = c.tick(1)
        self.assertTrue(out["partial"])
        self.assertEqual(out["reserved_bytes"], 32*MIB)
        self.assertEqual(out["observed_usage_drop_bytes"], 32*MIB)
        g.reclaimable = 0
        for i in range(3):
            out = c.tick(1.1+i/10)
        self.assertEqual(out["stopped"], "no_observed_progress")
        self.assertEqual(c.reserved_bytes, 128*MIB)
        c, g = make_controller()
        c.handle(anchor(), 0); enter(c, 0); c.tick(1)
        g.refault = 1_000_000
        self.assertEqual(c.tick(2.1)["reason"], "refault_guard")
        self.assertEqual(g.values["memory.high"], "max")

    def test_preview_no_writes_and_horizon_end(self):
        g = FakeCgroup()
        c = OperationController(FixtureLookup(), g, reuse_fault_max=20, turnover_burst_min=100)
        c.handle(anchor(), 0); enter(c, 0)
        self.assertFalse(c.tick(1)["executed"])
        self.assertFalse(g.writes)
        for i in range(1, 4):
            enter(c, i, 1+i)
        self.assertEqual(enter(c, 4, 5)["reason"], "forecast_horizon_complete")
        with self.assertRaises(ValueError):
            OperationController(FixtureLookup(), g, apply=True)


@unittest.skipUnless(os.environ.get("PARP_TEST_LOOKUP_LIBRARY"), "ABI fixture built by run_host_tests.py")
class NativeLookupABITest(unittest.TestCase):
    def setUp(self):
        self.path = Path(os.environ["PARP_TEST_LOOKUP_LIBRARY"])
        self.lookup = OperationLookup(self.path, "mib")

    def test_real_signed_c_abi_and_errors(self):
        self.assertEqual(self.lookup.predict("fixture-only", 123, 1, 14).wss_bytes, (200*MIB,)*4)
        self.assertEqual(self.lookup.predict("fixture-only", 123, 2, 14).valid_count, 3)
        self.assertEqual(self.lookup.predict("fixture-only", 123, 3, 14).valid_count, 0)
        with self.assertRaises(ValueError):
            self.lookup.predict("fixture-only", 123, 4, 14)
        with self.assertRaises(RuntimeError):
            self.lookup.predict("fixture-only", 123, 5, 14)
        for pressure in (1, 8*1024**3):
            with self.assertRaises(ValueError):
                self.lookup.predict("fixture-only", 123, 1, pressure)

    def test_query_and_stream_cli(self):
        command = [sys.executable, str(HERE/"operation_controller.py"), "--library", str(self.path),
                   "--wss-unit", "mib", "--reuse-fault-max", "20", "--turnover-burst-min", "100"]
        query = subprocess.run(command + ["--query", "fixture-only", "123", "2", "14", "--rss-bytes",
                                         str(1000*MIB)], check=True, capture_output=True, text=True)
        result = json.loads(query.stdout)
        self.assertEqual(result["raw_wss"], [200, 500, 500, -1])
        self.assertEqual(result["mode"], "reclaim")
        lines = [anchor(), dict(event="enter", generation=1, step=0), dict(event="stop")]
        stream = subprocess.run(command, input="".join(json.dumps(x)+"\n" for x in lines),
                                check=True, capture_output=True, text=True)
        events = [json.loads(line) for line in stream.stdout.splitlines()]
        self.assertEqual(events[0]["event"], "forecast")
        self.assertTrue(all(not event["executed"] for event in events))


if __name__ == "__main__":
    unittest.main()
