#!/usr/bin/env python3
"""Verify page counts, visible resizing, video motion, and trace for case 0050."""

from __future__ import annotations

import argparse
import csv
import itertools
import zipfile
from pathlib import Path


def slide_count(path: Path) -> int:
    with zipfile.ZipFile(path) as archive:
        return sum(
            1
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )


def checkpoint_objects(path: Path) -> tuple[bool, bool]:
    """Return whether slide 8 contains native video and a separate rectangle."""
    with zipfile.ZipFile(path) as archive:
        slide = archive.read("ppt/slides/slide8.xml")
    has_video = b"<a:videoFile " in slide and b"<p:video " in slide
    has_rectangle = b"<p:sp>" in slide and b'prst="rect"' in slide
    return has_video, has_rectangle


def change_ratio(first: Path, second: Path) -> float:
    """Return the fraction of differing bytes between two same-size XWD shots."""
    left = first.read_bytes()
    right = second.read_bytes()
    length = max(len(left), len(right), 1)
    changed = sum(
        a != b
        for a, b in itertools.zip_longest(left, right, fillvalue=-1)
    )
    return changed / length


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--final", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--image-small", required=True, type=Path)
    parser.add_argument("--image-large", required=True, type=Path)
    parser.add_argument("--video-first", required=True, type=Path)
    parser.add_argument("--video-later", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    required_files = (
        args.base,
        args.checkpoint,
        args.final,
        args.trace,
        args.image_small,
        args.image_large,
        args.video_first,
        args.video_later,
    )
    for path in required_files:
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"Required verification artifact is missing: {path}")

    base_slides = slide_count(args.base)
    checkpoint_slides = slide_count(args.checkpoint)
    final_slides = slide_count(args.final)
    checkpoint_has_video, checkpoint_has_rectangle = checkpoint_objects(args.checkpoint)
    image_change = change_ratio(args.image_small, args.image_large)
    video_change = change_ratio(args.video_first, args.video_later)

    with args.trace.open(encoding="utf-8", newline="") as source:
        completed = [row for row in csv.DictReader(source) if row["phase"] == "end"]

    failures = [row for row in completed if row["status"] != "success"]
    labels = {row["label"] for row in completed}
    checks = {
        "BASE_SLIDE_COUNT_IS_FIVE": base_slides == 5,
        "CHECKPOINT_SLIDE_COUNT_IS_EIGHT": checkpoint_slides == 8,
        "FINAL_SLIDE_COUNT_IS_FIVE": final_slides == 5,
        "CHECKPOINT_HAS_NATIVE_VIDEO": checkpoint_has_video,
        "CHECKPOINT_HAS_RECTANGLE": checkpoint_has_rectangle,
        "NO_FAILED_ACTIONS": not failures,
        "ADDED_THREE_NEW_SLIDES": all(
            label in labels
            for label in (
                "WPS_PPT_S7_DUPLICATE_LAST_SLIDE",
                "WPS_PPT_S8_NEW_IMAGE_SLIDE",
                "WPS_PPT_S9_NEW_VIDEO_SLIDE",
            )
        ),
        "DELETED_THREE_NEW_SLIDES": all(
            label in labels
            for label in (
                "WPS_PPT_DELETE_NEW_SLIDE_1",
                "WPS_PPT_DELETE_NEW_SLIDE_2",
                "WPS_PPT_DELETE_NEW_SLIDE_3",
            )
        ),
        "IMAGE_VISIBLE_SIZE_CHANGED": image_change >= 0.02,
        "VIDEO_VISIBLE_FRAMES_CHANGED": video_change >= 0.02,
        "NATIVE_WPS_VIDEO_PLAY_CLICKED": "WPS_PPT_NATIVE_VIDEO_PLAY" in labels,
        "WPS_CLOSED": "WPS_CLOSE" in labels,
    }
    lines = [
        f"BASE_SLIDE_COUNT={base_slides}",
        f"CHECKPOINT_SLIDE_COUNT={checkpoint_slides}",
        f"FINAL_SLIDE_COUNT={final_slides}",
        f"IMAGE_CHANGE_RATIO={image_change:.6f}",
        f"VIDEO_CHANGE_RATIO={video_change:.6f}",
        f"COMPLETED_ACTIONS={len(completed)}",
    ]
    for name, passed in checks.items():
        lines.append(f"{name}={str(passed).lower()}")
    print("\n".join(lines))
    if args.report:
        args.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
