from __future__ import annotations

import csv
from pathlib import Path

from runtime_monitor.core.dual_workload_markov import (
    DualWorkloadMarkov,
    reentry_combined_strength,
    select_highest_confidence,
)


KERNEL_SOURCE = (
    Path(__file__).resolve().parents[2]
    / "MGLRU-test/v0/mglru_kernel_transfer/linux-hwe-6.17-6.17.0/mm/vmscan.c"
)


def _dual(tmp_path: Path, **kwargs) -> DualWorkloadMarkov:
    return DualWorkloadMarkov(
        enabled=True,
        session_id="full-fix-test",
        model_dir=tmp_path / "model",
        review_dir=tmp_path / "review",
        **kwargs,
    )


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _observe(dual: DualWorkloadMarkov, app: str, app_id: int, workload: int,
             timestamp: int, *, changed: bool = True, valid: bool = True) -> None:
    dual.observe_workload(
        app_key=app, app_id=app_id, scope_name=f"{app.lower()}.scope",
        workload_id=workload, timestamp_ns=timestamp,
        foreground_app_key=app, foreground_app_id=app_id,
        state_changed=changed, sample_valid_scope=valid,
    )


def test_continue_window_resets_after_switch_out(tmp_path: Path) -> None:
    dual = _dual(tmp_path)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=1)
    _observe(dual, "A", 1, 1, 2)
    _observe(dual, "A", 1, 2, 3)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=4)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=5)
    _observe(dual, "A", 1, 3, 6)
    dual.close()
    assert not _rows(tmp_path / "model/continue_markov_transitions.csv")
    assert dual.result()["cross_epoch_transition_blocked"] == 1


def test_continue_new_epoch_does_not_use_old_history(tmp_path: Path) -> None:
    dual = _dual(tmp_path)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=1)
    for ts, workload in enumerate((1, 2, 3), 2):
        _observe(dual, "A", 1, workload, ts)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=6)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=7)
    for ts, workload in enumerate((4, 5, 6), 8):
        _observe(dual, "A", 1, workload, ts)
    dual.close()
    keys = {(r["previous_workload_id"], r["current_workload_id"], r["next_workload_id"])
            for r in _rows(tmp_path / "model/continue_markov_transitions.csv")}
    assert keys == {("1", "2", "3"), ("4", "5", "6")}


def test_first_reentry_workload_does_not_update_previous_epoch(tmp_path: Path) -> None:
    dual = _dual(tmp_path, ignore_initial_low_activity_s=0)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=1)
    _observe(dual, "A", 1, 1, 2)
    _observe(dual, "A", 1, 2, 3)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=4)
    _observe(dual, "B", 2, 6, 5)
    dual.close()
    assert not _rows(tmp_path / "model/continue_markov_transitions.csv")


def test_continue_pending_cancelled_on_switch_out(tmp_path: Path) -> None:
    dual = _dual(tmp_path)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=1)
    for ts, workload in enumerate((1, 2, 3, 1, 2), 2):
        _observe(dual, "A", 1, workload, ts)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=20)
    dual.close()
    rows = _rows(tmp_path / "model/continue_markov_predictions.csv")
    assert any(row["resolution_reason"] == "cancelled_switch_out" for row in rows)


def test_reentry_accepts_state_changed_false_sample(tmp_path: Path) -> None:
    dual = _dual(tmp_path, ignore_initial_low_activity_s=0)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=1)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=2)
    _observe(dual, "B", 2, 4, 3, changed=False)
    dual.close()
    row = _rows(tmp_path / "model/reentry_workload_samples.csv")[0]
    assert row["sample_valid"] == "true"
    assert row["sample_state_changed"] == "false"


def test_reentry_ignores_invalid_scope_sample(tmp_path: Path) -> None:
    dual = _dual(tmp_path, ignore_initial_low_activity_s=0)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=1)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=2)
    _observe(dual, "B", 2, 4, 3, valid=False)
    _observe(dual, "B", 2, 5, 4)
    dual.close()
    row = _rows(tmp_path / "model/reentry_workload_samples.csv")[0]
    assert row["first_valid_workload_id"] == "5"
    assert row["candidate_sample_count"] == "2"


