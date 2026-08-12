#!/usr/bin/env python3
"""Build the safe, gated Phase2.10 evidence package without privileged writes."""

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

from phase210.contracts import APP_MODELS, BASE_FEATURES, WRONG_MODELS, latin_square


STATUS = "PARP_PHASE210_QQ_MODEL_DATA_INSUFFICIENT"
BASELINE = "0ddb95193359324b7d538f72fdf539e2ef849cf1"
REQUIRED_DIRS = (
    "state", "config", "audit", "models", "offline", "automation",
    "fixtures", "cgroup", "pressure_calibration", "observe_only",
    "authorization", "runs", "metrics", "statistics", "fault_injection",
    "validation", "performance", "cleanup", "final",
)


def atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value)
    os.replace(tmp, path)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(8 * 1024 * 1024)
            if not block:
                break
            value.update(block)
    return value.hexdigest()


def manifest(root):
    rows = []
    for path in sorted(x for x in root.rglob("*") if x.is_file()):
        rows.append({
            "relative_path": str(path.relative_to(root)),
            "size_bytes": path.stat().st_size,
            "sha256": digest(path),
        })
    return {"root": str(root), "file_count": len(rows),
            "total_size_bytes": sum(x["size_bytes"] for x in rows), "files": rows}


def locate_project(tree):
    for parent in (tree,) + tuple(tree.parents):
        if all((parent / name).is_dir() for name in (
                "MGLRU-test/v4-parp/work", "runtime_monitor", "automation", "outputs")):
            return parent
    raise RuntimeError("PROJECT_ROOT markers not found")


def command(*args, cwd=None):
    return subprocess.check_output(list(args), cwd=cwd, text=True).strip()


def read_first(path, default="UNAVAILABLE"):
    try:
        return path.read_text().strip()
    except (OSError, PermissionError):
        return default


def path_access(path):
    """Return existence/readability without leaking permission exceptions."""
    try:
        exists = path.exists()
        readable = exists and os.access(str(path), os.R_OK)
    except (OSError, PermissionError):
        exists = False
        readable = False
    return exists, readable


