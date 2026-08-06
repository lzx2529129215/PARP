#!/usr/bin/env python3
"""真实 Online Causal Workload Markov 端到端驱动。

该脚本只把 generator 当作真实动作源；observed workload 始终来自目标
cgroup 的 memory delta 和 runtime_monitor.core.workload_classifier.classify_metrics。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from runtime_monitor.core.mglru_markov_debugfs import MGLRUMarkovDebugfsWriter
from runtime_monitor.core.online_causal_workload_markov import OnlineCausalWorkloadMarkov
from runtime_monitor.core.workload_classifier import WORKLOAD_NAMES, classify_metrics

SLICE = "huawei-test.slice"
SCOPE = "automation-files.scope"
APP_KEY = "FILES"
APP_ID = "3"
DEBUGFS = Path("/sys/kernel/debug/lru_gen_workload_markov")
METRICS = ["memory_current", "anon", "file", "pgfault", "pgmajfault",
           "workingset_refault_file", "pgscan", "pgsteal", "pswpin", "pswpout"]


def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=False, **kwargs)


def resolve_scope() -> Path | None:
    result = run(["systemctl", "--user", "show", SLICE, "-p", "ControlGroup", "--value"])
    control_group = result.stdout.strip()
    if result.returncode != 0 or not control_group:
        return None
    path = Path("/sys/fs/cgroup") / control_group.lstrip("/") / SCOPE
    return path if path.is_dir() else None


def read_scope(path: Path) -> dict[str, int]:
    values = {field: 0 for field in METRICS}
    try:
        values["memory_current"] = int((path / "memory.current").read_text().strip())
        for line in (path / "memory.stat").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in values:
                values[parts[0]] = int(parts[1])
        return values
    except (OSError, ValueError):
        return values


def snapshot_debugfs(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except PermissionError:
        result = run(["sudo", "-n", "cat", str(path)])
        return result.stdout if result.returncode == 0 else ""
    except OSError:
        return ""


def write_debugfs(command: str) -> tuple[str, str]:
    try:
        DEBUGFS.write_text(command + "\n", encoding="utf-8")
        return "ok", ""
    except PermissionError:
        result = run(["sudo", "-n", "tee", str(DEBUGFS)], input=command + "\n")
        return ("ok", "") if result.returncode == 0 else ("write_error", result.stderr.strip())
    except OSError as exc:
        return "write_error", str(exc)


class OnlineWriter:
    """Delegate real debugfs writes and keep a same-session online audit."""

    FIELDS = ["session_id", "timestamp_ns", "write_type", "app_key", "app_id", "cgroup_id",
              "prev_workload_id", "current_workload_id", "next_workload_id",
              "confidence_fixed", "boost_level", "command", "status", "error"]

    def __init__(self, work_dir: Path, session_id: str) -> None:
        # Keep every online Markov artifact in the canonical same-session tree.
        model = work_dir / "markov"
        review = work_dir / "markov"
        self.inner = MGLRUMarkovDebugfsWriter(enabled=True, strict=False, debugfs_path=DEBUGFS,
                                              session_id=session_id, model_dir=model,
                                              review_dir=review, ttl_ms=300000)
        self.path = work_dir / "markov" / "workload_markov_online_debugfs_writes.csv"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("w", encoding="utf-8", newline="")
        self.csv = csv.DictWriter(self.file, fieldnames=self.FIELDS)
        self.csv.writeheader()
        self.session_id = session_id
        self.workload_update_ok = 0
        self.markov_set_ok = 0

    def _row(self, write_type: str, command: str, status: str, error: str, **kwargs: Any) -> None:
        self.csv.writerow({"session_id": self.session_id, "timestamp_ns": time.time_ns(),
                           "write_type": write_type, "command": command, "status": status,
                           "error": error, **kwargs})
        self.file.flush()

    def write_workload_update(self, **kwargs: Any) -> tuple[str, str]:
        command = f"workload update {kwargs['cgroup_id']} {kwargs['app_id']} {kwargs['workload_id']}"
        status, error = self.inner.write_workload_update(**kwargs)
        if status == "ok":
            self.workload_update_ok += 1
        self._row("workload_update", command, status, error, app_key=kwargs.get("app_key", ""),
                  app_id=kwargs.get("app_id", ""), cgroup_id=kwargs.get("cgroup_id", ""),
                  next_workload_id=kwargs.get("workload_id", ""))
        return status, error

    def write_markov_set(self, **kwargs: Any) -> tuple[str, str]:
        entries = list(kwargs.get("entries", []))
        status, error = self.inner.write_markov_set(**kwargs)
        if status == "ok":
            self.markov_set_ok += 1
        first = entries[0] if entries else {}
        command = f"markov set {kwargs.get('app_id')} {kwargs.get('prev_workload_id')} {kwargs.get('current_workload_id')}"
        if first:
            command += f" {first.get('next_workload_id')} {first.get('confidence')} {first.get('boost_level')}"
        self._row("markov_set", command, status, error, app_key=kwargs.get("app_key", ""),
                  app_id=kwargs.get("app_id", ""), prev_workload_id=kwargs.get("prev_workload_id", ""),
                  current_workload_id=kwargs.get("current_workload_id", ""),
                  next_workload_id=first.get("next_workload_id", ""),
                  confidence_fixed=first.get("confidence", ""), boost_level=first.get("boost_level", ""))
        return status, error

    def write_app_binding(self, **kwargs: Any) -> tuple[str, str, str]:
        return self.inner.write_app_binding(**kwargs)

    def write_current_app(self, **kwargs: Any) -> None:
        self.inner.write_current_app(**kwargs)

    def close(self) -> None:
        self.file.flush()
        self.file.close()
        self.inner.close()


def parse_stat(text: str, name: str) -> int:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "stat" and parts[1] == name:
            try:
                return int(parts[2])
            except ValueError:
                return 0
    return 0


def run_e2e(work_dir: Path, session_id: str, phase_duration: float, hold_after: float) -> dict[str, Any]:
    for sub in ("precheck", "source", "tests", "runtime", "workload", "markov", "lstm", "logs", "reports"):
        (work_dir / sub).mkdir(parents=True, exist_ok=True)
    pre = work_dir / "precheck"
    (pre / "uname.txt").write_text(subprocess.check_output(["uname", "-a"], text=True))
    (pre / "proc_version.txt").write_text(Path("/proc/version").read_text())
    (pre / "proc_cmdline.txt").write_text(Path("/proc/cmdline").read_text())
    (pre / "running_kernel_config.txt").write_text(Path(f"/boot/config-{os.uname().release}").read_text())
    config = (pre / "running_kernel_config.txt").read_text()
    if os.uname().release != "6.17.13-mglru-tier2" or "CONFIG_TIER2_WATERMARK_MEMCG=y" in config:
        raise RuntimeError("运行内核或 Tier2 per-memcg 配置不符合要求")

    access_log = work_dir / "runtime" / "debugfs_permission_apply.log"
    access = run([str(PROJECT_ROOT / "runtime_monitor/scripts/prepare_mglru_debugfs_access.sh"), "--apply"])
    access_log.write_text(access.stdout + access.stderr)
    if access.returncode != 0:
        raise RuntimeError("debugfs 权限准备失败")

    # A user slice with no children is garbage-collected.  Keep the parent
    # materialized before launching the target scope, and clear a stale unit
    # name left by an earlier automation run.
    run(["systemctl", "--user", "start", SLICE])
    run(["systemctl", "--user", "stop", "automation-files.scope"])
    run(["systemctl", "--user", "reset-failed", "automation-files.scope"])

    pre_clear = snapshot_debugfs(DEBUGFS)
    (work_dir / "markov/debugfs_pre_clear.txt").write_text(pre_clear)
    write_debugfs("clear all")
    write_debugfs("policy mode observe")
    baseline = snapshot_debugfs(DEBUGFS)
    (work_dir / "markov/debugfs_baseline_after_clear.txt").write_text(baseline)

    # Start a real process in the requested systemd scope.
    trace_path = work_dir / "workload/workload_generator_trace.csv"
    generator = PROJECT_ROOT / "runtime_monitor/scripts/generate_markov_workload_sequence.py"
    env = os.environ.copy()
    env["MARKOV_SCOPE_PATH"] = str(resolve_scope() or "")
    command = ["systemd-run", "--user", "--scope", "--unit=automation-files",
               f"--slice={SLICE}", sys.executable, str(generator),
               "--sequence", "0,1,2,0,1,6,0,1,2,0",
               "--phase-duration-s", str(phase_duration), "--hold-after-s", str(hold_after),
               "--output", str(trace_path.resolve()), "--max-memory-mb", "256"]
    gen_log = (work_dir / "logs/generator.log").open("w", encoding="utf-8")
    gen_proc = subprocess.Popen(command, cwd=PROJECT_ROOT, env=env, stdout=gen_log, stderr=subprocess.STDOUT, text=True)
    scope_path = None
    for _ in range(30):
        scope_path = resolve_scope()
        if scope_path and (scope_path / "cgroup.procs").exists():
            break
        time.sleep(0.2)
    main_pid_result = run(["systemctl", "--user", "show", "automation-files.scope", "-p", "MainPID", "--value"])
    generator_pid = int(main_pid_result.stdout.strip() or 0)
    # Transient scopes can report MainPID=0 while the process is already
    # attached.  The cgroup membership file is the authoritative source.
    if not generator_pid and scope_path:
        try:
            members = [int(x) for x in (scope_path / "cgroup.procs").read_text().split() if x.isdigit()]
            generator_pid = next((pid for pid in members if str(generator.name) in Path(f"/proc/{pid}/cmdline").read_text(errors="ignore")), members[0] if members else 0)
        except (OSError, ValueError):
            generator_pid = 0
    proc_cgroup = Path(f"/proc/{generator_pid}/cgroup").read_text() if generator_pid and Path(f"/proc/{generator_pid}/cgroup").exists() else ""
    scope_inode = scope_path.stat().st_ino if scope_path and scope_path.exists() else 0
    pid_present = bool(scope_path and generator_pid and str(generator_pid) in (scope_path / "cgroup.procs").read_text())
    membership = {"generator_pid": generator_pid, "proc_cgroup": proc_cgroup,
                  "expected_scope": SCOPE, "actual_scope": SCOPE if scope_path else "",
                  "scope_path": str(scope_path or ""), "scope_inode": scope_inode,
                  "pid_present_in_cgroup_procs": pid_present,
                  "memory_current": bool(scope_path and (scope_path / "memory.current").exists()),
                  "memory_stat": bool(scope_path and (scope_path / "memory.stat").exists()),
                  "cgroup_events": bool(scope_path and (scope_path / "cgroup.events").exists()),
                  "membership_result": "PASS" if pid_present else "FAIL"}
    (work_dir / "runtime/test_scope_membership.json").write_text(json.dumps(membership, indent=2))
    (work_dir / "runtime/test_scope_membership.md").write_text("# Scope membership\n\n```json\n" + json.dumps(membership, indent=2) + "\n```\n")
    if not pid_present:
        gen_proc.terminate()
        raise RuntimeError("generator 未进入 automation-files.scope")

    cgroup_id = int(scope_inode)
    writer = OnlineWriter(work_dir, session_id)
    writer.inner.write_app_binding(APP_KEY, int(APP_ID), cgroup_id, 300000)
    writer.inner.write_current_app(APP_KEY, int(APP_ID), cgroup_id, 300000)
    markov = OnlineCausalWorkloadMarkov(session_id=session_id, model_dir=work_dir / "markov",
                                        review_dir=work_dir / "markov", debugfs_writer=writer)

    metrics_path = work_dir / "workload/cgroup_metrics_1s.csv"
    results_path = work_dir / "workload/workload_classifier_results_1s.csv"
    changes_path = work_dir / "workload/workload_state_changes.csv"
    fields = ["timestamp_ns", "app_key", "app_id", "scope_name"] + METRICS + [f"{m}_delta" for m in METRICS]
    result_fields = fields + ["requested_phase", "requested_workload_id", "observed_workload_id", "observed_workload_name", "classifier_rule", "state_changed"]
    prev: dict[str, int] | None = None
    last_observed: int | None = None
    metrics_file = metrics_path.open("w", encoding="utf-8", newline="")
    results_file = results_path.open("w", encoding="utf-8", newline="")
    changes_file = changes_path.open("w", encoding="utf-8", newline="")
    metrics_writer = csv.DictWriter(metrics_file, fieldnames=fields); metrics_writer.writeheader()
    result_writer = csv.DictWriter(results_file, fieldnames=result_fields); result_writer.writeheader()
    change_writer = csv.DictWriter(changes_file, fieldnames=result_fields); change_writer.writeheader()
    start_ns = time.time_ns()
    stress_started = False
    during_index = 0
    sequence = [0, 1, 2, 0, 1, 6, 0, 1, 2, 0]
    while gen_proc.poll() is None or (time.time_ns() - start_ns) / 1e9 < len(sequence) * phase_duration + 2:
        now = time.time_ns()
        values = read_scope(scope_path)
        deltas = {m: values[m] - prev[m] if prev else 0 for m in METRICS}
        row = {"timestamp_ns": now, "app_key": APP_KEY, "app_id": APP_ID, "scope_name": SCOPE, **values, **{f"{m}_delta": deltas[m] for m in METRICS}}
        metrics_writer.writerow(row); metrics_file.flush()
        classifier_values = {f: deltas[f] for f in ("memory_current", "anon", "file", "pgfault", "pgmajfault", "workingset_refault_file")}
        observed, reason = classify_metrics({f + "_delta": classifier_values[f] for f in classifier_values})
        phase = min(int((time.time_ns() - start_ns) / 1e9 // phase_duration), len(sequence) - 1)
        changed = last_observed is None or observed != last_observed
        out = {**row, "requested_phase": phase, "requested_workload_id": sequence[phase],
               "observed_workload_id": observed, "observed_workload_name": WORKLOAD_NAMES[observed],
               "classifier_rule": reason, "state_changed": str(changed).lower()}
        result_writer.writerow(out); results_file.flush()
        if changed:
            change_writer.writerow(out); changes_file.flush()
            markov.observe_workload(app_key=APP_KEY, app_id=APP_ID, scope_name=SCOPE,
                                    workload_id=observed, cgroup_id=cgroup_id,
                                    workload_name=WORKLOAD_NAMES[observed], timestamp_ns=now)
            last_observed = observed
        prev = values
        if not stress_started and time.time_ns() - start_ns >= int(len(sequence) * phase_duration * 1e9):
            stress_started = True
            # Keep pressure bounded inside the same user slice.  This creates
            # ordinary cgroup memory pressure without global drop_caches or
            # an OOM request, and lets the target app's lruvec be observed.
            stress = subprocess.Popen(
                ["systemd-run", "--user", "--scope", f"--slice={SLICE}",
                 "--unit=markov-pressure", "stress-ng", "--vm", "1",
                 "--vm-bytes", "3G", "--vm-keep", "--timeout", "30s"],
                stdout=(work_dir / "logs/stress.log").open("w"),
                stderr=subprocess.STDOUT,
            )
        if (time.time_ns() - start_ns) // 1_000_000_000 in {5, 15, 30}:
            marker = int((time.time_ns() - start_ns) // 1_000_000_000)
            if marker != during_index:
                during_index = marker
                (work_dir / f"markov/debugfs_during_{during_index}.txt").write_text(
                    snapshot_debugfs(DEBUGFS), encoding="utf-8"
                )
        time.sleep(1)
    if 'stress' in locals():
        stress.wait(timeout=40)
    gen_proc.wait(timeout=max(30, int(hold_after + 30)))
    gen_log.close()
    metrics_file.close(); results_file.close(); changes_file.close()
    markov.close()
    after = snapshot_debugfs(DEBUGFS)
    (work_dir / "markov/debugfs_after.txt").write_text(after)
    result = {"session_id": session_id, "generator_pid": generator_pid,
              "membership_result": membership["membership_result"],
              "observed_state_changes": sum(1 for _ in csv.DictReader(changes_path.open())),
              "debugfs_workload_update_ok": writer.workload_update_ok,
              "debugfs_markov_set_ok": writer.markov_set_ok,
              "online_predictions": markov.result().total_predictions,
              "online_predictions_resolved": markov.result().online_predictions_resolved,
              "causal_valid_predictions": markov.result().causal_valid_predictions,
              "prediction_hits": markov.result().prediction_hits,
              "prediction_misses": markov.result().prediction_misses,
              "unresolved_predictions": markov.result().unresolved_predictions,
              "future_information_rows": markov.future_information_rows,
              "kernel_prepare_calls_delta": parse_stat(after, "prepare_calls") - parse_stat(baseline, "prepare_calls"),
              "kernel_predictions_delta": parse_stat(after, "predictions") - parse_stat(baseline, "predictions"),
              "kernel_missing_hint_delta": parse_stat(after, "missing_hint") - parse_stat(baseline, "missing_hint"),
              "kernel_missing_transition_delta": parse_stat(after, "missing_transition") - parse_stat(baseline, "missing_transition"),
              "kernel_hist_lines_after": sum(x.startswith("hist ") for x in after.splitlines()),
              "kernel_markov_lines_after": sum(x.startswith("markov ") for x in after.splitlines()),
              "kernel_hint_lines_after": sum(x.startswith("hint ") for x in after.splitlines()),
              "kernel_hint_num_predicted": max(
                  [int(parts[5]) for line in after.splitlines()
                   for parts in [line.split()]
                   if line.startswith("hint ") and len(parts) >= 6 and parts[5].isdigit()] or [0]
              ),
              "per_folio_calls_delta": parse_stat(after, "per_folio_calls") - parse_stat(baseline, "per_folio_calls")}
    (work_dir / "markov/raw_e2e_result.json").write_text(json.dumps(result, indent=2))
    run([str(PROJECT_ROOT / "runtime_monitor/scripts/prepare_mglru_debugfs_access.sh"), "--restore"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--phase-duration-s", type=float, default=3)
    parser.add_argument("--hold-after-s", type=float, default=45)
    args = parser.parse_args()
    result = run_e2e(Path(args.work_dir), args.session_id, args.phase_duration_s, args.hold_after_s)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
