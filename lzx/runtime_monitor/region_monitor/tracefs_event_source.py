from __future__ import annotations

import os
import re
import select
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .capability_probe import find_tracefs
from .damon_event_source import DamonEventSource
from .models import DamonEvent


FIELD_RE = re.compile(r"field:[^;]*\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*;")
PRINT_RE = re.compile(r'^print fmt:\s*"(?P<fmt>(?:\\.|[^"])*)"\s*,\s*(?P<args>.*)$')
PRINT_ARG_RE = re.compile(r"(?:REC|__entry)->(?P<name>[A-Za-z_][A-Za-z0-9_]*)")
PRINTF_SPEC_RE = re.compile(r"%(?:[-+ #0]*\d*(?:\.\d+)?)?(?:hh|h|ll|l|z|t|j)?(?P<type>[diuoxXpsc])")


@dataclass(frozen=True)
class TracepointSchema:
    fields: list[str]
    print_format: str = ""
    print_fields: list[str] | None = None

    def parse_payload(self, payload: str) -> dict[str, str]:
        if not self.print_format or not self.print_fields:
            return {}
        pattern_parts: list[str] = []
        field_index = 0
        cursor = 0
        for match in PRINTF_SPEC_RE.finditer(self.print_format):
            pattern_parts.append(_literal_pattern(self.print_format[cursor : match.start()]))
            if field_index >= len(self.print_fields):
                return {}
            field = self.print_fields[field_index]
            field_index += 1
            value_pattern = r"(?:0x)?[0-9a-fA-F]+" if match.group("type") in "xXp" else r"-?\d+"
            if match.group("type") == "s":
                value_pattern = r"\S+"
            pattern_parts.append(f"(?P<{field}>{value_pattern})")
            cursor = match.end()
        pattern_parts.append(_literal_pattern(self.print_format[cursor:]))
        if field_index != len(self.print_fields):
            return {}
        match = re.fullmatch("".join(pattern_parts), payload.strip())
        return match.groupdict() if match else {}


class TracefsDamonEventSource(DamonEventSource):
    def __init__(self, tracefs_path: str | Path | None = None, instance_name: str = "region_monitor") -> None:
        self.tracefs_path = Path(tracefs_path) if tracefs_path else find_tracefs()
        self.instance_name = instance_name
        self.instance_path = self._resolve_instance_path()
        self.format_path = self.tracefs_path / "events" / "damon" / "damon_aggregated" / "format"
        self.schema = parse_tracepoint_schema(self.format_path.read_text(encoding="utf-8", errors="replace"))
        self.fields = self.schema.fields
        self.trace_pipe = None
        self._enabled_path: Path | None = None
        self._previous_enabled = "0"
        self._owns_instance = False
        self.unparsed_lines: list[str] = []

    def start(self) -> None:
        self._owns_instance = not self.instance_path.exists() and self.instance_path != self.tracefs_path
        self.instance_path.mkdir(parents=True, exist_ok=True)
        events_path = self.instance_path / "events" / "damon" / "damon_aggregated" / "enable"
        if not events_path.exists():
            events_path = self.tracefs_path / "events" / "damon" / "damon_aggregated" / "enable"
        self._previous_enabled = events_path.read_text(encoding="utf-8").strip() or "0"
        events_path.write_text("1\n", encoding="utf-8")
        self._enabled_path = events_path
        self.trace_pipe = (self.instance_path / "trace_pipe").open("r", encoding="utf-8", errors="replace")

    def events(self) -> Iterator[DamonEvent]:
        if self.trace_pipe is None:
            self.start()
        assert self.trace_pipe is not None
        while True:
            line = self.trace_pipe.readline()
            if not line:
                time.sleep(0.01)
                continue
            event = parse_trace_line(line, self.schema)
            if event is not None:
                yield event
            else:
                self.unparsed_lines.append(line.rstrip("\n"))

    def read_available(self, limit: int = 100) -> list[DamonEvent]:
        if self.trace_pipe is None:
            self.start()
        assert self.trace_pipe is not None
        events: list[DamonEvent] = []
        fd = self.trace_pipe.fileno()
        while len(events) < limit:
            readable, _, _ = select.select([fd], [], [], 0)
            if not readable:
                break
            line = self.trace_pipe.readline()
            if not line:
                break
            event = parse_trace_line(line, self.schema)
            if event is None:
                self.unparsed_lines.append(line.rstrip("\n"))
            else:
                events.append(event)
        return events

    def drain_unparsed_lines(self) -> list[str]:
        lines = self.unparsed_lines
        self.unparsed_lines = []
        return lines

    def close(self) -> None:
        if self.trace_pipe is not None:
            self.trace_pipe.close()
            self.trace_pipe = None
        if self._enabled_path is not None:
            try:
                self._enabled_path.write_text(f"{self._previous_enabled}\n", encoding="utf-8")
            except OSError:
                pass
            self._enabled_path = None
        if self._owns_instance:
            try:
                self.instance_path.rmdir()
            except OSError:
                pass
            self._owns_instance = False

    def _resolve_instance_path(self) -> Path:
        instances = self.tracefs_path / "instances"
        if instances.is_dir() or os.access(self.tracefs_path, os.W_OK):
            return instances / self.instance_name
        return self.tracefs_path


