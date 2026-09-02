#!/usr/bin/env python3
"""Create deterministic, offline content for PARP real-PC validation. lzx-note"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import wave
import zipfile
import zlib
from pathlib import Path


def png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload)) + kind + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def write_large_png(
    path: Path, width: int = 4096, height: int = 4096, variant: int = 0,
) -> None:
    """Create a compact PNG that expands to a 48 MiB decoded RGB image."""
    row = bytearray([0])
    for x in range(width):
        row.extend((
            ((x * 255) // (width - 1) + variant * 29) & 255,
            (x * (73 + variant * 2) + variant * 17) & 255,
            (x * (151 - variant * 3) + variant * 41) & 255,
        ))
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


def write_odt(path: Path, paragraph: str, paragraphs: int = 3200) -> None:
    """Create a deterministic editable ODT without depending on LibreOffice."""
    content = "".join(
        f"<text:p text:style-name='Standard'>PARP section {index}: "
        f"{paragraph * 18}</text:p>"
        for index in range(1, paragraphs + 1)
    )
    content_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<office:document-content "
        "xmlns:office='urn:oasis:names:tc:opendocument:xmlns:office:1.0' "
        "xmlns:text='urn:oasis:names:tc:opendocument:xmlns:text:1.0' "
        "office:version='1.3'><office:body><office:text>"
        f"{content}</office:text></office:body></office:document-content>"
    )
    styles_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<office:document-styles "
        "xmlns:office='urn:oasis:names:tc:opendocument:xmlns:office:1.0' "
        "office:version='1.3'><office:styles/></office:document-styles>"
    )
    manifest_xml = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<manifest:manifest "
        "xmlns:manifest='urn:oasis:names:tc:opendocument:xmlns:manifest:1.0' "
        "manifest:version='1.3'>"
        "<manifest:file-entry manifest:full-path='/' "
        "manifest:media-type='application/vnd.oasis.opendocument.text'/>"
        "<manifest:file-entry manifest:full-path='content.xml' manifest:media-type='text/xml'/>"
        "<manifest:file-entry manifest:full-path='styles.xml' manifest:media-type='text/xml'/>"
        "</manifest:manifest>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "mimetype", "application/vnd.oasis.opendocument.text",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("content.xml", content_xml, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr("styles.xml", styles_xml, compress_type=zipfile.ZIP_DEFLATED)
        archive.writestr(
            "META-INF/manifest.xml", manifest_xml,
            compress_type=zipfile.ZIP_DEFLATED,
        )


def write_serial_reuse_html(path: Path) -> None:
    """Create a real browser workload whose main thread serially reuses pages."""
    path.write_text(
        """<!doctype html>
<html><head><meta charset="utf-8"><title>PARP R6 INITIALIZING</title>
<style>body{font:18px sans-serif;max-width:1000px;margin:4rem auto}pre{white-space:pre-wrap}
#next{display:block;width:80%;height:45vh;margin:2rem auto;font-size:2rem}</style>
</head><body><h1>PARP serial hot-page reuse</h1>
<p>Click the button to synchronously touch the next deterministic page chunk.</p>
<button id="next" type="button">Reuse next 16 MiB chunk</button><pre id="state"></pre>
<script>
(() => {
  const query = new URLSearchParams(location.search);
  const allocationMiB = Math.max(64, Math.min(768, Number(query.get('mib') || 384)));
  const chunkMiB = Math.max(4, Math.min(64, Number(query.get('chunk') || 16)));
  const seed = Number(query.get('seed') || 1) >>> 0;
  const chunkBytes = chunkMiB * 1024 * 1024;
  const pageBytes = 4096;
  const pagesPerChunk = Math.floor(chunkBytes / pageBytes);
  const chunkCount = Math.floor(allocationMiB / chunkMiB);
  const buffers = [];
  let checksum = 0;
  let step = 0;
  let busy = false;
  const state = document.getElementById('state');
  const next = document.getElementById('next');
  const pageIndex = (index, chunk, stride) =>
    ((index * stride) + ((seed + chunk * 131) & (pagesPerChunk - 1))) % pagesPerChunk;
  for (let chunk = 0; chunk < chunkCount; chunk++) {
    const buffer = new Uint8Array(chunkBytes);
    for (let page = 0; page < pagesPerChunk; page++) {
      const index = pageIndex(page, chunk, 2053);
      buffer[index * pageBytes] = (page + chunk + seed) & 255;
    }
    buffers.push(buffer);
    document.title = `PARP R6 INIT ${chunk + 1}/${chunkCount}`;
  }
  window.parpSerialBuffers = buffers;
  state.textContent = `resident=${allocationMiB} MiB chunks=${chunkCount} chunk=${chunkMiB} MiB`;
  document.title = `PARP R6 READY 0/${chunkCount}`;

  function reuseNextChunk() {
    if (busy || step >= chunkCount) return;
    busy = true;
    const chunk = step;
    next.disabled = true;
    const buffer = buffers[chunk];
    for (let page = 0; page < pagesPerChunk; page++) {
      const index = pageIndex(page, chunk, 2039) * pageBytes;
      checksum = (checksum + buffer[index]) >>> 0;
      buffer[index] = (buffer[index] + 1) & 255;
    }
    step++;
    // The title is the serial-fault latency endpoint. Publish it as soon as
    // the browser main thread has synchronously touched every page in this
    // chunk. A requestAnimationFrame endpoint can be indefinitely throttled
    // while the reclaimed browser is being restored, which would mix frame
    // scheduling with the page-in latency under test. lzx-note
    state.textContent = `completed=${step}/${chunkCount} checksum=${checksum}`;
    document.title = `PARP R6 STEP ${String(step).padStart(2, '0')}/${chunkCount}`;
    next.disabled = step >= chunkCount;
    busy = false;
  }
  addEventListener('keydown', event => {
    if (event.key.toLowerCase() === 'n') {
      event.preventDefault();
      reuseNextChunk();
    }
  });
  next.addEventListener('click', reuseNextChunk);
})();
</script></body></html>
""",
        encoding="utf-8",
    )  # lzx-note


def write_oom_pressure_html(path: Path) -> None:
    """Create the R8 browser-only pressure source.

    A click commits exactly one 64 MiB ArrayBuffer, touches every 4 KiB page,
    and retains the buffer globally.  The title is the automation contract: a
    failed JavaScript allocation can never be confused with a completed burst.
    """
    path.write_text(
        """<!doctype html>
