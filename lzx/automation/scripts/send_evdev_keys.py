#!/usr/bin/env python3
"""Send a small, explicit set of global keyboard events through /dev/uinput."""

from __future__ import annotations

import argparse
import time

from evdev import UInput, ecodes


SPECIAL_KEYS = {
    "super": ecodes.KEY_LEFTMETA,
    "logo": ecodes.KEY_LEFTMETA,
    "enter": ecodes.KEY_ENTER,
    "return": ecodes.KEY_ENTER,
    "escape": ecodes.KEY_ESC,
    "esc": ecodes.KEY_ESC,
    "tab": ecodes.KEY_TAB,
    "backspace": ecodes.KEY_BACKSPACE,
    "space": ecodes.KEY_SPACE,
    "alt": ecodes.KEY_LEFTALT,
    "shift": ecodes.KEY_LEFTSHIFT,
    "ctrl": ecodes.KEY_LEFTCTRL,
}


def key_code(name: str) -> int:
    normalized = name.strip().lower()
    if normalized in SPECIAL_KEYS:
        return SPECIAL_KEYS[normalized]
    if len(normalized) == 1 and normalized.isalnum():
        code = getattr(ecodes, f"KEY_{normalized.upper()}", None)
        if code is not None:
            return code
    if normalized.startswith("f") and normalized[1:].isdigit():
        code = getattr(ecodes, f"KEY_{normalized.upper()}", None)
        if code is not None:
            return code
    raise ValueError(f"unsupported key: {name}")


def emit(ui: UInput, code: int, value: int) -> None:
    ui.write(ecodes.EV_KEY, code, value)
    ui.syn()


def tap(ui: UInput, code: int, delay_s: float) -> None:
    emit(ui, code, 1)
    time.sleep(delay_s)
    emit(ui, code, 0)


def type_text(ui: UInput, text: str, delay_s: float) -> None:
    for char in text:
        if char == " ":
            tap(ui, ecodes.KEY_SPACE, delay_s)
            continue
        if char.isupper():
            emit(ui, ecodes.KEY_LEFTSHIFT, 1)
        tap(ui, key_code(char), delay_s)
        if char.isupper():
            emit(ui, ecodes.KEY_LEFTSHIFT, 0)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    key_parser = sub.add_parser("key")
    key_parser.add_argument("keys", nargs="+")
    type_parser = sub.add_parser("type")
    type_parser.add_argument("text")
    parser.add_argument("--delay-ms", type=float, default=12.0)
    args = parser.parse_args()
    delay_s = max(0.0, args.delay_ms / 1000.0)

    capabilities = {
        ecodes.EV_KEY: sorted(
            set(SPECIAL_KEYS.values())
            | {ecodes.KEY_SPACE, ecodes.KEY_LEFTSHIFT}
            | {getattr(ecodes, f"KEY_{letter}") for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}
            | {getattr(ecodes, f"KEY_F{i}") for i in range(1, 13)}
        )
    }
    with UInput(capabilities, name="codex-test1-global-keyboard") as ui:
        # Give Mutter a moment to register the virtual keyboard device.
        time.sleep(0.15)
        if args.command == "key":
            for token in args.keys:
                parts = [part for part in token.split("+") if part]
                pressed = []
                for part in parts:
                    code = key_code(part)
                    emit(ui, code, 1)
                    pressed.append(code)
                for code in reversed(pressed):
                    emit(ui, code, 0)
                time.sleep(delay_s)
        else:
            type_text(ui, args.text, delay_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
