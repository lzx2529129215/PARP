#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Regression tests for the Phase 2.6 resumable runtime tools."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock


PARP_DIR = Path(__file__).resolve().parents[1]
PHASE26_DIR = PARP_DIR / "phase26"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StateToolTests(unittest.TestCase):
    def test_atomic_nested_update(self):
        tool = load_module("phase26_state_tool", PHASE26_DIR / "state_tool.py")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"timestamps": {}}\n', encoding="utf-8")
            data = json.loads(path.read_text(encoding="utf-8"))
            tool.assign(data, "file_hashes.bzImage", "abc")
            tool.atomic_write(path, data)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["file_hashes"]["bzImage"], "abc")
            self.assertIn("updated", written["timestamps"])


class RuntimeIdentityTests(unittest.TestCase):
    def test_bootfix_release_requires_matching_provenance(self):
        identity = load_module(
            "phase26_runtime_identity", PHASE26_DIR / "runtime_identity.py")
        phase = {
            "target_kernel_release": "6.17.13-parp-v4-phase26-observe",
            "actual_booted_kernel_release":
                "6.17.13-parp-v4-phase26-bootfix1-observe",
            "bootfix_release": "6.17.13-parp-v4-phase26-bootfix1-observe",
            "bootfix_source_head": "3879523d",
        }
        bootfix = {
            "bootfix_release": "6.17.13-parp-v4-phase26-bootfix1-observe",
            "source_final_head": "3879523d",
            "boot_verified": True,
        }
        result = identity.validate_identity(
            phase, bootfix, phase["bootfix_release"], source_is_ancestor=True)
        self.assertEqual(result["kind"], "BOOTFIX")
        bad = dict(bootfix, source_final_head="unrelated")
        with self.assertRaises(identity.IdentityError):
            identity.validate_identity(
                phase, bad, phase["bootfix_release"],
                source_is_ancestor=True)


class WorkloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workload = load_module(
            "phase26_memory_workload", PARP_DIR / "phase26_memory_workload.py")

    def test_rejects_above_ten_percent_even_below_absolute_cap(self):
        args = types.SimpleNamespace(total_mib=2, duration=1, interval=1,
                                     directory=None)
        with mock.patch.object(self.workload, "memtotal_bytes",
                               return_value=10 * self.workload.MIB):
            with self.assertRaises(ValueError):
                self.workload.run(args)

    def test_small_anon_and_file_run_is_bounded_and_cleans_file(self):
        with tempfile.TemporaryDirectory() as directory:
            args = types.SimpleNamespace(total_mib=2, duration=0.03,
                                         interval=0.01, directory=directory)
            output = io.StringIO()
            with mock.patch.object(self.workload, "memtotal_bytes",
                                   return_value=1024 * self.workload.MIB), \
                    contextlib.redirect_stdout(output):
                self.workload.run(args)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(records[0]["phase"], "ready")
            self.assertEqual(records[0]["source"], "RUNTIME_LEVEL3A")
            self.assertEqual(records[-1]["phase"], "stopped")
            self.assertEqual(list(Path(directory).iterdir()), [])


