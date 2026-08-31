"""Route newly created, LSTM-known user processes into per-App systemd slices."""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    from core.app_mapper import AppMapper, ProcessIdentity
except ModuleNotFoundError:  # package import used by unit tests
    from .app_mapper import AppMapper, ProcessIdentity


_SLUG_RE = re.compile(r"[^a-z0-9_.-]+")


@dataclass(frozen=True)
class ProcessRouteTarget:
    """A configured LSTM App ID and its cgroup parent slice."""

    app: str
    app_id: int
    target_slice: str
    role: str = "gui"


@dataclass(frozen=True)
class _RouteRequest:
    pid: int
    event_type: str
    source_seq: int
    requested_ts_ns: int
    expected_app: str
    expected_app_id: int
    expected_start_time: str
    expected_target_slice: str
    expected_role: str = "gui"


@dataclass(frozen=True)
class _ProcessSnapshot:
    pid: int
    owner_uid: int
    comm: str
    exe_path: str
    cgroup_path: str
    start_time: str

    def identity(self) -> ProcessIdentity:
        return ProcessIdentity(
            pid=self.pid,
            tgid=self.pid,
            comm=self.comm,
            exe_path=self.exe_path,
            cgroup_path=self.cgroup_path,
            start_time=self.start_time,
        )


def _app_slug(app: str) -> str:
    return _SLUG_RE.sub("-", str(app).strip().lower()).strip("-._") or "unknown"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_cgroup_path(proc: Path) -> str:
    for line in _read_text(proc / "cgroup").splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0":
            return parts[2]
    return ""


def _read_start_time(proc: Path) -> str:
    text = _read_text(proc / "stat")
    try:
        # /proc/<pid>/stat 的 comm 可含空格和右括号；从最后一个 ')' 以后解析。
        # starttime 是剩余部分的第 20 项，即内核文档中的总字段 22。
        return str(text.rsplit(")", 1)[1].strip().split()[19])
    except (IndexError, ValueError):
        return ""


def _read_snapshot(proc_root: Path, pid: int) -> _ProcessSnapshot | None:
    proc = proc_root / str(int(pid))
    try:
        owner_uid = int(proc.stat().st_uid)
    except OSError:
        return None
    try:
        exe_path = os.readlink(proc / "exe")
    except OSError:
        exe_path = ""
    return _ProcessSnapshot(
        pid=int(pid),
        owner_uid=owner_uid,
        comm=_read_text(proc / "comm").strip(),
        exe_path=exe_path,
        cgroup_path=_read_cgroup_path(proc),
        start_time=_read_start_time(proc),
    )


def _inside_slice(cgroup_path: str, target_slice: str) -> bool:
    """判断进程是否已经位于目标 App slice 本身或它的任意后代中。

    这里按 cgroup 路径组件做精确匹配，而不是字符串包含匹配。例如目标是
    ``parp-vlc.slice`` 时，下面两种路径都算已经归组：

    * ``.../parp-vlc.slice``；
    * ``.../parp-vlc.slice/parp-route-vlc-p123.scope``。

    但 ``parp-vlc.slice.backup`` 不会误匹配。这个判断是避免对子进程重复迁移的
    核心：父进程迁移后，新 fork 的子进程会由 cgroup v2 自动继承父进程的
    leaf scope；它们仍会被 createProcess 检查，但不会再次调用 systemd。
    """
    return target_slice in [part for part in str(cgroup_path).split("/") if part]


