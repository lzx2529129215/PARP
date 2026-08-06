#!/usr/bin/env python3
"""Create a multi-page PDF fixture for WPS case 0070."""

from __future__ import annotations

import argparse
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def create_fixture(output: Path, pages: int) -> None:
    if pages < 5:
        raise ValueError("The 0070 fixture must contain at least five pages")
    output.parent.mkdir(parents=True, exist_ok=True)

    width, height = A4
    document = canvas.Canvas(str(output), pagesize=A4)
    document.setTitle("WPS PDF Automation Fixture")
    document.setAuthor("Codex")

    palette = (
        colors.HexColor("#155EEF"),
        colors.HexColor("#079455"),
        colors.HexColor("#DC6803"),
        colors.HexColor("#7A5AF8"),
    )
    for page_number in range(1, pages + 1):
        accent = palette[(page_number - 1) % len(palette)]
        document.setFillColor(colors.HexColor("#F8FAFC"))
        document.rect(0, 0, width, height, fill=1, stroke=0)

        document.setFillColor(accent)
        document.rect(0, height - 86, width, 86, fill=1, stroke=0)

        document.setFillColor(colors.white)
        document.setFont("Helvetica-Bold", 24)
        document.drawString(42, height - 54, "WPS PDF Automation - Perf_WPS_0070")

        document.setFillColor(colors.HexColor("#101828"))
        document.setFont("Helvetica-Bold", 74)
        document.drawCentredString(width / 2, height / 2 + 28, f"PAGE {page_number}")

        document.setFillColor(colors.HexColor("#475467"))
        document.setFont("Helvetica", 18)
        document.drawCentredString(
            width / 2,
            height / 2 - 18,
            f"Navigation verification page {page_number} of {pages}",
        )

        document.setStrokeColor(colors.HexColor("#D0D5DD"))
        document.line(42, 92, width - 42, 92)
        document.setFillColor(colors.HexColor("#667085"))
        document.setFont("Helvetica", 11)
        document.drawString(42, 66, "Fixture purpose: scroll, first/last page, and window-state verification")
        document.drawRightString(width - 42, 66, f"{page_number} / {pages}")
        document.showPage()

    document.save()
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("PDF fixture was not created")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--pages", default=12, type=int)
    args = parser.parse_args()
    create_fixture(args.output, args.pages)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
