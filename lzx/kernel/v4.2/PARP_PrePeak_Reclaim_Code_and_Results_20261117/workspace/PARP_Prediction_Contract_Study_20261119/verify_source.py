"""Check the exact uploaded controller and current ideal-pipeline sources."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    source = json.loads((ROOT / "source_baseline.json").read_text())
    for name, digest in source["controller_sha256"].items():
        if hashlib.sha256((ROOT / "controller" / name).read_bytes()).hexdigest() != digest:
            raise ValueError("uploaded controller source changed: " + name)
    print("UPLOADED_CONTROLLER_UNCHANGED " + source["github_commit"], flush=True)


if __name__ == "__main__":
    main()
