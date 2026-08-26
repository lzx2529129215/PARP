import csv
from pathlib import Path

from runtime_monitor.core.dual_workload_markov import DualWorkloadMarkov


def test_suggestions_are_observe_only_and_mark_background_runtime_ignored(tmp_path: Path) -> None:
    dual = DualWorkloadMarkov(
        enabled=True, session_id="policy-test", model_dir=tmp_path / "model",
        review_dir=tmp_path / "review",
    )
    dual.observe_foreground(foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=1)
    dual.observe_workload(
        app_key="QQ", app_id=2, scope_name="qq", workload_id=2,
        foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=2,
    )
    dual.observe_workload(
        app_key="WPS", app_id=1, scope_name="wps", workload_id=4,
        foreground_app_key="QQ", foreground_app_id=2, timestamp_ns=3,
    )
    dual.close()
    with (tmp_path / "model" / "dual_markov_policy_suggestions.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    assert any(row["reason"] == "background_runtime_ignored" for row in rows)
    assert all(row["status"] == "observe_only" for row in rows)
    assert all(row["suggest_current_workload_protect"] in {"false", "true"} for row in rows)
