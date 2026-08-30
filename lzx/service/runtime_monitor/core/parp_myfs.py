"""Atomic LSTM prediction sink for the PARP ``/dev/myfs`` device.

The resident service submits one versioned, pointer-free ioctl containing
both application priors and all unambiguous live cgroup bindings.  No PARP
prediction is written through debugfs.  Device absence and malformed model
output are fail-closed so desktop event collection can continue.  lzx-note
"""

from __future__ import annotations

import csv
import ctypes
import errno
import fcntl
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Iterable

from core.working_set_predictor import WorkingSetPrediction, WorkingSetPredictor
from core.reclaim_workload import (  # lzx-note
    CgroupReclaimWorkloadProfiler,
    ReclaimWorkloadProfile,
    WORKLOAD_NAMES,
    profile_summary,
)


ABI_VERSION = 1
ABI_VERSION_V2 = 2
ABI_VERSION_V3 = 3  # lzx-note
MAX_APPS = 32
MAX_BINDINGS = 64
Q15_ONE = 32767
ENTRY_FOREGROUND = 1 << 0
BINDING_ACTIVE = 1 << 0
BINDING_WORKLOAD_VALID = 1 << 1  # lzx-note
STATE_WORKINGSET_VALID = 1 << 0
MYFS_MODES = {"off", "dry-run", "apply"}


