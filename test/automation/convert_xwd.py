#!/usr/bin/env python3
"""Convert the 24/32-bit TrueColor XWD screenshots used by the VM runner to PNG."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

import numpy as np
from PIL import Image


def _channel(pixels: np.ndarray, mask: int) -> np.ndarray:
    if not mask:
        return np.zeros(pixels.shape, dtype=np.uint8)
    shift = (mask & -mask).bit_length() - 1
    maximum = mask >> shift
    values = (pixels & mask) >> shift
    return ((values * 255 + maximum // 2) // maximum).astype(np.uint8)


def convert(source: Path, destination: Path) -> None:
    data = source.read_bytes()
    if len(data) < 100:
        raise ValueError(f"XWD header is truncated: {source}")

    header = struct.unpack(">25I", data[:100])
    (
        header_size,
        file_version,
        pixmap_format,
        _pixmap_depth,
        width,
        height,
        _xoffset,
        byte_order,
        _bitmap_unit,
        _bitmap_bit_order,
        _bitmap_pad,
        bits_per_pixel,
        bytes_per_line,
        _visual_class,
        red_mask,
        green_mask,
        blue_mask,
        _bits_per_rgb,
        _colormap_entries,
        ncolors,
        *_window_fields,
    ) = header

    if file_version != 7 or pixmap_format != 2:
        raise ValueError(f"Unsupported XWD format/version: {pixmap_format}/{file_version}")
    # xwd may report a 24-bit visual while storing each pixel in a 32-bit word.
    if bits_per_pixel not in (24, 32) or bytes_per_line % 4 or bytes_per_line < width * 4:
        raise ValueError(
            f"Only 24/32-bit TrueColor XWD stored in 32-bit words is supported, "
            f"got {bits_per_pixel}-bit with {bytes_per_line} bytes per row"
        )

    pixel_offset = header_size + ncolors * 12
    expected = bytes_per_line * height
    payload = memoryview(data)[pixel_offset : pixel_offset + expected]
    if len(payload) != expected:
        raise ValueError(f"XWD pixel payload is truncated: {source}")

    dtype = np.dtype("<u4" if byte_order == 0 else ">u4")
    row_words = bytes_per_line // 4
    pixels = np.frombuffer(payload, dtype=dtype).reshape(height, row_words)[:, :width]
    rgb = np.dstack(
        (_channel(pixels, red_mask), _channel(pixels, green_mask), _channel(pixels, blue_mask))
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb, "RGB").save(destination)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    for source in args.sources:
        destination = (args.output_dir or source.parent) / f"{source.stem}.png"
        convert(source, destination)
        print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
