"""Event-driven Test4B ballast control with foreground-only allocation.

The C ballast is intentionally a very small state machine.  This module owns
the *policy boundary*: it checks the foreground, PARP AppBind completion, the
ballast PID's cgroup, and the per-app/global size budgets before it ever sends
``ALLOCATE``.  It never detects GUI state itself and therefore cannot create a
new LSTM trigger.
"""
from __future__ import annotations

import csv
import json
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.writer import CsvWriter


MIB = 1024 * 1024
ALLOCATION_FIELDS = [
    "timestamp_ns", "app_key", "command", "foreground_app", "ballast_pid",
    "expected_cgroup", "actual_cgroup", "appbind_ready", "requested_bytes",
    "global_allocated_bytes", "status", "reject_reason", "response",
]
REACCESS_FIELDS = [
    "timestamp_ns", "app_key", "command", "status", "response", "error",
]
REGION_FIELDS = [
    "timestamp_ns", "app_key", "state", "allocated", "background_since_ns",
    "anon_cold_bytes", "anon_hot_bytes", "file_cold_bytes", "file_hot_bytes",
    "hot_accesses", "cold_accesses", "status", "error",
]
EVENT_FIELDS = [
    "timestamp_ns", "app_key", "source_event", "old_app", "foreground_app",
    "action", "status", "detail",
]


