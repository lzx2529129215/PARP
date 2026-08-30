#!/usr/bin/env python3
"""Create deterministic, offline content for PARP real-PC validation. lzx-note"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import wave
import zlib
from pathlib import Path


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def write_large_png(path: Path, width: int = 4096, height: int = 4096) -> None:
    """Create a compact PNG that expands to a 48 MiB decoded RGB image."""
    row = bytearray([0])
    for x in range(width):
        row.extend(((x * 255) // (width - 1), (x * 73) & 255, (x * 151) & 255))
    raw = bytes(row) * height
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(raw, 6))
        + png_chunk(b"IEND", b"")
    )


def write_audio(path: Path, seconds: int = 90, rate: int = 48_000) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        block = bytearray()
        for index in range(rate):
            left = int(7000 * math.sin(2 * math.pi * 440 * index / rate))
            right = int(7000 * math.sin(2 * math.pi * 554 * index / rate))
            block.extend(struct.pack("<hh", left, right))
        for _ in range(seconds):
            stream.writeframesraw(block)


def write_pdf(path: Path, pages: int = 240) -> None:
    """Create a valid multi-page PDF without external packages."""
    objects: list[bytes] = []
    page_ids = [3 + index * 2 for index in range(pages)]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(
        b"<< /Type /Pages /Kids ["
        + b" ".join(f"{item} 0 R".encode("ascii") for item in page_ids)
        + f"] /Count {pages} >>".encode("ascii")
    )
    font_id = 3 + pages * 2
    for index, page_id in enumerate(page_ids, start=1):
        content_id = page_id + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>"
            .encode("ascii")
        )
        text = (
            f"BT /F1 18 Tf 72 730 Td (PARP offline project review - page {index}) Tj "
            "0 -32 Td /F1 11 Tf (Desktop multitasking and memory-pressure validation.) Tj ET"
        ).encode("ascii")
        objects.append(
            f"<< /Length {len(text)} >>\nstream\n".encode("ascii")
            + text + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(payload)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.is_file() and not args.force:
        print(manifest_path)
        return 0

    paragraph = (
        "PARP offline desktop workload: project review, source browsing, email triage, "
        "document editing, media playback, and application switching under memory pressure. "
    )
    html_body = "".join(
        f"<article><h2>Project section {index}</h2><p>{paragraph * 48}</p></article>"
        for index in range(1, 1501)
    )
    (output / "local-page.html").write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>PARP local page</title>"
        "<style>body{max-width:980px;margin:auto;font:16px sans-serif}article{padding:12px}</style>"
        f"</head><body>{html_body}</body></html>\n",
        encoding="utf-8",
    )
    (output / "writer-test.txt").write_text(
        "\n\n".join(
            f"PARP weekly engineering report - section {index}\n{paragraph * 56}"
            for index in range(1, 1801)
        ) + "\n",
        encoding="utf-8",
    )
    (output / "mail-test.eml").write_text(
        "From: project@example.invalid\nTo: developer@example.invalid\n"
        "Subject: PARP local message\nMIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=UTF-8\n\n"
        + "\n".join(f"Thread message {index}: {paragraph * 28}" for index in range(1, 901)),
        encoding="utf-8",
    )
    write_large_png(output / "image-test.png")
    write_audio(output / "audio-test.wav")
    write_pdf(output / "document-test.pdf")

    assets = {}
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != manifest_path.name:
            assets[path.name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    manifest = {
        "schema_version": 1,
        "description": "Deterministic offline real-PC application content. lzx-note",
        "decoded_image_bytes": 4096 * 4096 * 3,
        "pdf_pages": 240,
        "audio_seconds": 90,
        "assets": assets,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
