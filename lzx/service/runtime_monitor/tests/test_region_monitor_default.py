from __future__ import annotations

from runtime_monitor.monitor import parse_args


def test_region_monitor_default_disabled() -> None:
    args = parse_args([])
    assert args.enable_region_monitor is False
    assert str(args.region_monitor_config).endswith("runtime_monitor/config/region_monitor.json")