class ShellToolTests(unittest.TestCase):
    def test_all_entry_points_are_strict_and_syntax_valid(self):
        scripts = sorted(PHASE26_DIR.glob("phase26_*.sh"))
        self.assertGreaterEqual(len(scripts), 14)
        for script in scripts:
            text = script.read_text(encoding="utf-8")
            self.assertIn("set -euo pipefail", text, script.name)
            self.assertIn('phase26_init "$@"', text, script.name)
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_hybrid_cgroup_discovers_memory_enabled_v2_mount(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy"
            unified = root / "unified"
            legacy.mkdir()
            unified.mkdir()
            (unified / "cgroup.controllers").write_text(
                "cpuset memory\n", encoding="ascii")
            (unified / "memory.reclaim").touch()
            command = (
                'source "$1"; '
                'phase26_find_cgroup2_memory_root "$2" "$3"')
            result = subprocess.run(
                ["bash", "-c", command, "bash", str(PHASE26_DIR / "_common.sh"),
                 str(legacy), str(unified)], check=True, text=True,
                stdout=subprocess.PIPE)
            self.assertEqual(result.stdout.strip(), str(unified))

    def test_apply_is_synthetic_only_and_restores_in_safe_order(self):
        text = (PHASE26_DIR / "phase26_apply_guarded.sh").read_text(
            encoding="utf-8")
        self.assertIn("parp-phase26-synthetic.scope", text)
        self.assertIn("trap restore EXIT INT TERM", text)
        self.assertIn("scan_budget_apply_domain", text)
        mode_restore = text.index("printf '1\\\\n'", text.index("restore()"))
        domain_clear = text.index("printf '0\\\\n'", mode_restore)
        self.assertLess(mode_restore, domain_clear)

    def test_build_uses_empty_certificate_paths(self):
        text = (PHASE26_DIR / "phase26_build.sh").read_text(encoding="utf-8")
        self.assertIn('--set-str SYSTEM_TRUSTED_KEYS ""', text)
        self.assertIn('--set-str SYSTEM_REVOCATION_KEYS ""', text)
        self.assertNotIn("-d SYSTEM_TRUSTED_KEYS", text)
        self.assertIn("LOCALVERSION=", text)

    def test_package_failure_reaches_staged_fallback(self):
        text = (PHASE26_DIR / "phase26_package.sh").read_text(
            encoding="utf-8")
        self.assertIn("if phase26_run make", text)
        self.assertIn("BINDEB_PKG_UNAVAILABLE", text)
        self.assertIn("staged-root", text)

    def test_bootfix_builder_forces_discovered_mptspi_stack_builtin(self):
        text = (PARP_DIR / "bootfix_build.sh").read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", text)
        self.assertIn("root_driver=$(", text)
        self.assertIn("[[ $root_driver = mptspi ]]", text)
        for symbol in ("FUSION", "FUSION_SPI", "SCSI", "BLK_DEV_SD",
                       "SCSI_SPI_ATTRS", "EXT4_FS", "JBD2"):
            self.assertIn("-e " + symbol, text)
        self.assertNotIn("rootdelay", text)

    def test_parp_large_trace_events_use_bpf_safe_payload_pointer(self):
        text = (PARP_DIR.parents[1] / "include/trace/events/parp.h").read_text(
            encoding="utf-8")
        self.assertIn("TP_PROTO(const struct parp_region_trace *event)", text)
        self.assertIn("TP_PROTO(const struct parp_scan_budget_trace *event)",
                      text)
        for field in ("sample=%llu", "nr_pages=%u", "native_units=%llu",
                      "proposed_units=%llu", "applied_units=%llu",
                      "generation=%u", "mode=%u"):
            self.assertIn(field, text)

    def test_shared_mm_limit_counts_only_matching_mm_tasks(self):
        text = (PARP_DIR.parents[1] / "mm/parp/adapter/damon_adapter.c").read_text(
            encoding="utf-8")
        skip = text.index("if (READ_ONCE(task->mm) != mm)")
        budget = text.index("parp_damon_mm_task_budget_exhausted", skip)
        context = text.index("parp_task_context(task", budget)
        self.assertLess(skip, budget)
        self.assertLess(budget, context)


class CompatibilityRunnerTests(unittest.TestCase):
    def test_boolean_optional_action(self):
        runner = load_module(
            "phase26_runtime_compat",
            PHASE26_DIR / "runtime_monitor_py38_compat_runner.py")
        parser = __import__("argparse").ArgumentParser()
        parser.add_argument("--feature", action=runner.BooleanOptionalAction,
                            default=True)
        self.assertTrue(parser.parse_args([]).feature)
        self.assertFalse(parser.parse_args(["--no-feature"]).feature)


if __name__ == "__main__":
    unittest.main()
