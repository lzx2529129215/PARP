"""Stage privileged setup on guest-local storage; stream results as normal user."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent


def main():
    if sys.platform != "linux" or os.getuid() == 0:
        raise RuntimeError("start staging as the ordinary VM user")
    target = ROOT / "results"
    target.mkdir(exist_ok=True)
    seen = {}
    with tempfile.TemporaryDirectory(prefix="parp-ideal-local-") as temporary:
        local = Path(temporary) / "source"
        shutil.copytree(ROOT, local, ignore=shutil.ignore_patterns("results", "control", "revisions", "__pycache__"))
        control = ROOT/"control"
        control.mkdir(exist_ok=True)
        (control/"STOP.json").unlink(missing_ok=True)
        (local/"bridge.json").write_text(json.dumps({"shared_root":str(ROOT)}))
        source = local / "results"

        def sync_results():
            if not source.exists():
                return
            for path in source.rglob("*"):
                if not path.is_file() or path.suffix == ".tmp":
                    continue
                record = (path.stat().st_mtime_ns, path.stat().st_size)
                relative = path.relative_to(source)
                if seen.get(relative) == record:
                    continue
                destination = target / relative
                destination.parent.mkdir(exist_ok=True, parents=True)
                staged = destination.with_suffix(destination.suffix + ".sync-tmp")
                staged.write_bytes(path.read_bytes())
                staged.replace(destination)
                seen[relative] = record

        (target / "STAGING.json").write_text(json.dumps({"guest_local_source": str(local),
                                                        "live_results": str(target)}, indent=2))
        print("GUEST_LOCAL_STAGE_READY " + str(local), flush=True)
        process = subprocess.Popen(["sudo", sys.executable, "-I", "-B", str(local / "prepare_cgroup.py"),
                                    "--uid", str(os.getuid()), "--gid", str(os.getgid())], cwd=local)
        try:
            while process.poll() is None:
                sync_results()
                time.sleep(.25)
        finally:
            sync_results()
        raise SystemExit(process.returncode)


if __name__ == "__main__":
    main()