def test_reentry_all_low_activity_fallback(tmp_path: Path) -> None:
    dual = _dual(tmp_path, ignore_initial_low_activity_s=1)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=1)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=2)
    _observe(dual, "B", 2, 0, 2_000_000_002, changed=False)
    dual.close()
    row = _rows(tmp_path / "model/reentry_workload_samples.csv")[0]
    assert row["selection_reason"] == "fallback_low_activity"


def test_reentry_switch_out_before_selection_invalid(tmp_path: Path) -> None:
    dual = _dual(tmp_path, ignore_initial_low_activity_s=2)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=1)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=2)
    _observe(dual, "B", 2, 0, 3, changed=False)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=4)
    dual.close()
    rows = _rows(tmp_path / "model/reentry_workload_samples.csv")
    assert any(r["selection_reason"] == "invalid_switched_out" for r in rows)


def test_reentry_one_event_one_sample(tmp_path: Path) -> None:
    dual = _dual(tmp_path, ignore_initial_low_activity_s=0)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=1)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=2)
    _observe(dual, "B", 2, 4, 3, changed=False)
    _observe(dual, "B", 2, 5, 4, changed=False)
    dual.close()
    assert len(_rows(tmp_path / "model/reentry_workload_samples.csv")) == 1


def test_reentry_sample_not_shared_between_events(tmp_path: Path) -> None:
    dual = _dual(tmp_path, ignore_initial_low_activity_s=0)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=1)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=2)
    _observe(dual, "B", 2, 4, 3, changed=False)
    dual.observe_foreground(foreground_app_key="A", foreground_app_id=1, timestamp_ns=4)
    dual.observe_foreground(foreground_app_key="B", foreground_app_id=2, timestamp_ns=5)
    _observe(dual, "B", 2, 5, 6, changed=False)
    dual.close()
    samples = [r["first_valid_workload_id"] for r in _rows(
        tmp_path / "model/reentry_workload_samples.csv") if r["valid"] == "true"]
    assert samples == ["4", "5"]


def test_continue_selects_highest_confidence() -> None:
    selected = select_highest_confidence([
        {"next_workload_id": 1, "confidence_fixed": 4000, "boost_level": 3, "rank": 1},
        {"next_workload_id": 2, "confidence_fixed": 7000, "boost_level": 1, "rank": 2},
    ])
    assert selected and selected["next_workload_id"] == 2


def test_continue_tie_break_by_boost() -> None:
    selected = select_highest_confidence([
        {"next_workload_id": 1, "confidence_fixed": 7000, "boost_level": 1, "rank": 0},
        {"next_workload_id": 2, "confidence_fixed": 7000, "boost_level": 3, "rank": 0},
    ])
    assert selected and selected["next_workload_id"] == 2


def test_continue_tie_break_by_workload_id() -> None:
    selected = select_highest_confidence([
        {"next_workload_id": 5, "confidence_fixed": 7000, "boost_level": 2, "rank": 0},
        {"next_workload_id": 2, "confidence_fixed": 7000, "boost_level": 2, "rank": 0},
    ])
    assert selected and selected["next_workload_id"] == 2


def test_reentry_selects_highest_confidence() -> None:
    test_continue_selects_highest_confidence()


def test_reentry_tie_break_is_stable() -> None:
    rows = [
        {"next_workload_id": 3, "confidence_fixed": 5000, "boost_level": 2, "rank": 0},
        {"next_workload_id": 1, "confidence_fixed": 5000, "boost_level": 2, "rank": 0},
    ]
    assert select_highest_confidence(rows) == select_highest_confidence(reversed(rows))


