from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError, UnicodeDecodeError):
        return None


def process_identity(pid: int, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    stat_text = _read_text(proc_root / str(pid) / "stat")
    boot_id = _read_text(proc_root / "sys" / "kernel" / "random" / "boot_id")
    cgroup_text = _read_text(proc_root / str(pid) / "cgroup")
    if stat_text is None:
        return {
            "pid": pid,
            "alive": False,
            "boot_id": boot_id,
            "start_ticks": None,
            "state": None,
            "comm": None,
            "cgroup": None,
        }
    closing = stat_text.rfind(")")
    opening = stat_text.find("(")
    if opening < 0 or closing <= opening:
        raise ValueError(f"unexpected /proc/{pid}/stat format")
    remainder = stat_text[closing + 2 :].split()
    if len(remainder) <= 19:
        raise ValueError(f"truncated /proc/{pid}/stat")
    cgroup = None
    if cgroup_text:
        for line in cgroup_text.splitlines():
            if line.startswith("0::"):
                cgroup = line[3:] or "/"
                break
    state = remainder[0]
    return {
        "pid": pid,
        "alive": state != "Z",
        "boot_id": boot_id,
        "start_ticks": int(remainder[19]),
        "state": state,
        "comm": stat_text[opening + 1 : closing],
        "cgroup": cgroup,
    }


def same_process(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(
        before.get("alive")
        and after.get("alive")
        and before.get("pid") == after.get("pid")
        and before.get("start_ticks") is not None
        and before.get("start_ticks") == after.get("start_ticks")
        and before.get("boot_id") == after.get("boot_id")
    )


def classify_transition(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    before_alive = bool(before and before.get("alive"))
    after_alive = bool(after and after.get("alive"))
    if not before_alive and after_alive:
        return "cold_start"
    if before_alive and not after_alive:
        return "terminated"
    if before_alive and after_alive:
        return "hot_resume" if same_process(before or {}, after or {}) else "cold_restart"
    return "not_running"


def survival_summary(started: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    current_by_pid = {int(value["pid"]): value for value in current}
    survivors = sum(
        same_process(value, current_by_pid.get(int(value["pid"]), {}))
        for value in started
    )
    total = len(started)
    return {
        "background_apps_started": total,
        "background_apps_alive": survivors,
        "background_app_survival_ratio": survivors / total if total else None,
        "cold_restart_count": total - survivors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture and classify Linux process lifecycle identity")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--pid", action="append", type=int, required=True)
    snapshot.add_argument("--output", required=True)
    classify = subparsers.add_parser("classify")
    classify.add_argument("--before", required=True)
    classify.add_argument("--after", required=True)
    classify.add_argument("--output")
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        value: Any = [process_identity(pid) for pid in args.pid]
        output = Path(args.output)
    else:
        before = json.loads(Path(args.before).read_text(encoding="utf-8"))
        after = json.loads(Path(args.after).read_text(encoding="utf-8"))
        value = survival_summary(before, after)
        output = Path(args.output) if args.output else None
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        print(text, end="")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
