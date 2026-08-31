from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from runtime_monitor.core.app_mapper import AppMapper, ProcessIdentity
from runtime_monitor.core.process_cgroup_router import SystemdProcessCgroupRouter


def make_fake_process(
    proc_root: Path,
    *,
    pid: int,
    comm: str,
    exe_path: str,
    cgroup_path: str,
    start_time: int = 12345,
) -> Path:
    proc = proc_root / str(pid)
    proc.mkdir()
    (proc / "comm").write_text(comm + "\n", encoding="utf-8")
    (proc / "exe").symlink_to(exe_path)
    (proc / "cgroup").write_text(f"0::{cgroup_path}\n", encoding="utf-8")
    fields = ["S", *(["0"] * 18), str(start_time), *(["0"] * 10)]
    (proc / "stat").write_text(
        f"{pid} ({comm}) " + " ".join(fields) + "\n",
        encoding="utf-8",
    )
    return proc


class ProcessCgroupRouterTests(unittest.TestCase):
    def test_fixture_alias_is_routed_by_create_event_with_fixture_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            proc_root.mkdir()
            proc = make_fake_process(
                proc_root,
                pid=246,
                comm="python3",
                exe_path="/usr/bin/python3",
                cgroup_path="/test.slice/automation-fixture-firefox.scope",
            )
            mapper = AppMapper(
                {"apps": {"FIREFOX": {
                    "keywords": ["firefox"],
                    "cgroup_units": ["automation-fixture-firefox.scope"],
                }}},
                target_apps=["FIREFOX"],
            )
            commands: list[list[str]] = []
            results: list[dict[str, object]] = []
            ready = threading.Event()

            def callback(result: dict[str, object]) -> None:
                results.append(result)
                ready.set()

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                (proc / "cgroup").write_text(
                    "0::/user.slice/parp-firefox.slice/"
                    "parp-route-firefox-fixture-p246-s12345.scope\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "ok", "")

            router = SystemdProcessCgroupRouter(
                mapper=mapper,
                app_ids={"FIREFOX": 1},
                fixture_scope_to_app={
                    "automation-fixture-firefox.scope": "FIREFOX"
                },
                callback=callback,
                proc_root=proc_root,
                expected_uid=os.getuid(),
                command_runner=runner,
            )
            router.start()
            try:
                identity = ProcessIdentity(
                    pid=246,
                    tgid=246,
                    comm="python3",
                    exe_path="/usr/bin/python3",
                    cgroup_path="/test.slice/automation-fixture-firefox.scope",
                    start_time="12345",
                )
                self.assertTrue(router.submit_created_process(
                    {"event_type": "PROCESS_START", "source_seq": 1}, identity,
                    app="FIREFOX", role="fixture",
                ))
                self.assertTrue(ready.wait(1.0))
            finally:
                router.stop()
            self.assertEqual(results[0]["status"], "MIGRATED")
            self.assertIn("parp-route-firefox-fixture-p246-s12345.scope", commands[0])

    def test_routes_only_existing_lstm_app_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            proc_root.mkdir()
            proc = make_fake_process(
                proc_root,
                pid=321,
                comm="epiphany",
                exe_path="/usr/bin/epiphany-browser",
                cgroup_path="/user.slice/app.slice/app-gnome-web.scope",
            )
            mapper = AppMapper(
                {"apps": {
                    "FIREFOX": {"keywords": ["epiphany"]},
                    "WPS": {"keywords": ["wps"]},
                }},
                target_apps=["FIREFOX", "WPS"],
            )
            results: list[dict[str, object]] = []
            ready = threading.Event()
            commands: list[list[str]] = []

            def callback(result: dict[str, object]) -> None:
                results.append(result)
                ready.set()

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                (proc / "cgroup").write_text(
                    "0::/user.slice/user-1000.slice/user@1000.service/"
                    "parp.slice/parp-firefox.slice/route.scope\n",
                    encoding="utf-8",
                )
                return subprocess.CompletedProcess(command, 0, "ok", "")

            router = SystemdProcessCgroupRouter(
                mapper=mapper,
                app_ids={"FIREFOX": 1},
                callback=callback,
                proc_root=proc_root,
                expected_uid=os.getuid(),
                command_runner=runner,
            )
            router.start()
            try:
                identity = ProcessIdentity(
                    pid=321,
                    tgid=321,
                    comm="epiphany",
                    exe_path="/usr/bin/epiphany-browser",
                    start_time="12345",
                )
                self.assertTrue(router.submit_created_process(
                    {"event_type": "PROCESS_START", "source_seq": 7}, identity
                ))
                self.assertTrue(ready.wait(1.0))
            finally:
                router.stop()

            self.assertEqual(results[0]["status"], "MIGRATED")
            self.assertEqual(results[0]["app"], "FIREFOX")
            self.assertEqual(results[0]["app_id"], 1)
            self.assertEqual(results[0]["target_slice"], "parp-firefox.slice")
            self.assertIn("PIDs", commands[0])
            self.assertIn("321", commands[0])

            # Mapper 可以识别 WPS，但 WPS 不在传入的固定 LSTM App ID 表中，
            # 因而不能产生动态 ID，也不能发起 systemd 迁移。
            wps = ProcessIdentity(
                pid=999, tgid=999, comm="wps", exe_path="/usr/bin/wps"
            )
            self.assertIsNone(router.target_for_identity(wps))

    def test_rejects_pid_reuse_before_systemd_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            proc_root.mkdir()
            make_fake_process(
                proc_root,
                pid=654,
                comm="vlc",
                exe_path="/usr/bin/vlc",
                cgroup_path="/user.slice/session.scope",
                start_time=222,
            )
            mapper = AppMapper(
                {"apps": {"VLC": {"keywords": ["vlc"]}}},
                target_apps=["VLC"],
            )
            commands: list[list[str]] = []
            results: list[dict[str, object]] = []
            ready = threading.Event()

            def callback(result: dict[str, object]) -> None:
                results.append(result)
                ready.set()

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                return subprocess.CompletedProcess(command, 0, "ok", "")

            router = SystemdProcessCgroupRouter(
                mapper=mapper,
                app_ids={"VLC": 3},
                callback=callback,
                proc_root=proc_root,
                expected_uid=os.getuid(),
                command_runner=runner,
            )
            router.start()
            try:
                self.assertTrue(router.submit_created_process(
                    {"event_type": "PROCESS_START", "source_seq": 9},
                    ProcessIdentity(
                        pid=654,
                        tgid=654,
                        comm="vlc",
                        exe_path="/usr/bin/vlc",
                        start_time="111",
                    ),
                ))
                self.assertTrue(ready.wait(1.0))
            finally:
                router.stop()

            self.assertEqual(results[0]["status"], "PID_REUSED")
            self.assertEqual(results[0]["target_slice"], "parp-vlc.slice")
            self.assertEqual(commands, [])

    def test_created_child_inherits_app_scope_without_second_migration(self) -> None:
        """子进程已继承 App scope 时仍接受检查，但不得再次调用 systemd。"""
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            proc_root.mkdir()
            make_fake_process(
                proc_root,
                pid=789,
                comm="vlc",
                exe_path="/usr/bin/vlc",
                cgroup_path=(
                    "/user.slice/user-1000.slice/user@1000.service/parp.slice/"
                    "parp-vlc.slice/parp-route-vlc-parent.scope"
                ),
                start_time=333,
            )
            mapper = AppMapper(
                {"apps": {"VLC": {"keywords": ["vlc"]}}},
                target_apps=["VLC"],
            )
            commands: list[list[str]] = []
            results: list[dict[str, object]] = []
            ready = threading.Event()

            def callback(result: dict[str, object]) -> None:
                results.append(result)
                ready.set()

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                return subprocess.CompletedProcess(command, 0, "ok", "")

            router = SystemdProcessCgroupRouter(
                mapper=mapper,
                app_ids={"VLC": 3},
                callback=callback,
                proc_root=proc_root,
                expected_uid=os.getuid(),
                command_runner=runner,
            )
            router.start()
            try:
                self.assertTrue(router.submit_created_process(
                    {"event_type": "PROCESS_START", "source_seq": 10},
                    ProcessIdentity(
                        pid=789,
                        tgid=789,
                        comm="vlc",
                        exe_path="/usr/bin/vlc",
                        start_time="333",
                    ),
                ))
                self.assertTrue(ready.wait(1.0))
            finally:
                router.stop()

            self.assertEqual(results[0]["status"], "ALREADY_ROUTED")
            self.assertEqual(commands, [])


if __name__ == "__main__":
    unittest.main()
