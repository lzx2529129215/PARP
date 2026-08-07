from __future__ import annotations

import json
from pathlib import Path

from runtime_monitor.region_monitor.models import DamonEvent
from runtime_monitor.region_monitor.region_vocab import RegionVocab
from runtime_monitor.region_monitor.vma_interval_index import VMAIntervalIndex
from runtime_monitor.region_monitor.vma_parser import parse_maps_line
from runtime_monitor.region_monitor.window_aggregator import WindowAggregator


def test_window_flush_and_vocab_stability(tmp_path: Path) -> None:
    vocab = RegionVocab.load(tmp_path / "region_vocab.json")
    agg = WindowAggregator(output_dir=tmp_path, vocab=vocab, bucket_bytes=262144, window_ms=500)
    vma = parse_maps_line("100000-180000 r--p 00000000 08:01 77 /opt/libfoo.so", pid=1, process_starttime=1, process_role="WPS_MAIN")
    event = DamonEvent(timestamp_ns=1_000_000_000, target_id="1", start=0x100000, end=0x110000, nr_accesses=3, age=1, nr_regions=1, raw_line="raw")
    agg.add_event(app_id="WPS", foreground_epoch_id="epoch1", event=event, pid=1, process_starttime=1, process_role="WPS_MAIN", index=VMAIntervalIndex([vma]))
    agg.flush_all()
    agg.close()
    rows = [json.loads(line) for line in (tmp_path / "region_windows.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["mapped_event_count"] == 1
    vocab.save()
    loaded = RegionVocab.load(tmp_path / "region_vocab.json")
    assert len(loaded.regions) == 1
    assert next(iter(loaded.regions.values()))["region_id"] == 1


def test_corrupted_vocab_reports_error(tmp_path: Path) -> None:
    path = tmp_path / "region_vocab.json"
    path.write_text("{bad", encoding="utf-8")
    vocab = RegionVocab.load(path)
    assert vocab.corrupted

