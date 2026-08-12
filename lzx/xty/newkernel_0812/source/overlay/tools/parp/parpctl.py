#!/usr/bin/env python3
"""Minimal observe-only PARP control client."""

import argparse
import json
from pathlib import Path

from lstm_prior_bridge import DebugfsTransport

MODE = {"disabled": "0", "observe": "1", "apply": "2"}
MODE_PATH = Path("/sys/kernel/debug/parp/mode")
SCAN_BUDGET_MODE_PATH = Path("/sys/kernel/debug/parp/scan_budget_mode")
SCAN_BUDGET_APPLY_DOMAIN_PATH = Path(
    "/sys/kernel/debug/parp/scan_budget_apply_domain")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("mode-get")
    mode_set = sub.add_parser("mode-set")
    mode_set.add_argument("mode", choices=MODE)
    sub.add_parser("scan-budget-mode-get")
    budget_set = sub.add_parser("scan-budget-mode-set")
    budget_set.add_argument("mode", choices=MODE)
    sub.add_parser("scan-budget-apply-domain-get")
    apply_domain = sub.add_parser("scan-budget-apply-domain-set")
    apply_domain.add_argument("domain_id", type=int)
    batch = sub.add_parser("prior-batch-submit")
    batch.add_argument("json_file", type=Path)
    batch.add_argument("--debugfs", type=Path,
                       default=Path("/sys/kernel/debug/parp/app_prior_batch"))
    batch.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.command == "mode-get":
        print(MODE_PATH.read_text(encoding="ascii").strip())
    elif args.command == "mode-set":
        MODE_PATH.write_text(MODE[args.mode] + "\n", encoding="ascii")
    elif args.command == "scan-budget-mode-get":
        print(SCAN_BUDGET_MODE_PATH.read_text(encoding="ascii").strip())
    elif args.command == "scan-budget-mode-set":
        SCAN_BUDGET_MODE_PATH.write_text(MODE[args.mode] + "\n",
                                         encoding="ascii")
    elif args.command == "scan-budget-apply-domain-get":
        print(SCAN_BUDGET_APPLY_DOMAIN_PATH.read_text(
            encoding="ascii").strip())
    elif args.command == "scan-budget-apply-domain-set":
        if args.domain_id < 0:
            parser.error("domain_id must be non-negative")
        SCAN_BUDGET_APPLY_DOMAIN_PATH.write_text(
            str(args.domain_id) + "\n", encoding="ascii")
    else:
        payload = json.loads(args.json_file.read_text(encoding="utf-8"))
        line = DebugfsTransport.encode_batch(payload)
        if args.dry_run:
            print(line, end="")
        else:
            DebugfsTransport(args.debugfs).submit(payload)


if __name__ == "__main__":
    main()
