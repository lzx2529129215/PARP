from __future__ import annotations

import fcntl
import os
from pathlib import Path

from .models import CapabilityStatus, ProcessInfo, RegionMonitorConfig


class DamonController:
    """Own one append-only DAMON sysfs kdamond and remove only that instance."""

    def __init__(self, config: RegionMonitorConfig, sysfs_root: str | Path = "/sys/kernel/mm/damon/admin") -> None:
        self.config = config
        self.sysfs_root = Path(sysfs_root)
        self.lock_file: object | None = None
        self.started = False
        self.status = CapabilityStatus.UNSUPPORTED.value
        self.error = ""
        self.cleanup_ok = True
        self.cleanup_error = ""
        self.initial_nr_kdamonds: int | None = None
        self.kdamond_index: int | None = None
        self.context_index: int | None = None
        self.target_pid_map: dict[str, ProcessInfo] = {}

    @property
    def kdamond_path(self) -> Path | None:
        if self.kdamond_index is None:
            return None
        return self.sysfs_root / "kdamonds" / str(self.kdamond_index)

    def start(self, targets: list[ProcessInfo]) -> bool:
        self.error = ""
        if not self.sysfs_root.exists():
            return self._fail(CapabilityStatus.MISSING_SYSFS.value, f"DAMON sysfs not found: {self.sysfs_root}")
        if not os.access(self.sysfs_root, os.R_OK | os.W_OK):
            return self._fail(
                CapabilityStatus.SUPPORTED_NEEDS_ROOT.value,
                f"no read/write permission for DAMON sysfs: {self.sysfs_root}",
            )
        unique_targets = _unique_live_targets(targets)
        if not unique_targets:
            return self._fail(CapabilityStatus.UNSUPPORTED.value, "no live target PID to configure")
        if not self._acquire_lock():
            return False

        try:
            nr_path = self.sysfs_root / "kdamonds" / "nr_kdamonds"
            self.initial_nr_kdamonds = self._read_int(nr_path)
            self.kdamond_index = self.initial_nr_kdamonds
            self._write(nr_path, self.initial_nr_kdamonds + 1)
            kdamond = self._required_kdamond_path()
            contexts = kdamond / "contexts"
            self._write(contexts / "nr_contexts", 1)
            self.context_index = 0
            context = contexts / "0"
            available = set((context / "avail_operations").read_text(encoding="utf-8").split())
            if "vaddr" not in available:
                raise RuntimeError(f"vaddr is not available; avail_operations={sorted(available)}")
            self._write(context / "operations", "vaddr")
            attrs = context / "monitoring_attrs"
            intervals = attrs / "intervals"
            self._write(intervals / "sample_us", self.config.damon_sample_us)
            self._write(intervals / "aggr_us", self.config.damon_aggr_us)
            self._write(intervals / "update_us", self.config.damon_update_us)
            self._write(attrs / "nr_regions" / "min", self.config.damon_min_nr_regions)
            self._write(attrs / "nr_regions" / "max", self.config.damon_max_nr_regions)
            target_pid_map = self._configure_targets(unique_targets)
            self._write(kdamond / "state", "on")
            if self._read(kdamond / "state") != "on":
                raise RuntimeError(f"kdamond did not enter on state: {self._read(kdamond / 'state')}")
            self.target_pid_map = target_pid_map
        except Exception as exc:
            self.error = f"failed to configure DAMON sysfs: {exc}"
            self.status = CapabilityStatus.UNSUPPORTED.value
            self._cleanup_owned_instance()
            self._release_lock()
            return False

        self.status = CapabilityStatus.SUPPORTED.value
        self.started = True
        return True

    def update_targets(self, targets: list[ProcessInfo]) -> bool:
        if not self.started:
            return False
        unique_targets = _unique_live_targets(targets)
        if not unique_targets:
            self.error = "refusing to commit an empty DAMON target set"
            return False
        if [item.pid for item in unique_targets] == [item.pid for item in self.target_pid_map.values()]:
            return True
        try:
            target_pid_map = self._configure_targets(unique_targets)
            self._write(self._required_kdamond_path() / "state", "commit")
            self.target_pid_map = target_pid_map
            return True
        except Exception as exc:
            self.error = f"failed to update DAMON targets: {exc}"
            return False

    def stop(self) -> None:
        self._cleanup_owned_instance()
        self._release_lock()
        self.started = False

    def _configure_targets(self, targets: list[ProcessInfo]) -> dict[str, ProcessInfo]:
        kdamond = self._required_kdamond_path()
        targets_dir = kdamond / "contexts" / "0" / "targets"
        self._write(targets_dir / "nr_targets", len(targets))
        mapping: dict[str, ProcessInfo] = {}
        for target_index, target in enumerate(targets):
            self._write(targets_dir / str(target_index) / "pid_target", target.pid)
            mapping[str(target_index)] = target
        return mapping

    def _cleanup_owned_instance(self) -> None:
        self.cleanup_ok = True
        self.cleanup_error = ""
        kdamond = self.kdamond_path
        if kdamond is None or self.initial_nr_kdamonds is None:
            return
        errors: list[str] = []
        try:
            if kdamond.exists() and self._read(kdamond / "state") != "off":
                self._write(kdamond / "state", "off")
        except Exception as exc:
            errors.append(f"failed to stop kdamond {self.kdamond_index}: {exc}")
        try:
            nr_path = self.sysfs_root / "kdamonds" / "nr_kdamonds"
            current = self._read_int(nr_path)
            expected = self.initial_nr_kdamonds + 1
            if current == expected and self.kdamond_index == current - 1:
                self._write(nr_path, self.initial_nr_kdamonds)
            elif kdamond.exists():
                errors.append(
                    f"owned kdamond {self.kdamond_index} is no longer the last instance; "
                    f"left stopped instead of deleting unrelated instances (nr_kdamonds={current})"
                )
        except Exception as exc:
            errors.append(f"failed to remove owned kdamond {self.kdamond_index}: {exc}")
        self.cleanup_ok = not errors
        self.cleanup_error = "; ".join(errors)
        self.target_pid_map.clear()
        self.kdamond_index = None
        self.context_index = None
        self.initial_nr_kdamonds = None

    def _acquire_lock(self) -> bool:
        lock_path = Path("/tmp/runtime_monitor_region_damon.lock")
        self.lock_file = lock_path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as exc:
            self._release_lock()
            return self._fail(
                CapabilityStatus.PERMISSION_DENIED.value,
                f"failed to acquire DAMON lock {lock_path}: {exc}",
            )

    def _release_lock(self) -> None:
        if self.lock_file is None:
            return
        try:
            fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_UN)
            self.lock_file.close()
        except OSError:
            pass
        self.lock_file = None

    def _required_kdamond_path(self) -> Path:
        path = self.kdamond_path
        if path is None or not path.is_dir():
            raise RuntimeError(f"owned kdamond directory is missing: {path}")
        return path

    @staticmethod
    def _write(path: Path, value: str | int) -> None:
        path.write_text(f"{value}\n", encoding="utf-8")

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8").strip()

    def _read_int(self, path: Path) -> int:
        return int(self._read(path))

    def _fail(self, status: str, error: str) -> bool:
        self.status = status
        self.error = error
        return False


def _unique_live_targets(targets: list[ProcessInfo]) -> list[ProcessInfo]:
    unique: dict[tuple[int, int], ProcessInfo] = {}
    for target in targets:
        if target.pid <= 0 or not Path("/proc", str(target.pid)).exists():
            continue
        unique[target.identity] = target
    return sorted(unique.values(), key=lambda item: item.pid)
