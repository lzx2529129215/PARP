from __future__ import annotations

import argparse
import os

from .protocol import ProtocolError, wait_for_markers, write_marker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write an exact monotonic launch marker, then exec an application")
    parser.add_argument("--marker", required=True)
    parser.add_argument(
        "--ready-file",
        action="append",
        default=[],
        help="wait for this collector-ready marker before launching; may be repeated",
    )
    parser.add_argument("--ready-timeout", type=float, default=60.0)
    parser.add_argument("--stop-marker", help="run as a wrapper and mark this file when the child exits")
    parser.add_argument(
        "--done-file",
        action="append",
        default=[],
        help="in wrapper mode, keep the cgroup alive until these collector-done markers exist",
    )
    parser.add_argument("--done-timeout", type=float, default=60.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    try:
        wait_for_markers(args.ready_file, args.ready_timeout)
    except ProtocolError as error:
        print(str(error))
        return 6

    write_marker(args.marker, "workload_start", command=command)
    if not args.stop_marker:
        os.execvp(command[0], command)
        return 127

    import subprocess

    process = subprocess.Popen(command, start_new_session=False)
    return_code = process.wait()
    write_marker(args.stop_marker, "workload_stop", command=command, child_pid=process.pid, return_code=return_code)
    try:
        wait_for_markers(args.done_file, args.done_timeout)
    except ProtocolError as error:
        print(str(error))
        return 6
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
