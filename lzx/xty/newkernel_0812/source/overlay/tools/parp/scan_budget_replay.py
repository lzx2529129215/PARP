#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Offline Phase 2.5 acceptance replay for scenarios A through H."""

import argparse
import json

from scan_budget_reference import compute_scan_budget


def replay():
    cases = {
        "A": compute_scan_budget(1000, 30000, True, "NORMAL", "OBSERVE"),
        "B": compute_scan_budget(1000, 30000, False, "NORMAL", "OBSERVE"),
        "C": compute_scan_budget(1000, 1000, False, "NORMAL", "OBSERVE"),
        "D": compute_scan_budget(1000, 30000, False, "NORMAL", "OBSERVE",
                                 prior_valid=False),
        "E": compute_scan_budget(1000, 30000, False, "NORMAL", "OBSERVE",
                                 scope="GLOBAL_KSWAPD"),
        "F": compute_scan_budget(1000, 30000, False, "NORMAL", "OBSERVE",
                                 scope="GLOBAL_DIRECT"),
        "G": compute_scan_budget(1000, 30000, True, "EMERGENCY", "OBSERVE"),
        "H": compute_scan_budget(1000, 30000, False, "NORMAL", "APPLY"),
    }
    checks = {
        "A": 0 < cases["A"]["proposed"] < 1000 and cases["A"]["applied"] == 1000,
        "B": cases["A"]["proposed"] <= cases["B"]["proposed"] < 1000 and cases["B"]["applied"] == 1000,
        "C": 1000 < cases["C"]["proposed"] <= 1500 and cases["C"]["applied"] == 1000,
        "D": cases["D"]["proposed"] == cases["D"]["applied"] == 1000,
        "E": cases["E"]["proposed"] == cases["E"]["applied"] == 1000,
        "F": cases["F"]["proposed"] == cases["F"]["applied"] == 1000,
        "G": cases["G"]["proposed"] == cases["G"]["applied"] == 1000,
        "H": cases["H"]["applied"] == cases["H"]["proposed"] < 1000,
    }
    return {"passed": all(checks.values()), "checks": checks, "cases": cases}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    args = parser.parse_args()
    result = replay()
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(payload)
    else:
        print(payload, end="")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
