#!/usr/bin/env python3
"""Subscribe to GNOME runtime window events and trigger app prediction."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from runtime_monitor.mapping import AppMapper
    from runtime_monitor.predictor import NullPredictor, OnlineLSTMPredictor
    from runtime_monitor.state import RuntimeState, RuntimeUpdate
else:
    from .mapping import AppMapper
    from .predictor import NullPredictor, OnlineLSTMPredictor
    from .state import RuntimeState, RuntimeUpdate


MONITOR_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = MONITOR_DIR.parent
OPERATION_PREDICTOR_ROOT = Path(
    os.environ.get(
        "OPERATION_PREDICTOR_ROOT",
        WORKSPACE_ROOT / "huawei_mem" / "lzx" / "operation_predictor",
    )
).resolve()
BUS_NAME = "org.huawei.RuntimeAppMonitor"
OBJECT_PATH = "/org/huawei/RuntimeAppMonitor"
INTERFACE = "org.huawei.RuntimeAppMonitor"
SIGNAL = "WindowEvent"
CSV_FIELDS = ["user_id", "timestamp", "foreground_app", "opened_apps", "user_group", "event_type"]


class RuntimeMonitor:
    def __init__(self, args: argparse.Namespace, predictor: Any | None = None) -> None:
        self.args = args
        mapper = AppMapper(args.mapping, args.app_vocab)
        self.state = RuntimeState(
            mapper=mapper,
            user_id=args.user_id,
            user_group=args.user_group,
            history_len=args.history_len,
        )
        self.predictor = predictor if predictor is not None else self._build_predictor(args)
        self.raw_events = Path(args.raw_events)
        self.predictions = Path(args.predictions)
        self.app_events = Path(args.app_events)
        self.raw_events.parent.mkdir(parents=True, exist_ok=True)
        self.predictions.parent.mkdir(parents=True, exist_ok=True)
        self.app_events.parent.mkdir(parents=True, exist_ok=True)
        self.raw_events.touch(exist_ok=True)
        self.predictions.touch(exist_ok=True)
        self._ensure_csv_header()

    def process_event(self, event: dict[str, Any]) -> RuntimeUpdate:
        self._append_jsonl(self.raw_events, event)
        update = self.state.handle_event(event)
        if update.csv_row is not None:
            self._append_csv(update.csv_row)
        if update.should_predict:
            rows = self.predictor.predict(update.history_apps, update.opened_apps, update.timestamp)
            if rows:
                self._append_jsonl(
                    self.predictions,
                    {
                        "timestamp": update.timestamp,
                        "event_type": event.get("event_type"),
                        "foreground_app": update.foreground_app,
                        "history_apps": update.history_apps,
                        "opened_apps": update.opened_apps,
                        "predictions": rows,
                    },
                )
                self._print_prediction(update, rows)
            elif self.args.verbose:
                print(f"[predict skipped] {update.timestamp} foreground={update.foreground_app}")
        elif self.args.verbose:
            mapped = update.mapping.app or "unmapped"
            print(f"[event] {event.get('event_type')} mapped={mapped}")
        return update

    def run_stdin(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            self.process_event(json.loads(line))

    def run_dbus(self) -> None:
        try:
            from gi.repository import GLib, Gio
        except (ImportError, ValueError) as exc:
            raise RuntimeError("python3-gi is required for D-Bus monitoring on GNOME") from exc

        loop = GLib.MainLoop()

        def callback(_connection: Any, _sender: str, _path: str, _iface: str, _signal: str, parameters: Any) -> None:
            try:
                payload = parameters.unpack()[0]
                self.process_event(json.loads(payload))
            except Exception as exc:  # keep the monitor alive on malformed events
                print(f"error: failed to process D-Bus event: {exc}", file=sys.stderr)

        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        connection.signal_subscribe(
            None,
            INTERFACE,
            SIGNAL,
            OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            callback,
        )
        print(f"listening on session bus signal {INTERFACE}.{SIGNAL} at {OBJECT_PATH}")
        print(f"raw events: {self.raw_events}")
        print(f"app events: {self.app_events}")
        print(f"predictions: {self.predictions}")
        loop.run()

    def _build_predictor(self, args: argparse.Namespace) -> Any:
        if args.no_predict:
            return NullPredictor("disabled by --no-predict")
        try:
            return OnlineLSTMPredictor(
                checkpoint=args.checkpoint,
                app_vocab=args.app_vocab,
                group_vocab=args.group_vocab,
                user_group=args.user_group,
                top_k=args.top_k,
                score_mode=args.score_mode,
                device_name=args.device,
            )
        except Exception as exc:
            if args.require_predictor:
                raise
            print(f"warning: predictor disabled: {exc}", file=sys.stderr)
            return NullPredictor(str(exc))

    def _ensure_csv_header(self) -> None:
        if self.app_events.exists() and self.app_events.stat().st_size > 0:
            return
        with self.app_events.open("w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()

    def _append_csv(self, row: dict[str, str]) -> None:
        with self.app_events.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writerow(row)

    @staticmethod
    def _append_jsonl(path: Path, item: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    @staticmethod
    def _print_prediction(update: RuntimeUpdate, rows: list[dict[str, Any]]) -> None:
        print(
            f"[predict] {update.timestamp} foreground={update.foreground_app} "
            f"history={update.history_apps} opened={update.opened_apps}"
        )
        for row in rows[:5]:
            print(
                f"  horizon={row['horizon']} rank={row['rank']} "
                f"app={row['app']} probability={row['probability']:.4f}"
            )


def default_paths() -> dict[str, Path]:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    return {
        "raw_events": WORKSPACE_ROOT / "outputs" / "runtime_monitor" / f"events_{stamp}.jsonl",
        "predictions": WORKSPACE_ROOT / "outputs" / "runtime_monitor" / f"predictions_{stamp}.jsonl",
        "app_events": MONITOR_DIR / "data" / "raw" / "runtime" / "app_events.csv",
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    paths = default_paths()
    parser = argparse.ArgumentParser(description="Monitor GNOME app runtime events and run online app prediction.")
    parser.add_argument("--mapping", default=WORKSPACE_ROOT / "configs" / "runtime" / "app_mapping.json")
    parser.add_argument("--app-vocab", default=OPERATION_PREDICTOR_ROOT / "data" / "vocab" / "app_vocab.json")
    parser.add_argument("--group-vocab", default=OPERATION_PREDICTOR_ROOT / "data" / "vocab" / "user_group_vocab.json")
    parser.add_argument(
        "--checkpoint",
        default=OPERATION_PREDICTOR_ROOT / "outputs" / "checkpoints" / "app_lstm" / "lsapp_app_lstm.pt",
    )
    parser.add_argument("--raw-events", default=paths["raw_events"])
    parser.add_argument("--predictions", default=paths["predictions"])
    parser.add_argument("--app-events", default=paths["app_events"])
    parser.add_argument("--user-id", default="local")
    parser.add_argument("--user-group", default="通用用户")
    parser.add_argument("--history-len", type=int, default=5)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--score-mode", choices=["softmax", "sigmoid"], default="softmax")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    parser.add_argument("--stdin", action="store_true", help="Read JSON events from stdin instead of D-Bus.")
    parser.add_argument("--no-predict", action="store_true", help="Log events and CSV rows without loading the predictor.")
    parser.add_argument("--require-predictor", action="store_true", help="Exit if the predictor cannot be loaded.")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    monitor = RuntimeMonitor(args)
    try:
        if args.stdin:
            monitor.run_stdin()
        else:
            monitor.run_dbus()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
