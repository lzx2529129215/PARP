#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Resolve the relocated PARP experiment workspace without hard-coded paths.  #lzx"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class WorkspaceLayout:  #lzx
    """The versioned v4-parp tree and its explicit sibling dependencies.  #lzx"""

    lzx_root: Path
    v4_parp_root: Path
    automation_root: Path
    runtime_monitor_root: Path

    def missing_dependencies(self) -> tuple[Path, ...]:  #lzx
        required = (  #lzx
            self.automation_root / "app_automation.py",
            self.runtime_monitor_root / "core" / "parp_bridge.py",
        )
        return tuple(path for path in required if not path.is_file())  #lzx

    def as_json(self) -> dict[str, object]:  #lzx
        value = {name: str(path) for name, path in asdict(self).items()}  #lzx
        value["missing_dependencies"] = [  #lzx
            str(path) for path in self.missing_dependencies()
        ]
        return value  #lzx


def _ancestors(start: Path) -> Iterable[Path]:  #lzx
    current = start.resolve()  #lzx
    yield current  #lzx
    yield from current.parents  #lzx


def resolve_workspace(start: Path | None = None) -> WorkspaceLayout:  #lzx
    """Locate lzx/kernel/v4-parp from any source or tooling path below it.  #lzx"""

    origin = Path.cwd() if start is None else Path(start)  #lzx
    for candidate in _ancestors(origin):  #lzx
        if (candidate.name != "v4-parp" or  #lzx
                candidate.parent.name != "kernel" or  #lzx
                candidate.parent.parent.name != "lzx"):  #lzx
            continue
        if not (candidate / "work").is_dir():  #lzx
            continue
        lzx_root = candidate.parent.parent  #lzx
        return WorkspaceLayout(  #lzx
            lzx_root=lzx_root,
            v4_parp_root=candidate,
            automation_root=lzx_root / "tool" / "automation",  #lzx
            runtime_monitor_root=lzx_root / "tool" / "runtime_monitor",  #lzx
        )
    raise ValueError(f"cannot locate lzx/kernel/v4-parp above {origin}")  #lzx


def main() -> int:  #lzx
    parser = argparse.ArgumentParser(description=__doc__)  #lzx
    parser.add_argument(  #lzx
        "--start",
        type=Path,
        default=Path(__file__),
        help="source or tooling path below the relocated v4-parp tree",
    )
    parser.add_argument("--json", action="store_true")  #lzx
    parser.add_argument("--require-dependencies", action="store_true")  #lzx
    args = parser.parse_args()  #lzx

    try:
        layout = resolve_workspace(args.start)  #lzx
    except ValueError as exc:
        parser.error(str(exc))  #lzx
    missing = layout.missing_dependencies()  #lzx
    if args.require_dependencies and missing:  #lzx
        parser.error("missing required dependency: " +  #lzx
                     ", ".join(str(path) for path in missing))

    if args.json:  #lzx
        print(json.dumps(layout.as_json(), indent=2, sort_keys=True))  #lzx
    else:
        for name, path in layout.as_json().items():  #lzx
            print(f"{name}={path}")  #lzx
    return 0  #lzx


if __name__ == "__main__":
    raise SystemExit(main())  #lzx
