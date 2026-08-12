#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Strictly resolve the running Phase 2.6 target or its audited bootfix."""

import argparse
import json
from pathlib import Path
import subprocess


class IdentityError(RuntimeError):
    """The running kernel cannot be tied to the recorded Phase 2.6 source."""


def validate_identity(phase, bootfix, running, source_is_ancestor):
    target = phase.get("target_kernel_release")
    if target and running == target:
        return {"kind": "ORIGINAL_TARGET", "release": running}

    phase_release = phase.get("bootfix_release")
    actual = phase.get("actual_booted_kernel_release")
    bootfix_release = bootfix.get("bootfix_release")
    phase_head = phase.get("bootfix_source_head")
    bootfix_head = bootfix.get("source_final_head")
    checks = {
        "phase_bootfix_release": phase_release == running,
        "phase_actual_release": actual == running,
        "bootfix_release": bootfix_release == running,
        "source_head": bool(phase_head) and phase_head == bootfix_head,
        "boot_verified": bootfix.get("boot_verified") is True,
        "source_is_ancestor": source_is_ancestor is True,
    }
    if not all(checks.values()):
        failed = ",".join(key for key, value in checks.items() if not value)
        raise IdentityError("bootfix identity check failed: " + failed)
    return {
        "kind": "BOOTFIX",
        "release": running,
        "source_head": bootfix_head,
    }


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("phase_state", type=Path)
    parser.add_argument("bootfix_state", type=Path)
    parser.add_argument("running")
    parser.add_argument("work_tree", type=Path)
    args = parser.parse_args()
    phase = load(args.phase_state)
    bootfix = load(args.bootfix_state) if args.bootfix_state.is_file() else {}
    source_head = phase.get("bootfix_source_head", "")
    ancestor = False
    if source_head:
        result = subprocess.run(
            ["git", "-C", str(args.work_tree), "merge-base", "--is-ancestor",
             source_head, "HEAD"], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ancestor = result.returncode == 0
    resolved = validate_identity(phase, bootfix, args.running, ancestor)
    print(resolved["release"])


if __name__ == "__main__":
    main()
