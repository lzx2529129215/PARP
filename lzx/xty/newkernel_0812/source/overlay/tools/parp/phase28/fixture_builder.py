#!/usr/bin/env python3
"""Build isolated small/medium/large WPS fixtures without user documents."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import zipfile


def atomic_copy(source, destination):
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


def deterministic_bytes(size):
    seed = hashlib.sha256(b"PARP Phase2.8 controlled large-document fixture").digest()
    remaining = size
    while remaining:
        block = seed[:min(remaining, len(seed))]
        yield block
        remaining -= len(block)
        seed = hashlib.sha256(seed).digest()


def build(samples, output):
    output.mkdir(parents=True, exist_ok=True)
    small = samples / "word_0040_fixture.docx"
    if not small.is_file():
        raise SystemExit("controlled source fixture missing")
    atomic_copy(small, output / "wps_small.docx")
    for name, padding_size in (("wps_medium.docx", 1536 * 1024),
                               ("wps_large.docx", 16 * 1024 * 1024)):
        temporary = output / (name + ".tmp")
        shutil.copyfile(small, temporary)
        with zipfile.ZipFile(temporary, "a", compression=zipfile.ZIP_STORED) as archive:
            with archive.open("parp-fixture/phase28-padding.bin", "w") as member:
                for block in deterministic_bytes(padding_size):
                    member.write(block)
        os.replace(temporary, output / name)
    for name in ("wps_small.docx", "wps_medium.docx", "wps_large.docx"):
        path = output / name
        with zipfile.ZipFile(path) as archive:
            if "[Content_Types].xml" not in archive.namelist():
                raise AssertionError("invalid docx: %s" % path)
    return {name: (output / name).stat().st_size
            for name in ("wps_small.docx", "wps_medium.docx", "wps_large.docx")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.samples, args.output))


if __name__ == "__main__":
    main()
