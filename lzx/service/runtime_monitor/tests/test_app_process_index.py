from __future__ import annotations

import unittest
import sys
from pathlib import Path
from unittest.mock import Mock

MONITOR_DIR = Path(__file__).resolve().parents[1]
if str(MONITOR_DIR) not in sys.path:
    sys.path.insert(0, str(MONITOR_DIR))

from runtime_monitor.collectors.process import ProcessCollector
from runtime_monitor.core.app_mapper import ProcessIdentity
from runtime_monitor.core.app_process_index import AppProcessIndex


class _Mapper:
    """按 comm 映射的极小测试替身，专门验证索引状态转换。"""

    def map_process(self, identity: ProcessIdentity) -> str:
        return {
            "wps": "WPS",
            "vlc": "VLC",
        }.get(identity.comm, "")


def _identity(pid: int, comm: str, start_time: str = "10") -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        tgid=pid,
        comm=comm,
        exe_path=f"/usr/bin/{comm}",
        cgroup_path="/user.slice/app.slice/test.scope",
        start_time=start_time,
    )


class AppProcessIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index = AppProcessIndex(_Mapper(), ["WPS", "VLC"])  # type: ignore[arg-type]

    def test_exec_can_add_move_and_remove_one_pid(self) -> None:
        # FORK 时还是未知 launcher，不应过早归入任何 App。
        self.index.process_start(_identity(101, "launcher"), source_seq=1)
        self.assertEqual(self.index.pids(), [])

        # EXEC 给出最终 wps 身份，索引新增该 PID。
        added = self.index.process_exec(_identity(101, "wps"), source_seq=2)
        self.assertEqual((added.previous_app, added.current_app), ("", "WPS"))
        self.assertEqual(self.index.pids_for_app("WPS"), {101})

        # 同一 PID 又 EXEC 成 VLC，必须从 WPS 原子移动到 VLC。
        moved = self.index.process_exec(_identity(101, "vlc"), source_seq=3)
        self.assertEqual((moved.previous_app, moved.current_app), ("WPS", "VLC"))
        self.assertEqual(self.index.pids_for_app("WPS"), set())
        self.assertEqual(self.index.pids_for_app("VLC"), {101})

        # EXEC 成未定义程序后，它不再属于 LSTM AppProcessIndex。
        removed = self.index.process_exec(_identity(101, "helper"), source_seq=4)
        self.assertEqual((removed.previous_app, removed.current_app), ("VLC", ""))
        self.assertEqual(self.index.pids(), [])

    def test_exit_removes_exact_process_instance_and_reports_empty_once(self) -> None:
        self.index.process_start(_identity(201, "wps", "10"), source_seq=1)
        removed = self.index.process_exit(_identity(201, "wps", "10"))
        self.assertEqual(removed.previous_app, "WPS")
        self.assertEqual(self.index.pids_for_app("WPS"), set())
        self.assertEqual(self.index.pop_empty_apps(0), ["WPS"])
        self.assertEqual(self.index.pop_empty_apps(0), [])

    def test_late_exit_cannot_delete_reused_pid(self) -> None:
        self.index.process_start(_identity(301, "wps", "old"), source_seq=1)
        self.index.process_start(_identity(301, "wps", "new"), source_seq=2)

        stale = self.index.process_exit(_identity(301, "wps", "old"))

        self.assertEqual(stale.current_app, "WPS")
        self.assertEqual(self.index.entry(301).identity.start_time, "new")  # type: ignore[union-attr]

    def test_bootstrap_is_a_clean_projection_without_fake_close(self) -> None:
        self.index.bootstrap([_identity(401, "wps"), _identity(402, "unknown")])
        self.assertEqual(self.index.snapshot(), {"VLC": set(), "WPS": {401}})
        self.assertEqual(self.index.pop_empty_apps(0), [])

    def test_fixture_is_indexed_for_resources_but_not_gui_close_state(self) -> None:
        index = AppProcessIndex(
            _Mapper(),  # type: ignore[arg-type]
            ["WPS", "VLC"],
            fixture_scope_to_app={"automation-fixture-wps.scope": "WPS"},
        )
        fixture = ProcessIdentity(
            pid=501,
            tgid=501,
            comm="python3",
            exe_path="/usr/bin/python3",
            cgroup_path="/test.slice/automation-fixture-wps.scope",
            start_time="50",
        )
        change = index.process_exec(fixture, source_seq=5)
        self.assertEqual((change.current_app, change.current_role), ("WPS", "fixture"))
        self.assertEqual(index.pids_for_app("WPS"), {501})
        self.assertEqual(index.pids_for_app("WPS", role="gui"), set())
        index.process_exit(fixture)
        self.assertEqual(index.pop_empty_apps(0), [])


class IndexedProcessSamplingTests(unittest.TestCase):
    def test_explicit_empty_pid_set_never_enumerates_all_proc(self) -> None:
        collector = ProcessCollector(
            mapper=_Mapper(),  # type: ignore[arg-type]
            target_app="WPS",
            target_apps=["WPS", "VLC"],
        )
        collector._all_pids = Mock(  # type: ignore[method-assign]
            side_effect=AssertionError("steady-state must not enumerate /proc")
        )

        self.assertEqual(collector.sample([]), [])
        collector._all_pids.assert_not_called()


if __name__ == "__main__":
    unittest.main()
