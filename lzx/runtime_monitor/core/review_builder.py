"""Post-processing review file generator.

Reads all model/ CSV files at session end and generates review/ output:
- session_summary.md
- timeline.csv
- app_switches.csv
- opened_apps_timeline.csv
- operations_timeline.csv
- checks.csv
- foreground_debug_brief.csv
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from core.schema import (
    APP_SWITCHES_FIELDS,
    CHECKS_FIELDS,
    FOREGROUND_DEBUG_BRIEF_FIELDS,
    OPENED_APPS_TIMELINE_FIELDS,
    OPERATIONS_TIMELINE_FIELDS,
    TIMELINE_FIELDS,
)


class ReviewBuilder:
    def __init__(self, model_dir: Path, review_dir: Path, session_id: str) -> None:
        self.model_dir = model_dir
        self.review_dir = review_dir
        self.session_id = session_id

        # Cached data
        self.global_rows: list[dict[str, str]] = []
        self.fg_event_rows: list[dict[str, str]] = []
        self.lifecycle_rows: list[dict[str, str]] = []
        self.op_rows: list[dict[str, str]] = []
        self.debug_rows: list[dict[str, str]] = []
        self.app_rows: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # public entry point
    # ------------------------------------------------------------------

    def generate(self) -> None:
        self.review_dir.mkdir(parents=True, exist_ok=True)
        self._read_all()
        self._build_timeline()
        self._build_app_switches()
        self._build_opened_apps_timeline()
        self._build_operations_timeline()
        self._build_checks()
        self._build_foreground_debug_brief()
        self._build_session_summary()

    # ------------------------------------------------------------------
    # data loading
    # ------------------------------------------------------------------

    def _read_all(self) -> None:
        self.global_rows = _read_csv(self.model_dir / "global_state_1s.csv")
        self.fg_event_rows = _read_csv(self.model_dir / "foreground_events.csv")
        self.lifecycle_rows = _read_csv(self.model_dir / "app_lifecycle_events.csv")
        self.op_rows = _read_csv(self.model_dir / "operation_events.csv")
        self.debug_rows = _read_csv(self.model_dir / "foreground_debug.csv")
        self.app_rows = _read_csv(self.model_dir / "app_state_1s.csv")

    # ------------------------------------------------------------------
    # 1. session_summary.md
    # ------------------------------------------------------------------

    def _build_session_summary(self) -> None:
        lines: list[str] = []
        lines.append(f"# Session Summary: {self.session_id}")
        lines.append("")

        # Basic stats
        lines.append(f"- Total feature windows: {len(self.global_rows)}")
        lines.append(f"- Foreground switches: {sum(1 for r in self.fg_event_rows if r.get('event_type') == 'APP_SWITCH')}")
        lines.append(f"- App lifecycle events: {len(self.lifecycle_rows)}")
        lines.append(f"- Operations: {len(self.op_rows)}")

        # Time range
        if self.global_rows:
            first = self.global_rows[0].get("timestamp", "?")
            last = self.global_rows[-1].get("timestamp", "?")
            lines.append(f"- Time range: {first} → {last}")

        # Apps observed
        apps: set[str] = set()
        for row in self.global_rows:
            for field in ("open_apps", "observed_apps"):
                for app in row.get(field, "").split("|"):
                    if app:
                        apps.add(app)
        lines.append(f"- Apps observed: {', '.join(sorted(apps)) if apps else 'none'}")

        # Foreground distribution
        fg_count: dict[str, int] = {}
        for row in self.global_rows:
            fg = row.get("foreground_app", "")
            if fg:
                fg_count[fg] = fg_count.get(fg, 0) + 1
        lines.append("")
        lines.append("## Foreground App Distribution")
        lines.append("| App | Seconds | Pct |")
        lines.append("|-----|---------|-----|")
        total = sum(fg_count.values()) or 1
        for app in sorted(fg_count, key=fg_count.get, reverse=True):  # type: ignore[arg-type]
            pct = fg_count[app] / total * 100
            lines.append(f"| {app} | {fg_count[app]} | {pct:.1f}% |")

        # Operation summary
        if self.op_rows:
            lines.append("")
            lines.append("## Operations")
            lines.append("| Operation | App | Action | Duration (ms) | Status |")
            lines.append("|-----------|-----|--------|---------------|--------|")
            for op in self.op_rows:
                lines.append(
                    f"| {op.get('operation_label','')} | {op.get('operation_app','')} "
                    f"| {op.get('action','')} | {op.get('duration_ms','')} "
                    f"| {op.get('status','')} |"
                )

        # Peak memory
        lines.append("")
        lines.append("## Peak Memory (from global_state_1s.csv)")
        max_mem = 0
        max_row_idx = -1
        for i, row in enumerate(self.global_rows):
            try:
                mem = int(row.get("global_mem_available_kb", "0"))
            except (ValueError, TypeError):
                mem = 0
            if mem > max_mem:
                max_mem = mem
                max_row_idx = i
        if max_row_idx >= 0:
            ts = self.global_rows[max_row_idx].get("timestamp", "?")
            lines.append(f"- Peak MemAvailable: {max_mem} KB at {ts}")

        check_rows = _read_csv(self.review_dir / "checks.csv")
        warnings = [row for row in check_rows if row.get("result") == "WARN"]
        failures = [row for row in check_rows if row.get("result") == "FAIL"]
        lines.append("")
        lines.append("## Warnings")
        if warnings:
            for row in warnings:
                lines.append(f"- {row.get('check_name')}: {row.get('observed')}")
        else:
            lines.append("- none")
        lines.append("")
        lines.append("## Failed Checks")
        if failures:
            for row in failures:
                lines.append(f"- {row.get('check_name')}: {row.get('observed')}")
        else:
            lines.append("- none")

        _write_text(self.review_dir / "session_summary.md", "\n".join(lines) + "\n")

    # ------------------------------------------------------------------
    # 2. timeline.csv — state-change rows only
    # ------------------------------------------------------------------

    def _build_timeline(self) -> None:
        rows: list[dict[str, str]] = []
        prev_fg = ""
        prev_ops = ""
        prev_opened = ""

        for row in self.global_rows:
            fg = row.get("foreground_app", "")
            ops = row.get("current_operation_label", "")
            opened = row.get("open_apps", "")

            if fg == prev_fg and ops == prev_ops and opened == prev_opened:
                continue  # skip unchanged rows

            prev_fg, prev_ops, prev_opened = fg, ops, opened

            rows.append({
                "time": row.get("timestamp", ""),
                "foreground_app": fg,
                "window_title": row.get("foreground_window_title", ""),
                "opened_apps": opened,
                "current_operation": ops,
                "operation_app": row.get("current_operation_app", ""),
                "note": "",
            })

        _write_csv(self.review_dir / "timeline.csv", TIMELINE_FIELDS, rows)

    # ------------------------------------------------------------------
    # 3. app_switches.csv
    # ------------------------------------------------------------------

    def _build_app_switches(self) -> None:
        rows: list[dict[str, str]] = []
        for event in self.fg_event_rows:
            if event.get("event_type") != "APP_SWITCH":
                continue
            ts = event.get("timestamp", "")
            from_app = event.get("old_app", "")
            to_app = event.get("new_app", "")

            # Find expected operation at this time
            expected_op = ""
            result = ""
            op_match = self._find_op_at_time(ts)
            if op_match:
                expected_op = op_match.get("operation_label", "")
                op_app = op_match.get("operation_app", "")
                result = "PASS" if to_app and op_app and to_app.upper() == op_app.upper() else "WARN"

            dur = event.get("duration_ms", "0")
            try:
                dur_s = f"{int(dur) / 1000:.1f}"
            except (ValueError, TypeError):
                dur_s = "0.0"

            rows.append({
                "time": ts,
                "from_app": from_app,
                "to_app": to_app,
                "window_title": event.get("window_title", ""),
                "duration_s": dur_s,
                "expected_operation": expected_op,
                "result": result,
                "note": "",
            })

        _write_csv(self.review_dir / "app_switches.csv", APP_SWITCHES_FIELDS, rows)

    # ------------------------------------------------------------------
    # 4. opened_apps_timeline.csv
    # ------------------------------------------------------------------

    def _build_opened_apps_timeline(self) -> None:
        rows: list[dict[str, str]] = []
        for event in self.lifecycle_rows:
            etype = event.get("event_type", "")
            app = event.get("app", "")
            before = event.get("open_apps_before", "")
            after = event.get("open_apps_after", "")
            result = "PASS"
            if etype == "APP_OPEN":
                if app and app not in after:
                    result = "FAIL"
            elif etype == "APP_CLOSE":
                if app and app in after:
                    result = "FAIL"

            rows.append({
                "time": event.get("timestamp", ""),
                "event": etype,
                "app": app,
                "opened_apps_before": before,
                "opened_apps_after": after,
                "result": result,
                "note": "",
            })

        _write_csv(self.review_dir / "opened_apps_timeline.csv", OPENED_APPS_TIMELINE_FIELDS, rows)

    # ------------------------------------------------------------------
    # 5. operations_timeline.csv
    # ------------------------------------------------------------------

    def _build_operations_timeline(self) -> None:
        rows: list[dict[str, str]] = []
        for op in self.op_rows:
            start_ns = _to_int(op.get("start_ns", "0"))
            end_ns = _to_int(op.get("end_ns", "0"))
            op_app = op.get("operation_app", "")

            # Check foreground during operation
            fg_matches = 0
            fg_total = 0
            for row in self.global_rows:
                w_start = _to_int(row.get("window_start_ns", "0"))
                w_end = _to_int(row.get("window_end_ns", "0"))
                if w_start >= end_ns or w_end <= start_ns:
                    continue
                fg_total += 1
                if row.get("foreground_app", "").upper() == op_app.upper():
                    fg_matches += 1

            fg_during = f"{fg_matches}/{fg_total}" if fg_total > 0 else "N/A"
            result = "PASS" if fg_total == 0 or fg_matches == fg_total else "WARN"

            # Sample opened_apps from a midpoint window
            opened_sample = ""
            for row in self.global_rows:
                w_start = _to_int(row.get("window_start_ns", "0"))
                if w_start >= start_ns and w_start < end_ns:
                    opened_sample = row.get("open_apps", "")
                    break

            rows.append({
                "start_time": op.get("start_time", ""),
                "end_time": op.get("end_time", ""),
                "app": op_app,
                "operation": op.get("operation_label", ""),
                "action": op.get("action", ""),
                "status": op.get("status", ""),
                "foreground_during_operation": fg_during,
                "opened_apps": opened_sample,
                "result": result,
                "note": "",
            })

        _write_csv(self.review_dir / "operations_timeline.csv", OPERATIONS_TIMELINE_FIELDS, rows)

    # ------------------------------------------------------------------
    # 6. checks.csv — 13 automated checks
    # ------------------------------------------------------------------

    def _build_checks(self) -> None:
        checks: list[dict[str, str]] = []

        # Helper to find first/last window where an operation was active
        def windows_during(op_label: str) -> list[dict[str, str]]:
            """Return global rows whose window overlaps the named operation."""
            op = self._find_op_by_label(op_label)
            if not op:
                return []
            start_ns = _to_int(op.get("start_ns", "0"))
            end_ns = _to_int(op.get("end_ns", "0"))
            result: list[dict[str, str]] = []
            for row in self.global_rows:
                w_start = _to_int(row.get("window_start_ns", "0"))
                w_end = _to_int(row.get("window_end_ns", "0"))
                if _overlap(w_start, w_end, start_ns, end_ns) > 0:
                    result.append(row)
            return result

        def check_fg(check_name: str, op_label: str, expected_app: str) -> dict[str, str]:
            wins = windows_during(op_label)
            if op_label.endswith("_LAUNCH"):
                op = self._find_op_by_label(op_label)
                if op:
                    end_ns = _to_int(op.get("end_ns", "0"))
                    horizon_ns = end_ns + 10_000_000_000
                    wins = [
                        row for row in self.global_rows
                        if end_ns <= _to_int(row.get("window_start_ns", "0")) <= horizon_ns
                    ]
            if not wins:
                return _check_fail(check_name, f"foreground = {expected_app}", "no windows during operation")
            match = sum(1 for r in wins if r.get("foreground_app", "").upper() == expected_app.upper())
            pct = match / len(wins) * 100 if wins else 0
            if pct >= 50:
                return _check_ok(check_name, f"foreground = {expected_app}", f"{match}/{len(wins)} windows correct ({pct:.0f}%)")
            return _check_fail(check_name, f"foreground = {expected_app}", f"only {match}/{len(wins)} windows ({pct:.0f}%)")

        def rows_after_op(op_label: str, seconds: int) -> list[dict[str, str]]:
            op = self._find_op_by_label(op_label)
            if not op:
                return []
            end_ns = _to_int(op.get("end_ns", "0"))
            horizon_ns = end_ns + seconds * 1_000_000_000
            return [
                row for row in self.global_rows
                if end_ns <= _to_int(row.get("window_start_ns", "0")) <= horizon_ns
            ]

        def app_opened(app_id: str) -> bool:
            return any(
                row.get("event_type") == "APP_OPEN" and row.get("app") == app_id
                for row in self.lifecycle_rows
            )

        def check_qq_launch_foreground() -> dict[str, str]:
            wins = rows_after_op("QQ_LAUNCH", 5)
            if any(row.get("foreground_app", "").upper() == "QQ" for row in wins):
                return _check_pass("QQ_LAUNCH foreground", "QQ within 5s after QQ_LAUNCH", "foreground_app=QQ observed")
            if app_opened("QQ"):
                observed = ",".join(row.get("foreground_app", "") for row in wins) or "no foreground samples"
                return _check_warn("QQ_LAUNCH foreground", "QQ within 5s after QQ_LAUNCH", f"QQ APP_OPEN found, foreground={observed}")
            return _check_fail("QQ_LAUNCH foreground", "QQ APP_OPEN and foreground_app=QQ", "QQ APP_OPEN not found or mapping abnormal")

        def check_qq_switch_foreground() -> dict[str, str]:
            result = check_switch_foreground("APP_SWITCH_QQ foreground", "APP_SWITCH_QQ", "QQ", 3)
            if result["result"] == "PASS":
                return result
            wins = rows_after_op("APP_SWITCH_QQ", 3)
            qq_later = any(row.get("foreground_app", "").upper() == "QQ" for row in self.global_rows)
            observed = ",".join(row.get("foreground_app", "") for row in wins) or "no foreground samples"
            if qq_later:
                return _check_warn("APP_SWITCH_QQ foreground", "QQ within 3s after APP_SWITCH_QQ", f"QQ observed outside 3s; window foreground={observed}")
            return _check_fail("APP_SWITCH_QQ foreground", "QQ within 3s after APP_SWITCH_QQ", f"foreground={observed}")

        def check_switch_foreground(check_name: str, op_label: str, expected_app: str, seconds: int = 3) -> dict[str, str]:
            ops = self._find_ops_by_label(op_label)
            if not ops:
                return _check_fail(check_name, f"{expected_app} within {seconds}s after {op_label}", "operation not found")
            matched = 0
            details: list[str] = []
            for op in ops:
                start_ns = _to_int(op.get("start_ns", "0"))
                end_ns = _to_int(op.get("end_ns", "0"))
                horizon_ns = end_ns + seconds * 1_000_000_000
                wins = [
                    row for row in self.global_rows
                    if _overlap(
                        _to_int(row.get("window_start_ns", "0")),
                        _to_int(row.get("window_end_ns", "0")),
                        start_ns,
                        horizon_ns,
                    ) > 0
                ]
                if any(row.get("foreground_app", "").upper() == expected_app.upper() for row in wins):
                    matched += 1
                else:
                    seen = ",".join(row.get("foreground_app", "") for row in wins) or "no foreground samples"
                    details.append(f"step {op.get('step_id', '')}: {seen}")
            if matched == len(ops):
                return _check_pass(check_name, f"{expected_app} within {seconds}s after {op_label}", f"{matched}/{len(ops)} switch operations observed")
            if matched > 0:
                return _check_warn(check_name, f"{expected_app} within {seconds}s after {op_label}", f"{matched}/{len(ops)} switch operations observed; {'; '.join(details)}")
            return _check_fail(check_name, f"{expected_app} within {seconds}s after {op_label}", f"0/{len(ops)} switch operations observed; {'; '.join(details)}")

        def check_verify_foreground(app_id: str) -> dict[str, str]:
            label = f"{app_id}_VERIFY_MAIN_WINDOW"
            op = self._find_op_by_label(label)
            if not op:
                return _check_fail(label, f"verify_foreground {app_id} success", "operation not found")
            status = op.get("status", "")
            if status == "success":
                return _check_pass(label, f"verify_foreground {app_id} success", "operation status=success")
            return _check_fail(label, f"verify_foreground {app_id} success", f"operation status={status}; error={op.get('error', '')}")

        def check_app_opened(label: str, expected_app: str) -> dict[str, str]:
            lifecycle_apps = {r.get("app", "") for r in self.lifecycle_rows if r.get("event_type") == "APP_OPEN"}
            if expected_app in lifecycle_apps:
                return _check_ok(label, f"{expected_app} in opened_apps", f"APP_OPEN found for {expected_app}")
            # Fallback: check global rows
            for row in self.global_rows:
                if expected_app.upper() in row.get("open_apps", "").upper():
                    return _check_ok(label, f"{expected_app} in opened_apps", "found in global_state open_apps")
            return _check_fail(label, f"{expected_app} in opened_apps", "not found in any open_apps")

        def check_app_closed(label: str, expected_app: str) -> dict[str, str]:
            lifecycle_apps = {r.get("app", "") for r in self.lifecycle_rows if r.get("event_type") == "APP_CLOSE"}
            if expected_app in lifecycle_apps:
                return _check_ok(label, f"{expected_app} removed from opened_apps", f"APP_CLOSE found for {expected_app}")
            # Check final row
            if self.global_rows:
                last_opens = self.global_rows[-1].get("open_apps", "")
                if expected_app.upper() not in last_opens.upper():
                    return _check_ok(label, f"{expected_app} removed from opened_apps", "not present in final open_apps")
            return _check_fail(label, f"{expected_app} removed from opened_apps", "still in open_apps at end")

        def check_event_recorded(label: str, event_name: str) -> dict[str, str]:
            for row in self.op_rows:
                if event_name.upper() in row.get("operation_label", "").upper():
                    return _check_pass(label, f"{event_name} recorded", "found in operation_events")
            return _check_fail(label, f"{event_name} recorded", "not found in operation_events")

        def check_app_comm(label: str, app_id: str, allowed: set[str]) -> dict[str, str]:
            seen: set[str] = set()
            bad: set[str] = set()
            for row in self.app_rows:
                if row.get("app_id") != app_id:
                    continue
                for comm in filter(None, row.get("comm", "").split("|")):
                    seen.add(comm)
                    if comm not in allowed:
                        bad.add(comm)
            if bad:
                return _check_fail(label, f"{app_id} comm in {sorted(allowed)}", f"bad={sorted(bad)} seen={sorted(seen)}")
            if seen:
                return _check_pass(label, f"{app_id} comm in {sorted(allowed)}", f"seen={sorted(seen)}")
            return _check_warn(label, f"{app_id} comm observed", "no comm observed")

        def check_open_apps_regression() -> dict[str, str]:
            opens = [row.get("open_apps", "") for row in self.global_rows]
            pattern = ("FILES|QQ|WPS", "WPS", "QQ|WPS")
            for i in range(len(opens) - 2):
                if tuple(opens[i:i + 3]) == pattern:
                    return _check_fail("open_apps abnormal regression", "no FILES|QQ|WPS -> WPS -> QQ|WPS", f"found at rows {i}-{i+2}")
            return _check_pass("open_apps abnormal regression", "no FILES|QQ|WPS -> WPS -> QQ|WPS", "not found")

        # --- run required checks ---
        checks.append(check_fg("WPS_LAUNCH foreground", "WPS_LAUNCH", "WPS"))
        checks.append(check_qq_launch_foreground())
        checks.append(check_fg("FILES_LAUNCH foreground", "FILES_LAUNCH", "FILES"))
        checks.append(check_qq_switch_foreground())
        checks.append(check_switch_foreground("APP_SWITCH_FILES foreground", "APP_SWITCH_FILES", "FILES", 3))
        checks.append(check_verify_foreground("QQ"))
        checks.append(check_app_opened("WPS in opened_apps after WPS_LAUNCH", "WPS"))
        checks.append(check_app_opened("QQ in opened_apps after QQ_LAUNCH", "QQ"))
        checks.append(check_app_opened("FILES in opened_apps after FILES_LAUNCH", "FILES"))
        checks.append(check_app_closed("FILES removed after FILES_CLOSE", "FILES"))
        checks.append(check_app_closed("QQ removed after QQ_CLOSE", "QQ"))
        checks.append(check_app_closed("WPS removed after WPS_CLOSE", "WPS"))
        checks.append(check_event_recorded("WPS_OPEN_DOC_DIALOG recorded", "WPS_OPEN_DOC_DIALOG"))
        checks.append(check_event_recorded("WPS_CANCEL_DIALOG recorded", "WPS_CANCEL_DIALOG"))
        checks.append(check_app_comm("FILES only nautilus", "FILES", {"nautilus"}))
        checks.append(check_app_comm("QQ only qq", "QQ", {"qq"}))
        checks.append(check_app_comm("WPS only wps/wpsoffice", "WPS", {"wps", "wpsoffice", "bash"}))
        checks.append(check_open_apps_regression())

        _write_csv(self.review_dir / "checks.csv", CHECKS_FIELDS, checks)

    # ------------------------------------------------------------------
    # 7. foreground_debug_brief.csv
    # ------------------------------------------------------------------

    def _build_foreground_debug_brief(self) -> None:
        rows: list[dict[str, str]] = []
        for drow in self.debug_rows:
            ts = drow.get("timestamp", "")

            # Find expected app at this time
            expected = ""
            op = self._find_op_at_time(ts)
            if op:
                expected = op.get("operation_app", "")

            fg = drow.get("foreground_app", "")
            result = ""
            if expected and fg:
                result = "PASS" if fg.upper() == expected.upper() else "WARN"

            rows.append({
                "time": ts,
                "expected_app": expected,
                "foreground_app": fg,
                "window_title": drow.get("window_title", ""),
                "wm_class": drow.get("wm_class", ""),
                "pid": drow.get("net_wm_pid", drow.get("xdotool_pid", "")),
                "result": result,
                "note": "",
            })

        _write_csv(self.review_dir / "foreground_debug_brief.csv", FOREGROUND_DEBUG_BRIEF_FIELDS, rows)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _find_op_at_time(self, timestamp: str) -> dict[str, str] | None:
        """Find operation active at the given timestamp string."""
        for op in self.op_rows:
            if op.get("start_time", "") <= timestamp <= op.get("end_time", ""):
                return op
        return None

    def _find_op_by_label(self, label: str) -> dict[str, str] | None:
        """Find first operation whose label contains the given string."""
        upper = label.upper()
        for op in self.op_rows:
            if upper in op.get("operation_label", "").upper():
                return op
        return None

    def _find_ops_by_label(self, label: str) -> list[dict[str, str]]:
        """Find all operations whose label contains the given string."""
        upper = label.upper()
        return [op for op in self.op_rows if upper in op.get("operation_label", "").upper()]


# ------------------------------------------------------------------
# file I/O helpers
# ------------------------------------------------------------------

def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _to_int(value: str) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> int:
    return max(0, min(end_a, end_b) - max(start_a, start_b))


def _check_pass(name: str, expected: str, observed: str) -> dict[str, str]:
    return {"check_name": name, "expected": expected, "observed": observed, "result": "PASS", "details": ""}


def _check_warn(name: str, expected: str, observed: str) -> dict[str, str]:
    return {"check_name": name, "expected": expected, "observed": observed, "result": "WARN", "details": ""}


def _check_fail(name: str, expected: str, observed: str) -> dict[str, str]:
    return {"check_name": name, "expected": expected, "observed": observed, "result": "FAIL", "details": ""}


_check_ok = _check_pass
