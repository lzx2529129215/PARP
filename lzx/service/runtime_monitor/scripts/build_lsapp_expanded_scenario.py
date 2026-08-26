#!/usr/bin/env python3
"""Build a login-free GUI scenario from held-out LSAPP transition chains."""

# lzx-note: Runtime Monitor fifteen-app held-out replay generator.
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import random
import struct
import wave
from collections import defaultdict
from pathlib import Path
from typing import Any


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
LZX_ROOT = RUNTIME_ROOT.parents[1]
OPERATION_PREDICTOR_ROOT = LZX_ROOT / "tool/operation_predictor"  # lzx-note
DEFAULT_DATASET = OPERATION_PREDICTOR_ROOT / "data/lsapp_expanded/processed/app_lstm_duration_switch/test.csv"
DEFAULT_VOCAB = OPERATION_PREDICTOR_ROOT / "data/vocab/lsapp_expanded/app_vocab.json"

APP_TEMPLATES: dict[str, dict[str, Any]] = {
    "Firefox": {
        "key": "FIREFOX", "name": "firefox", "class": "org.gnome.Epiphany|Epiphany|epiphany", "title": "PARP LSAPP|Web",  # lzx-note
        "command": "env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY epiphany-browser --private-instance --profile=\"${FIXTURE_DIR}/firefox-profile\" --new-window file://${FIXTURE_DIR}/local-page.html",  # lzx-note
        "process_names": ["epiphany", "epiphany-browser"], "operation": "Page_Down",  # lzx-note
    },
    "LibreOffice": {
        "key": "LIBREOFFICE", "name": "libreoffice", "class": "libreoffice-writer|soffice", "title": "writer-test|Writer|LibreOffice",
        "command": "libreoffice -env:UserInstallation=file://${FIXTURE_DIR}/libreoffice-profile --writer --norestore \"${FIXTURE_DIR}/writer-test.txt\"",
        "process_names": ["soffice.bin", "soffice"], "operation": "Page_Down",
    },
    "VLC": {
        "key": "VLC", "name": "vlc", "class": "vlc|Vlc", "title": "VLC|audio-test",
        "command": "vlc --no-one-instance --no-video-title-show \"${FIXTURE_DIR}/audio-test.wav\"",
        "process_names": ["vlc"], "operation": "space",
    },
    "GIMP": {
        "key": "GIMP", "name": "gimp", "class": "gimp|Gimp", "title": "GIMP|image-test",
        "command": "gimp \"${FIXTURE_DIR}/image-test.ppm\"", "process_names": ["gimp", "gimp-2.10"], "operation": "plus",
    },
    "Audacity": {
        "key": "AUDACITY", "name": "audacity", "class": "audacity|Audacity", "title": "Audacity|audio-test",
        "command": "audacity \"${FIXTURE_DIR}/audio-test.wav\"", "process_names": ["audacity"], "operation": "space",
    },
    "Thunderbird": {
        "key": "THUNDERBIRD", "name": "thunderbird", "class": "thunderbird|Thunderbird", "title": "PARP local message|Thunderbird",
        "command": "thunderbird --no-remote --profile \"${FIXTURE_DIR}/thunderbird-profile\" \"${FIXTURE_DIR}/mail-test.eml\"",
        "process_names": ["thunderbird"], "operation": "Page_Down",
    },
    "Evince": {
        "key": "EVINCE", "name": "evince", "class": "evince|Evince", "title": "document-test|Document Viewer|文档查看器",
        "command": "evince \"${FIXTURE_DIR}/document-test.pdf\"", "process_names": ["evince"], "operation": "Page_Down",
    },
    "Files": {
        "key": "FILES", "name": "files", "class": "org.gnome.Nautilus|Nautilus|nautilus", "title": "lsapp-fixtures|Files|文件",
        "command": "nautilus --new-window \"${FIXTURE_DIR}\"", "process_names": ["nautilus"], "operation": "Page_Down",
    },
    "Calculator": {
        "key": "CALCULATOR", "name": "calculator", "class": "gnome-calculator|Gnome-calculator|Calculator", "title": "Calculator|计算器",
        "command": "gnome-calculator", "process_names": ["gnome-calculator"], "operation": "1",
    },
    "Calendar": {
        "key": "CALENDAR", "name": "calendar", "class": "org.gnome.Calendar|Gnome-calendar|gnome-calendar", "title": "Calendar|日历",
        "command": "gnome-calendar --date=2026-08-14", "process_names": ["gnome-calendar"], "operation": "Right",
    },
    "Rhythmbox": {
        "key": "RHYTHMBOX", "name": "rhythmbox", "class": "rhythmbox|Rhythmbox", "title": "Rhythmbox|Music|音乐",
        "command": "rhythmbox --no-registration --dry-run --rhythmdb-file=\"${FIXTURE_DIR}/rhythmdb.xml\" \"${FIXTURE_DIR}/audio-test.wav\"",
        "process_names": ["rhythmbox"], "operation": "space",
    },
    "ImageViewer": {
        "key": "IMAGE_VIEWER", "name": "image-viewer", "class": "eog|Eog", "title": "image-test|Image Viewer|图像查看器",
        "command": "eog --new-instance \"${FIXTURE_DIR}/image-test.ppm\"", "process_names": ["eog"], "operation": "Right",
    },
    "Shotwell": {
        "key": "SHOTWELL", "name": "shotwell", "class": "shotwell|Shotwell", "title": "image-test|Shotwell|Photo",
        "command": "shotwell --datadir=\"${FIXTURE_DIR}/shotwell\" --no-runtime-monitoring --no-startup-progress \"${FIXTURE_DIR}/image-test.ppm\"",
        "process_names": ["shotwell"], "operation": "Right",
    },
    "SystemMonitor": {
        "key": "SYSTEM_MONITOR", "name": "system-monitor", "class": "gnome-system-monitor|Gnome-system-monitor", "title": "System Monitor|系统监视器",
        "command": "gnome-system-monitor --show-resources-tab", "process_names": ["gnome-system-monitor"], "operation": "Page_Down",
    },
    "Solitaire": {
        "key": "SOLITAIRE", "name": "solitaire", "class": "aisleriot|Aisleriot|sol", "title": "AisleRiot|Solitaire|Klondike|纸牌",
        "command": "/usr/games/sol --variation=klondike", "process_names": ["sol"], "operation": "F2",
    },
}