class SystemdProcessCgroupRouter:
    """把已有 LSTM App ID 的进程异步归入对应的 user-systemd App slice。

    ``mapper`` 和 ``app_ids`` 直接来自 monitor 已加载的 runtime App scope，
    因此本类只使用已经存在、允许预测的固定 App ID，不会看到未知进程就动态
    创建 App 或分配新 ID。无法映射的进程保留在 GNOME、systemd 或父进程为它
    选择的原 cgroup 中。

    这里区分两个层次：

    * ``parp-<app>.slice`` 是整个 App 的汇总父 cgroup；同一 App 的所有独立
      启动实例最终都位于这个 slice 下。
    * ``parp-route-...scope`` 是一次独立启动的进程树根。根进程迁移完成后，
      以后 fork 出来的后代会自动继承这个 leaf scope，不需要逐个实际迁移。

    仍然检查每个 PROCESS_START，是为了处理“主进程迁移完成前已经 fork 的
    子进程”、GNOME 再次启动的独立实例、D-Bus/portal 代为启动的组件，以及
    被其他 systemd unit 移出的进程。检查不等于迁移；已经继承目标 slice 的
    进程会返回 ``ALREADY_ROUTED``。

    真正迁移通过 user-systemd ``StartTransientUnit(PIDs=[pid])`` 完成，不直接
    mkdir cgroup 或写 ``cgroup.procs``，从而保持 systemd unit 状态和 cgroup v2
    层级一致。D-Bus 往返在独立 worker 线程中执行，不阻塞全系统事件接收线程。
    """

    def __init__(
        self,
        *,
        mapper: AppMapper,
        app_ids: dict[str, int],
        callback: Callable[[dict[str, Any]], None],
        proc_root: str | Path = "/proc",
        expected_uid: int | None = None,
        busctl: str = "busctl",
        timeout_s: float = 2.0,
        queue_capacity: int = 4096,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        fixture_scope_to_app: dict[str, str] | None = None,
    ) -> None:
        normalized_ids = {
            str(app).strip().upper(): int(app_id)
            for app, app_id in app_ids.items()
            if str(app).strip() and int(app_id) > 0
        }
        if not normalized_ids:
            raise ValueError("process cgroup routing requires existing LSTM App IDs")
        self.mapper = mapper
        self.app_ids = normalized_ids
        self.callback = callback
        self.proc_root = Path(proc_root)
        self.expected_uid = os.getuid() if expected_uid is None else int(expected_uid)
        self.busctl = str(busctl)
        self.timeout_s = max(0.1, float(timeout_s))
        self.command_runner = command_runner or subprocess.run
        self.fixture_scope_to_app = {
            str(scope): str(app).strip().upper()
            for scope, app in (fixture_scope_to_app or {}).items()
            if str(scope) and str(app).strip().upper() in self.app_ids
        }
        self._queue: queue.Queue[_RouteRequest | None] = queue.Queue(
            maxsize=max(1, int(queue_capacity))
        )
        self._pending: set[int] = set()
        self._pending_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def apps(self) -> list[str]:
        return sorted(self.app_ids)

    @property
    def started(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return
        if self.command_runner is subprocess.run and shutil.which(self.busctl) is None:
            raise FileNotFoundError(f"systemd bus client not found: {self.busctl}")
        self._thread = threading.Thread(
            target=self._run,
            name="runtime-monitor-cgroup-router",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=max(3.0, self.timeout_s + 1.0))
        self._thread = None

    def target_for_identity(self, identity: ProcessIdentity) -> ProcessRouteTarget | None:
        """只有 AppMapper 命中已有 LSTM App ID 时才生成目标 slice。"""
        app = str(self.mapper.map_process(identity) or "").strip().upper()
        app_id = int(self.app_ids.get(app, 0) or 0)
        if not app or app_id <= 0:
            return None
        components = {
            item for item in str(identity.cgroup_path or "").split("/") if item
        }
        role = "gui"
        if any(
            scope in components and fixture_app == app
            for scope, fixture_app in self.fixture_scope_to_app.items()
        ):
            role = "fixture"
        elif any(
            component.startswith(f"parp-route-{_app_slug(app)}-fixture-")
            for component in components
        ):
            role = "fixture"
        return ProcessRouteTarget(
            app=app,
            app_id=app_id,
            target_slice=f"parp-{_app_slug(app)}.slice",
            role=role,
        )

    def _target_for_known_app(self, app: str, role: str) -> ProcessRouteTarget | None:
        normalized = str(app).strip().upper()
        app_id = int(self.app_ids.get(normalized, 0) or 0)
        if app_id <= 0:
            return None
        return ProcessRouteTarget(
            app=normalized,
            app_id=app_id,
            target_slice=f"parp-{_app_slug(normalized)}.slice",
            role="fixture" if role == "fixture" else "gui",
        )

    def submit_created_process(
        self,
        event: dict[str, Any],
        identity: ProcessIdentity,
        *,
        app: str = "",
        role: str = "gui",
    ) -> bool:
        """接收每次 createProcess 判断，并按需把迁移检查放入 worker 队列。

        每个 FORK 都可以到达这里，但只有能映射到固定 App ID 的进程才入队。
        是否已经通过父进程继承目标 cgroup，要在 worker 中重新读取实时 /proc
        后决定；事件携带的 cgroup 可能已经因并发迁移而过时。
        """
        target = self._target_for_known_app(app, role) if app else self.target_for_identity(identity)
        if target is None:
            return False
        return self._enqueue(_RouteRequest(
            pid=int(identity.pid),
            event_type=str(event.get("event_type", "PROCESS_START")),
            source_seq=int(event.get("source_seq", 0) or 0),
            requested_ts_ns=int(event.get("timestamp_ns", 0) or time.time_ns()),
            expected_app=target.app,
            expected_app_id=target.app_id,
            expected_start_time=str(identity.start_time),
            expected_target_slice=target.target_slice,
            expected_role=target.role,
        ))

    def submit_exec_process(
        self,
        event: dict[str, Any],
        identity: ProcessIdentity,
        *,
        app: str = "",
        role: str = "gui",
    ) -> bool:
        """EXEC 后按最终 comm/exe 复核，覆盖 launcher、脚本包装器等情况。"""
        target = self._target_for_known_app(app, role) if app else self.target_for_identity(identity)
        if target is None:
            return False
        return self._enqueue(_RouteRequest(
            pid=int(identity.pid),
            event_type="PROCESS_EXEC",
            source_seq=int(event.get("source_seq", 0) or 0),
            requested_ts_ns=int(event.get("timestamp_ns", 0) or time.time_ns()),
            expected_app=target.app,
            expected_app_id=target.app_id,
            expected_start_time=str(identity.start_time),
            expected_target_slice=target.target_slice,
            expected_role=target.role,
        ))

    def _enqueue(self, request: _RouteRequest) -> bool:
        if request.pid <= 0 or self._stop.is_set():
            return False
        with self._pending_lock:
            # 同一 PID 的 FORK、紧随其后的 EXEC 和离散索引重建可能同时到达。
            # pending 去重保证任一时刻最多只有一个 worker 操作该进程；worker
            # 会读取最新 comm/exe/cgroup，所以不会依赖已经过时的事件快照。
            if request.pid in self._pending:
                return False
            self._pending.add(request.pid)
        try:
            self._queue.put_nowait(request)
            return True
        except queue.Full:
            with self._pending_lock:
                self._pending.discard(request.pid)
            self._emit(self._result(
                request,
                status="QUEUE_FULL",
                detail="cgroup route queue capacity exceeded",
            ))
            return False

    def _run(self) -> None:
        while True:
            if self._stop.is_set():
                return
            try:
                request = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue
            if request is None:
                return
            try:
                self._emit(self._route(request))
            except Exception as exc:
                self._emit(self._result(
                    request, status="ROUTER_ERROR", detail=str(exc)
                ))
            finally:
                with self._pending_lock:
                    self._pending.discard(request.pid)

    def _route(self, request: _RouteRequest) -> dict[str, Any]:
        start_ns = time.monotonic_ns()
        # 请求从事件线程排队到 worker 执行存在时间差，不能直接相信事件中的
        # cgroup/进程名。这里重新读取 /proc，随后依次防御进程退出、PID 复用、
        # EXEC 导致 App 身份变化以及跨 UID 迁移。
        snapshot = _read_snapshot(self.proc_root, request.pid)
        if snapshot is None:
            return self._result(request, status="GONE", latency_start_ns=start_ns)
        if (
            request.expected_start_time
            and snapshot.start_time != request.expected_start_time
        ):
            return self._result(
                request,
                snapshot=snapshot,
                status="PID_REUSED",
                detail=(
                    "PID starttime changed before migration: "
                    f"expected={request.expected_start_time}, actual={snapshot.start_time}"
                ),
                latency_start_ns=start_ns,
            )
        target = self.target_for_identity(snapshot.identity())
        if target is None and _inside_slice(
            snapshot.cgroup_path, request.expected_target_slice
        ):
            # 迁移后的通用 fixture/worker 可能无法再靠 exe 识别；队列请求中的
            # 固定 App ID 与目标 slice 仍可证明它没有离开原归属。
            target = self._target_for_known_app(
                request.expected_app, request.expected_role
            )
        if (
            target is None
            or target.app != request.expected_app
            or target.app_id != request.expected_app_id
            or target.role != request.expected_role
        ):
            return self._result(
                request,
                snapshot=snapshot,
                status="IDENTITY_CHANGED",
                detail="process no longer maps to the queued LSTM App ID",
                latency_start_ns=start_ns,
            )
        if snapshot.owner_uid != self.expected_uid:
            return self._result(
                request,
                snapshot=snapshot,
                target=target,
                status="FOREIGN_UID",
                detail=f"pid owner uid={snapshot.owner_uid}, expected={self.expected_uid}",
                latency_start_ns=start_ns,
            )
        # 最常见的后续子进程会走到这个分支：根进程已经被迁入 App scope，
        # 子进程 fork 时自然继承相同 cgroup。它虽然按要求经过了 createProcess，
        # 但这里只记录 ALREADY_ROUTED，绝不会再调用 StartTransientUnit。
        if _inside_slice(snapshot.cgroup_path, target.target_slice):
            return self._result(
                request,
                snapshot=snapshot,
                target=target,
                status="ALREADY_ROUTED",
                new_cgroup=snapshot.cgroup_path,
                latency_start_ns=start_ns,
            )
        if not snapshot.start_time:
            return self._result(
                request,
                snapshot=snapshot,
                target=target,
                status="NO_START_TIME",
                detail="cannot protect migration against PID reuse",
                latency_start_ns=start_ns,
            )

        # 走到这里说明它是一个尚未归组的独立进程树根，或者是在父进程迁移前
        # 已经 fork、因而仍留在旧 GNOME/systemd scope 的竞争遗漏进程。为它创建
        # 独立 leaf scope，但所有这类 scope 都挂在同一个 parp-<app>.slice 下，
        # 所以 App 级资源统计/控制仍可统一作用于父 slice。
        role_marker = "fixture-" if target.role == "fixture" else ""
        scope_name = (
            f"parp-route-{_app_slug(target.app)}-{role_marker}p{snapshot.pid}-"
            f"s{snapshot.start_time}.scope"
        )
        command = self._start_transient_scope_command(scope_name, target, snapshot.pid)
        try:
            completed = self.command_runner(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._result(
                request,
                snapshot=snapshot,
                target=target,
                target_scope=scope_name,
                status="SYSTEMD_ERROR",
                detail=str(exc),
                latency_start_ns=start_ns,
            )
        if int(completed.returncode) != 0:
            return self._result(
                request,
                snapshot=snapshot,
                target=target,
                target_scope=scope_name,
                status="SYSTEMD_ERROR",
                detail=(completed.stderr or completed.stdout or "").strip(),
                latency_start_ns=start_ns,
            )

        # StartTransientUnit 返回异步 job；必须观察真实 membership 后才记成功。
        deadline = time.monotonic() + min(1.0, self.timeout_s)
        after: _ProcessSnapshot | None = None
        while time.monotonic() < deadline:
            after = _read_snapshot(self.proc_root, snapshot.pid)
            if after is None or _inside_slice(after.cgroup_path, target.target_slice):
                break
            time.sleep(0.01)
        if after is None:
            return self._result(
                request,
                snapshot=snapshot,
                target=target,
                target_scope=scope_name,
                status="GONE_AFTER_REQUEST",
                latency_start_ns=start_ns,
            )
        if after.start_time != snapshot.start_time:
            return self._result(
                request,
                snapshot=snapshot,
                target=target,
                target_scope=scope_name,
                new_cgroup=after.cgroup_path,
                status="PID_REUSED",
                detail="PID starttime changed while migration was pending",
                latency_start_ns=start_ns,
            )
        migrated = _inside_slice(after.cgroup_path, target.target_slice)
        return self._result(
            request,
            snapshot=snapshot,
            target=target,
            target_scope=scope_name,
            new_cgroup=after.cgroup_path,
            status="MIGRATED" if migrated else "MIGRATION_NOT_OBSERVED",
            detail="" if migrated else "systemd accepted the job but membership did not change before timeout",
            latency_start_ns=start_ns,
        )

    def _start_transient_scope_command(
        self,
        scope_name: str,
        target: ProcessRouteTarget,
        pid: int,
    ) -> list[str]:
        # PIDs 属性只迁移当前进程（包含它的所有线程），不会回溯搬运已经存在的
        # 子进程；这也是 createProcess 必须检查每个 FORK 的原因。迁移完成以后
        # 新创建的后代会自然继承；迁移前的竞争遗漏由对应 START/EXEC 事件处理。
        return [
            self.busctl,
            "--user",
            "call",
            "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
            "StartTransientUnit",
            "ssa(sv)a(sa(sv))",
            scope_name,
            "fail",
            "4",
            "Description",
            "s",
            f"PARP App {target.app_id} ({target.app}) process {pid}",
            "Slice",
            "s",
            target.target_slice,
            "PIDs",
            "au",
            "1",
            str(int(pid)),
            "CollectMode",
            "s",
            "inactive-or-failed",
            "0",
        ]

    def _result(
        self,
        request: _RouteRequest,
        *,
        snapshot: _ProcessSnapshot | None = None,
        target: ProcessRouteTarget | None = None,
        target_scope: str = "",
        new_cgroup: str = "",
        status: str,
        detail: str = "",
        latency_start_ns: int = 0,
    ) -> dict[str, Any]:
        return {
            "timestamp_ns": time.time_ns(),
            "event_type": request.event_type,
            "source_seq": request.source_seq,
            "requested_timestamp_ns": request.requested_ts_ns,
            "app": target.app if target is not None else request.expected_app,
            "app_id": target.app_id if target is not None else request.expected_app_id,
            "pid": request.pid,
            "comm": snapshot.comm if snapshot is not None else "",
            "exe_path": snapshot.exe_path if snapshot is not None else "",
            "old_cgroup": snapshot.cgroup_path if snapshot is not None else "",
            "target_slice": (
                target.target_slice
                if target is not None
                else request.expected_target_slice
            ),
            "target_scope": target_scope,
            "new_cgroup": new_cgroup,
            "status": status,
            "detail": detail,
            "latency_us": (
                max(0, (time.monotonic_ns() - latency_start_ns) // 1000)
                if latency_start_ns else 0
            ),
        }

    def _emit(self, result: dict[str, Any]) -> None:
        try:
            self.callback(result)
        except Exception:
            pass
