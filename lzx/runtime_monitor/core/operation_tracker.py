"""Operation tracker — reads automation_trace.csv and maps feature windows to operations.

Provides real-time operation context for feature row annotation:
- Which operation (label) is active during this feature window
- Carry-forward state_label through WAIT periods
- Emit operation_events.csv rows when operations are first detected
"""

from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from typing import Any


# Fields written to operation_events.csv (mirrors align_automation_monitor.py LABEL_FIELDS)
TRACE_FIELDS = [
    "session_id", "ts_ns", "ts_iso", "scenario_id", "step_id",
    "phase", "action", "app", "label", "status", "optional",
    "command", "window_match", "pid", "tgid", "cgroup_path",
    "window_id", "window_title", "error",
]


class OperationTracker:
    """Reads an automation trace CSV and determines the active operation."""

    def __init__(self, model_dir: Path, session_id: str) -> None:
        self.model_dir = model_dir
        self.session_id = session_id
        self.trace_path = model_dir / "automation_trace.csv"
        self._trace_mtime: float = 0.0
        self._ops: list[dict[str, Any]] = []       # parsed operation intervals
        self._op_events_emitted: set[str] = set()   # dedup (scenario_id, step_id)
        self._state_label: str = ""
        self._pending_end: dict[str, dict[str, Any]] = {}  # start rows waiting for end

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read trace file if its mtime changed."""
        try:
            mtime = self.trace_path.stat().st_mtime
        except OSError:
            return
        if mtime == self._trace_mtime:
            return
        self._trace_mtime = mtime
        self._parse_trace()

    def get_current(
        self, window_start_ns: int, window_end_ns: int
    ) -> dict[str, str]:
        """Return the active operation context for a feature window.

        Uses overlap-based selection: non-WAIT labels always beat WAIT;
        among equal priority, the largest overlap wins.
        Carries forward state_label through WAIT-only windows.
        """
        matches: list[tuple[int, int, dict[str, Any]]] = []
        for op in self._ops:
            overlap_ns = _overlap(window_start_ns, window_end_ns, op["start_ns"], op["end_ns"])
            if overlap_ns <= 0:
                continue
            priority = 0 if op["label"].upper().startswith("WAIT") else 1
            matches.append((priority, overlap_ns, op))

        if not matches:
            return {
                "operation_label": "",
                "operation_app": "",
                "action": "",
                "state_label": self._state_label,
                "manual_label": "",
                "scenario_id": "",
                "step_id": "",
            }

        # Best match: max priority first, then max overlap
        best = max(matches, key=lambda m: (m[0], m[1]))
        op = best[2]

        # Update state_label (carry-forward for non-WAIT)
        if not op["label"].upper().startswith("WAIT"):
            self._state_label = op["label"]

        return {
            "operation_label": op["label"],
            "operation_app": op["app"],
            "action": op["action"],
            "state_label": self._state_label,
            "manual_label": op["label"],
            "scenario_id": op["scenario_id"],
            "step_id": op["step_id"],
        }

    def pop_new_operation_events(
        self,
        foreground_app: str,
        open_apps: str,
    ) -> list[dict[str, Any]]:
        """Return operation_events rows for operations first seen since last call.

        Each operation is emitted once with whatever foreground/open_apps context
        is available at emission time.
        """
        rows: list[dict[str, Any]] = []
        for op in self._ops:
            op_key = f"{op['scenario_id']}:{op['step_id']}"
            if op_key in self._op_events_emitted:
                continue
            if op["phase"] != "end":
                continue
            self._op_events_emitted.add(op_key)
            rows.append({
                "session_id": self.session_id,
                "operation_id": f"{self.session_id}:{op['scenario_id']}:{op['step_id']}",
                "scenario_id": op["scenario_id"],
                "step_id": op["step_id"],
                "operation_label": op["label"],
                "operation_app": op["app"],
                "action": op["action"],
                "start_ns": op["start_ns"],
                "end_ns": op["end_ns"],
                "start_time": _ns_to_str(op["start_ns"]),
                "end_time": _ns_to_str(op["end_ns"]),
                "duration_ms": op["duration_ms"],
                "status": op["status"],
                "optional": op["optional"],
                "foreground_app_at_start": foreground_app,
                "foreground_app_at_end": "",
                "open_apps_at_start": open_apps,
                "open_apps_at_end": "",
                "source": "automation_trace",
            })
        return rows

    def current_state_label(self) -> str:
        return self._state_label

    def operation_label_rows(self) -> list[dict[str, Any]]:
        self.refresh()
        rows: list[dict[str, Any]] = []
        for op in self._ops:
            rows.append({
                "session_id": self.session_id,
                "operation_id": f"{self.session_id}:{op['scenario_id']}:{op['step_id']}",
                "scenario_id": op["scenario_id"],
                "step_id": op["step_id"],
                "operation_label": op["label"],
                "operation_app": op["app"],
                "action": op["action"],
                "start_ns": op["start_ns"],
                "end_ns": op["end_ns"],
                "start_time": _ns_to_str(op["start_ns"]),
                "end_time": _ns_to_str(op["end_ns"]),
                "duration_ms": op["duration_ms"],
                "status": op["status"],
                "optional": op["optional"],
                "source": "automation_trace",
            })
        return rows

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _parse_trace(self) -> None:
        """Read trace CSV, pair start/end rows into operation intervals."""
        if not self.trace_path.exists():
            self._ops = []
            return

        with self.trace_path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        self._pending_end = {}
        ops: list[dict[str, Any]] = []

        for row in rows:
            key = f"{row.get('session_id','')}:{row.get('scenario_id','')}:{row.get('step_id','')}"
            phase = row.get("phase", "").strip()
            try:
                ts_ns = int(row.get("ts_ns", "0"))
            except (ValueError, TypeError):
                ts_ns = 0

            if phase == "start":
                self._pending_end[key] = {
                    "scenario_id": row.get("scenario_id", ""),
                    "step_id": row.get("step_id", ""),
                    "label": row.get("label", ""),
                    "app": row.get("app", ""),
                    "action": row.get("action", ""),
                    "status": row.get("status", "running"),
                    "optional": row.get("optional", "false"),
                    "start_ns": ts_ns,
                    "end_ns": ts_ns,  # temporary, will be updated
                    "duration_ms": 0,
                    "phase": "start",
                }
            elif phase == "end":
                start_op = self._pending_end.pop(key, None)
                if start_op and ts_ns >= start_op["start_ns"]:
                    duration_ms = max(0, int((ts_ns - start_op["start_ns"]) / 1_000_000))
                    ops.append({
                        "scenario_id": start_op["scenario_id"],
                        "step_id": start_op["step_id"],
                        "label": start_op["label"],
                        "app": start_op["app"],
                        "action": start_op["action"],
                        "status": row.get("status", "success"),
                        "optional": start_op["optional"],
                        "start_ns": start_op["start_ns"],
                        "end_ns": ts_ns,
                        "duration_ms": duration_ms,
                        "phase": "end",
                    })

        # Sort by start_ns for deterministic ordering
        ops.sort(key=lambda o: o["start_ns"])
        self._ops = ops


# ------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------

def _overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b))


def _ns_to_str(ts_ns: int) -> str:
    try:
        return dt.datetime.fromtimestamp(ts_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError):
        return ""
