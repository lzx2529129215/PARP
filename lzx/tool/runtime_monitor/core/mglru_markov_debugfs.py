"""User-space writer for the MGLRU workload Markov debugfs interface."""

from __future__ import annotations

import csv
import datetime as dt
import subprocess
import sys
from pathlib import Path
from typing import Iterable


MGLRU_MARKOV_WRITE_FIELDS = [
    "session_id",
    "timestamp_ns",
    "timestamp",
    "event_type",
    "command",
    "status",
    "error",
    "foreground_app_key",
    "foreground_app_id",
    "foreground_cgroup_id",
    "predicted_app_ids",
    "predicted_confidences",
    "app_key",
    "app_id",
    "cgroup_id",
    "workload_id",
    "workload_name",
    "prev_workload_id",
    "current_workload_id",
    "next_workload_ids",
    "confidences",
    "boost_levels",
    "probability_fixed",
    "probability_float",
    "probability_source",
    "ttl_ms",
    "policy_command",
    "debugfs_path",
]

RANK_BASED_CONFIDENCE = [8000, 5000, 3000, 1000]

DUAL_MARKOV_WRITE_FIELDS = [
    "session_id", "timestamp_ns", "mode", "event_type", "app_key",
    "runtime_app_id", "cgroup_id", "previous_workload_id",
    "current_workload_id", "next_workload_id", "confidence_fixed",
    "boost_level", "command", "status", "error",
]


def resolve_scope_cgroup_id(slice_name: str, scope_name: str) -> int | None:
    """Resolve a user systemd scope path and return its cgroup inode."""
    if not slice_name or not scope_name:
        return None
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", slice_name, "-p", "ControlGroup", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"warning: failed to resolve cgroup control group for {slice_name}: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        stderr = result.stderr.strip()
        print(f"warning: systemctl failed for {slice_name}: {stderr}", file=sys.stderr)
        return None
    control_group = result.stdout.strip()
    if not control_group:
        print(f"warning: empty ControlGroup for {slice_name}", file=sys.stderr)
        return None
    scope_path = Path("/sys/fs/cgroup") / control_group.lstrip("/") / scope_name
    try:
        return int(scope_path.stat().st_ino)
    except OSError as exc:
        print(f"warning: cgroup scope missing for {scope_name}: {scope_path}: {exc}", file=sys.stderr)
        return None


def fixed_point_confidence(value: object, rank: int) -> int:
    """Convert probability-like values to 0..10000, with rank fallback."""
    fallback = RANK_BASED_CONFIDENCE[rank] if rank < len(RANK_BASED_CONFIDENCE) else 1000
    try:
        prob = float(value)
    except (TypeError, ValueError):
        return fallback
    if prob <= 0:
        return fallback
    if prob <= 1:
        return max(0, min(10000, int(round(prob * 10000))))
    if prob <= 10000:
        return max(0, min(10000, int(round(prob))))
    return fallback