def write_pdf(path: Path) -> None:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length 73 >>\nstream\nBT /F1 18 Tf 72 720 Td (PARP LSAPP document) Tj 0 -30 Td (Offline fixture) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(payload)); payload.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(payload); payload.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]: payload.extend(f"{offset:010d} 00000 n \n".encode())
    payload.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(payload)


def write_fixtures(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("firefox-profile", "thunderbird-profile", "libreoffice-profile", "shotwell"):
        (root / name).mkdir(exist_ok=True)
    (root / "local-page.html").write_text(
        "<html><head><title>PARP LSAPP</title></head><body>" +
        "".join(f"<h2>Local section {i}</h2><p>{'offline workload ' * 100}</p>" for i in range(40)) +
        "</body></html>\n", encoding="utf-8",
    )
    (root / "writer-test.txt").write_text(("PARP offline office workload " * 120 + "\n") * 40, encoding="utf-8")
    (root / "mail-test.eml").write_text(
        "From: parp@example.invalid\nTo: local@example.invalid\nSubject: PARP local message\n"
        "MIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\n\n" + "Local mail workload.\n" * 200,
        encoding="utf-8",
    )
    with wave.open(str(root / "audio-test.wav"), "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
        samples = (int(5000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(8_000 * 12))
        stream.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    width = height = 512
    pixels = bytearray()
    for y in range(height):
        for x in range(width): pixels.extend(((x * 255) // 511, (y * 255) // 511, 128))
    (root / "image-test.ppm").write_bytes(f"P6\n{width} {height}\n255\n".encode() + pixels)
    (root / "rhythmdb.xml").write_text("<?xml version='1.0'?><rhythmdb version='2.0'></rhythmdb>\n", encoding="utf-8")
    write_pdf(root / "document-test.pdf")


def read_chains(dataset: Path) -> list[list[dict[str, str]]]:
    sessions: dict[str, list[dict[str, str]]] = defaultdict(list)
    with dataset.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("trigger_type") == "foreground_transition" and row.get("has_next_switch") == "1":
                sessions[row["session_id"]].append(row)
    chains: list[list[dict[str, str]]] = []
    for rows in sessions.values():
        rows.sort(key=lambda row: row["timestamp"])
        chain: list[dict[str, str]] = []
        for row in rows:
            if chain and chain[-1]["next_app"] != row["current_app"]:
                if chain: chains.append(chain)
                chain = []
            chain.append(row)
        if chain: chains.append(chain)
    return chains


def windows(chains: list[list[dict[str, str]]], maximum: int = 12) -> list[list[dict[str, str]]]:
    result: list[list[dict[str, str]]] = []
    for chain in chains:
        for start in range(0, len(chain), maximum):
            result.append(chain[start:start + maximum])
    return [window for window in result if window]


def apps_in(rows: list[dict[str, str]]) -> set[str]:
    return {value for row in rows for value in (row["current_app"], row["next_app"])}


def select_blocks(candidates: list[list[dict[str, str]]], vocab: set[str], count: int, seed: int) -> list[list[dict[str, str]]]:
    rng = random.Random(seed)
    shuffled = list(candidates); rng.shuffle(shuffled)
    selected: list[list[dict[str, str]]] = []
    uncovered = set(vocab)
    used: set[tuple[str, str]] = set()
    while sum(len(block) for block in selected) < count or uncovered:  # lzx-note
        ranked = []
        for block in shuffled:
            identity = (block[0]["session_id"], block[0]["timestamp"])
            if identity in used: continue
            new_apps = apps_in(block) & uncovered
            ranked.append((len(new_apps), len(apps_in(block)), len(block), rng.random(), block))
        if not ranked: break
        block = max(ranked, key=lambda item: item[:4])[4]
        remaining = count - sum(len(item) for item in selected)  # lzx-note
        if remaining > 0 and not (apps_in(block) & uncovered):  # lzx-note
            block = block[:remaining]  # lzx-note
        selected.append(block); used.add((block[0]["session_id"], block[0]["timestamp"]))
        uncovered -= apps_in(block)
    if uncovered:
        raise SystemExit("held-out transition blocks do not cover: " + ",".join(sorted(uncovered)))
    return selected


def dwell_seconds(row: dict[str, str]) -> tuple[float, str]:
    values = [float(item) for item in row.get("history_durations_s", "").split("|") if item]
    source = values[-1] if values else 1.0
    if source <= 10: return 0.8, "LE_10S"
    if source <= 60: return 1.2, "LE_60S"
    if source <= 300: return 1.8, "LE_300S"
    return 2.5, "GT_300S"


def launch_actions(app: str) -> list[dict[str, Any]]:
    item = APP_TEMPLATES[app]
    return [
        {"type": "launch", "name": item["name"], "app_key": item["key"], "command": item["command"], "label": f"LSAPP_LAUNCH_{item['key']}"},
        {"type": "wait_window", "name": item["name"], "app_key": item["key"], "class": item["class"], "title": item["title"], "timeout": 60, "label": f"LSAPP_WAIT_{item['key']}"},
    ]


def switch_actions(app: str, label: str) -> list[dict[str, Any]]:
    item = APP_TEMPLATES[app]
    return [
        {"type": "switch", "name": item["name"], "app_key": item["key"], "class": item["class"], "title": item["title"], "label": label},
        {"type": "verify_foreground", "name": item["name"], "app_key": item["key"], "class": item["class"], "title": item["title"], "label": label + "_VERIFY"},
    ]


def build_scenario(blocks: list[list[dict[str, str]]], fixture_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for app in APP_TEMPLATES: actions.extend(launch_actions(app))
    actions.append({"type": "wait", "seconds": 2, "label": "LSAPP_ALL_APPS_READY"})
    transitions: list[dict[str, Any]] = []
    for block_index, block in enumerate(blocks, start=1):
        current = block[0]["current_app"]
        actions.extend(switch_actions(current, f"LSAPP_BLOCK_{block_index:03d}_START_{APP_TEMPLATES[current]['key']}"))
        actions.append({"type": "trace_marker", "event_type": "LSAPP_BLOCK_START", "status": "success", "app_key": APP_TEMPLATES[current]["key"], "label": f"LSAPP_BLOCK_{block_index:03d}_START"})
        for transition_index, row in enumerate(block, start=1):
            dwell, bucket = dwell_seconds(row)
            actions.append({"type": "wait", "seconds": dwell, "label": f"LSAPP_DWELL_{bucket}"})
            target = row["next_app"]
            label = f"LSAPP_BLOCK_{block_index:03d}_TRANSITION_{transition_index:03d}_{APP_TEMPLATES[target]['key']}"
            actions.extend(switch_actions(target, label))
            actions.append({"type": "trace_marker", "event_type": "LSAPP_TRANSITION_DONE", "status": "success", "app_key": APP_TEMPLATES[target]["key"], "label": label + "_DONE"})
            actions.append({"type": "key", "name": APP_TEMPLATES[target]["name"], "app_key": APP_TEMPLATES[target]["key"], "key": APP_TEMPLATES[target]["operation"], "label": label + "_OPERATION"})
            transitions.append({
                "block": block_index, "index": transition_index, "session_id": row["session_id"],
                "timestamp": row["timestamp"], "current_app": row["current_app"], "next_app": target,
                "source_history_durations_s": row["history_durations_s"], "replay_dwell_s": dwell, "dwell_bucket": bucket,
            })
    for app in reversed(list(APP_TEMPLATES)):
        item = APP_TEMPLATES[app]
        actions.append({
            "type": "close", "name": item["name"], "app_key": item["key"], "class": item["class"], "title": item["title"],
            "process_names": item["process_names"], "wait_after_window_close": 0.2, "force_after_seconds": 2,
            "optional": True, "label": f"LSAPP_CLOSE_{item['key']}",
        })
    scenario = {
        "description": "Fifteen login-free Linux apps replaying held-out LSAPP transition blocks.",
        "validation_mode": True, "variables": {"FIXTURE_DIR": str(fixture_dir)}, "actions": actions,
    }
    coverage = {"transition_count": len(transitions), "block_count": len(blocks), "transitions": transitions}
    return scenario, coverage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--vocab", type=Path, default=DEFAULT_VOCAB)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--coverage-output", type=Path)
    parser.add_argument("--transitions", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20260814)
    args = parser.parse_args()
    if args.transitions < 15: raise SystemExit("--transitions must be at least 15")
    vocab = set(json.loads(args.vocab.read_text(encoding="utf-8")))
    if vocab != set(APP_TEMPLATES):
        raise SystemExit(f"scenario/vocab mismatch: scenario={sorted(APP_TEMPLATES)} vocab={sorted(vocab)}")
    write_fixtures(args.fixture_dir.resolve())
    blocks = select_blocks(windows(read_chains(args.dataset)), vocab, args.transitions, args.seed)
    scenario, coverage = build_scenario(blocks, args.fixture_dir.resolve())
    coverage.update({
        "status": "PASS", "seed": args.seed, "dataset": str(args.dataset.resolve()),
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        "apps": sorted({app for block in blocks for app in apps_in(block)}),
        "all_vocab_apps_covered": sorted({app for block in blocks for app in apps_in(block)}) == sorted(vocab),
        "generated_at": dt.datetime.now().isoformat(),
    })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    coverage_path = args.coverage_output or args.output.with_suffix(".coverage.json")
    coverage_path.write_text(json.dumps(coverage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: coverage[key] for key in ("status", "transition_count", "block_count", "apps", "dataset_sha256")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
