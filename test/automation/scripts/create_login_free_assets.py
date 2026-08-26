#!/usr/bin/env python3
"""Create deterministic local media used by the login-free app scenario."""

# lzx-note: Keep expanded desktop automation independent of network and accounts.
from __future__ import annotations

import argparse
import datetime as dt
import math
import struct
import wave
import zlib
from pathlib import Path


def write_png(path: Path, width: int = 640, height: int = 480) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            rows.extend(((x * 255) // (width - 1), (y * 255) // (height - 1), 128))
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(signature + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def write_wav(path: Path, seconds: int = 12, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        frames = (int(5000 * math.sin(2 * math.pi * 440 * index / rate)) for index in range(rate * seconds))
        stream.writeframes(b"".join(struct.pack("<h", frame) for frame in frames))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    write_png(args.output / "automation-image.png")
    audio = args.output / "automation-audio.wav"
    write_wav(audio)
    now = int(dt.datetime.now().timestamp())
    # lzx-note: --no-registration cannot accept command-line media, so seed its isolated library.
    (args.output / "rhythmdb.xml").write_text(
        "<?xml version='1.0' standalone='yes'?>\n<rhythmdb version='2.0'>\n"
        "  <entry type='song'>\n"
        "    <title>PARP Automation Tone</title><genre>Automation</genre>"
        "<artist>PARP</artist><album>Login Free</album>\n"
        f"    <duration>12</duration><file-size>{audio.stat().st_size}</file-size>"
        f"<location>{audio.resolve().as_uri()}</location>\n"
        f"    <mtime>{now}</mtime><first-seen>{now}</first-seen><last-seen>{now}</last-seen>\n"
        "  </entry>\n</rhythmdb>\n",
        encoding="utf-8",
    )
    (args.output / "shotwell").mkdir(exist_ok=True)
    print(f"login-free assets ready: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
