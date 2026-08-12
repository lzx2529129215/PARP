#!/usr/bin/env python3
"""Validate JSON and emit deterministic binary model payloads."""

import argparse
import json
import struct


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as source:
        model = json.load(source)
    values = model["q15_values"]
    if any(not 0 <= value <= 32767 for value in values):
        raise SystemExit("q15 value out of range")
    payload = struct.pack("<II", 1, len(values))
    payload += struct.pack("<" + "H" * len(values), *values)
    with open(args.output, "wb") as target:
        target.write(payload)


if __name__ == "__main__":
    main()