class MGLRUMarkovDebugfsWriter:
    def __init__(
        self,
        *,
        enabled: bool,
        strict: bool,
        debugfs_path: str | Path,
        session_id: str,
        model_dir: Path,
        review_dir: Path,
        ttl_ms: int,
    ) -> None:
        self.enabled = bool(enabled)
        self.strict = bool(strict)
        self.debugfs_path = Path(debugfs_path)
        self.session_id = session_id
        self.ttl_ms = int(ttl_ms)
        self.csv_path = model_dir / "mglru_markov_debugfs_writes.csv"
        self.dual_csv_path = model_dir / "dual_markov_debugfs_writes.csv"
        self.summary_path = review_dir / "mglru_markov_debugfs_summary.md"
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.csv_path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=MGLRU_MARKOV_WRITE_FIELDS)
        self._writer.writeheader()
        self._file.flush()
        self._dual_file = self.dual_csv_path.open("w", encoding="utf-8", newline="")
        self._dual_writer = csv.DictWriter(
            self._dual_file, fieldnames=DUAL_MARKOV_WRITE_FIELDS
        )
        self._dual_writer.writeheader()
        self._dual_file.flush()

        self.debugfs_exists = self._debugfs_exists()
        self.total_write_attempts = 0
        self.current_app_write_ok = 0
        self.predicted_apps_write_ok = 0
        self.workload_update_write_attempts = 0
        self.workload_update_write_ok = 0
        self.workload_update_write_error = 0
        self.workload_update_skipped = 0
        self.markov_set_write_attempts = 0
        self.markov_set_write_ok = 0
        self.markov_set_write_error = 0
        self.markov_set_skipped = 0
        self.app_bind_write_attempts = 0
        self.app_bind_write_ok = 0
        self.app_bind_write_error = 0
        self.app_probability_write_attempts = 0
        self.app_probability_write_ok = 0
        self.app_probability_write_error = 0
        self.policy_config_write_attempts = 0
        self.policy_config_write_ok = 0
        self.policy_config_write_error = 0
        self.debugfs_missing_count = 0
        self.write_error_count = 0
        self.skipped_prediction_count = 0
        self.disabled_count = 0
        self.dry_run_count = 0
        self.confidence_source = "rank_based"
        self.workload_markov_transitions_csv = (
            model_dir / "workload_markov_transitions.csv"
        )
        self.workload_markov_summary = review_dir / "workload_markov_summary.md"
        self.foreground_workload_update_attempts = 0
        self.foreground_workload_update_ok = 0
        self.foreground_workload_update_error = 0
        self.continue_markov_set_attempts = 0
        self.continue_markov_set_ok = 0
        self.continue_markov_set_error = 0
        self.reentry_markov_set_attempts = 0
        self.reentry_markov_set_ok = 0
        self.reentry_markov_set_error = 0
        self.runtime_mode_write_attempts = 0
        self.runtime_mode_write_ok = 0
        self.runtime_mode_write_error = 0

        if self.enabled and not self.debugfs_exists:
            msg = f"mglru markov debugfs not found: {self.debugfs_path}"
            print(f"warning: {msg}", file=sys.stderr)
            self.debugfs_missing_count += 1

    def close(self) -> None:
        self._file.flush()
        self._file.close()
        self._dual_file.flush()
        self._dual_file.close()
        self.write_summary()

    def write_current_app(self, app_key: str, app_id: int, cgroup_id: int | None, ttl_ms: int | None = None) -> None:
        ttl = self.ttl_ms if ttl_ms is None else int(ttl_ms)
        if not cgroup_id:
            self._record(
                event_type="current_app",
                command="",
                status="dry_run",
                error="missing_cgroup_id",
                foreground_app_key=app_key,
                foreground_app_id=app_id,
                foreground_cgroup_id="",
            )
            return
        command = f"app current {int(app_id)} {int(cgroup_id)} {ttl}"
        status, error = self._write_command(command)
        if status == "ok":
            self.current_app_write_ok += 1
        self._record(
            event_type="current_app",
            command=command,
            status=status,
            error=error,
            foreground_app_key=app_key,
            foreground_app_id=app_id,
            foreground_cgroup_id=cgroup_id,
        )

    def write_predicted_apps(self, predictions: Iterable[tuple[int, int]], ttl_ms: int | None = None) -> None:
        ttl = self.ttl_ms if ttl_ms is None else int(ttl_ms)
        pairs = [(int(app_id), int(conf)) for app_id, conf in predictions if int(app_id) > 0]
        if not pairs:
            self.skipped_prediction_count += 1
            self._record(
                event_type="predicted_apps",
                command="",
                status="dry_run",
                error="no_valid_predictions",
            )
            return
        args = " ".join(f"{app_id} {max(0, min(10000, conf))}" for app_id, conf in pairs)
        command = f"app predict {ttl} {args}"
        status, error = self._write_command(command)
        if status == "ok":
            self.predicted_apps_write_ok += 1
        self._record(
            event_type="predicted_apps",
            command=command,
            status=status,
            error=error,
            predicted_app_ids="|".join(str(app_id) for app_id, _ in pairs),
            predicted_confidences="|".join(str(conf) for _, conf in pairs),
        )

    def skip_prediction(self, reason: str) -> None:
        self.skipped_prediction_count += 1
        self._record(
            event_type="predicted_apps",
            command="",
            status="dry_run",
            error=reason,
        )

    def write_app_binding(
        self,
        app_key: str,
        app_id: int,
        cgroup_id: int | None,
        ttl_ms: int,
    ) -> tuple[str, str, str]:
        if not cgroup_id:
            status, error, command = "dry_run", "missing_cgroup_id", ""
        else:
            command = f"app bind {int(app_id)} {int(cgroup_id)} {int(ttl_ms)}"
            self.app_bind_write_attempts += 1
            status, error = self._write_command(command)
            if status == "ok":
                self.app_bind_write_ok += 1
            else:
                self.app_bind_write_error += 1
        self._record(
            event_type="app_bind",
            command=command,
            status=status,
            error=error,
            app_key=app_key,
            app_id=app_id,
            cgroup_id="" if cgroup_id is None else cgroup_id,
            ttl_ms=ttl_ms,
        )
        return command, status, error

    def clear_app_bindings(self) -> tuple[str, str]:
        """仅清理内核 App Bind 表，保留 probability、Markov 与 policy 状态。"""
        command = "clear bind"
        status, error = self._write_command(command)
        self._record(
            event_type="app_bind_clear",
            command=command,
            status=status,
            error=error,
        )
        self._record_dual(
            mode="BIND",
            event_type="app_bind_clear",
            command=command,
            status=status,
            error=error,
        )
        return status, error

    def write_app_probability(
        self,
        app_key: str,
        app_id: int,
        probability: float | None,
        probability_source: str,
        ttl_ms: int,
    ) -> tuple[str, str, str]:
        if probability is None or probability_source == "unavailable":
            command, status, error = "", "dry_run", "probability_unavailable"
            probability_fixed: int | str = ""
        else:
            normalized = max(0.0, min(1.0, float(probability)))
            probability_fixed = max(0, min(10000, int(round(normalized * 10000))))
            command = f"app probability {int(app_id)} {probability_fixed} {int(ttl_ms)}"
            self.app_probability_write_attempts += 1
            status, error = self._write_command(command)
            if status == "ok":
                self.app_probability_write_ok += 1
            else:
                self.app_probability_write_error += 1
        self._record(
            event_type="app_probability",
            command=command,
            status=status,
            error=error,
            app_key=app_key,
            app_id=app_id,
            probability_fixed=probability_fixed,
            probability_float="" if probability is None else probability,
            probability_source=probability_source,
            ttl_ms=ttl_ms,
        )
        return command, status, error

    def write_all_app_probabilities(
        self,
        probabilities: Iterable[dict[str, object]],
        ttl_ms: int,
    ) -> list[tuple[str, str, str]]:
        results: list[tuple[str, str, str]] = []
        for entry in probabilities:
            raw_probability = entry.get("probability")
            probability = None if raw_probability in (None, "") else float(raw_probability)
            results.append(
                self.write_app_probability(
                    app_key=str(entry.get("app_key", "")),
                    app_id=int(entry.get("app_id", 0)),
                    probability=probability,
                    probability_source=str(entry.get("probability_source", "unavailable")),
                    ttl_ms=ttl_ms,
                )
            )
        return results

    def write_policy_command(self, command: str) -> tuple[str, str, str]:
        self.policy_config_write_attempts += 1
        status, error = self._write_command(command)
        if status == "ok":
            self.policy_config_write_ok += 1
        else:
            self.policy_config_write_error += 1
        self._record(
            event_type="policy_config",
            command=command,
            policy_command=command,
            status=status,
            error=error,
        )
        return command, status, error

    def read_snapshot(self) -> str:
        try:
            return self.debugfs_path.read_text(encoding="utf-8")
        except PermissionError:
            pass
        except OSError:
            return ""
        try:
            result = subprocess.run(
                ["sudo", "-n", "cat", str(self.debugfs_path)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return result.stdout if result.returncode == 0 else ""

    def write_workload_update(
        self,
        cgroup_id: int | None,
        app_id: int | None,
        workload_id: int | None,
        *,
        app_key: str = "",
        workload_name: str = "",
    ) -> tuple[str, str]:
        if not cgroup_id or not app_id or workload_id is None:
            self.workload_update_skipped += 1
            missing = [
                name
                for name, value in (
                    ("cgroup_id", cgroup_id),
                    ("app_id", app_id),
                    ("workload_id", workload_id),
                )
                if value is None or (name != "workload_id" and not value)
            ]
            self._record(
                event_type="workload_update",
                command="",
                status="dry_run",
                error=f"missing_{'_'.join(missing)}",
                app_key=app_key,
                app_id="" if app_id is None else app_id,
                cgroup_id="" if cgroup_id is None else cgroup_id,
                workload_id="" if workload_id is None else workload_id,
                workload_name=workload_name,
            )
            self._record_dual(
                mode="RUNTIME", event_type="runtime_workload_update", command="",
                status="dry_run", error=f"missing_{'_'.join(missing)}", app_key=app_key,
                runtime_app_id=app_id, cgroup_id=cgroup_id,
                current_workload_id=workload_id,
            )
            return "dry_run", "missing_required_field"
        command = (
            f"workload update {int(cgroup_id)} {int(app_id)} {int(workload_id)}"
        )
        self.workload_update_write_attempts += 1
        status, error = self._write_command(command)
        if status == "ok":
            self.workload_update_write_ok += 1
        else:
            self.workload_update_write_error += 1
        self._record(
            event_type="workload_update",
            command=command,
            status=status,
            error=error,
            app_key=app_key,
            app_id=app_id,
            cgroup_id=cgroup_id,
            workload_id=workload_id,
            workload_name=workload_name,
        )
        self._record_dual(
            mode="RUNTIME", event_type="runtime_workload_update", command=command,
            status=status, error=error, app_key=app_key, runtime_app_id=app_id,
            cgroup_id=cgroup_id, current_workload_id=workload_id,
        )
        return status, error

    def write_runtime_workload(self, **kwargs: object) -> tuple[str, str]:
        """新 ABI 名称；保留 write_workload_update 作为兼容别名。"""
        return self.write_workload_update(**kwargs)

    def write_dual_runtime_mode(self, mode: str = "dual") -> tuple[str, str]:
        valid_modes = {"disabled", "legacy", "dual", "both_observe"}
        if mode not in valid_modes:
            self.runtime_mode_write_error += 1
            self._record_dual(
                mode=mode.upper(), event_type="runtime_mode", command="",
                status="dry_run", error="invalid_runtime_mode",
            )
            return "dry_run", "invalid_runtime_mode"
        command = f"markov runtime_mode {mode}"
        self.runtime_mode_write_attempts += 1
        status, error = self._write_command(command)
        if status == "ok":
            self.runtime_mode_write_ok += 1
        else:
            self.runtime_mode_write_error += 1
        self._record_dual(
            mode=mode.upper(), event_type="runtime_mode", command=command,
            status=status, error=error,
        )
        return status, error

    @staticmethod
    def _valid_workload(workload_id: int | None) -> bool:
        return workload_id is not None and 0 <= int(workload_id) <= 6

    def write_foreground_workload(
        self,
        *,
        cgroup_id: int | None,
        app_id: int | None,
        workload_id: int | None,
        ttl_ms: int | None = None,
        app_key: str = "",
    ) -> tuple[str, str]:
        ttl = self.ttl_ms if ttl_ms is None else int(ttl_ms)
        if not cgroup_id or not app_id or not self._valid_workload(workload_id) or ttl <= 0:
            self.foreground_workload_update_error += 1
            self._record_dual(
                mode="CONTINUE", event_type="foreground_workload_update", command="",
                status="dry_run", error="invalid_cgroup_app_workload_or_ttl",
                app_key=app_key, runtime_app_id=app_id, cgroup_id=cgroup_id,
                current_workload_id=workload_id,
            )
            return "dry_run", "invalid_arguments"
        command = f"foreground workload {int(cgroup_id)} {int(app_id)} {int(workload_id)} {ttl}"
        self.foreground_workload_update_attempts += 1
        status, error = self._write_command(command)
        if status == "ok":
            self.foreground_workload_update_ok += 1
        else:
            self.foreground_workload_update_error += 1
        self._record_dual(
            mode="CONTINUE", event_type="foreground_workload_update", command=command,
            status=status, error=error, app_key=app_key, runtime_app_id=app_id,
            cgroup_id=cgroup_id, current_workload_id=workload_id,
        )
        return status, error

    def write_continue_markov(
        self,
        *,
        app_id: int | None,
        previous_workload_id: int | None,
        current_workload_id: int | None,
        next_workload_id: int | None,
        confidence_fixed: int,
        boost_level: int,
        app_key: str = "",
    ) -> tuple[str, str]:
        valid = (
            app_id is not None and int(app_id) > 0
            and self._valid_workload(previous_workload_id)
            and self._valid_workload(current_workload_id)
            and self._valid_workload(next_workload_id)
            and 0 <= int(confidence_fixed) <= 10000
            and 0 <= int(boost_level) <= 3
        )
        if not valid:
            self.continue_markov_set_error += 1
            self._record_dual(
                mode="CONTINUE", event_type="continue_markov_set", command="",
                status="dry_run", error="invalid_arguments", app_key=app_key,
                runtime_app_id=app_id, previous_workload_id=previous_workload_id,
                current_workload_id=current_workload_id, next_workload_id=next_workload_id,
                confidence_fixed=confidence_fixed, boost_level=boost_level,
            )
            return "dry_run", "invalid_arguments"
        command = (
            f"markov continue set {int(app_id)} {int(previous_workload_id)} "
            f"{int(current_workload_id)} {int(next_workload_id)} "
            f"{int(confidence_fixed)} {int(boost_level)}"
        )
        self.continue_markov_set_attempts += 1
        status, error = self._write_command(command)
        if status == "ok":
            self.continue_markov_set_ok += 1
        else:
            self.continue_markov_set_error += 1
        self._record_dual(
            mode="CONTINUE", event_type="continue_markov_set", command=command,
            status=status, error=error, app_key=app_key, runtime_app_id=app_id,
            previous_workload_id=previous_workload_id,
            current_workload_id=current_workload_id, next_workload_id=next_workload_id,
            confidence_fixed=confidence_fixed, boost_level=boost_level,
        )
        return status, error

    def write_reentry_markov(
        self,
        *,
        app_id: int | None,
        next_workload_id: int | None,
        confidence_fixed: int,
        boost_level: int,
        app_key: str = "",
    ) -> tuple[str, str]:
        valid = (
            app_id is not None and int(app_id) > 0
            and self._valid_workload(next_workload_id)
            and 0 <= int(confidence_fixed) <= 10000
            and 0 <= int(boost_level) <= 3
        )
        if not valid:
            self.reentry_markov_set_error += 1
            self._record_dual(
                mode="REENTRY", event_type="reentry_markov_set", command="",
                status="dry_run", error="invalid_arguments", app_key=app_key,
                runtime_app_id=app_id, next_workload_id=next_workload_id,
                confidence_fixed=confidence_fixed, boost_level=boost_level,
            )
            return "dry_run", "invalid_arguments"
        command = (
            f"markov reentry set {int(app_id)} {int(next_workload_id)} "
            f"{int(confidence_fixed)} {int(boost_level)}"
        )
        self.reentry_markov_set_attempts += 1
        status, error = self._write_command(command)
        if status == "ok":
            self.reentry_markov_set_ok += 1
        else:
            self.reentry_markov_set_error += 1
        self._record_dual(
            mode="REENTRY", event_type="reentry_markov_set", command=command,
            status=status, error=error, app_key=app_key, runtime_app_id=app_id,
            next_workload_id=next_workload_id, confidence_fixed=confidence_fixed,
            boost_level=boost_level,
        )
        return status, error

    def write_markov_set(
        self,
        app_id: int | None,
        prev_workload_id: int | None,
        current_workload_id: int | None,
        entries: Iterable[dict[str, int]],
        *,
        app_key: str = "",
    ) -> tuple[str, str]:
        normalized: list[tuple[int, int, int]] = []
        for entry in entries:
            try:
                next_workload_id = int(entry["next_workload_id"])
                confidence = max(0, min(10000, int(entry["confidence"])))
                boost_level = max(0, int(entry["boost_level"]))
            except (KeyError, TypeError, ValueError):
                continue
            normalized.append((next_workload_id, confidence, boost_level))
            if len(normalized) >= 4:
                break
        if (
            app_id is None
            or prev_workload_id is None
            or current_workload_id is None
            or not normalized
        ):
            self.markov_set_skipped += 1
            self._record(
                event_type="markov_set",
                command="",
                status="dry_run",
                error="missing_key_or_entries",
                app_key=app_key,
                app_id="" if app_id is None else app_id,
                prev_workload_id=(
                    "" if prev_workload_id is None else prev_workload_id
                ),
                current_workload_id=(
                    "" if current_workload_id is None else current_workload_id
                ),
            )
            return "dry_run", "missing_key_or_entries"
        args = " ".join(
            f"{next_id} {confidence} {boost}"
            for next_id, confidence, boost in normalized
        )
        command = (
            f"markov set {int(app_id)} {int(prev_workload_id)} "
            f"{int(current_workload_id)} {args}"
        )
        self.markov_set_write_attempts += 1
        status, error = self._write_command(command)
        if status == "ok":
            self.markov_set_write_ok += 1
        else:
            self.markov_set_write_error += 1
        self._record(
            event_type="markov_set",
            command=command,
            status=status,
            error=error,
            app_key=app_key,
            app_id=app_id,
            prev_workload_id=prev_workload_id,
            current_workload_id=current_workload_id,
            next_workload_ids="|".join(
                str(next_id) for next_id, _, _ in normalized
            ),
            confidences="|".join(
                str(confidence) for _, confidence, _ in normalized
            ),
            boost_levels="|".join(
                str(boost) for _, _, boost in normalized
            ),
        )
        return status, error

    def _write_command(self, command: str) -> tuple[str, str]:
        self.total_write_attempts += 1
        if not self.enabled:
            self.disabled_count += 1
            return "disabled", ""
        if not self._debugfs_exists():
            self.debugfs_exists = False
            self.debugfs_missing_count += 1
            return "debugfs_missing", f"debugfs path does not exist: {self.debugfs_path}"
        self.debugfs_exists = True
        try:
            self.debugfs_path.write_text(command + "\n", encoding="utf-8")
        except PermissionError:
            try:
                result = subprocess.run(
                    ["sudo", "-n", "tee", str(self.debugfs_path)],
                    input=command + "\n",
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.write_error_count += 1
                return "write_error", str(exc)
            if result.returncode != 0:
                self.write_error_count += 1
                return "write_error", result.stderr.strip() or f"sudo tee exit={result.returncode}"
        except OSError as exc:
            self.write_error_count += 1
            return "write_error", str(exc)
        return "ok", ""

    def _record(
        self,
        *,
        event_type: str,
        command: str,
        status: str,
        error: str,
        foreground_app_key: str = "",
        foreground_app_id: int | str = "",
        foreground_cgroup_id: int | str = "",
        predicted_app_ids: str = "",
        predicted_confidences: str = "",
        app_key: str = "",
        app_id: int | str = "",
        cgroup_id: int | str = "",
        workload_id: int | str = "",
        workload_name: str = "",
        prev_workload_id: int | str = "",
        current_workload_id: int | str = "",
        next_workload_ids: str = "",
        confidences: str = "",
        boost_levels: str = "",
        probability_fixed: int | str = "",
        probability_float: float | str = "",
        probability_source: str = "",
        ttl_ms: int | str = "",
        policy_command: str = "",
    ) -> None:
        if status == "dry_run":
            self.dry_run_count += 1
        self._writer.writerow(
            {
                "session_id": self.session_id,
                "timestamp_ns": __import__("time").time_ns(),
                "timestamp": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "event_type": event_type,
                "command": command,
                "status": status,
                "error": error,
                "foreground_app_key": foreground_app_key,
                "foreground_app_id": foreground_app_id,
                "foreground_cgroup_id": foreground_cgroup_id,
                "predicted_app_ids": predicted_app_ids,
                "predicted_confidences": predicted_confidences,
                "app_key": app_key,
                "app_id": app_id,
                "cgroup_id": cgroup_id,
                "workload_id": workload_id,
                "workload_name": workload_name,
                "prev_workload_id": prev_workload_id,
                "current_workload_id": current_workload_id,
                "next_workload_ids": next_workload_ids,
                "confidences": confidences,
                "boost_levels": boost_levels,
                "probability_fixed": probability_fixed,
                "probability_float": probability_float,
                "probability_source": probability_source,
                "ttl_ms": ttl_ms,
                "policy_command": policy_command,
                "debugfs_path": str(self.debugfs_path),
            }
        )
        self._file.flush()

    def _record_dual(
        self,
        *,
        mode: str,
        event_type: str,
        command: str,
        status: str,
        error: str,
        app_key: str = "",
        runtime_app_id: int | str | None = "",
        cgroup_id: int | str | None = "",
        previous_workload_id: int | str | None = "",
        current_workload_id: int | str | None = "",
        next_workload_id: int | str | None = "",
        confidence_fixed: int | str | None = "",
        boost_level: int | str | None = "",
    ) -> None:
        self._dual_writer.writerow({
            "session_id": self.session_id,
            "timestamp_ns": __import__("time").time_ns(),
            "mode": mode,
            "event_type": event_type,
            "app_key": app_key,
            "runtime_app_id": "" if runtime_app_id is None else runtime_app_id,
            "cgroup_id": "" if cgroup_id is None else cgroup_id,
            "previous_workload_id": "" if previous_workload_id is None else previous_workload_id,
            "current_workload_id": "" if current_workload_id is None else current_workload_id,
            "next_workload_id": "" if next_workload_id is None else next_workload_id,
            "confidence_fixed": "" if confidence_fixed is None else confidence_fixed,
            "boost_level": "" if boost_level is None else boost_level,
            "command": command,
            "status": status,
            "error": error,
        })
        self._dual_file.flush()

    def final_result(self) -> str:
        if not self.enabled:
            return "PASS"
        if self.strict:
            if (
                self._debugfs_exists()
                and self.total_write_attempts > 0
                and self.debugfs_missing_count == 0
                and self.write_error_count == 0
                and self.workload_update_write_error == 0
                and self.markov_set_write_error == 0
                and self.foreground_workload_update_error == 0
                and self.continue_markov_set_error == 0
                and self.reentry_markov_set_error == 0
                and self.runtime_mode_write_error == 0
                and (
                    self.workload_update_write_attempts == 0
                    or self.workload_update_write_ok > 0
                )
                and (
                    self.markov_set_write_attempts == 0
                    or self.markov_set_write_ok > 0
                )
            ):
                return "PASS"
            return "FAIL"
        if self.write_error_count:
            return "PASS_WITH_WARNINGS"
        return "PASS_WITH_WARNINGS" if self.debugfs_missing_count else "PASS"

    def write_summary(self) -> None:
        self.debugfs_exists = self._debugfs_exists()
        lines = [
            "# MGLRU Markov debugfs 汇总",
            "",
            f"- enabled: {str(self.enabled).lower()}",
            f"- debugfs_path: `{self.debugfs_path}`",
            f"- debugfs_exists: {str(self.debugfs_exists).lower()}",
            f"- strict: {str(self.strict).lower()}",
            f"- total_write_attempts: {self.total_write_attempts}",
            f"- current_app_write_ok: {self.current_app_write_ok}",
            f"- predicted_apps_write_ok: {self.predicted_apps_write_ok}",
            f"- workload_update_write_attempts: {self.workload_update_write_attempts}",
            f"- workload_update_write_ok: {self.workload_update_write_ok}",
            f"- workload_update_write_error: {self.workload_update_write_error}",
            f"- workload_update_skipped: {self.workload_update_skipped}",
            f"- foreground_workload_update_attempts: {self.foreground_workload_update_attempts}",
            f"- foreground_workload_update_ok: {self.foreground_workload_update_ok}",
            f"- foreground_workload_update_error: {self.foreground_workload_update_error}",
            f"- continue_markov_set_attempts: {self.continue_markov_set_attempts}",
            f"- continue_markov_set_ok: {self.continue_markov_set_ok}",
            f"- continue_markov_set_error: {self.continue_markov_set_error}",
            f"- reentry_markov_set_attempts: {self.reentry_markov_set_attempts}",
            f"- reentry_markov_set_ok: {self.reentry_markov_set_ok}",
            f"- reentry_markov_set_error: {self.reentry_markov_set_error}",
            f"- runtime_mode_write_attempts: {self.runtime_mode_write_attempts}",
            f"- runtime_mode_write_ok: {self.runtime_mode_write_ok}",
            f"- runtime_mode_write_error: {self.runtime_mode_write_error}",
            f"- dual_markov_debugfs_csv: `{self.dual_csv_path}`",
            f"- markov_set_write_attempts: {self.markov_set_write_attempts}",
            f"- markov_set_write_ok: {self.markov_set_write_ok}",
            f"- markov_set_write_error: {self.markov_set_write_error}",
            f"- markov_set_skipped: {self.markov_set_skipped}",
            f"- app_bind_write_attempts: {self.app_bind_write_attempts}",
            f"- app_bind_write_ok: {self.app_bind_write_ok}",
            f"- app_bind_write_error: {self.app_bind_write_error}",
            f"- app_probability_write_attempts: {self.app_probability_write_attempts}",
            f"- app_probability_write_ok: {self.app_probability_write_ok}",
            f"- app_probability_write_error: {self.app_probability_write_error}",
            f"- policy_config_write_attempts: {self.policy_config_write_attempts}",
            f"- policy_config_write_ok: {self.policy_config_write_ok}",
            f"- policy_config_write_error: {self.policy_config_write_error}",
            f"- workload_markov_transitions_csv: `{self.workload_markov_transitions_csv}`",
            f"- workload_markov_summary: `{self.workload_markov_summary}`",
            f"- debugfs_missing_count: {self.debugfs_missing_count}",
            f"- write_error_count: {self.write_error_count}",
            f"- skipped_prediction_count: {self.skipped_prediction_count}",
            f"- confidence_source: {self.confidence_source}",
            f"- csv_path: `{self.csv_path}`",
            f"- final_result: {self.final_result()}",
        ]
        self.summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _debugfs_exists(self) -> bool:
        try:
            if self.debugfs_path.exists():
                return True
        except OSError as exc:
            print(f"warning: cannot access MGLRU Markov debugfs path {self.debugfs_path}: {exc}", file=sys.stderr)
        try:
            result = subprocess.run(
                ["sudo", "-n", "test", "-e", str(self.debugfs_path)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
