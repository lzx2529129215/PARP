from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _run(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=5, check=False)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    value = (result.stdout or result.stderr).strip()
    return value or None


def executable_metadata(command: str) -> dict[str, Any]:
    resolved = shutil.which(command)
    if resolved is None:
        return {"command": command, "resolved_path": None, "sha256": None, "package": None, "package_version": None}
    path = Path(resolved).resolve()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    owner = _run(["dpkg-query", "-S", str(path)])
    package = owner.split(":", 1)[0] if owner else None
    version = _run(["dpkg-query", "-W", "-f=${Version}", package]) if package else None
    return {
        "command": command,
        "resolved_path": str(path),
        "sha256": digest,
        "package": package,
        "package_version": version,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record an application's executable and package identity")
    parser.add_argument("--command", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    value = executable_metadata(args.command)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if value["resolved_path"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
