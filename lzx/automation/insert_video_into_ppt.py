#!/usr/bin/env python3
"""Prepare presentation fixtures used by WPS case 0050.

The current UI scenario starts from exactly five slides and performs all three
temporary-page operations visibly in WPS Presentation.  The older image/video
fixture mode remains available for compatibility with previous runs.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import posixpath
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZIP_DEFLATED, ZipFile

import uno
from com.sun.star.beans import PropertyValue


def prop(name: str, value: object) -> PropertyValue:
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def file_url(path: Path) -> str:
    return uno.systemPathToFileUrl(str(path.resolve()))


def point(x: int, y: int):
    value = uno.createUnoStruct("com.sun.star.awt.Point")
    value.X, value.Y = x, y
    return value


def size(width: int, height: int):
    value = uno.createUnoStruct("com.sun.star.awt.Size")
    value.Width, value.Height = width, height
    return value


def ensure_base_slides(document, pages, count: int) -> None:
    """Make the presentation contain exactly ``count`` starting slides."""
    while pages.getCount() < count:
        pages.insertNewByIndex(pages.getCount())
    while pages.getCount() > count:
        pages.remove(pages.getByIndex(pages.getCount() - 1))


def add_image_slide(document, pages, image_path: Path) -> None:
    """Create a blank slide, insert an image, and explicitly resize it."""
    page = pages.insertNewByIndex(pages.getCount())
    image = document.createInstance("com.sun.star.drawing.GraphicObjectShape")
    image.GraphicURL = file_url(image_path)
    image.setPosition(point(2600, 1500))
    # WPS opens this visible, small inserted state.  The scenario then drags
    # the on-screen resize handle so the user can watch the enlargement.
    image.setSize(size(10000, 5625))
    page.add(image)


def add_video_slide(document, pages, video_path: Path) -> None:
    page = pages.insertNewByIndex(pages.getCount())
    video = document.createInstance("com.sun.star.presentation.MediaShape")
    video.MediaURL = file_url(video_path)
    video.setPosition(point(1800, 1200))
    video.setSize(size(22000, 12400))
    page.add(video)

    rectangle = document.createInstance("com.sun.star.drawing.RectangleShape")
    rectangle.setPosition(point(20500, 11800))
    # Insert then resize the rectangle; keep it below the video playback area.
    rectangle.setSize(size(1300, 550))
    rectangle.setSize(size(2600, 1100))
    rectangle.FillColor = 0xFFCC00
    rectangle.LineColor = 0xC08000
    page.add(rectangle)


def embed_video_in_pptx(pptx_path: Path, video_path: Path) -> None:
    """Convert LibreOffice's external media relationships to embedded PPTX media.

    WPS on Linux can render an external video preview but may not advance the
    player.  Embedding the exact same MP4 keeps the presentation portable and
    lets WPS resolve the media from inside the PPTX package.
    """
    rel_namespace = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    type_namespace = "{http://schemas.openxmlformats.org/package/2006/content-types}"
    media_name = "ppt/media/media1.mp4"
    with ZipFile(pptx_path, "r") as source:
        files = {item.filename: source.read(item.filename) for item in source.infolist()}

    media_relationships = 0
    for name, payload in tuple(files.items()):
        if not name.startswith("ppt/slides/_rels/") or not name.endswith(".rels"):
            continue
        root = ET.fromstring(payload)
        changed = False
        for relation in root.findall(f"{rel_namespace}Relationship"):
            relation_type = relation.get("Type", "")
            if relation_type.endswith("/video") or relation_type.endswith("/media"):
                relation.set("Target", "../media/media1.mp4")
                relation.attrib.pop("TargetMode", None)
                changed = True
                media_relationships += 1
        if changed:
            files[name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    if media_relationships == 0:
        raise RuntimeError("No PPTX video relationship was created")
    files[media_name] = video_path.read_bytes()

    content_types = ET.fromstring(files["[Content_Types].xml"])
    if not any(item.get("Extension") == "mp4" for item in content_types.findall(f"{type_namespace}Default")):
        ET.SubElement(content_types, f"{type_namespace}Default", {
            "Extension": "mp4", "ContentType": "video/mp4",
        })
        files["[Content_Types].xml"] = ET.tostring(content_types, encoding="utf-8", xml_declaration=True)

    for name, payload in tuple(files.items()):
        if name.startswith("ppt/slides/slide") and name.endswith(".xml") and b"p14:media r:link=" in payload:
            files[name] = payload.replace(b"p14:media r:link=", b"p14:media r:embed=")

    with NamedTemporaryFile(dir=pptx_path.parent, suffix=".pptx", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with ZipFile(temporary_path, "w", ZIP_DEFLATED) as target:
            for name, payload in files.items():
                target.writestr(name, payload)
        temporary_path.replace(pptx_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def connect_desktop(port: int):
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local
    )
    target = f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            context = resolver.resolve(target)
            return context.ServiceManager.createInstanceWithContext(
                "com.sun.star.frame.Desktop", context
            )
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("LibreOffice headless service did not become ready")


def prepare_base_presentation(
    source_path: Path,
    output_path: Path,
    slide_count: int,
    port: int,
) -> None:
    """Create a clean PPTX fixture containing exactly ``slide_count`` slides."""
    if slide_count < 1:
        raise ValueError("--slide-count must be at least 1")
    if not source_path.is_file() or source_path.stat().st_size == 0:
        raise FileNotFoundError(f"PPT file is missing or empty: {source_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    listener = subprocess.Popen(
        [
            "libreoffice", "--headless", "--nologo", "--nodefault",
            "--nofirststartwizard",
            f"--accept=socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    document = None
    try:
        desktop = connect_desktop(port)
        document = desktop.loadComponentFromURL(
            file_url(source_path), "_blank", 0, (prop("Hidden", True),)
        )
        if document is None:
            raise RuntimeError(f"Unable to open presentation: {source_path}")
        pages = document.getDrawPages()
        ensure_base_slides(document, pages, slide_count)
        if pages.getCount() != slide_count:
            raise RuntimeError(
                f"Expected {slide_count} base slides, got {pages.getCount()}"
            )
        document.storeAsURL(
            file_url(output_path),
            (prop("FilterName", "Impress MS PowerPoint 2007 XML"), prop("Overwrite", True)),
        )
        document.close(True)
        document = None
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("Base PPTX output was not created")
    finally:
        if document is not None:
            try:
                document.close(True)
            except Exception:
                pass
        listener.terminate()
        try:
            listener.wait(timeout=5)
        except subprocess.TimeoutExpired:
            listener.kill()


def trim_presentation(presentation_path: Path, keep_slide_count: int, port: int) -> None:
    """Delete temporary trailing slides directly in the PPTX package.

    WPS may keep a just-closed presentation locked long enough for LibreOffice
    to reject it.  The PPTX package operation is independent of that lock and
    preserves the first ``keep_slide_count`` slides exactly.
    """
    if keep_slide_count < 1:
        raise ValueError("--keep-slide-count must be at least 1")

    presentation_namespace = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    office_relationship_namespace = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package_relationship_namespace = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    content_type_namespace = "{http://schemas.openxmlformats.org/package/2006/content-types}"

    with ZipFile(presentation_path, "r") as source:
        files = {item.filename: source.read(item.filename) for item in source.infolist()}

    presentation = ET.fromstring(files["ppt/presentation.xml"])
    slide_id_list = presentation.find(f"{presentation_namespace}sldIdLst")
    if slide_id_list is None:
        raise RuntimeError("PPTX has no slide list")
    slide_ids = list(slide_id_list)
    if len(slide_ids) < keep_slide_count:
        raise RuntimeError(
            f"Cannot keep {keep_slide_count} slides; presentation only has {len(slide_ids)}"
        )

    remove_relationship_ids = {
        slide.get(f"{office_relationship_namespace}id")
        for slide in slide_ids[keep_slide_count:]
    }
    for slide in slide_ids[keep_slide_count:]:
        slide_id_list.remove(slide)
    files["ppt/presentation.xml"] = ET.tostring(
        presentation, encoding="utf-8", xml_declaration=True
    )

    relationships_name = "ppt/_rels/presentation.xml.rels"
    relationships = ET.fromstring(files[relationships_name])
    removed_parts: set[str] = set()
    for relationship in list(relationships):
        if relationship.get("Id") not in remove_relationship_ids:
            continue
        target = relationship.get("Target", "")
        removed_parts.add(posixpath.normpath(posixpath.join("ppt", target)))
        relationships.remove(relationship)
    files[relationships_name] = ET.tostring(
        relationships, encoding="utf-8", xml_declaration=True
    )

    for slide_name in removed_parts:
        files.pop(slide_name, None)
        files.pop(
            f"ppt/slides/_rels/{posixpath.basename(slide_name)}.rels",
            None,
        )

    content_types = ET.fromstring(files["[Content_Types].xml"])
    for override in list(content_types.findall(f"{content_type_namespace}Override")):
        if override.get("PartName", "").lstrip("/") in removed_parts:
            content_types.remove(override)
    files["[Content_Types].xml"] = ET.tostring(
        content_types, encoding="utf-8", xml_declaration=True
    )

    with NamedTemporaryFile(dir=presentation_path.parent, suffix=".pptx", delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with ZipFile(temporary_path, "w", ZIP_DEFLATED) as target:
            for name, payload in files.items():
                target.writestr(name, payload)
        temporary_path.replace(presentation_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--base-slide-count", default=5, type=int)
    parser.add_argument("--prepare-base", type=Path)
    parser.add_argument("--slide-count", default=5, type=int)
    parser.add_argument("--trim-presentation", type=Path)
    parser.add_argument("--keep-slide-count", default=5, type=int)
    parser.add_argument("--port", default=2002, type=int)
    args = parser.parse_args()

    if args.prepare_base:
        if args.output is None:
            raise ValueError("--output is required with --prepare-base")
        prepare_base_presentation(
            args.prepare_base,
            args.output,
            args.slide_count,
            args.port,
        )
        print(args.output)
        return 0

    if args.trim_presentation:
        if not args.trim_presentation.is_file() or args.trim_presentation.stat().st_size == 0:
            raise FileNotFoundError(f"PPT file is missing or empty: {args.trim_presentation}")
        trim_presentation(args.trim_presentation, args.keep_slide_count, args.port)
        print(args.trim_presentation)
        return 0

    if args.base_slide_count < 1:
        raise ValueError("--base-slide-count must be at least 1")
    if not all((args.input, args.image, args.video, args.output)):
        raise ValueError("--input, --image, --video, and --output are required when creating a PPTX")

    for path, label in ((args.input, "PPT"), (args.image, "image"), (args.video, "video")):
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"{label} file is missing or empty: {path}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        args.output.unlink()

    listener = subprocess.Popen(
        [
            "libreoffice", "--headless", "--nologo", "--nodefault", "--nofirststartwizard",
            f"--accept=socket,host=127.0.0.1,port={args.port};urp;StarOffice.ComponentContext",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    document = None
    try:
        desktop = connect_desktop(args.port)
        document = desktop.loadComponentFromURL(
            file_url(args.input), "_blank", 0, (prop("Hidden", True),)
        )
        if document is None:
            raise RuntimeError(f"Unable to open presentation: {args.input}")
        pages = document.getDrawPages()
        ensure_base_slides(document, pages, args.base_slide_count)
        add_image_slide(document, pages, args.image)
        add_video_slide(document, pages, args.video)
        expected_pages = args.base_slide_count + 2
        if pages.getCount() != expected_pages:
            raise RuntimeError(
                f"Expected {expected_pages} slides after inserting image and video, "
                f"got {pages.getCount()}"
            )
        document.storeAsURL(
            file_url(args.output),
            (prop("FilterName", "Impress MS PowerPoint 2007 XML"), prop("Overwrite", True)),
        )
        document.close(True)
        document = None
        embed_video_in_pptx(args.output, args.video)
        if not args.output.is_file() or args.output.stat().st_size == 0:
            raise RuntimeError("PPTX output was not created")
        print(args.output)
        return 0
    finally:
        if document is not None:
            try:
                document.close(True)
            except Exception:
                pass
        listener.terminate()
        try:
            listener.wait(timeout=5)
        except subprocess.TimeoutExpired:
            listener.kill()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Failed to create video presentation: {error}", file=sys.stderr)
        raise SystemExit(1)