def _fields(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for token in text.strip().split():
        if "=" in token:
            key, value = token.split("=", 1)
            result[key] = value
    return result


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class BallastSpec:
    app_key: str
    socket_path: Path
    log_path: Path
    file_path: Path
    scope_name: str
    anon_cold_bytes: int = 32 * MIB
    anon_hot_bytes: int = 8 * MIB
    file_cold_bytes: int = 48 * MIB
    file_hot_bytes: int = 8 * MIB

    @property
    def total_bytes(self) -> int:
        return self.anon_cold_bytes + self.anon_hot_bytes + self.file_cold_bytes + self.file_hot_bytes


@dataclass
class BallastStatus:
    app_key: str
    state: str = "UNAVAILABLE"
    allocated: bool = False
    pid: int = 0
    background_since_ns: int = 0
    expected_cgroup: str = ""
    actual_cgroup: str = ""
    error: str = ""
    hot_accesses: int = 0
    cold_accesses: int = 0
    cold_bytes: int = 0
    total_bytes: int = 0

    def quiet_ns(self, now_ns: int) -> int:
        return max(0, int(now_ns) - self.background_since_ns) if self.background_since_ns else 0


class BallastClient:
    def __init__(self, path: Path, timeout_s: float = 1.5) -> None:
        self.path = path
        self.timeout_s = timeout_s

    def command(self, value: str) -> tuple[bool, str]:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(self.timeout_s)
                stream.connect(str(self.path))
                stream.sendall((value + "\n").encode("ascii"))
                response = stream.recv(2048).decode("utf-8", errors="replace").strip()
        except OSError as exc:
            return False, f"SOCKET_{type(exc).__name__}"
        return response.startswith("OK "), response


class Test4BBallastCoordinator:
    """Coordinate all sidecars without polling X11 or creating model events."""

    def __init__(self, *, session_id: str, output_dir: Path, runtime_scope: Any, config_path: Path) -> None:
        self.session_id = session_id
        self.output_dir = Path(output_dir)
        self.scope = runtime_scope
        self.config_path = Path(config_path)
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.marker = str(raw.get("slice", "test4b-experiment.slice"))
        if self.marker != "test4b-experiment.slice":
            raise ValueError("Test4B accepts only test4b-experiment.slice")
        self.max_global_bytes = _int(raw.get("max_global_bytes"), 3 * 96 * MIB)
        # Test4B-2 uses one existing sidecar per 4 MiB region chunk.  Keep
        # the single-worker v4B configuration accepted for older sessions.
        self.external_preallocated = str(raw.get("allocation_mode", "event")) == "external_preallocated"
        self.specs: dict[str, BallastSpec] = {}
        self.worker_specs: dict[str, list[BallastSpec]] = {}
        scope_names = dict(getattr(runtime_scope, "app_key_to_scope_name", {}) or {})
        for app, item in dict(raw.get("apps", {})).items():
            if app not in scope_names:
                raise ValueError(f"ballast app missing from runtime scope: {app}")
            raw_workers = item.get("workers") if isinstance(item, dict) else None
            entries = raw_workers if isinstance(raw_workers, list) and raw_workers else [item]
            workers: list[BallastSpec] = []
            for worker in entries:
                if not isinstance(worker, dict):
                    raise ValueError(f"invalid ballast worker for {app}")
                workers.append(BallastSpec(
                    app_key=app, socket_path=Path(worker["socket_path"]), log_path=Path(worker["log_path"]),
                    file_path=Path(worker["file_path"]), scope_name=scope_names[app],
                    anon_cold_bytes=_int(worker.get("anon_cold_bytes"), 0),
                    anon_hot_bytes=_int(worker.get("anon_hot_bytes"), 0),
                    file_cold_bytes=_int(worker.get("file_cold_bytes"), 0),
                    file_hot_bytes=_int(worker.get("file_hot_bytes"), 0),
                ))
            self.worker_specs[app] = workers
            # ``specs`` remains a compatibility view for existing callers;
            # aggregate sizes are computed from ``worker_specs`` below.
            self.specs[app] = workers[0]
        self.current_foreground = ""
        self.allocated: set[str] = set()
        self.reclaimed: set[str] = set()
        self.statuses: dict[str, BallastStatus] = {app: BallastStatus(app) for app in self.specs}
        ballast_dir = self.output_dir / "ballast"
        self.allocation_writer = CsvWriter(ballast_dir / "ballast_allocation_audit.csv", ALLOCATION_FIELDS)
        self.reaccess_writer = CsvWriter(ballast_dir / "ballast_reaccess.csv", REACCESS_FIELDS)
        self.region_writer = CsvWriter(ballast_dir / "ballast_region_stats.csv", REGION_FIELDS)
        self.event_writer = CsvWriter(ballast_dir / "ballast_controller_events.csv", EVENT_FIELDS)

    def _workers(self, app: str) -> list[BallastSpec]:
        return self.worker_specs.get(app, [])

    def _app_total_bytes(self, app: str) -> int:
        return sum(spec.total_bytes for spec in self._workers(app))

    def _app_cold_bytes(self, app: str) -> int:
        return sum(spec.anon_cold_bytes + spec.file_cold_bytes for spec in self._workers(app))

    def _slice_path(self) -> Path | None:
        try:
            outcome = subprocess.run(
                ["systemctl", "--user", "show", self.marker, "-p", "ControlGroup", "--value"],
                check=False, capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if outcome.returncode != 0 or not outcome.stdout.strip():
            return None
        return Path("/sys/fs/cgroup") / outcome.stdout.strip().lstrip("/")

    def _expected_path(self, app: str) -> Path | None:
        parent = self._slice_path()
        spec = self.specs.get(app)
        if parent is None or spec is None:
            return None
        path = parent / spec.scope_name
        return path if path.is_dir() else None

    @staticmethod
    def _pid_cgroup(pid: int) -> str:
        try:
            for line in (Path("/proc") / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines():
                if line.startswith("0::"):
                    return line.split("::", 1)[1].strip()
        except OSError:
            pass
        return ""

    def refresh(self, app: str, now_ns: int | None = None) -> BallastStatus:
        now = int(now_ns or time.time_ns())
        status = self.statuses.get(app, BallastStatus(app))
        workers = self._workers(app)
        if not workers:
            return status
        expected = self._expected_path(app)
        observed: list[tuple[BallastSpec, bool, str, dict[str, str], int, str]] = []
        for spec in workers:
            ok, response = BallastClient(spec.socket_path).command("STATUS")
            fields = _fields(response)
            pid = _int(fields.get("pid"))
            relative = self._pid_cgroup(pid) if pid else ""
            actual = str(Path("/sys/fs/cgroup") / relative.lstrip("/")) if relative else ""
            observed.append((spec, ok, response, fields, pid, actual))
        all_ok = all(item[1] for item in observed)
        all_allocated = bool(observed) and all(item[3].get("allocated") == "1" for item in observed)
        states = {item[3].get("state", "UNAVAILABLE") for item in observed if item[1]}
        state = next(iter(states)) if len(states) == 1 and all_ok else ("PARTIAL" if all_ok else "UNAVAILABLE")
        actuals = {item[5] for item in observed}
        actual = next(iter(actuals)) if len(actuals) == 1 else ""
        pids = [item[4] for item in observed if item[4] > 0]
        background_times = [_int(item[3].get("background_since_ns")) for item in observed]
        status = BallastStatus(
            app_key=app, state=state, allocated=all_allocated, pid=pids[0] if pids else 0,
            background_since_ns=max(background_times, default=0),
            expected_cgroup=str(expected or ""), actual_cgroup=actual,
            error="" if all_ok else "|".join(item[2] for item in observed if not item[1]),
            hot_accesses=sum(_int(item[3].get("hot_accesses")) for item in observed),
            cold_accesses=sum(_int(item[3].get("cold_accesses")) for item in observed),
            cold_bytes=self._app_cold_bytes(app), total_bytes=self._app_total_bytes(app),
        )
        self.statuses[app] = status
        self.region_writer.write_row({
            "timestamp_ns": now, "app_key": app, "state": status.state,
            "allocated": str(status.allocated).lower(), "background_since_ns": status.background_since_ns,
            "anon_cold_bytes": sum(spec.anon_cold_bytes for spec in workers), "anon_hot_bytes": sum(spec.anon_hot_bytes for spec in workers),
            "file_cold_bytes": sum(spec.file_cold_bytes for spec in workers), "file_hot_bytes": sum(spec.file_hot_bytes for spec in workers),
            "hot_accesses": status.hot_accesses, "cold_accesses": status.cold_accesses,
            "status": "OK" if ok else "ERROR", "error": status.error,
        })
        return status

    def _allocation_reason(self, app: str, bind_ready: set[str], status: BallastStatus) -> str:
        if not self.current_foreground:
            return "FOREGROUND_UNKNOWN"
        if app != self.current_foreground:
            return "APP_NOT_FOREGROUND"
        if app not in bind_ready:
            return "APPBIND_MISSING"
        if status.pid <= 0 or not status.expected_cgroup:
            return "CGROUP_DISAPPEARED"
        if status.actual_cgroup != status.expected_cgroup:
            return "BALLAST_WRONG_CGROUP"
        if app in self.allocated or status.allocated:
            return "ALREADY_ALLOCATED"
        if self._app_total_bytes(app) <= 0:
            return "APPBIND_MISMATCH"
        if sum(self._app_total_bytes(key) for key in self.allocated) + self._app_total_bytes(app) > self.max_global_bytes:
            return "GLOBAL_BUDGET_EXCEEDED"
        return ""

    def _audit_allocation(self, *, app: str, status: BallastStatus, command: str, reason: str, response: str, now: int) -> None:
        self.allocation_writer.write_row({
            "timestamp_ns": now, "app_key": app, "command": command,
            "foreground_app": self.current_foreground, "ballast_pid": status.pid,
            "expected_cgroup": status.expected_cgroup, "actual_cgroup": status.actual_cgroup,
            "appbind_ready": str(app in self._last_bind_ready).lower(), "requested_bytes": self._app_total_bytes(app),
            "global_allocated_bytes": sum(self._app_total_bytes(key) for key in self.allocated),
            "status": "REJECTED" if reason else "OK", "reject_reason": reason, "response": response,
        })

    _last_bind_ready: set[str] = set()

    def _foreground_allocate_if_safe(self, app: str, bind_ready: set[str], now: int) -> None:
        if app not in self.specs:
            return
        self._last_bind_ready = set(bind_ready)
        status = self.refresh(app, now)
        if self.external_preallocated:
            if status.allocated:
                self.allocated.add(app)
                self._enter_foreground(app, now, "EXTERNAL_PREALLOCATED")
                return
            self._audit_allocation(app=app, status=status, command="ALLOCATE", reason="EXTERNAL_PREALLOCATION_REQUIRED", response="", now=now)
            return
        reason = self._allocation_reason(app, bind_ready, status)
        if reason:
            # ALREADY_ALLOCATED is normal after the first foreground entry;
            # it remains an audit event, never a new allocation attempt.
            self._audit_allocation(app=app, status=status, command="ALLOCATE", reason=reason, response="", now=now)
            return
        entered = self._worker_command(app, "ENTER_FOREGROUND")
        if not entered[0]:
            self._audit_allocation(app=app, status=status, command="ENTER_FOREGROUND", reason="BALLAST_COMMAND_FAILED", response=entered[1], now=now)
            return
        allocated = self._worker_command(app, "ALLOCATE")
        if allocated[0]:
            self.allocated.add(app)
            self.refresh(app, now)
            self._audit_allocation(app=app, status=self.statuses[app], command="ALLOCATE", reason="", response=allocated[1], now=now)
        else:
            self._audit_allocation(app=app, status=status, command="ALLOCATE", reason="BALLAST_COMMAND_FAILED", response=allocated[1], now=now)

    def _worker_command(self, app: str, command: str) -> tuple[bool, str]:
        outcomes = [BallastClient(spec.socket_path).command(command) for spec in self._workers(app)]
        failed = [response for ok, response in outcomes if not ok]
        return not failed, "|".join(failed or [response for _ok, response in outcomes])

    def _enter_foreground(self, app: str, now: int, event_type: str) -> None:
        if app not in self.specs or app not in self.allocated:
            return
        ok, response = self._worker_command(app, "ENTER_FOREGROUND")
        self.event_writer.write_row({"timestamp_ns": now, "app_key": app, "source_event": event_type,
                                     "old_app": "", "foreground_app": self.current_foreground,
                                     "action": "ENTER_FOREGROUND", "status": "OK" if ok else "ERROR", "detail": response})
        self.refresh(app, now)

    def _enter_background(self, app: str, now: int, event_type: str) -> None:
        if app not in self.specs:
            return
        if app not in self.allocated and self.external_preallocated and self.refresh(app, now).allocated:
            self.allocated.add(app)
        if app not in self.allocated:
            return
        ok, response = self._worker_command(app, "ENTER_BACKGROUND")
        self.event_writer.write_row({"timestamp_ns": now, "app_key": app, "source_event": event_type,
                                     "old_app": app, "foreground_app": self.current_foreground,
                                     "action": "ENTER_BACKGROUND", "status": "OK" if ok else "ERROR", "detail": response})
        self.refresh(app, now)

    def observe_event(self, *, event: dict[str, Any], foreground_app: str, bind_ready: set[str], now_ns: int | None = None) -> None:
        """Use an existing direct X11 event; this method never emits one."""
        now = int(now_ns or time.time_ns())
        event_type = str(event.get("event_type", ""))
        old = str(event.get("old_app", ""))
        foreground = str(foreground_app or "")
        if event_type in {"APP_OPEN", "APP_SWITCH"}:
            self.current_foreground = foreground if foreground in self.specs else ""
            if old and old != self.current_foreground:
                self._enter_background(old, now, event_type)
            if self.current_foreground:
                if self.current_foreground in self.reclaimed:
                    self._reaccess(self.current_foreground, now)
                    self.reclaimed.discard(self.current_foreground)
                if self.current_foreground in self.allocated:
                    self._enter_foreground(self.current_foreground, now, event_type)
                else:
                    self._foreground_allocate_if_safe(self.current_foreground, bind_ready, now)
        elif event_type in {"APP_MINIMIZE", "APP_CLOSE"}:
            app = str(event.get("app", ""))
            self._enter_background(app, now, event_type)

    def tick(self, *, foreground_app: str, bind_ready: set[str], now_ns: int | None = None) -> None:
        """Retry a pending *foreground* allocation after asynchronous AppBind succeeds."""
        now = int(now_ns or time.time_ns())
        self.current_foreground = foreground_app if foreground_app in self.specs else ""
        if self.current_foreground and self.current_foreground not in self.allocated:
            self._foreground_allocate_if_safe(self.current_foreground, bind_ready, now)

    def _reaccess(self, app: str, now: int) -> None:
        for command in ("ENTER_FOREGROUND", "REACCESS_HOT", "REACCESS_COLD", "VERIFY"):
            ok, response = self._worker_command(app, command)
            self.reaccess_writer.write_row({"timestamp_ns": now, "app_key": app, "command": command,
                                             "status": "OK" if ok else "ERROR", "response": response,
                                             "error": "" if ok else response})
            if not ok:
                break
        self.refresh(app, now)

    def mark_reclaimed(self, app: str) -> None:
        if app in self.allocated:
            self.reclaimed.add(app)

    def states(self, now_ns: int | None = None) -> dict[str, BallastStatus]:
        now = int(now_ns or time.time_ns())
        for app in self.specs:
            self.refresh(app, now)
        return dict(self.statuses)

    def close(self) -> None:
        for app in self.specs:
            ok, response = self._worker_command(app, "STOP")
            self.event_writer.write_row({"timestamp_ns": time.time_ns(), "app_key": app, "source_event": "CLOSE",
                                         "old_app": "", "foreground_app": self.current_foreground,
                                         "action": "STOP", "status": "OK" if ok else "ERROR", "detail": response})
        self.allocation_writer.close()
        self.reaccess_writer.close()
        self.region_writer.close()
        self.event_writer.close()
