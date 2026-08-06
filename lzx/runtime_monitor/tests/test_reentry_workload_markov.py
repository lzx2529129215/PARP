import csv
from pathlib import Path

from runtime_monitor.core.dual_workload_markov import DualWorkloadMarkov


def test_reentry_uses_app_only_and_emits_one_sample_per_switch(tmp_path: Path) -> None:
    dual = DualWorkloadMarkov(
        enabled=True, session_id="reentry-test", model_dir=tmp_path / "model",
        review_dir=tmp_path / "review", ignore_initial_low_activity_s=0,
    )
    dual.observe_foreground(foreground_app_key="WPS", foreground_app_id=1, timestamp_ns=1)
    dual.observe_foreground(foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=2)
    dual.observe_workload(
        app_key="QQ", app_id=2, scope_name="qq", workload_id=6,
        foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=3,
    )
    dual.observe_foreground(foreground_app_key="WPS", foreground_app_id=1, timestamp_ns=4)
    dual.observe_foreground(foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=5)
    dual.observe_workload(
        app_key="QQ", app_id=2, scope_name="qq", workload_id=2,
        foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=6,
    )
    dual.close()
    with (tmp_path / "model" / "reentry_workload_samples.csv").open(newline="") as f:
        samples = list(csv.DictReader(f))
    with (tmp_path / "model" / "reentry_markov_transitions.csv").open(newline="") as f:
        transitions = list(csv.DictReader(f))
    assert len(samples) == 3
    valid_samples = [row for row in samples if row["sample_valid"] == "true"]
    assert len(valid_samples) == 2
    assert {row["app_id"] for row in valid_samples} == {"2"}
    assert {row["next_workload_id"] for row in transitions} == {"2", "6"}


def test_reentry_without_valid_sample_is_invalid(tmp_path: Path) -> None:
    dual = DualWorkloadMarkov(
        enabled=True, session_id="invalid-reentry", model_dir=tmp_path / "model",
        review_dir=tmp_path / "review", ignore_initial_low_activity_s=2,
    )
    dual.observe_foreground(foreground_app_key="FILES", foreground_app_id=3, timestamp_ns=1)
    dual.observe_foreground(foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=2)
    dual.observe_workload(
        app_key="QQ", app_id=2, scope_name="qq", workload_id=0,
        foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=3,
    )
    dual.close()
    with (tmp_path / "model" / "reentry_workload_samples.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["sample_valid"] == "false"