def test_background_runtime_workload_not_in_reentry_key() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    block = source[source.index("static void mglru_dual_markov_prepare_reclaim"):source.index("/* --- debugfs write helpers")]
    assert "mglru_dual_background_runtime_workload_ignored++" in block
    assert "mglru_dual_select_candidate_locked(MGLRU_DUAL_REENTRY, app_id" in block


def test_background_reentry_uses_lstm_probability() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "mglru_dual_lstm_probability_found++" in source


def test_reentry_combined_strength() -> None:
    assert reentry_combined_strength(7000, 5000) == 3500


def test_combined_strength_rounding() -> None:
    assert reentry_combined_strength(1, 5000) == 1


def test_hint_counters_are_separate() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    for field in (
        "continue_lookup_hits", "continue_hint_generation_events",
        "continue_hint_state_updates", "reentry_lookup_hits",
        "reentry_hint_generation_events", "reentry_hint_state_updates",
    ):
        assert field in source


def test_repeated_hint_does_not_overwrite_full_state() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "hint->repeated_hit_count++" in source
    assert "if (!changed)" in source


def test_reentry_missing_transition_common_only() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "suggestion_mask = MGLRU_DUAL_SUGGEST_REENTRY_COMMON" in source
    assert "suggestion_mask |= MGLRU_DUAL_SUGGEST_REENTRY_WORKLOAD" in source


def test_reentry_common_independent_of_legacy() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    block = source[source.index("static void mglru_dual_markov_prepare_reclaim"):source.index("/* --- debugfs write helpers")]
    assert "mglru_markov_prediction.valid" not in block


def test_foreground_history_ttl_valid() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "mglru_lstm_entry_expired(state->updated_at_jiffies" in source


def test_foreground_history_ttl_expired() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "mglru_dual_continue_expired_history++" in source


def test_foreground_history_ttl_zero() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "if (!state->ttl_ms)" in source


def test_runtime_mode_disabled() -> None:
    assert '"disabled"' in KERNEL_SOURCE.read_text(encoding="utf-8")


def test_runtime_mode_legacy() -> None:
    assert '"legacy"' in KERNEL_SOURCE.read_text(encoding="utf-8")


def test_runtime_mode_dual() -> None:
    assert 'MGLRU_MARKOV_RUNTIME_DUAL' in KERNEL_SOURCE.read_text(encoding="utf-8")


def test_runtime_mode_both_observe() -> None:
    assert '"both_observe"' in KERNEL_SOURCE.read_text(encoding="utf-8")


def test_real_continue_hint_not_transition_dump() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "mglru_dual_commit_hint_locked(hint, MGLRU_DUAL_CONTINUE" in source


def test_real_reentry_hint_not_transition_dump() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "mglru_dual_commit_hint_locked(hint, MGLRU_DUAL_REENTRY" in source


def test_suggestion_mask_continue() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "MGLRU_DUAL_SUGGEST_CONTINUE_CURRENT" in source


def test_suggestion_mask_reentry() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "MGLRU_DUAL_SUGGEST_REENTRY_COMMON" in source


def test_observe_applied_equals_original() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "policy->mode == MGLRU_LSTM_POLICY_APPLY ? proposed : original" in source


def test_no_generation_adjustment() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    block = source[source.index("mglru_markov_apply_generation_adjustment"):source.index("static void mglru_markov_prepare_reclaim")]
    assert "folio_set" not in block and "folio->" not in block and "list_move" not in block


def test_no_region_protection() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    block = source[source.index("static void mglru_dual_markov_prepare_reclaim"):source.index("/* --- debugfs write helpers")]
    assert "protect(" not in block


def test_no_prefetch() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    block = source[source.index("static void mglru_dual_markov_prepare_reclaim"):source.index("/* --- debugfs write helpers")]
    assert "prefetch" not in block.lower()


def test_per_folio_zero() -> None:
    source = KERNEL_SOURCE.read_text(encoding="utf-8")
    assert "mglru_dual_per_folio_calls++" not in source
