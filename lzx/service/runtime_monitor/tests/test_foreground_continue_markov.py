import csv
from pathlib import Path

from runtime_monitor.core.dual_workload_markov import DualWorkloadMarkov


def test_foreground_continue_has_second_order_key_and_is_app_isolated(tmp_path: Path) -> None:
    dual = DualWorkloadMarkov(
        enabled=True, session_id="continue-test", model_dir=tmp_path / "model",
        review_dir=tmp_path / "review",
    )
    dual.observe_foreground(foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=1)
    for stamp, workload in enumerate((0, 2, 6), 2):
        dual.observe_workload(
            app_key="QQ", app_id=2, scope_name="qq", workload_id=workload,
            foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=stamp,
        )
    dual.observe_workload(
        app_key="WPS", app_id=1, scope_name="wps", workload_id=4,
        foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=10,
    )
    dual.close()
    with (tmp_path / "model" / "continue_markov_transitions.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert any(
        row["app_id"] == "2"
        and row["previous_workload_id"] == "0"
        and row["current_workload_id"] == "2"
        and row["next_workload_id"] == "6"
        for row in rows
    )
    assert all(row["app_id"] != "1" for row in rows)


def test_continue_history_is_frozen_when_app_is_background(tmp_path: Path) -> None:
    dual = DualWorkloadMarkov(
        enabled=True, session_id="freeze-test", model_dir=tmp_path / "model",
        review_dir=tmp_path / "review",
    )
    dual.observe_foreground(foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=1)
    dual.observe_workload(
        app_key="QQ", app_id=2, scope_name="qq", workload_id=0,
        foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=2,
    )
    dual.observe_workload(
        app_key="QQ", app_id=2, scope_name="qq", workload_id=4,
        foreground_app_key="WPS", foreground_app_id=1, timestamp_ns=3,
    )
    dual.close()
    with (tmp_path / "model" / "foreground_workload_history.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert [row["current_foreground_workload_id"] for row in rows] == ["0"]
