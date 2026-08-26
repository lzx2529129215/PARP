import csv
from pathlib import Path

from runtime_monitor.core.dual_workload_markov import DualWorkloadMarkov
from runtime_monitor.core.mglru_markov_debugfs import MGLRUMarkovDebugfsWriter


def test_continue_uses_foreground_samples_only(tmp_path: Path) -> None:
    writer = MGLRUMarkovDebugfsWriter(
        enabled=False,
        strict=False,
        debugfs_path=tmp_path / "missing_debugfs",
        session_id="test",
        model_dir=tmp_path / "model",
        review_dir=tmp_path / "review",
        ttl_ms=1000,
    )
    dual = DualWorkloadMarkov(
        enabled=True,
        session_id="test",
        model_dir=tmp_path / "model",
        review_dir=tmp_path / "review",
        debugfs_writer=writer,
    )
    dual.observe_foreground(foreground_app_key="WPS", foreground_app_id=1, timestamp_ns=1)
    dual.observe_workload(app_key="QQ", app_id=2, scope_name="qq", workload_id=1,
                          foreground_app_key="WPS", foreground_app_id=1, timestamp_ns=2)
    dual.observe_workload(app_key="WPS", app_id=1, scope_name="wps", workload_id=1,
                          cgroup_id=10, foreground_app_key="WPS", foreground_app_id=1, timestamp_ns=3)
    dual.observe_workload(app_key="WPS", app_id=1, scope_name="wps", workload_id=2,
                          cgroup_id=10, foreground_app_key="WPS", foreground_app_id=1, timestamp_ns=4)
    dual.observe_workload(app_key="WPS", app_id=1, scope_name="wps", workload_id=3,
                          cgroup_id=10, foreground_app_key="WPS", foreground_app_id=1, timestamp_ns=5)
    dual.close()
    writer.close()
    rows = (tmp_path / "model" / "continue_markov_transitions.csv").read_text().splitlines()
    assert any(",1,2,3," in row for row in rows[1:])
    assert all(",QQ," not in row for row in rows[1:])
    dual_writes = (tmp_path / "model" / "dual_markov_debugfs_writes.csv").read_text()
    assert "foreground_workload_update" in dual_writes
    assert "continue_markov_set" in dual_writes


def test_reentry_ignores_initial_low_activity_and_selects_once(tmp_path: Path) -> None:
    dual = DualWorkloadMarkov(
        enabled=True,
        session_id="test",
        model_dir=tmp_path / "model",
        review_dir=tmp_path / "review",
        reentry_window_s=5,
        ignore_initial_low_activity_s=2,
    )
    dual.observe_foreground(foreground_app_key="FILES", foreground_app_id=3, timestamp_ns=0)
    dual.observe_foreground(foreground_app_key="WPS", foreground_app_id=1, timestamp_ns=1_000_000_000)
    dual.observe_workload(app_key="WPS", app_id=1, scope_name="wps", workload_id=0,
                          foreground_app_key="WPS", foreground_app_id=1,
                          timestamp_ns=2_000_000_000)
    dual.observe_workload(app_key="WPS", app_id=1, scope_name="wps", workload_id=4,
                          foreground_app_key="WPS", foreground_app_id=1,
                          timestamp_ns=3_000_000_000)
    dual.observe_workload(app_key="WPS", app_id=1, scope_name="wps", workload_id=5,
                          foreground_app_key="WPS", foreground_app_id=1,
                          timestamp_ns=4_000_000_000)
    dual.close()
    with (tmp_path / "model" / "reentry_workload_samples.csv").open(newline="") as f:
        samples = list(csv.DictReader(f))
    assert len(samples) == 1
    assert samples[0]["first_valid_workload_id"] == "4"
    assert samples[0]["candidate_sample_count"] == "2"
    assert samples[0]["ignored_low_activity_count"] == "1"
    assert samples[0]["selection_reason"] == "first_non_low_activity"
    assert samples[0]["sample_valid"] == "true"
    transitions = (tmp_path / "model" / "reentry_markov_transitions.csv").read_text()
    assert ",4,1,1," in transitions
