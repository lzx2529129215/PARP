from __future__ import annotations

from runtime_monitor.region_monitor.tracefs_event_source import parse_trace_line, parse_tracepoint_format, parse_tracepoint_schema


def test_tracepoint_format_parser_is_field_name_based() -> None:
    text = """
field:unsigned long start; offset:16; size:8; signed:0;
field:unsigned long end; offset:24; size:8; signed:0;
field:unsigned int nr_accesses; offset:32; size:4; signed:0;
"""
    assert parse_tracepoint_format(text) == ["start", "end", "nr_accesses"]


def test_trace_line_parser_accepts_dynamic_field_names() -> None:
    line = "task-1 [000] .... 12.345678: damon_aggregated: target_id=42 start=0x1000 end=0x3000 nr_accesses=7 age=3 nr_regions=2"
    event = parse_trace_line(line, ["target_id", "start", "end"])
    assert event is not None
    assert event.target_id == "42"
    assert event.start == 0x1000
    assert event.end == 0x3000
    assert event.nr_accesses == 7


def test_trace_line_parser_uses_current_kernel_print_format() -> None:
    format_text = '''
field:unsigned long target_id; offset:8; size:8; signed:0;
field:unsigned int nr_regions; offset:16; size:4; signed:0;
field:unsigned long start; offset:24; size:8; signed:0;
field:unsigned long end; offset:32; size:8; signed:0;
field:unsigned int nr_accesses; offset:40; size:4; signed:0;
field:unsigned int age; offset:44; size:4; signed:0;
print fmt: "target_id=%lu nr_regions=%u %lu-%lu: %u %u", REC->target_id, REC->nr_regions, REC->start, REC->end, REC->nr_accesses, REC->age
'''
    schema = parse_tracepoint_schema(format_text)
    line = "kdamond.0-7 [000] .... 12.345678: damon_aggregated: target_id=0 nr_regions=17 4096-12288: 9 3"
    event = parse_trace_line(line, schema)
    assert event is not None
    assert event.target_id == "0"
    assert event.nr_regions == 17
    assert event.start == 4096
    assert event.end == 12288
    assert event.nr_accesses == 9
    assert event.age == 3