<html><head><meta charset="utf-8"><title>PARP R8 INITIALIZING</title>
<style>body{font:18px sans-serif;max-width:1000px;margin:4rem auto}
#allocate{display:block;width:80%;height:45vh;margin:2rem auto;font-size:2rem}
pre{white-space:pre-wrap}</style></head><body>
<h1>PARP R8 application-owned OOM pressure</h1>
<p>Each click commits and retains one 64 MiB browser ArrayBuffer.</p>
<button id="allocate" type="button">Allocate next 64 MiB chunk</button>
<pre id="state"></pre>
<script>
(() => {
  const query = new URLSearchParams(location.search);
  const chunkMiB = 64;
  const targetMiB = Math.max(chunkMiB, Number(query.get('mib') || 512));
  const targetChunks = Math.ceil(targetMiB / chunkMiB);
  const chunkBytes = chunkMiB * 1024 * 1024;
  const pageBytes = 4096;
  const buffers = [];
  let committedMiB = 0;
  let failed = false;
  const button = document.getElementById('allocate');
  const state = document.getElementById('state');
  window.parpR8PressureBuffers = buffers;
  const publish = () => {
    state.textContent = `committed=${committedMiB} MiB target=${targetMiB} MiB buffers=${buffers.length}`;
    document.title = `PARP R8 ALLOCATED ${committedMiB}/${targetMiB} MiB`;
  };
  function allocateChunk() {
    if (failed || buffers.length >= targetChunks) return;
    button.disabled = true;
    try {
      const buffer = new Uint8Array(chunkBytes);
      for (let offset = 0; offset < buffer.length; offset += pageBytes) {
        buffer[offset] = ((offset / pageBytes) + buffers.length + 1) & 255;
      }
      buffers.push(buffer);
      committedMiB += chunkMiB;
      publish();
      button.disabled = buffers.length >= targetChunks;
    } catch (error) {
      failed = true;
      state.textContent = `FAILED committed=${committedMiB} MiB ${String(error)}`;
      document.title = `PARP R8 FAILED ${committedMiB}/${targetMiB} MiB`;
      button.disabled = true;
    }
  }
  button.addEventListener('click', allocateChunk);
  addEventListener('keydown', event => {
    if (event.key.toLowerCase() === 'n') {
      event.preventDefault();
      allocateChunk();
    }
  });
  state.textContent = `committed=0 MiB target=${targetMiB} MiB buffers=0`;
  document.title = `PARP R8 READY 0/${targetMiB} MiB`;
})();
</script></body></html>
""",
        encoding="utf-8",
    )


def write_r8_extras(output: Path) -> None:
    """Write assets used only by the isolated R8 profile."""
    write_oom_pressure_html(output / "oom-pressure.html")
    audio_uri = (output / "audio-test.wav").resolve().as_uri()
    (output / "rhythmdb-r8.xml").write_text(
        "<?xml version='1.0' standalone='yes'?>\n"
        "<rhythmdb version='2.0'>\n"
        + "".join(
            "<entry type='song'><title>PARP R8 Track " + str(index)
            + "</title><artist>PARP Offline</artist><album>OOM Survival</album>"
            + "<location>" + audio_uri + "</location><duration>90</duration></entry>\n"
            for index in range(1, 65)
        )
        + "</rhythmdb>\n",
        encoding="utf-8",
    )
    files = output / "files-workload"
    files.mkdir(exist_ok=True)
    for stale in files.glob("project-item-*.txt"):
        stale.unlink()
    payload = "PARP R8 deterministic offline file browser workload.\n" * 64
    for index in range(1, 4097):
        (files / f"project-item-{index:04d}.txt").write_text(
            f"item={index}\n{payload}", encoding="utf-8",
        )


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
    parser.add_argument("--profile", choices=("default", "r8"), default="default")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = output / "manifest.json"
    if manifest_path.is_file() and not args.force:
        try:
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        required = {
            "local-page.html", "serial-reuse.html", "writer-test.odt", "mail-test.eml",
            "audio-test.wav", "document-test.pdf",
            *(f"image-test-{index:02d}.png" for index in range(1, 9)),
        }
        if args.profile == "r8":
            required.update({"oom-pressure.html", "rhythmdb-r8.xml", "files-workload"})
        expected_schema = 7 if args.profile == "r8" else 5
        expected_profile = "r8" if args.profile == "r8" else current.get("profile", "default")
        required_files = [name for name in required if name != "files-workload"]
        if (
            int(current.get("schema_version", 0)) >= expected_schema
            and expected_profile == args.profile
            and all((output / name).is_file() for name in required_files)
            and all((output / name).exists() for name in required)
        ):
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
    image_body = "".join(
        f"<figure><img src='image-test-{index:02d}.png' width='1024' height='1024' "
        f"alt='PARP decoded image {index}'><figcaption>Working set image {index}</figcaption></figure>"
        for index in range(1, 9)
    )
    (output / "local-page.html").write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>PARP local page</title>"
        "<style>body{max-width:1100px;margin:auto;font:16px sans-serif}article,figure{padding:12px}"
        "img{display:block;max-width:100%;height:auto}</style></head><body>"
        f"{image_body}{html_body}"
        "<canvas id='parp-canvas' width='4096' height='4096'></canvas>"
        "<script>const c=document.getElementById('parp-canvas'),x=c.getContext('2d');"
        "for(let y=0;y<4096;y+=32){x.fillStyle=`rgb(${y%255},${(y*3)%255},${(y*7)%255})`;"
        "x.fillRect(0,y,4096,32);}document.title='PARP local page READY';</script>"
        "</body></html>\n",
        encoding="utf-8",
    )
    write_serial_reuse_html(output / "serial-reuse.html")
    (output / "writer-test.txt").write_text(
        "\n\n".join(
            f"PARP weekly engineering report - section {index}\n{paragraph * 56}"
            for index in range(1, 1801)
        ) + "\n",
        encoding="utf-8",
    )
    write_odt(output / "writer-test.odt", paragraph)
    (output / "mail-test.eml").write_text(
        "From: project@example.invalid\nTo: developer@example.invalid\n"
        "Subject: PARP local message\nMIME-Version: 1.0\n"
        "Content-Type: text/plain; charset=UTF-8\n\n"
        + "\n".join(f"Thread message {index}: {paragraph * 28}" for index in range(1, 901)),
        encoding="utf-8",
    )
    for index in range(1, 9):
        write_large_png(output / f"image-test-{index:02d}.png", variant=index)
    shutil_source = output / "image-test-01.png"
    (output / "image-test.png").write_bytes(shutil_source.read_bytes())
    write_audio(output / "audio-test.wav")
    write_pdf(output / "document-test.pdf")
    if args.profile == "r8":
        write_r8_extras(output)

    assets = {}
    asset_paths = output.rglob("*") if args.profile == "r8" else output.iterdir()
    for path in sorted(asset_paths):
        if path.is_file() and path.name != manifest_path.name:
            name = str(path.relative_to(output)) if args.profile == "r8" else path.name
            assets[name] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    manifest = {
        "schema_version": 7 if args.profile == "r8" else 5,
        "description": "Deterministic offline real-PC application content. lzx-note",
        "decoded_image_bytes": 4096 * 4096 * 3,
        "decoded_image_count": 8,
        "pdf_pages": 240,
        "audio_seconds": 90,
        "serial_reuse_default_mib": 384,
        "serial_reuse_default_chunk_mib": 16,
        "assets": assets,
    }
    if args.profile == "r8":
        manifest.update({
            "profile": "r8",
            "oom_pressure_chunk_mib": 64,
            "files_workload_count": 4096,
        })
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
