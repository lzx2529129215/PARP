#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Small atomic state-file helper for Phase 2.6 shell entry points."""

import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone


def lookup(data, dotted):
    value = data
    for component in dotted.split("."):
        value = value[component]
    return value


def assign(data, dotted, value):
    components = dotted.split(".")
    target = data
    for component in components[:-1]:
        target = target.setdefault(component, {})
    target[components[-1]] = value
    data.setdefault("timestamps", {})["updated"] = datetime.now(
        timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write(path, data):
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", dir=str(path.parent), text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv):
    if len(argv) not in (4, 5):
        raise SystemExit("usage: state_tool.py get FILE FIELD | set FILE FIELD JSON")
    command, filename, field = argv[1:4]
    path = Path(filename)
    data = json.loads(path.read_text(encoding="utf-8"))
    if command == "get" and len(argv) == 4:
        value = lookup(data, field)
        if isinstance(value, (dict, list, bool)) or value is None:
            print(json.dumps(value, sort_keys=True))
        else:
            print(value)
        return
    if command == "set" and len(argv) == 5:
        assign(data, field, json.loads(argv[4]))
        atomic_write(path, data)
        return
    raise SystemExit("invalid state command")


if __name__ == "__main__":
    main(sys.argv)
