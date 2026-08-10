#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def sha256(path: Path) -> str:
  h = hashlib.sha256()
  with path.open("rb") as fp:
    for chunk in iter(lambda: fp.read(1024 * 1024), b""):
      h.update(chunk)
  return h.hexdigest()


def main() -> int:
  if len(sys.argv) != 3:
    print("usage: validate_bundle.py <bundle-root> <manifest.json>", file=sys.stderr)
    return 2

  bundle = Path(sys.argv[1])
  manifest = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
  ok = True
  for item in manifest["files"]:
    rel = item["path"]
    current = bundle / rel
    if not current.exists():
      print(f"missing: {rel}")
      ok = False
      continue
    got = sha256(current)
    if got != item["sha256"]:
      print(f"hash mismatch: {rel}")
      print(f"  expected: {item['sha256']}")
      print(f"  got:      {got}")
      ok = False
  return 0 if ok else 1


if __name__ == "__main__":
  raise SystemExit(main())