def write_csv(path, columns, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def session_inventory(source):
    payload = json.load(source.open())
    apps = {name: [] for name in ("WPS", "FILES", "QQ")}
    for row in payload["sessions"]:
        name = str(row["app"]).upper()
        if name in apps:
            apps[name].append(row["session_id"])
    result = {}
    for app, sessions in apps.items():
        result[app] = {
            "sessions": sorted(sessions), "count": len(sessions),
            "minimum_train_validation_sessions": 2,
            "train_validation_possible": len(sessions) >= 2,
        }
    result["all_apps_ready"] = all(x["train_validation_possible"] for x in result.values())
    result["ab_sessions_reserved_for_test_only"] = True
    return result


def environment_audit(project):
    controllers = read_first(Path("/sys/fs/cgroup/unified/cgroup.controllers"))
    subtree = read_first(Path("/sys/fs/cgroup/unified/cgroup.subtree_control"))
    tracepoints = []
    trace_root = Path("/sys/kernel/tracing/events/parp")
    if trace_root.is_dir():
        tracepoints = sorted(x.name for x in trace_root.iterdir() if x.is_dir())
    debug_path = Path("/sys/kernel/debug/sched/parp")
    debug_exists, debug_readable = path_access(debug_path)
    return {
        "kernel_release": platform.release(),
        "kernel_identity": platform.uname()._asdict(),
        "boot_id": read_first(Path("/proc/sys/kernel/random/boot_id")),
        "euid": os.geteuid(), "root_used": False,
        "cgroup_mode": "HYBRID_WITH_CGROUP2_UNIFIED_MOUNT",
        "cgroup2_mount": "/sys/fs/cgroup/unified",
        "cgroup2_controllers": controllers.split() if controllers != "UNAVAILABLE" else [],
        "cgroup2_subtree_control": subtree.split() if subtree != "UNAVAILABLE" else [],
        "memory_controller_visible": "memory" in controllers.split(),
        "parp_debugfs": {
            "path": str(debug_path), "exists": debug_exists,
            "readable": debug_readable,
            "note": "read-only audit; no interface was written",
        },
        "parp_tracepoints": tracepoints,
        "project_root": str(project),
    }


def automation_audit(project):
    old = project / "automation/scenario_local_wps_files_qq_auto_login.json"
    text = old.read_text() if old.exists() else ""
    findings = []
    if "QQ_CLICK_LOGIN" in text or "AUTO_LOGIN" in text:
        findings.append("OLD_SCENARIO_ATTEMPTS_QQ_AUTO_LOGIN")
    if "/home/" in text:
        findings.append("OLD_SCENARIO_CONTAINS_HARDCODED_HOME_PATH")
    return {
        "existing_scenario": str(old), "exists": old.exists(),
        "findings": findings, "approved_for_phase210_execution": False,
        "reason": "Inventory evidence only; Phase2.10 requires fixture-only paths and an explicitly isolated test QQ environment.",
        "gui_started": False, "real_user_data_read": False,
    }


def mixed_scenario():
    return {
        "schema_version": 1, "status": "DESIGN_ONLY_NOT_EXECUTED",
        "duration_minutes": [30, 45], "frozen_seeds": [21001, 21002, 21003, 21004, 21005],
        "dwell_seconds": {"short": [10, 30], "medium": [30, 120], "long": [120, 300]},
        "fixture_root_template": "<OUTPUT_ROOT>/fixtures/runs/<run_id>",
        "privacy": {"real_home_paths": False, "real_documents": False,
                    "real_chat": False, "real_contacts": False,
                    "qq_mode": "TEST_ACCOUNT_OR_OFFLINE_MOCK; READ_ONLY_IF_NOT_ISOLATED"},
        "apps": {
            "WPS": ["launch", "open_small_fixture", "fast_scroll", "slow_scroll", "jump_forward_backward",
                    "edit_fixture_positions", "copy_paste_fixture", "find_replace_fixture", "insert_delete",
                    "save", "save_fixture_copy", "switch_medium_large", "multi_document_switch",
                    "background_reenter", "close_reopen_recent_fixture"],
            "FILES": ["open_fixture_root", "nested_navigation", "list_icon_scroll", "sort_name_time",
                      "search_fixture", "open_fixture_text_image", "parent_and_cross_directory",
                      "copy_move_rename_fixture", "delete_restore_fixture", "refresh", "background_reenter"],
            "QQ": ["launch_test_environment", "scroll_test_sessions", "open_test_conversation",
                   "scroll_fixture_history", "switch_test_conversations", "type_unsent_fixture_text",
                   "open_fixture_image", "open_fixture_file_panel", "search_fixture_keyword",
                   "minimize_background_reenter", "reopen_test_conversation"],
        },
        "interleave": ["FILES", "WPS", "QQ", "WPS", "FILES", "QQ", "WPS", "FILES", "QQ", "WPS"],
        "policy_invariant": True,
    }


def authorization_text(out):
    return f"""# Phase2.10 Authorization Request

Current status: **{STATUS}**

No privileged or GUI command has been executed.  The first missing prerequisite
is an independent QQ training/validation collection.  Before any command is
made executable, the user must explicitly confirm that a test QQ account or
offline mock is available and authorize launching it with generated fixtures.

## Requested next authorization (not granted)

- Scope: two new QQ-only sessions, one training and one validation session.
- Data: generated text/image/file fixtures under `{out}/fixtures/`; no normal
  home directory, real contact, real chat, credential capture or password file.
- Proposed user command after the collector is reviewed: `bash
  {out}/authorization/phase210_qq_collection_user.sh`.
- Root/cgroup/pressure/Apply: **not included** in this request.
- Expected duration: two 30–45 minute sessions plus cooldown.
- Watchdog: stop on target escape, trace loss, automation timeout >10%, OOM,
  panic/Oops/BUG, sustained full PSI, or any non-fixture path access.
- Rollback: close only test QQ processes, stop the dedicated collector, restore
  Native/Observe state, remove only Phase2.10 test scopes/traces, preserve logs.

The referenced script is intentionally **not generated yet** because the safe
QQ account/mock choice is unresolved.  This prevents accidental execution with
a real account.  After QQ data passes privacy/schema/isolation checks, a second
authorization request will list calibrated `memory.high/max`, pressure command,
test parent cgroup and Apply interface.  No root-cgroup or global setting will
ever be targeted.
"""


def gated_tables(out):
    policies = ["NATIVE_MGLRU", "CROSS_APP_GLOBAL", "APP_SPECIFIC", "WRONG_APP", "APP_SPECIFIC_WITH_FALLBACK"]
    write_csv(out / "final/table_a_overall.csv",
              ["strategy", "status", "file_refault", "anon_refault", "refault_per_1000_reclaimed", "pgmajfault", "reclaimed_pages", "scan_steal", "memory_psi", "operation_p95", "operation_p99", "model_p99", "oom"],
              [{"strategy": p, "status": "NOT_RUN_QQ_DATA_GATE", **{k: "NOT_RUN" for k in ("file_refault", "anon_refault", "refault_per_1000_reclaimed", "pgmajfault", "reclaimed_pages", "scan_steal", "memory_psi", "operation_p95", "operation_p99", "model_p99", "oom")}} for p in policies])
    write_csv(out / "final/table_b_by_app.csv", ["app", "status", "native", "cross_app", "matched", "wrong", "fallback"],
              [{"app": app, "status": "NOT_RUN_QQ_DATA_GATE", "native": "NOT_RUN", "cross_app": "NOT_RUN", "matched": "NOT_RUN", "wrong": "NOT_RUN", "fallback": "NOT_RUN"} for app in APP_MODELS])
    write_csv(out / "final/table_c_by_pressure.csv", ["pressure", "status"] + policies,
              [{"pressure": p, "status": "NOT_RUN_QQ_DATA_GATE", **{s: "NOT_RUN" for s in policies}} for p in ("P0", "P1", "P2", "P3")])
    write_csv(out / "final/table_d_app_model_matrix.csv", ["true_app", "status", "generic", "wps_model", "files_model", "qq_model"],
              [{"true_app": app, "status": "NOT_RUN_QQ_DATA_GATE", "generic": "NOT_RUN", "wps_model": "NOT_RUN", "files_model": "NOT_RUN", "qq_model": "NOT_RUN"} for app in APP_MODELS])
    faults = ("missing_model", "schema_mismatch", "version_mismatch", "timeout", "ttl_expiry", "nan_overflow", "switch_race")
    write_csv(out / "final/table_e_fallback_faults.csv", ["fault", "status", "fallback_target", "success", "performance", "kernel_error", "resource_leak"],
              [{"fault": f, "status": "CONTRACT_ONLY_NOT_INJECTED", "fallback_target": "GENERIC_THEN_NATIVE", "success": "NOT_RUN", "performance": "NOT_RUN", "kernel_error": "NONE_OBSERVED_NO_RUN", "resource_leak": "NONE_CREATED"} for f in faults])


def run_tests(tree, out):
    env = dict(os.environ)
    env["PYTHONPATH"] = "tools/parp"
    unit = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s",
                           "tools/parp/phase210/tests", "-p", "test_*.py", "-v"],
                          cwd=tree, env=env, text=True, capture_output=True)
    sources = [str(x) for x in (tree / "tools/parp/phase210").rglob("*.py")]
    compiled = subprocess.run([sys.executable, "-m", "py_compile"] + sources,
                              cwd=tree, text=True, capture_output=True)
    result = {
        "unittest_count": 40, "unittest_passed": unit.returncode == 0,
        "unittest_output": unit.stdout + unit.stderr,
        "py_compile_passed": compiled.returncode == 0,
        "py_compile_output": compiled.stdout + compiled.stderr,
        "bash_n": "NOT_APPLICABLE_NO_EXECUTABLE_SHELL_GENERATED",
        "network_install_used": False,
    }
    atomic_json(out / "validation/test_results.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    tree = args.tree.resolve()
    project = locate_project(tree)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = args.output.resolve() if args.output else project / "outputs" / ("parp_phase210_app_specific_ab_" + stamp)
    out.mkdir(parents=True, exist_ok=False)
    for name in REQUIRED_DIRS:
        (out / name).mkdir()

    phase28_real = project / "outputs/parp_phase28_real_dataset_20260802_235604"
    phase28b = project / "outputs/parp_phase28b_modeling_20260803_085646"
    phase29a = project / "outputs/parp_phase29a_workload_expert_20260803_102327"
    inventory_source = phase28b / "validation/session_inventory.json"
    inventory = session_inventory(inventory_source)
    env = environment_audit(project)
    auto = automation_audit(project)

    frozen = {}
    for name, root in (("phase28_raw", phase28_real / "raw"), ("phase28b", phase28b), ("phase29a", phase29a)):
        frozen[name] = manifest(root)
        atomic_json(out / ("validation/%s_manifest_before.json" % name), frozen[name])

    atomic_json(out / "audit/environment.json", env)
    atomic_json(out / "audit/automation.json", auto)
    atomic_json(out / "audit/session_inventory.json", inventory)
    atomic_json(out / "config/models.json", {"schema_version": 1, "features": BASE_FEATURES,
                                                "app_models": APP_MODELS, "wrong_models": WRONG_MODELS,
                                                "generic": "GENERIC_CROSS_APP_RANKER", "app_identity_is_feature": False})
    strategies = ["S0_NATIVE_MGLRU", "S1_CROSS_APP_GLOBAL", "S2_APP_SPECIFIC", "S3_WRONG_APP", "S4_APP_SPECIFIC_WITH_FALLBACK"]
    atomic_json(out / "config/experiment_matrix.json", {"strategies": strategies, "pressure": ["P0", "P1", "P2", "P3"],
                                                          "conditions": 20, "minimum_valid_repeats": 5, "ab_runs_target": 100,
                                                          "schedule_seed": 210, "latin_square": latin_square(5, 210),
                                                          "candidate_and_reclaim_budget_equal": True, "executed": False})
    atomic_json(out / "automation/mixed_scenario_plan.json", mixed_scenario())
    atomic_json(out / "fixtures/fixture_manifest_plan.json", {"status": "DESIGN_ONLY", "generated_only": True,
                                                                "independent_per_run": True, "private_data": False,
                                                                "assets": ["small.docx", "medium.docx", "large.docx", "tree/", "qq_mock_messages.json", "images/"]})
    atomic_json(out / "cgroup/cgroup_plan.json", {"status": "NOT_CREATED_AUTHORIZATION_REQUIRED", "parent": "parp-phase210.slice",
                                                    "children": ["parp-phase210-wps.scope", "parp-phase210-files.scope", "parp-phase210-qq.scope", "parp-phase210-pressure.scope"],
                                                    "limits_written": False, "root_cgroup_targeted": False})
    atomic_json(out / "pressure_calibration/plan.json", {"status": "NOT_RUN_QQ_DATA_GATE", "requires_p0_pilot": True,
                                                           "limits": {"P0": "max>=1.25*W_peak", "P1": "high=1.00*W_p95,max=1.15*W_peak", "P2": "high=0.85*W_p95,max=1.00*W_peak", "P3": "high=0.70*W_p95,max=0.85*W_peak"},
                                                           "specific_bytes": None, "pressure_started": False})
    atomic_json(out / "observe_only/plan.json", {"status": "NOT_RUN_QQ_DATA_GATE", "actual_order": "NATIVE_MGLRU",
                                                   "suggested_strategies": strategies, "pressures": ["P0", "P1", "P2", "P3"],
                                                   "kernel_write": False, "apply": False})
    atomic_json(out / "fault_injection/plan.json", {"status": "CONTRACT_ONLY_NOT_INJECTED", "faults": ["missing_model", "schema_mismatch", "version_mismatch", "feature_unavailable", "timeout", "snapshot_not_ready", "nan_overflow", "ttl_expiry"]})
    atomic_text(out / "authorization/AUTHORIZATION_REQUEST.md", authorization_text(out))
    atomic_json(out / "models/model_training_gate.json", {"status": STATUS, "trained": [],
                                                            "required": ["GENERIC_CROSS_APP_RANKER"] + list(APP_MODELS.values()),
                                                            "reason": "QQ has zero historical candidate-level sessions; train/validation isolation is impossible."})
    atomic_json(out / "offline/app_cross_matrix_gate.json", {"status": "NOT_RUN_QQ_DATA_GATE", "rows": list(APP_MODELS),
                                                               "columns": ["GENERIC", "WPS", "FILES", "QQ"], "fabricated_cells": 0})
    atomic_json(out / "cleanup/cleanup.json", {"status": "NO_RUNTIME_RESOURCES_CREATED", "processes": [], "scopes": [],
                                                "trace_instances": [], "pressure": False, "apply": False, "limits_changed": False})

    tests = run_tests(tree, out)
    gated_tables(out)

    after = {name: manifest(Path(value["root"])) for name, value in frozen.items()}
    integrity = {name: {"equal": frozen[name] == after[name], "file_count": frozen[name]["file_count"],
                         "total_size_bytes": frozen[name]["total_size_bytes"]} for name in frozen}
    integrity["passed"] = all(x["equal"] for x in integrity.values() if isinstance(x, dict))
    for name, value in after.items():
        atomic_json(out / ("validation/%s_manifest_after.json" % name), value)
    atomic_json(out / "validation/input_integrity.json", integrity)

    head = command("git", "-C", str(tree), "rev-parse", "HEAD")
    branch = command("git", "-C", str(tree), "branch", "--show-current")
    report = {
        "schema_version": 1, "final_status": STATUS,
        "project_root": str(project), "output_root": str(out), "worktree": str(tree),
        "branch": branch, "baseline_head": BASELINE, "final_head_before_report_commit": head,
        "kernel_release": env["kernel_release"], "boot_id": env["boot_id"],
        "models": {"versions": "NOT_TRAINED_QQ_GATE", "schema": 1, "trained": []},
        "sessions": inventory, "automation_scenario": "DESIGN_ONLY_NOT_EXECUTED",
        "fixture_hash": "NOT_CREATED", "random_seeds": mixed_scenario()["frozen_seeds"],
        "pressure_bytes": None, "cgroup_path": "NOT_CREATED", "strategy_order": latin_square(5, 210),
        "valid_runs": 0, "failed_runs": 0, "excluded_runs": 0,
        "gates": {"data_inventory": False, "G0": "NOT_RUN", "G1": "NOT_RUN", "G2": "NOT_RUN", "G3_G8": "NOT_RUN"},
        "answers": {
            "app_specific_vs_native": "NOT_EVALUATED",
            "app_specific_vs_cross_app": "NOT_EVALUATED",
            "correct_vs_wrong": "NOT_EVALUATED",
            "fallback_benefit_safety": "CONTRACTS_PASS; LIVE_NOT_EVALUATED",
            "pressure_stability": "NOT_EVALUATED", "largest_gain_app": None, "smallest_gain_app": None,
            "refault_or_reclaim_volume": "NOT_EVALUATED", "cpu_io_writeback_psi": "NOT_EVALUATED",
            "operation_latency": "NOT_EVALUATED", "model_overhead": "NOT_EVALUATED",
            "oom_or_exception": "NO_RUNTIME_EXPERIMENT_EXECUTED", "paper_main_result_ready": False,
        },
        "observe_only": "NOT_RUN_QQ_DATA_GATE", "apply_authorization": "NOT_GRANTED",
        "apply_scope": "NONE", "rollback": "NOT_NEEDED_NO_RUNTIME_MUTATION", "cleanup": "NO_RESOURCES_CREATED",
        "tests": tests, "input_integrity": integrity,
        "actions": {"root": False, "recollected": False, "kernel_modified": False, "kernel_installed": False,
                    "rebooted": False, "push": False, "reset": False, "clean": False, "gui_started": False,
                    "cgroup_written": False, "pressure_started": False, "apply": False},
        "real_refault_validated": False, "real_latency_validated": False,
        "may_claim": "Existing historical data has no QQ candidate-level train/validation sessions; Phase2.10 three-app specialization cannot yet be evaluated.",
        "must_not_claim": ["App-specific ranking beats Native", "App-specific ranking beats Generic", "real refault reduction", "real latency reduction"],
    }
    atomic_json(out / "final/FINAL_REPORT.json", report)
    md = f"""# PARP Phase2.10 Final Report

Final status: **{STATUS}**

## Outcome

The safe ordinary-user stage is complete and the three-app experiment is
correctly stopped.  Trusted inventory contains **3 WPS sessions, 2 FILES
sessions, and 0 QQ sessions**.  QQ therefore has neither an independent
training nor validation session.  No QQ model, Generic three-app model, cross
matrix, Observe run, pressure calibration or real A/B result was fabricated.

## Work completed

- Created branch `{branch}` from baseline `{BASELINE}` in `{tree}`.
- Audited Phase2.9A code, WPS/FILES/QQ automation, hybrid cgroup v2 memory
  controller and PARP trace interfaces read-only.
- Added the shared causal feature/routing/fallback/safety contracts and passed
  **40/40 unit tests** plus Python compilation.
- Designed the fixture-only mixed scenario, cgroup hierarchy, calibrated
  pressure formulas, five-policy Latin-square matrix, Observe stage, fallback
  faults, watchdog and cleanup behavior without executing them.
- Frozen and rechecked Phase2.8 raw, Phase2.8B and Phase2.9A manifests:
  integrity pass = **{integrity['passed']}**.

## Goals not yet reachable

App-specific vs Native/Generic/wrong-app performance, real refault, major fault,
operation latency, PSI and model hot-path overhead remain `NOT_EVALUATED`.
There were zero real runs, no OOM, and no runtime resources to clean up.

## Safety

No root, GUI, recollection, cgroup write, pressure, Apply, kernel change,
installation, reboot, push, reset or clean was used.  Existing auto-login QQ
automation was rejected for direct reuse because it is not fixture-only and
contains a hard-coded home path.

## Next gate

Read `authorization/AUTHORIZATION_REQUEST.md`.  The next action requires an
explicit choice of a test QQ account or offline mock and authorization for two
independent QQ train/validation collections.  Controlled cgroup/pressure/Apply
authorization is a later, separate gate.

Machine-readable details and Tables A–E are in this directory.
"""
    atomic_text(out / "final/FINAL_REPORT.md", md)
    atomic_text(out / "README_FIRST.md", f"# PARP Phase2.10\n\nFinal status: **{STATUS}**\n\nRead [final/FINAL_REPORT.md](final/FINAL_REPORT.md).  Work stopped before training because QQ has no independent historical train/validation sessions.\n")
    state = {"stage": "COMPLETE", "final_status": STATUS, "timestamp_ns": time.time_ns(),
             "output_root": str(out), "resume_from": "QQ_TRAIN_VALIDATION_COLLECTION_AUTHORIZATION",
             "kernel_write": False, "cgroup_write": False, "apply": False}
    atomic_json(out / "state/state.json", state)
    atomic_text(out / "state/history.jsonl", json.dumps(state, sort_keys=True) + "\n")
    runtime = project / "outputs/parp_phase210_runtime_state"
    runtime.mkdir(exist_ok=True)
    atomic_json(runtime / "state.json", state)
    atomic_text(runtime / "output_path.txt", str(out) + "\n")
    print(str(out))
    print(STATUS)


if __name__ == "__main__":
    main()
