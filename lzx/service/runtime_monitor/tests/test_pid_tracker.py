from __future__ import annotations

from pathlib import Path

from runtime_monitor.region_monitor.cgroup_pid_tracker import CgroupPidTracker, parse_proc_stat_starttime
from runtime_monitor.region_monitor.models import ProcessInfo
from runtime_monitor.region_monitor.process_role_resolver import ProcessRoleResolver


def test_pid_tracker_handles_missing_cgroup(tmp_path: Path) -> None:
    tracker = CgroupPidTracker("WPS", tmp_path / "missing", ProcessRoleResolver.for_config({}))
    added, exited = tracker.refresh(now_ns=1)
    assert added == []
    assert exited == []


def test_pid_identity_includes_starttime(tmp_path: Path) -> None:
    first = ProcessInfo(10, 100, "wps", "wps", "/opt/wps", "WPS_MAIN", 1, 1, "running")
    reused = ProcessInfo(10, 200, "wps", "wps", "/opt/wps", "WPS_MAIN", 2, 2, "running")
    assert first.identity != reused.identity


def test_parse_proc_stat_starttime_with_comm_parentheses() -> None:
    stat = "123 (wps helper) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 123456 20 21"
    assert parse_proc_stat_starttime(stat) == 123456
