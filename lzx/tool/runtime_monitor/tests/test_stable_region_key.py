from __future__ import annotations

from runtime_monitor.region_monitor.stable_region_key import stable_region_for_address
from runtime_monitor.region_monitor.vma_parser import parse_maps_line


def test_file_stable_key_uses_file_offset_bucket_not_virtual_address() -> None:
    vma1 = parse_maps_line("100000-190000 r--p 00000000 08:01 77 /opt/libfoo.so", pid=1, process_starttime=1, process_role="WPS_MAIN")
    vma2 = parse_maps_line("300000-390000 r--p 00000000 08:01 77 /opt/libfoo.so", pid=2, process_starttime=1, process_role="WPS_MAIN")
    key1 = stable_region_for_address("WPS", vma1, 0x100000 + 262144, 262144)
    key2 = stable_region_for_address("WPS", vma2, 0x300000 + 262144, 262144)
    assert key1.stable_key == key2.stable_key
    assert "100000" not in key1.stable_key
    assert key1.canonical_key == key2.canonical_key


def test_file_bucket_boundary_changes_key() -> None:
    vma = parse_maps_line("100000-190000 r--p 00000000 08:01 77 /opt/libfoo.so", pid=1, process_starttime=1, process_role="WPS_MAIN")
    key0 = stable_region_for_address("WPS", vma, 0x100000 + 262143, 262144)
    key1 = stable_region_for_address("WPS", vma, 0x100000 + 262144, 262144)
    assert key0.stable_key != key1.stable_key


def test_anonymous_relative_bucket_and_confidence() -> None:
    vma = parse_maps_line("100000-190000 rw-p 00000000 00:00 0 [anon:wps-cache]", pid=1, process_starttime=1, process_role="WPS_MAIN")
    key = stable_region_for_address("WPS", vma, 0x100000 + 262144, 262144)
    assert '"relative_offset_bucket":1' in key.stable_key
    assert key.identity_confidence == "HIGH"

