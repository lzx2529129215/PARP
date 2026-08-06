#!/usr/bin/env python3
"""重放既有 Runtime Monitor session 的双模式 Markov，不写 debugfs。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from runtime_monitor.core.dual_workload_markov import replay_dual_markov


def main() -> int:
    parser = argparse.ArgumentParser(description="重放双模式 workload Markov")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--reentry-window-s", type=float, default=5.0)
    parser.add_argument("--ignore-initial-low-activity-s", type=float, default=2.0)
    args = parser.parse_args()
    result = replay_dual_markov(
        session_dir=Path(args.session_dir),
        output_dir=Path(args.output_dir),
        reentry_window_s=args.reentry_window_s,
        ignore_initial_low_activity_s=args.ignore_initial_low_activity_s,
    )
    print(result)
    return 0 if result.get("final_result") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
