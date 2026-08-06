from __future__ import annotations

from runtime_monitor.region_monitor.vma_parser import parse_maps_line


def test_parse_maps_line_with_spaces_and_deleted_file() -> None:
    line = "7f000000-7f001000 r--p 00001000 08:02 12345 /tmp/my document.docx (deleted)"
    rec = parse_maps_line(line, pid=10, process_starttime=20, process_role="WPS_MAIN")
    assert rec.start_addr == 0x7F000000
    assert rec.end_addr == 0x7F001000
    assert rec.file_offset == 0x1000
    assert rec.dev_major == 0x08
    assert rec.dev_minor == 0x02
    assert rec.inode == 12345
    assert rec.pathname == "/tmp/my document.docx (deleted)"
    assert rec.mapping_type == "DOCUMENT_FILE"


def test_parse_heap_stack_named_and_unknown_anonymous() -> None:
    heap = parse_maps_line("1000-2000 rw-p 00000000 00:00 0 [heap]", pid=1, process_starttime=2, process_role="WPS_MAIN")
    stack = parse_maps_line("2000-3000 rw-p 00000000 00:00 0 [stack]", pid=1, process_starttime=2, process_role="WPS_MAIN")
    named = parse_maps_line("3000-4000 rw-p 00000000 00:00 0 [anon:wps-cache]", pid=1, process_starttime=2, process_role="WPS_MAIN")
    anon = parse_maps_line("4000-5000 rw-p 00000000 00:00 0", pid=1, process_starttime=2, process_role="WPS_MAIN")
    assert heap.mapping_type == "ANON_HEAP"
    assert stack.mapping_type == "ANON_STACK"
    assert named.mapping_type == "NAMED_ANON"
    assert named.anon_name == "anon:wps-cache"
    assert anon.mapping_type == "UNKNOWN_ANON"