def parse_tracepoint_schema(text: str) -> TracepointSchema:
    fields: list[str] = []
    print_format = ""
    print_fields: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        match = FIELD_RE.search(stripped)
        if match:
            fields.append(match.group("name"))
        print_match = PRINT_RE.match(stripped)
        if print_match:
            print_format = bytes(print_match.group("fmt"), "utf-8").decode("unicode_escape")
            print_fields = [item.group("name") for item in PRINT_ARG_RE.finditer(print_match.group("args"))]
    return TracepointSchema(fields=fields, print_format=print_format, print_fields=print_fields)


def parse_tracepoint_format(text: str) -> list[str]:
    return parse_tracepoint_schema(text).fields


def parse_trace_line(line: str, fields: list[str] | TracepointSchema | None = None) -> DamonEvent | None:
    schema = fields if isinstance(fields, TracepointSchema) else TracepointSchema(fields or [])
    payload = line.split("damon_aggregated:", 1)[1].strip() if "damon_aggregated:" in line else line.strip()
    values = schema.parse_payload(payload)
    values.update(_parse_key_values(payload))
    if not values:
        return None
    start = _first_int(values, ["start", "saddr", "addr", "from"])
    end = _first_int(values, ["end", "eaddr", "to"])
    if start is None or end is None:
        return None
    nr_accesses = _first_float(values, ["nr_accesses", "nr_accesses_bp", "accesses", "nr_access"])
    age = _first_int(values, ["age", "age_us", "age_regions"]) or 0
    nr_regions = _first_int(values, ["nr_regions", "regions"]) or 0
    target = _first_str(values, ["target_id", "target", "pid", "target_idx"]) or ""
    return DamonEvent(
        timestamp_ns=_timestamp_ns(line),
        target_id=target,
        start=start,
        end=end,
        nr_accesses=nr_accesses if nr_accesses is not None else 0.0,
        age=age,
        nr_regions=nr_regions,
        raw_line=line.rstrip("\n"),
    )


def _literal_pattern(text: str) -> str:
    parts = re.split(r"(\s+)", text)
    return "".join(r"\s+" if part.isspace() else re.escape(part) for part in parts if part)


def _parse_key_values(line: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in re.split(r"\s+", line.strip()):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        values[key.strip()] = value.strip().strip(",")
    return values


def _timestamp_ns(line: str) -> int:
    match = re.search(r"\s(?P<ts>\d+\.\d+):\s", line)
    if match:
        return int(float(match.group("ts")) * 1_000_000_000)
    return time.time_ns()


def _parse_int(value: str) -> int | None:
    try:
        return int(value, 0)
    except (TypeError, ValueError):
        return None


def _first_int(values: dict[str, str], keys: list[str]) -> int | None:
    for key in keys:
        if key in values:
            parsed = _parse_int(values[key])
            if parsed is not None:
                return parsed
    return None


def _first_float(values: dict[str, str], keys: list[str]) -> float | None:
    for key in keys:
        if key in values:
            try:
                return float(values[key])
            except ValueError:
                continue
    return None


def _first_str(values: dict[str, str], keys: list[str]) -> str | None:
    for key in keys:
        if key in values:
            return values[key]
    return None
