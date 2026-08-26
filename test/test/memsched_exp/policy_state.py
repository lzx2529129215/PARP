"""Optional PARP policy adapter.

The measurement core does not import or require this module. Experiments opt in
when they need to bind a manifest to a PARP debugfs policy state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError):
        return None


def _field(text: str | None, name: str) -> str | None:
    if not text:
        return None
    pattern = re.compile(rf"(?:^|\s){re.escape(name)}[=: ]+([^\s]+)", re.MULTILINE)
    match = pattern.search(text)
    return match.group(1) if match else None


def capture_policy_state(root: str | Path = "/sys/kernel/debug/parp") -> dict[str, Any]:
    directory = Path(root)
    mode = _read(directory / "effective_tier_mode")
    stats = _read(directory / "effective_tier_stats")
    config = _read(directory / "effective_tier_config")
    apply_raw = _field(stats, "apply_compiled")
    apply_compiled = None if apply_raw is None else apply_raw.lower() in {"1", "true", "yes", "on"}
    model_provenance = _field(config, "model_provenance")
    readable = mode is not None and stats is not None and config is not None
    raw = {"mode": mode, "stats": stats, "config": config}
    return {
        "debugfs_root": str(directory),
        "readable": readable,
        "mode": mode,
        "apply_compiled": apply_compiled,
        "model_provenance": model_provenance,
        "raw_sha256": hashlib.sha256(
            json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "raw": raw,
    }


def verify_policy_state(
    value: dict[str, Any],
    expected_mode: str | None = None,
    expected_apply_compiled: bool | None = None,
    expected_model_provenance: str | None = None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not value.get("readable"):
        reasons.append("PARP effective policy debugfs files are not all readable")
    if expected_mode is not None and value.get("mode") != expected_mode:
        reasons.append(f"policy mode mismatch: expected {expected_mode!r}, observed {value.get('mode')!r}")
    if expected_apply_compiled is not None and value.get("apply_compiled") != expected_apply_compiled:
        reasons.append(
            "apply_compiled mismatch: "
            f"expected {expected_apply_compiled!r}, observed {value.get('apply_compiled')!r}"
        )
    if expected_model_provenance is not None and value.get("model_provenance") != expected_model_provenance:
        reasons.append(
            "model_provenance mismatch: "
            f"expected {expected_model_provenance!r}, observed {value.get('model_provenance')!r}"
        )
    return {**value, "valid": not reasons, "invalid_reasons": reasons}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture and verify an optional PARP policy adapter")
    parser.add_argument("--root", default="/sys/kernel/debug/parp")
    parser.add_argument("--expected-mode")
    parser.add_argument("--expected-apply-compiled", choices=("true", "false"))
    parser.add_argument("--expected-model-provenance")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    expected_apply = None
    if args.expected_apply_compiled is not None:
        expected_apply = args.expected_apply_compiled == "true"
    value = verify_policy_state(
        capture_policy_state(args.root),
        args.expected_mode,
        expected_apply,
        args.expected_model_provenance,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0 if value["valid"] else 5


if __name__ == "__main__":
    raise SystemExit(main())
