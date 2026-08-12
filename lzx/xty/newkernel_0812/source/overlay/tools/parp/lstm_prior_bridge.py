#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Asynchronous full-whitelist LSTM-to-PARP prior batch bridge."""

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import json
from pathlib import Path
import re
import threading
import time

Q15_ONE = 32767
SCHEMA_VERSION = 1
DEFAULT_HORIZON_NS = 5 * 60 * 1_000_000_000
DEFAULT_TTL_NS = 60 * 1_000_000_000
MAX_HORIZON_NS = 60 * 60 * 1_000_000_000
MAX_TTL_NS = 10 * 60 * 1_000_000_000
U32_MAX = (1 << 32) - 1


class GenerationQueryError(RuntimeError):
    """The kernel generation cannot be queried safely."""


def score_to_q15(score):
    value = min(max(float(score), 0.0), 1.0)
    return int(round(value * Q15_ONE))


class BatchBuilder:
    def __init__(self, whitelist, model_version, generation=0):
        if not whitelist or model_version <= 0:
            raise ValueError("whitelist and model_version are required")
        self.whitelist = dict(whitelist)
        self.model_version = int(model_version)
        self._generation = int(generation)
        self._lock = threading.Lock()

    def _next_generation(self):
        with self._lock:
            if self._generation >= U32_MAX:
                raise OverflowError("prediction generation exhausted")
            self._generation += 1
            return self._generation

    def synchronize_generation(self, current_generation):
        current_generation = int(current_generation)
        if not 0 <= current_generation <= U32_MAX:
            raise GenerationQueryError("kernel generation is outside u32")
        with self._lock:
            self._generation = max(self._generation, current_generation)
            return self._generation

    def build(self, rows, foreground_app, timestamp_ns=None,
              horizon_ns=DEFAULT_HORIZON_NS, ttl_ns=DEFAULT_TTL_NS):
        horizon_ns = int(horizon_ns)
        ttl_ns = int(ttl_ns)
        if not 0 < horizon_ns <= MAX_HORIZON_NS:
            raise ValueError("horizon is outside the kernel contract")
        if not 0 < ttl_ns <= MAX_TTL_NS:
            raise ValueError("TTL is outside the kernel contract")
        timestamp_ns = time.monotonic_ns() if timestamp_ns is None else int(timestamp_ns)
        scores = {key: 0.0 for key in self.whitelist}
        for row in rows:
            key = str(row.get("app_key", ""))
            if key in scores:
                scores[key] = float(row.get("probability",
                                            row.get("next_use_probability", 0.0)))
        ranked = sorted(self.whitelist, key=lambda key: (-scores[key],
                                                         self.whitelist[key]))
        entries = []
        for rank, key in enumerate(ranked, 1):
            entries.append({
                "app_id": int(self.whitelist[key]),
                "score_q15": score_to_q15(scores[key]),
                "rank": rank,
                "foreground": key == foreground_app,
                "valid": True,
                "flags": 0,
                "app_key": key,
            })
        return {
            "schema_version": SCHEMA_VERSION,
            "model_version": self.model_version,
            "prediction_generation": self._next_generation(),
            "timestamp_ns": timestamp_ns,
            "horizon_ns": horizon_ns,
            "expiry_ns": timestamp_ns + ttl_ns,
            "entries": entries,
        }


class MockTransport:
    def __init__(self, fail_once=False, current_generation=0):
        self.fail_once = fail_once
        self._current_generation = int(current_generation)
        self.batches = []
        self.attempts = []

    def current_generation(self):
        return self._current_generation

    def submit(self, batch):
        generation = int(batch["prediction_generation"])
        if generation <= self._current_generation:
            raise IOError("stale or equal prediction generation")
        if self.fail_once:
            self.fail_once = False
            self.attempts.append({"ok": False, "generation":
                                  generation})
            raise IOError("injected transport failure")
        self.batches.append(batch)
        self._current_generation = generation
        self.attempts.append({"ok": True, "generation": generation})
        return {"transport": "mock", "accepted": True}


class DebugfsTransport:
    def __init__(self, path="/sys/kernel/debug/parp/app_prior_batch"):
        self.path = Path(path)

    @staticmethod
    def encode_batch(batch):
        fields = [batch["schema_version"], batch["model_version"],
                  batch["prediction_generation"], batch["timestamp_ns"],
                  batch["horizon_ns"], batch["expiry_ns"],
                  len(batch["entries"])]
        for entry in batch["entries"]:
            fields.extend([entry["app_id"], entry["score_q15"], entry["rank"],
                           int(entry["foreground"]), int(entry["valid"]),
                           entry.get("flags", 0)])
        return " ".join(str(value) for value in fields) + "\n"

    def current_generation(self):
        try:
            metadata = self.path.read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            raise GenerationQueryError(
                "cannot read kernel app-prior generation: {}".format(exc))
        match = re.search(r"(?:^|\s)generation=(\d+)(?:\s|$)", metadata)
        if not match:
            raise GenerationQueryError(
                "kernel app-prior generation metadata is unavailable")
        generation = int(match.group(1))
        if not 0 <= generation <= U32_MAX:
            raise GenerationQueryError("kernel generation is outside u32")
        return generation

    def submit(self, batch):
        self.path.write_text(self.encode_batch(batch), encoding="ascii")
        return {"transport": "debugfs", "accepted": True}


class AsyncPriorBridge:
    def __init__(self, builder, transport, dry_run=False):
        self.builder = builder
        self.transport = transport
        self.dry_run = dry_run
        query = getattr(transport, "current_generation", None)
        if not callable(query):
            raise GenerationQueryError(
                "transport has no kernel generation query interface")
        try:
            self.kernel_generation_at_start = int(query())
        except GenerationQueryError:
            raise
        except Exception as exc:
            raise GenerationQueryError(
                "kernel generation query failed: {}".format(exc))
        self.builder.synchronize_generation(self.kernel_generation_at_start)
        self._executor = ThreadPoolExecutor(max_workers=1,
                                            thread_name_prefix="parp-prior")
        self._events = {}
        self._lock = threading.Lock()
        self.results = []

    def submit_event(self, event_id, rows, foreground_app):
        with self._lock:
            if event_id in self._events:
                future = Future()
                previous = dict(self._events[event_id])
                previous["duplicate_event"] = True
                future.set_result(previous)
                return future
            self._events[event_id] = {"reserved": True}
        return self._executor.submit(self._submit, event_id, list(rows),
                                     foreground_app)

    def _submit(self, event_id, rows, foreground_app):
        batch = self.builder.build(rows, foreground_app)
        result = {"event_id": event_id,
                  "generation": batch["prediction_generation"],
                  "batch": batch,
                  "duplicate_event": False, "dry_run": self.dry_run,
                  "submitted": False}
        try:
            if not self.dry_run:
                result["transport_result"] = self.transport.submit(batch)
                result["submitted"] = True
        except Exception as exc:
            result["error"] = str(exc)
        with self._lock:
            self._events[event_id] = dict(result)
            self.results.append(dict(result))
        return result

    def close(self):
        self._executor.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("batch_json", type=Path)
    parser.add_argument("--debugfs", default="/sys/kernel/debug/parp/app_prior_batch")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    batch = json.loads(args.batch_json.read_text(encoding="utf-8"))
    if args.dry_run:
        print(DebugfsTransport.encode_batch(batch), end="")
    else:
        DebugfsTransport(args.debugfs).submit(batch)


if __name__ == "__main__":
    main()
