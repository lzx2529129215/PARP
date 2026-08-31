"""Event-maintained mapping between configured applications and live processes."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from core.app_mapper import AppMapper, ProcessIdentity


@dataclass(frozen=True)
class IndexedProcess:
    """一个已经被确认属于固定 runtime App ID 的进程实例。

    ``start_time`` 来自 ``/proc/<pid>/stat`` 字段 22。PID 会被内核复用，所以删除
    索引项时必须同时核对 start_time，不能让旧进程的迟到 EXIT 删除同 PID 的新进程。
    """

    identity: ProcessIdentity
    app: str
    # gui 参与窗口/App 生命周期；fixture 只参与进程归组、资源与 PARP binding。
    role: str = "gui"
    source_seq: int = 0
    last_event: str = ""


@dataclass(frozen=True)
class AppProcessChange:
    """START/EXEC/EXIT 对统一索引造成的可审计变化。"""

    pid: int
    previous_app: str = ""
    current_app: str = ""
    previous_role: str = ""
    current_role: str = ""
    removed: IndexedProcess | None = None


class AppProcessIndex:
    """由内核 START/EXEC/EXIT 事件维护的唯一 App↔PID 权威表。

    该索引只保存能映射到 ``runtime_app_scope`` 固定 App 的进程。未知系统进程仍
    会经过 createProcess/exeProcess/destroyProcess 并写全量 process_events.csv，
    但不占用 App 索引。AppRegistry、资源采样和 PARP binding 都应读取本表，而不
    再通过每秒枚举整个 ``/proc`` 重新发现进程。

    FORK 发生在 EXEC 之前：createProcess 看到的 comm/exe 可能仍属于 launcher。
    因此 EXEC 必须能够新增、移动或移除 PID；只实现 START/EXIT 会漏掉大量由
    GNOME、shell、portal 和 D-Bus 启动的应用。
    """

    def __init__(
        self,
        mapper: AppMapper,
        target_apps: Iterable[str],
        *,
        fixture_scope_to_app: dict[str, str] | None = None,
    ) -> None:
        self.mapper = mapper
        self.target_apps = {str(app).strip() for app in target_apps if str(app).strip()}
        self.fixture_scope_to_app = {
            str(scope): str(app)
            for scope, app in (fixture_scope_to_app or {}).items()
            if str(scope) and str(app) in self.target_apps
        }
        self._by_pid: dict[int, IndexedProcess] = {}
        self._by_app: dict[str, set[int]] = {
            app: set() for app in sorted(self.target_apps)
        }
        self._gui_by_app: dict[str, set[int]] = {
            app: set() for app in sorted(self.target_apps)
        }
        # 某 App 从非空变为空后先进入 grace；若期间新进程 START/EXEC，则取消关闭。
        # 这替代 LifecycleEventBuilder 每秒维护的第二份 app_pid_sets。
        self._empty_since_ns: dict[str, int] = {}
        self._reported_empty: set[str] = set()

    def bootstrap(self, identities: Iterable[ProcessIdentity]) -> None:
        """用一次性 /proc 基线重建索引；不会把启动时本来为空的 App 报成关闭。"""
        self._by_pid.clear()
        for pids in self._by_app.values():
            pids.clear()
        for pids in self._gui_by_app.values():
            pids.clear()
        self._empty_since_ns.clear()
        self._reported_empty.clear()
        for identity in identities:
            app, role = self._classify(identity)
            if app:
                self._insert(
                    identity, app, role=role,
                    source_seq=0, last_event="BOOTSTRAP",
                )

    def process_start(
        self,
        identity: ProcessIdentity,
        *,
        source_seq: int = 0,
        parent_pid: int = 0,
    ) -> AppProcessChange:
        """处理 FORK：结束 PID 的旧实例，并在已能识别时加入新实例。"""
        previous = self._by_pid.get(int(identity.pid))
        previous_app = previous.app if previous is not None else ""
        previous_role = previous.role if previous is not None else ""
        # PROCESS_START 明确表示一个新实例；即使 start_time 暂时为空，也必须清除
        # 可能残留的同 PID 旧实例，避免 PID reuse 污染 App 集合。
        if previous is not None:
            self._remove_pid(int(identity.pid), expected_start_time="")
        parent = self._by_pid.get(int(parent_pid)) if int(parent_pid) > 0 else None
        app, role = self._classify(identity, inherited=parent)
        if app:
            self._insert(
                identity, app, role=role,
                source_seq=source_seq, last_event="PROCESS_START",
            )
        return AppProcessChange(
            pid=int(identity.pid), previous_app=previous_app, current_app=app,
            previous_role=previous_role, current_role=role,
            removed=previous,
        )

    def process_exec(
        self, identity: ProcessIdentity, *, source_seq: int = 0
    ) -> AppProcessChange:
        """处理 EXEC：按最终程序身份新增、移动、刷新或移除 PID。"""
        previous = self._by_pid.get(int(identity.pid))
        previous_app = previous.app if previous is not None else ""
        previous_role = previous.role if previous is not None else ""
        app, role = self._classify(identity, inherited=previous)
        if previous is not None and (previous.app != app or previous.role != role):
            self._remove_pid(
                int(identity.pid), expected_start_time=str(previous.identity.start_time)
            )
        if app:
            self._insert(
                identity, app, role=role,
                source_seq=source_seq, last_event="PROCESS_EXEC",
            )
        return AppProcessChange(
            pid=int(identity.pid), previous_app=previous_app, current_app=app,
            previous_role=previous_role, current_role=role,
            removed=(
                previous
                if previous_app and (previous_app != app or previous_role != role)
                else None
            ),
        )

    def process_exit(self, identity: ProcessIdentity) -> AppProcessChange:
        """处理 EXIT，并用 start_time 防止迟到事件删除已经复用该 PID 的新实例。"""
        previous = self._by_pid.get(int(identity.pid))
        if previous is None:
            return AppProcessChange(pid=int(identity.pid))
        expected = str(identity.start_time or "")
        indexed_start = str(previous.identity.start_time or "")
        if expected and indexed_start and expected != indexed_start:
            return AppProcessChange(
                pid=int(identity.pid), previous_app=previous.app,
                current_app=previous.app, previous_role=previous.role,
                current_role=previous.role,
            )
        removed = self._remove_pid(int(identity.pid), expected_start_time=indexed_start)
        return AppProcessChange(
            pid=int(identity.pid), previous_app=previous.app,
            previous_role=previous.role,
            removed=removed,
        )

    def prune_unreadable(self, sampled_pids: Iterable[int]) -> list[IndexedProcess]:
        """只校验索引内 PID；移除已无法从 /proc 得到目标 App 样本的陈旧项。

        这不是全系统发现轮询。正常 EXIT 会先删除索引；该方法仅覆盖进程在事件
        队列处理前消失、读取竞态或 connector 已报告 delivery gap 的兜底情况。
        """
        present = {int(pid) for pid in sampled_pids}
        removed: list[IndexedProcess] = []
        for pid in sorted(set(self._by_pid) - present):
            item = self._remove_pid(pid, expected_start_time="")
            if item is not None:
                removed.append(item)
        return removed

    def pids(self) -> list[int]:
        return sorted(self._by_pid)

    def pids_for_app(self, app: str, *, role: str = "") -> set[int]:
        if role == "gui":
            return set(self._gui_by_app.get(str(app), set()))
        if role == "fixture":
            return {
                pid for pid in self._by_app.get(str(app), set())
                if self._by_pid.get(pid) is not None
                and self._by_pid[pid].role == "fixture"
            }
        return set(self._by_app.get(str(app), set()))

    def entry(self, pid: int) -> IndexedProcess | None:
        return self._by_pid.get(int(pid))

    def entries(self) -> list[IndexedProcess]:
        """返回按 PID 排序的稳定快照，供 cgroup/eBPF 事件侧同步使用。"""
        return [self._by_pid[pid] for pid in sorted(self._by_pid)]

    def app_for_pid(self, pid: int) -> str:
        item = self.entry(pid)
        return item.app if item is not None else ""

    def role_for_pid(self, pid: int) -> str:
        item = self.entry(pid)
        return item.role if item is not None else ""

    def snapshot(self) -> dict[str, set[int]]:
        return {app: set(pids) for app, pids in self._by_app.items()}

    def pop_empty_apps(self, grace_s: float, *, now_ns: int | None = None) -> list[str]:
        """返回超过 grace 且仍无 PID 的 App，每次空周期只返回一次。"""
        now = int(now_ns if now_ns is not None else time.monotonic_ns())
        grace_ns = max(0, int(float(grace_s) * 1_000_000_000))
        ready: list[str] = []
        for app, since_ns in sorted(self._empty_since_ns.items()):
            if self._gui_by_app.get(app) or app in self._reported_empty:
                continue
            if now - since_ns >= grace_ns:
                ready.append(app)
                self._reported_empty.add(app)
        return ready

    def _classify(
        self,
        identity: ProcessIdentity,
        *,
        inherited: IndexedProcess | None = None,
    ) -> tuple[str, str]:
        """返回固定 App 和进程角色；cgroup alias 比通用可执行名更权威。"""
        components = {
            item for item in str(identity.cgroup_path or "").split("/") if item
        }
        for scope_name, app in self.fixture_scope_to_app.items():
            if scope_name in components:
                return app, "fixture"
        # 迁移后的 fixture scope 名显式携带 ``-fixture-``；服务重启做基线时
        # 仍能恢复角色，不依赖已被 systemd 回收的旧 alias scope。
        for component in components:
            if component.startswith("parp-route-") and "-fixture-" in component:
                for app in self.target_apps:
                    slug = app.lower().replace("_", "-")
                    if component.startswith(f"parp-route-{slug}-fixture-"):
                        return app, "fixture"
        app = str(self.mapper.map_process(identity) or "").strip()
        if app in self.target_apps:
            # 若 mapper 是通过目标 cgroup 而不是最终 executable 命中，沿用已知
            # role；普通首次命中默认为 GUI。
            role = inherited.role if inherited is not None and inherited.app == app else "gui"
            return app, role
        if inherited is not None:
            # 同一进程在目标 App cgroup 内 EXEC 通用 helper 时保持既有归属。
            target_slice = f"parp-{inherited.app.lower().replace('_', '-')}.slice"
            if target_slice in components:
                return inherited.app, inherited.role
        return "", ""

    def _insert(
        self,
        identity: ProcessIdentity,
        app: str,
        *,
        role: str,
        source_seq: int,
        last_event: str,
    ) -> None:
        pid = int(identity.pid)
        previous = self._by_pid.get(pid)
        if previous is not None and previous.app != app:
            self._remove_pid(pid, expected_start_time="")
        self._by_pid[pid] = IndexedProcess(
            identity=identity,
            app=app,
            role=role,
            source_seq=int(source_seq),
            last_event=last_event,
        )
        self._by_app.setdefault(app, set()).add(pid)
        if role == "gui":
            self._gui_by_app.setdefault(app, set()).add(pid)
            self._empty_since_ns.pop(app, None)
            self._reported_empty.discard(app)

    def _remove_pid(
        self, pid: int, *, expected_start_time: str
    ) -> IndexedProcess | None:
        item = self._by_pid.get(int(pid))
        if item is None:
            return None
        indexed_start = str(item.identity.start_time or "")
        if expected_start_time and indexed_start and expected_start_time != indexed_start:
            return None
        self._by_pid.pop(int(pid), None)
        app_pids = self._by_app.setdefault(item.app, set())
        app_pids.discard(int(pid))
        gui_pids = self._gui_by_app.setdefault(item.app, set())
        gui_pids.discard(int(pid))
        if item.role == "gui" and not gui_pids:
            self._empty_since_ns[item.app] = time.monotonic_ns()
            self._reported_empty.discard(item.app)
        return item
