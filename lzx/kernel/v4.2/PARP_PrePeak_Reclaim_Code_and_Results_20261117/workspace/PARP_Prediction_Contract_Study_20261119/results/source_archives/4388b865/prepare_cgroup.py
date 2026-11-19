"""One-time delegation of ONLY a newly created isolated experiment subtree."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0 or args.uid <= 0 or args.gid < 0:
        raise ValueError("local sudo setup for an ordinary user required")
    mount = Path("/sys/fs/cgroup")
    if "memory" not in (mount / "cgroup.subtree_control").read_text().split():
        raise RuntimeError("memory controller is not already enabled at the root; no global setting changed")
    base = mount / ("parp-ideal-" + uuid.uuid4().hex[:12])
    base.mkdir()
    (base / "cgroup.subtree_control").write_text("+memory")
    (base / "memory.max").write_text(str(2 * 1024**3))
    (base / "memory.swap.max").write_text("0")
    launcher = base / "launcher"
    launcher.mkdir()
    for directory in (base, launcher):
        os.chown(directory, args.uid, args.gid)
        for path in directory.iterdir():
            if path.is_file():
                os.chown(path, args.uid, args.gid)
    result = ROOT / "results"
    result.mkdir(exist_ok=True)
    os.chown(result, args.uid, args.gid)
    destination = result / "delegation.json"
    destination.write_text(json.dumps({"base": str(base), "uid": args.uid, "gid": args.gid,
                                       "root_memory_max_bytes": 2 * 1024**3,
                                       "swap_max_bytes": 0, "global_controls_changed": False}, indent=2) + "\n")
    os.chown(destination, args.uid, args.gid)
    print("IDEAL_TEST_DELEGATION_READY " + str(base), flush=True)
    # Start ONLY the ordinary-user experiment inside this delegation. Moving a
    # worker from user.slice across the root boundary would otherwise fail the
    # cgroup-v2 common-ancestor permission check. No root command channel exists.
    pid = os.fork()
    if pid == 0:
        try:
            (launcher / "cgroup.procs").write_text(str(os.getpid()))
            os.setgroups([])
            os.setgid(args.gid)
            os.setuid(args.uid)
            os.chdir(ROOT)
            os.execv(sys.executable, [sys.executable, "-B", "-u", str(ROOT / "vm_service.py")])
        except BaseException:
            import traceback
            traceback.print_exc()
            os._exit(1)
    try:
        _, status = os.waitpid(pid, 0)
    finally:
        # The unique task-owned subtree is the only privileged cleanup target.
        if base.parent.resolve() != mount or not base.name.startswith("parp-ideal-"):
            raise RuntimeError("unexpected cleanup target")
        if (base / "cgroup.events").read_text().splitlines()[0] == "populated 1":
            (base / "cgroup.kill").write_text("1")
            time.sleep(.2)
        for directory in sorted((p for p in base.rglob("*") if p.is_dir()),
                                key=lambda p: len(p.parts), reverse=True):
            directory.rmdir()
        base.rmdir()
        print("IDEAL_TEST_ISOLATED_SUBTREE_REMOVED", flush=True)
    raise SystemExit(os.waitstatus_to_exitcode(status))


if __name__ == "__main__":
    main()
