#!/usr/bin/env python3
"""Structural checks for the temporary and final Perf_WPS_0050 PPTX files."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile


SLIDE_RE = re.compile(r"^ppt/slides/slide([1-9][0-9]*)\.xml$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pptx", type=Path)
    parser.add_argument("--slides", type=int, required=True)
    parser.add_argument("--require-image", action="store_true")
    parser.add_argument("--require-video", action="store_true")
    parser.add_argument("--require-rectangle-on-last", action="store_true")
    args = parser.parse_args()

    if not args.pptx.is_file() or args.pptx.stat().st_size == 0:
        raise SystemExit(f"missing_or_empty={args.pptx}")
    try:
        with ZipFile(args.pptx) as archive:
            names = archive.namelist()
            slide_names = sorted(
                (name for name in names if SLIDE_RE.fullmatch(name)),
                key=lambda name: int(SLIDE_RE.fullmatch(name).group(1)),
            )
            media_names = [name for name in names if name.startswith("ppt/media/")]
            image_names = [
                name for name in media_names
                if Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
            ]
            video_names = [
                name for name in media_names
                if Path(name).suffix.lower() in {".mp4", ".mov", ".avi", ".wmv", ".mkv", ".webm"}
            ]
            relationship_payload = b"\n".join(
                archive.read(name).lower()
                for name in names
                if name.startswith("ppt/slides/_rels/") and name.endswith(".rels")
            )
            external_video_relationship = any(
                marker in relationship_payload
                for marker in (b".mp4", b".mov", b".avi", b".wmv", b".webm")
            )
            last_slide = archive.read(slide_names[-1]) if slide_names else b""
    except BadZipFile as error:
        raise SystemExit(f"invalid_pptx={args.pptx}: {error}") from error

    checks = {
        "SLIDE_COUNT_OK": len(slide_names) == args.slides,
        "IMAGE_EMBEDDED": (not args.require_image) or bool(image_names),
        "VIDEO_PRESENT": (
            (not args.require_video)
            or bool(video_names)
            or external_video_relationship
        ),
        "RECTANGLE_ON_LAST_SLIDE": (
            (not args.require_rectangle_on_last)
            or b"<p:sp>" in last_slide
            or b"<p:sp " in last_slide
        ),
    }
    print(f"PPTX={args.pptx}")
    print(f"SLIDES={len(slide_names)}")
    print(f"IMAGES={','.join(image_names)}")
    print(f"VIDEOS={','.join(video_names)}")
    print(f"EXTERNAL_VIDEO_RELATIONSHIP={str(external_video_relationship).lower()}")
    for name, passed in checks.items():
        print(f"{name}={str(passed).lower()}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
