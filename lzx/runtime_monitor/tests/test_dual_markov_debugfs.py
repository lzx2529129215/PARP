import csv
from pathlib import Path

from runtime_monitor.core.mglru_markov_debugfs import MGLRUMarkovDebugfsWriter


def test_dual_debugfs_commands_are_audited_without_writing_when_disabled(tmp_path: Path) -> None:
    writer = MGLRUMarkovDebugfsWriter(
        enabled=False, strict=False, debugfs_path=tmp_path / "missing",
        session_id="debugfs-test", model_dir=tmp_path / "model",
        review_dir=tmp_path / "review", ttl_ms=1000,
    )
    writer.write_foreground_workload(cgroup_id=10, app_id=2, workload_id=4)
    writer.write_continue_markov(
        app_id=2, previous_workload_id=0, current_workload_id=2,
        next_workload_id=6, confidence_fixed=6500, boost_level=2,
    )
    writer.write_reentry_markov(
        app_id=2, next_workload_id=6, confidence_fixed=5000, boost_level=2,
    )
    writer.write_dual_runtime_mode("dual")
    writer.clear_app_bindings()
    writer.close()
    with (tmp_path / "model" / "dual_markov_debugfs_writes.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert {row["event_type"] for row in rows} == {
        "foreground_workload_update", "continue_markov_set", "reentry_markov_set",
        "runtime_mode", "app_bind_clear",
    }
    assert all(row["status"] == "disabled" for row in rows)