class PredictEntryV1(ctypes.Structure):
    _fields_ = [
        ("app_id", ctypes.c_uint32),
        ("score_q15", ctypes.c_uint16),
        ("rank", ctypes.c_uint16),
        ("flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


class PredictBindingV1(ctypes.Structure):
    _fields_ = [
        ("domain_id", ctypes.c_uint64),
        ("app_id", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("epoch_id", ctypes.c_uint64),
        ("reserved", ctypes.c_uint64),
    ]


class PredictBindingV3(ctypes.Structure):  # lzx-note
    _fields_ = [
        ("domain_id", ctypes.c_uint64),
        ("app_id", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("epoch_id", ctypes.c_uint64),
        ("workload_hint", ctypes.c_uint64),
    ]


class PredictStateV1(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("struct_size", ctypes.c_uint32),
        ("schema_version", ctypes.c_uint32),
        ("model_version", ctypes.c_uint32),
        ("generation", ctypes.c_uint32),
        ("nr_predictions", ctypes.c_uint32),
        ("nr_bindings", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("timestamp_ns", ctypes.c_uint64),
        ("horizon_ns", ctypes.c_uint64),
        ("ttl_ns", ctypes.c_uint64),
        ("reserved", ctypes.c_uint64 * 5),
        ("predictions", PredictEntryV1 * MAX_APPS),
        ("bindings", PredictBindingV1 * MAX_BINDINGS),
    ]


class PredictStateV2(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("struct_size", ctypes.c_uint32),
        ("schema_version", ctypes.c_uint32),
        ("model_version", ctypes.c_uint32),
        ("generation", ctypes.c_uint32),
        ("nr_predictions", ctypes.c_uint32),
        ("nr_bindings", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("timestamp_ns", ctypes.c_uint64),
        ("horizon_ns", ctypes.c_uint64),
        ("ttl_ns", ctypes.c_uint64),
        ("policy_domain_id", ctypes.c_uint64),
        ("predicted_workingset_bytes", ctypes.c_uint64),
        ("predicted_resident_bytes", ctypes.c_uint64),
        ("workingset_confidence_q15", ctypes.c_uint16),
        ("workingset_estimator_version", ctypes.c_uint16),
        ("reserved32", ctypes.c_uint32),
        ("reserved64", ctypes.c_uint64),
        ("predictions", PredictEntryV1 * MAX_APPS),
        ("bindings", PredictBindingV1 * MAX_BINDINGS),
    ]


class PredictStateV3(ctypes.Structure):  # lzx-note
    _fields_ = [
        ("abi_version", ctypes.c_uint32),
        ("struct_size", ctypes.c_uint32),
        ("schema_version", ctypes.c_uint32),
        ("model_version", ctypes.c_uint32),
        ("generation", ctypes.c_uint32),
        ("nr_predictions", ctypes.c_uint32),
        ("nr_bindings", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("timestamp_ns", ctypes.c_uint64),
        ("horizon_ns", ctypes.c_uint64),
        ("ttl_ns", ctypes.c_uint64),
        ("policy_domain_id", ctypes.c_uint64),
        ("predicted_workingset_bytes", ctypes.c_uint64),
        ("predicted_resident_bytes", ctypes.c_uint64),
        ("workingset_confidence_q15", ctypes.c_uint16),
        ("workingset_estimator_version", ctypes.c_uint16),
        ("reserved32", ctypes.c_uint32),
        ("reserved64", ctypes.c_uint64),
        ("predictions", PredictEntryV1 * MAX_APPS),
        ("bindings", PredictBindingV3 * MAX_BINDINGS),
    ]


def _ioc(direction: int, ioctl_type: int, number: int, size: int) -> int:
    return (direction << 30) | (ioctl_type << 8) | number | (size << 16)


PARP_PREDICT_SET_STATE = _ioc(1, 0xB7, 1, ctypes.sizeof(PredictStateV1))
PARP_PREDICT_GET_STATE = _ioc(2, 0xB7, 2, ctypes.sizeof(PredictStateV1))
PARP_PREDICT_SET_STATE_V2 = _ioc(1, 0xB7, 3, ctypes.sizeof(PredictStateV2))
PARP_PREDICT_GET_STATE_V2 = _ioc(2, 0xB7, 4, ctypes.sizeof(PredictStateV2))
PARP_PREDICT_SET_STATE_V3 = _ioc(1, 0xB7, 5, ctypes.sizeof(PredictStateV3))  # lzx-note
PARP_PREDICT_GET_STATE_V3 = _ioc(2, 0xB7, 6, ctypes.sizeof(PredictStateV3))  # lzx-note


AUDIT_FIELDS = [
    "timestamp_ns", "event_type", "prediction_id", "generation", "current_app",
    "nr_predictions", "nr_bindings", "ambiguous_domains", "device", "mode",
    "ioctl_attempted", "ioctl_success", "errno", "latency_us", "status", "error",
    "kernel_abi_version", "workingset_valid", "policy_domain_id",
    "predicted_workingset_bytes", "predicted_resident_bytes",
    "predicted_growth_bytes", "workingset_confidence_q15", "workingset_action_hint",
    "workload_profiles", "workload_valid_profiles", "workload_classes",
    "workload_binding_details",  # lzx-note
]


def _q15(value: Any) -> int | None:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        return None
    return max(0, min(Q15_ONE, int(round(probability * Q15_ONE))))


class PARPMyfsBridge:
    """Convert one successful v3 inference into one atomic kernel update."""

    def __init__(
        self,
        *,
        mode: str,
        device: str | Path,
        runtime_scope: Any,
        output_dir: str | Path,
        session_id: str,
        model_version: int = 401,
        schema_version: int = 1,
        prior_ttl_ms: int = 180_000,
        horizon_ms: int = 180_000,
        cgroup_root: str | Path = "/sys/fs/cgroup",
    ) -> None:
        if mode not in MYFS_MODES:
            raise ValueError(f"invalid /dev/myfs mode: {mode}")
        if ctypes.sizeof(PredictStateV1) >= (1 << 14):
            raise RuntimeError("PARP myfs UAPI exceeds Linux ioctl size field")
        if (ctypes.sizeof(PredictStateV2) != ctypes.sizeof(PredictStateV1)
                or ctypes.sizeof(PredictStateV3) != ctypes.sizeof(PredictStateV1)):
            raise RuntimeError("PARP myfs v2 must preserve the v1 ioctl payload size")
        self.mode = mode
        self.device = Path(device)
        self.runtime_scope = runtime_scope
        self.session_id = session_id
        self.model_version = int(model_version)
        self.schema_version = int(schema_version)
        self.prior_ttl_ms = max(1, int(prior_ttl_ms))
        self.horizon_ms = max(1, int(horizon_ms))
        self.cgroup_root = Path(cgroup_root)
        self.generation = 0
        self.kernel_abi_version = 0
        self._last_alias_binding_count = 0
        self._last_binding_paths: dict[int, tuple[int, str, Path]] = {}
        self._alias_paths: dict[str, tuple[Path, ...]] = {}
        self._alias_scan_after_ns = 0
        self._successful_apps: set[str] = set()
        self._closed = False
        self._stats: dict[str, int] = {
            "events_received": 0,
            "successful_inferences": 0,
            "ioctl_attempts": 0,
            "ioctl_success": 0,
            "ioctl_failures": 0,
            "dry_runs": 0,
            "missing_device": 0,
            "invalid_predictions": 0,
            "ambiguous_domains": 0,
            "bindings_submitted": 0,
            "alias_bindings_submitted": 0,
            "alias_tree_scans": 0,
            "workingset_v2_submitted": 0,
            "workingset_v1_fallbacks": 0,
            "workload_v3_submitted": 0,  # lzx-note
            "workload_v2_fallbacks": 0,  # lzx-note
        }
        self.parp_dir = Path(output_dir) / "parp"
        self.parp_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.parp_dir / "myfs_events.csv"
        self.summary_path = self.parp_dir / "myfs_summary.json"
        self._audit_file = self.audit_path.open("w", encoding="utf-8", newline="")
        self._audit = csv.DictWriter(self._audit_file, fieldnames=AUDIT_FIELDS)
        self._audit.writeheader()
        self._audit_file.flush()
        self.workingset_predictor = WorkingSetPredictor(output_dir=self.parp_dir)
        self.workload_profiler = CgroupReclaimWorkloadProfiler()  # lzx-note
        self._synchronize_generation()
        self.preflight()

    def _open(self) -> int:
        return os.open(self.device, os.O_RDWR | os.O_CLOEXEC)

    def _get_state_with(self, state_type: Any, command: int) -> Any:
        buffer = bytearray(ctypes.sizeof(state_type))
        fd = self._open()
        try:
            fcntl.ioctl(fd, command, buffer, True)
        finally:
            os.close(fd)
        return state_type.from_buffer_copy(buffer)

    def _get_state(self) -> PredictStateV1 | PredictStateV2 | PredictStateV3:
        try:
            state = self._get_state_with(PredictStateV3, PARP_PREDICT_GET_STATE_V3)
            if (
                state.abi_version == ABI_VERSION_V3
                and state.struct_size == ctypes.sizeof(PredictStateV3)
            ):
                self.kernel_abi_version = ABI_VERSION_V3
                return state
        except OSError as exc:
            if exc.errno not in {errno.ENOTTY, errno.EINVAL}:
                raise
        try:
            state = self._get_state_with(PredictStateV2, PARP_PREDICT_GET_STATE_V2)
            if (
                state.abi_version == ABI_VERSION_V2
                and state.struct_size == ctypes.sizeof(PredictStateV2)
            ):
                self.kernel_abi_version = ABI_VERSION_V2
                return state
        except OSError as exc:
            if exc.errno not in {errno.ENOTTY, errno.EINVAL}:
                raise
        state = self._get_state_with(PredictStateV1, PARP_PREDICT_GET_STATE)
        if state.abi_version == ABI_VERSION:
            self.kernel_abi_version = ABI_VERSION
        return state

    def _synchronize_generation(self) -> None:
        if self.mode == "off":
            return
        try:
            state = self._get_state()
        except OSError:
            return
        if state.struct_size == ctypes.sizeof(PredictStateV1):
            self.generation = int(state.generation)

    def preflight(self) -> dict[str, Any]:
        exists = self.device.exists()
        writable = bool(exists and os.access(self.device, os.R_OK | os.W_OK))
        result = {
            "interface": "parp/myfs",
            "device": str(self.device),
            "mode": self.mode,
            "service_abi_version": ABI_VERSION_V3,  # lzx-note
            "kernel_abi_version": self.kernel_abi_version,
            "struct_size": ctypes.sizeof(PredictStateV1),
            "v2_struct_size": ctypes.sizeof(PredictStateV2),
            "workingset_prediction_supported": self.kernel_abi_version >= ABI_VERSION_V2,
            "workload_prediction_supported": self.kernel_abi_version >= ABI_VERSION_V3,  # lzx-note
            "exists": exists,
            "read_write": writable,
            "generation": self.generation,
            "status": "READY" if self.mode != "apply" or writable else "WAITING_FOR_DEVICE",
            "fail_closed": True,
            "debugfs_prediction_interface": False,
        }
        (self.parp_dir / "myfs_preflight.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return result

    def startup_bindings(self) -> None:
        # Bindings are sampled at the exact inference event and submitted in
        # the same ioctl; publishing startup-only bindings would be non-atomic.
        return

    def successful_binding_apps(self) -> set[str]:
        return set(self._successful_apps)

    def observe_working_sets(self, process_samples: Iterable[Any]) -> None:
        """Continuously learn GUI plus fixture WSS, independently of events."""
        if self.mode == "off" or self.runtime_scope is None:
            return
        app_ids = {
            int(app.app_id) for app in self.runtime_scope.apps
            if getattr(app, "prediction_enabled", False) and int(app.app_id) > 0
        }
        self._bindings(process_samples, app_ids)
        self.workingset_predictor.observe(
            self._last_binding_paths, time.monotonic_ns()
        )

    def submit_prediction(
        self,
        feature_row: dict[str, Any],
        prediction_result: dict[str, Any],
        *,
        process_samples: Iterable[Any] = (),
        event: dict[str, Any] | None = None,
    ) -> None:
        self._stats["events_received"] += 1
        if self.mode == "off":
            return
        if prediction_result.get("status") != "success":
            return
        self._stats["successful_inferences"] += 1

        entries, current_app = self._prediction_entries(feature_row, prediction_result)
        if not entries:
            self._stats["invalid_predictions"] += 1
            self._record(event, prediction_result, current_app, 0, 0, 0,
                         status="DROPPED", error="no valid runtime-scope predictions")
            return
        bindings, successful_apps, ambiguous = self._bindings(process_samples, {row[0] for row in entries})
        self._stats["ambiguous_domains"] += ambiguous
        self._stats["bindings_submitted"] += len(bindings)
        self._stats["alias_bindings_submitted"] += self._last_alias_binding_count
        self.generation += 1
        now_ns = time.monotonic_ns()
        self.workingset_predictor.observe(self._last_binding_paths, now_ns)
        workingset = self.workingset_predictor.predict(
            entries,
            prediction_id=str(prediction_result.get("prediction_id", "")),
            timestamp_ns=now_ns,
            foreground_flag=ENTRY_FOREGROUND,
        )
        workload_profiles = self.workload_profiler.sample(self._last_binding_paths)  # lzx-note
        state_v1 = self._make_state_v1(entries, bindings, event, now_ns)
        state_v2 = self._make_state_v2(
            entries, bindings, event, now_ns, workingset
        )
        state_v3 = self._make_state_v3(  # lzx-note
            entries, bindings, event, now_ns, workingset, workload_profiles
        )

        if self.mode == "dry-run":
            self._stats["dry_runs"] += 1
            self._record(event, prediction_result, current_app, len(entries), len(bindings), ambiguous,
                         status="DRY_RUN", workingset=workingset,
                         workload_profiles=workload_profiles)
            return

        start_ns = time.monotonic_ns()
        error_number = 0
        error_text = ""
        for attempt in range(2):
            self._stats["ioctl_attempts"] += 1
            try:
                fd = self._open()
                try:
                    use_v3 = self.kernel_abi_version == ABI_VERSION_V3  # lzx-note
                    use_v2 = self.kernel_abi_version == ABI_VERSION_V2
                    state = state_v3 if use_v3 else state_v2 if use_v2 else state_v1
                    command = (PARP_PREDICT_SET_STATE_V3 if use_v3 else
                               PARP_PREDICT_SET_STATE_V2 if use_v2 else
                               PARP_PREDICT_SET_STATE)
                    payload = bytearray(bytes(state))
                    fcntl.ioctl(fd, command, payload, True)
                finally:
                    os.close(fd)
                self._stats["ioctl_success"] += 1
                if use_v2:
                    self._stats["workingset_v2_submitted"] += 1
                elif not use_v3:
                    self._stats["workingset_v1_fallbacks"] += 1
                if use_v3:
                    self._stats["workload_v3_submitted"] += 1
                else:
                    self._stats["workload_v2_fallbacks"] += 1
                self._successful_apps = successful_apps
                self._record(
                    event, prediction_result, current_app, len(entries), len(bindings), ambiguous,
                    attempted=True, success=True,
                    latency_us=(time.monotonic_ns() - start_ns) // 1000, status="APPLIED",
                    workingset=workingset, workload_profiles=workload_profiles,
                )
                return
            except OSError as exc:
                error_number = int(exc.errno or 0)
                error_text = str(exc)
                if error_number == errno.ENOENT:
                    self._stats["missing_device"] += 1
                if attempt == 0 and error_number in {errno.EALREADY, errno.ESTALE}:
                    self._synchronize_generation()
                    self.generation += 1
                    state_v1.generation = self.generation
                    state_v2.generation = self.generation
                    state_v3.generation = self.generation  # lzx-note
                    continue
                break
        self._stats["ioctl_failures"] += 1
        self._record(
            event, prediction_result, current_app, len(entries), len(bindings), ambiguous,
            attempted=True, success=False, error_number=error_number,
            latency_us=(time.monotonic_ns() - start_ns) // 1000,
            status="FAIL_CLOSED", error=error_text, workingset=workingset,
            workload_profiles=workload_profiles,  # lzx-note
        )

    def _prediction_entries(
        self, feature_row: dict[str, Any], prediction_result: dict[str, Any]
    ) -> tuple[list[tuple[int, int, int, int]], str]:
        if self.runtime_scope is None:
            return [], ""
        by_key = {str(app.app_key): app for app in self.runtime_scope.apps}
        by_vocab = {str(app.vocab_name): app for app in self.runtime_scope.apps}
        current_key = str(feature_row.get("foreground_app", "")).strip()
        current = by_key.get(current_key) or by_vocab.get(
            str(prediction_result.get("mapped_foreground_app", "")).strip()
        )
        scores: dict[int, tuple[Any, int]] = {}
        rows = prediction_result.get("all_probabilities") or prediction_result.get("outputs") or []
        if isinstance(rows, Iterable) and not isinstance(rows, (str, bytes, dict)):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                app = by_vocab.get(str(row.get("app", "")).strip()) or by_key.get(
                    str(row.get("app_key", "")).strip()
                )
                if app is None or not app.prediction_enabled or int(app.app_id) <= 0:
                    continue
                score = _q15(row.get("probability", row.get("next_use_probability")))
                if score is None:
                    continue
                scores[int(app.app_id)] = (app, score)
        if current is not None:
            scores.pop(int(current.app_id), None)
        ordered = sorted(scores.values(), key=lambda item: (-item[1], int(item[0].app_id)))
        result: list[tuple[int, int, int, int]] = []
        next_rank = 1
        if current is not None and current.prediction_enabled and int(current.app_id) > 0:
            result.append((int(current.app_id), Q15_ONE, next_rank, ENTRY_FOREGROUND))
            next_rank += 1
        for app, score in ordered[: MAX_APPS - len(result)]:
            result.append((int(app.app_id), score, next_rank, 0))
            next_rank += 1
        return result, str(getattr(current, "app_key", current_key) if current is not None else current_key)

    def _bindings(
        self, process_samples: Iterable[Any], allowed_app_ids: set[int]
    ) -> tuple[list[tuple[int, int]], set[str], int]:
        if self.runtime_scope is None:
            return [], set(), 0
        by_key = {str(app.app_key): app for app in self.runtime_scope.apps}
        domains: dict[int, dict[int, tuple[str, Path]]] = {}
        alias_domain_ids: set[int] = set()
        for sample in process_samples:
            app = by_key.get(str(getattr(sample, "app_id", "")))
            if app is None or int(app.app_id) not in allowed_app_ids:
                continue
            cgroup_path = str(getattr(getattr(sample, "identity", None), "cgroup_path", ""))
            if not cgroup_path:
                continue
            path = self.cgroup_root / cgroup_path.lstrip("/")
            try:
                domain_id = int(path.stat().st_ino)
            except OSError:
                continue
            domains.setdefault(domain_id, {})[int(app.app_id)] = (
                str(app.app_key), path
            )
        # Fixture scopes are binding-only aliases. Resolve their live cgroup
        # directories directly so their large synthetic working sets receive
        # the same App ID without entering GUI process/lifecycle accounting.
        # lzx-note
        alias_owners: dict[str, tuple[int, str]] = {}
        for app in self.runtime_scope.apps:
            app_id = int(getattr(app, "app_id", 0) or 0)
            if app_id not in allowed_app_ids:
                continue
            for scope_name in getattr(app, "binding_scope_names", ()):
                alias_owners[str(scope_name)] = (app_id, str(app.app_key))
        now_ns = time.monotonic_ns()
        if alias_owners and now_ns >= self._alias_scan_after_ns:
            discovered: dict[str, list[Path]] = {
                name: [] for name in alias_owners
            }
            try:
                for path in self.cgroup_root.rglob("*"):
                    if path.name in discovered:
                        discovered[path.name].append(path)
            except OSError:
                pass
            self._alias_paths = {
                name: tuple(paths) for name, paths in discovered.items()
            }
            self._alias_scan_after_ns = now_ns + 1_000_000_000
            self._stats["alias_tree_scans"] += 1
        for scope_name, (app_id, app_key) in alias_owners.items():
            # One cgroup-tree walk discovers every fixture alias.  Continuous
            # WSS sampling must not perform one recursive walk per app. lzx-note
            for path in self._alias_paths.get(scope_name, ()):
                try:
                    domain_id = int(path.stat().st_ino)
                except OSError:
                    continue
                domains.setdefault(domain_id, {})[app_id] = (app_key, path)
                alias_domain_ids.add(domain_id)
        result: list[tuple[int, int]] = []
        apps: set[str] = set()
        ambiguous = 0
        submitted_aliases = 0
        binding_paths: dict[int, tuple[int, str, Path]] = {}
        for domain_id, owners in sorted(domains.items()):
            if len(owners) != 1:
                ambiguous += 1
                continue
            app_id, (app_key, path) = next(iter(owners.items()))
            result.append((domain_id, app_id))
            binding_paths[domain_id] = (app_id, app_key, path)
            apps.add(app_key)
            if domain_id in alias_domain_ids:
                submitted_aliases += 1
            if len(result) == MAX_BINDINGS:
                break
        self._last_alias_binding_count = submitted_aliases
        self._last_binding_paths = binding_paths
        return result, apps, ambiguous

    def _fill_state(
        self,
        state: PredictStateV1 | PredictStateV2 | PredictStateV3,
        entries: list[tuple[int, int, int, int]],
        bindings: list[tuple[int, int]],
        event: dict[str, Any] | None,
        now_ns: int,
        workload_profiles: dict[int, ReclaimWorkloadProfile] | None = None,
    ) -> None:
        state.schema_version = self.schema_version
        state.model_version = self.model_version
        state.generation = self.generation
        state.nr_predictions = len(entries)
        state.nr_bindings = len(bindings)
        state.timestamp_ns = now_ns
        state.horizon_ns = self.horizon_ms * 1_000_000
        state.ttl_ns = self.prior_ttl_ms * 1_000_000
        epoch_id = int((event or {}).get("ts_ns") or state.timestamp_ns)
        for index, (app_id, score, rank, flags) in enumerate(entries):
            state.predictions[index] = PredictEntryV1(app_id, score, rank, flags, 0)
        for index, (domain_id, app_id) in enumerate(bindings):
            profile = (workload_profiles or {}).get(domain_id)
            if isinstance(state, PredictStateV3):
                flags = BINDING_ACTIVE
                hint = 0
                if profile is not None and profile.valid:
                    flags |= BINDING_WORKLOAD_VALID
                    hint = profile.workload_hint()
                state.bindings[index] = PredictBindingV3(
                    domain_id, app_id, flags, epoch_id, hint
                )
            else:
                state.bindings[index] = PredictBindingV1(
                    domain_id, app_id, BINDING_ACTIVE, epoch_id, 0
                )

    def _make_state_v1(
        self,
        entries: list[tuple[int, int, int, int]],
        bindings: list[tuple[int, int]],
        event: dict[str, Any] | None,
        now_ns: int,
    ) -> PredictStateV1:
        state = PredictStateV1()
        state.abi_version = ABI_VERSION
        state.struct_size = ctypes.sizeof(PredictStateV1)
        self._fill_state(state, entries, bindings, event, now_ns)
        return state

    def _make_state_v2(
        self,
        entries: list[tuple[int, int, int, int]],
        bindings: list[tuple[int, int]],
        event: dict[str, Any] | None,
        now_ns: int,
        workingset: WorkingSetPrediction,
    ) -> PredictStateV2:
        state = PredictStateV2()
        state.abi_version = ABI_VERSION_V2
        state.struct_size = ctypes.sizeof(PredictStateV2)
        self._fill_state(state, entries, bindings, event, now_ns)
        if workingset.valid:
            state.flags |= STATE_WORKINGSET_VALID
            state.policy_domain_id = workingset.policy_domain_id
            state.predicted_workingset_bytes = workingset.predicted_workingset_bytes
            state.predicted_resident_bytes = workingset.predicted_resident_bytes
            state.workingset_confidence_q15 = workingset.confidence_q15
            state.workingset_estimator_version = workingset.estimator_version
        return state

    def _make_state_v3(
        self,
        entries: list[tuple[int, int, int, int]],
        bindings: list[tuple[int, int]],
        event: dict[str, Any] | None,
        now_ns: int,
        workingset: WorkingSetPrediction,
        workload_profiles: dict[int, ReclaimWorkloadProfile],
    ) -> PredictStateV3:
        """Publish LSTM, WSS, and cgroup composition in one ioctl.  lzx-note"""
        state = PredictStateV3()
        state.abi_version = ABI_VERSION_V3
        state.struct_size = ctypes.sizeof(PredictStateV3)
        self._fill_state(
            state, entries, bindings, event, now_ns, workload_profiles
        )
        if workingset.valid:
            state.flags |= STATE_WORKINGSET_VALID
            state.policy_domain_id = workingset.policy_domain_id
            state.predicted_workingset_bytes = workingset.predicted_workingset_bytes
            state.predicted_resident_bytes = workingset.predicted_resident_bytes
            state.workingset_confidence_q15 = workingset.confidence_q15
            state.workingset_estimator_version = workingset.estimator_version
        return state

    def _record(
        self,
        event: dict[str, Any] | None,
        result: dict[str, Any],
        current_app: str,
        nr_predictions: int,
        nr_bindings: int,
        ambiguous: int,
        *,
        attempted: bool = False,
        success: bool = False,
        error_number: int = 0,
        latency_us: int = 0,
        status: str,
        error: str = "",
        workingset: WorkingSetPrediction = WorkingSetPrediction(),
        workload_profiles: dict[int, ReclaimWorkloadProfile] | None = None,
    ) -> None:
        workload_details: list[dict[str, Any]] = []  # lzx-note
        for domain_id, profile in sorted((workload_profiles or {}).items()):
            binding = self._last_binding_paths.get(domain_id)
            workload_details.append({
                "domain_id": int(domain_id),
                "app_id": int(binding[0]) if binding else 0,
                "app_key": str(binding[1]) if binding else "",
                "scope": str(binding[2].name) if binding else "",
                "class": WORKLOAD_NAMES.get(profile.workload_class, "UNKNOWN"),
                "valid": bool(profile.valid),
                "swappiness": int(profile.swappiness),
                "allow_writepage": bool(profile.allow_writepage),
            })
        self._audit.writerow({
            "timestamp_ns": time.monotonic_ns(),
            "event_type": str((event or {}).get("event_type", result.get("trigger_type", ""))),
            "prediction_id": str(result.get("prediction_id", "")),
            "generation": self.generation,
            "current_app": current_app,
            "nr_predictions": nr_predictions,
            "nr_bindings": nr_bindings,
            "ambiguous_domains": ambiguous,
            "device": str(self.device),
            "mode": self.mode,
            "ioctl_attempted": str(attempted).lower(),
            "ioctl_success": str(success).lower(),
            "errno": error_number,
            "latency_us": latency_us,
            "status": status,
            "error": error,
            "kernel_abi_version": self.kernel_abi_version,
            "workingset_valid": str(workingset.valid).lower(),
            "policy_domain_id": workingset.policy_domain_id,
            "predicted_workingset_bytes": workingset.predicted_workingset_bytes,
            "predicted_resident_bytes": workingset.predicted_resident_bytes,
            "predicted_growth_bytes": workingset.predicted_growth_bytes,
            "workingset_confidence_q15": workingset.confidence_q15,
            "workingset_action_hint": workingset.action_hint,
            "workload_profiles": len(workload_profiles or {}),
            "workload_valid_profiles": sum(
                int(profile.valid) for profile in (workload_profiles or {}).values()
            ),
            "workload_classes": json.dumps(
                profile_summary(workload_profiles or {}).get("classes", {}),
                sort_keys=True,
            ),
            "workload_binding_details": json.dumps(workload_details, sort_keys=True),  # lzx-note
        })
        self._audit_file.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        summary = {
            "session_id": self.session_id,
            "interface": "parp/myfs",
            "device": str(self.device),
            "mode": self.mode,
            "service_abi_version": ABI_VERSION_V3,  # lzx-note
            "kernel_abi_version": self.kernel_abi_version,
            "struct_size": ctypes.sizeof(PredictStateV1),
            "last_generation": self.generation,
            "successful_binding_apps": sorted(self._successful_apps),
            "stats": self._stats,
            "debugfs_prediction_interface": False,
        }
        self.summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        self.workingset_predictor.close()
        self._audit_file.close()
