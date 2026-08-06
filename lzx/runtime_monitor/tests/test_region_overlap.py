from __future__ import annotations

from pathlib import Path

from runtime_monitor.region_monitor.models import DamonEvent
from runtime_monitor.region_monitor.region_vocab import RegionVocab
from runtime_monitor.region_monitor.vma_interval_index import VMAIntervalIndex
from runtime_monitor.region_monitor.vma_parser import parse_maps_line
from runtime_monitor.region_monitor.window_aggregator import map_event_to_regions


def test_damon_region_crosses_vmas_and_buckets(tmp_path: Path) -> None:
    records = [
        parse_maps_line("100000-160000 r--p 00000000 08:01 77 /opt/libfoo.so", pid=1, process_starttime=1, process_role="WPS_MAIN"),
        parse_maps_line("160000-1c0000 rw-p 00000000 00:00 0 [heap]", pid=1, process_starttime=1, process_role="WPS_MAIN"),
    ]
    event = DamonEvent(timestamp_ns=1_000_000_000, target_id="1", start=0x120000, end=0x1A0000, nr_accesses=8, age=2, nr_regions=1, raw_line="")
    vocab = RegionVocab.load(tmp_path / "region_vocab.json")
    observations = map_event_to_regions(
        app_id="WPS",
        event=event,
        pid=1,
        process_starttime=1,
        process_role="WPS_MAIN",
        index=VMAIntervalIndex(records),
        vocab=vocab,
        bucket_bytes=262144,
    )
    assert len(observations) >= 2
    assert abs(sum(obs.weighted_accesses for obs in observations) - 8.0) < 0.001
    assert any(obs.region_type == "SHARED_LIBRARY" for obs in observations)
    assert any(obs.region_type == "ANON_HEAP" for obs in observations)


def test_same_file_region_multi_pid_dedup_canonical(tmp_path: Path) -> None:
    vma1 = parse_maps_line("100000-180000 r--p 00000000 08:01 77 /opt/libfoo.so", pid=1, process_starttime=1, process_role="WPS_MAIN")
    vma2 = parse_maps_line("300000-380000 r--p 00000000 08:01 77 /opt/libfoo.so", pid=2, process_starttime=1, process_role="WPS_MAIN")
    vocab = RegionVocab.load(tmp_path / "region_vocab.json")
    event1 = DamonEvent(1, "1", 0x100000, 0x110000, 1, 0, 1, "")
    event2 = DamonEvent(2, "2", 0x300000, 0x310000, 1, 0, 1, "")
    obs1 = map_event_to_regions(app_id="WPS", event=event1, pid=1, process_starttime=1, process_role="WPS_MAIN", index=VMAIntervalIndex([vma1]), vocab=vocab, bucket_bytes=262144)
    obs2 = map_event_to_regions(app_id="WPS", event=event2, pid=2, process_starttime=1, process_role="WPS_MAIN", index=VMAIntervalIndex([vma2]), vocab=vocab, bucket_bytes=262144)
    assert obs1[0].canonical_region_id == obs2[0].canonical_region_id

