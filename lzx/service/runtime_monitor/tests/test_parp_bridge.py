from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

from runtime_monitor.core.parp_bridge import PARPDebugfsBridge


def fake_scope() -> SimpleNamespace:
    apps = [
        SimpleNamespace(app_key="A", app_id=11, vocab_name="AppA", scope_name="a.scope", prediction_enabled=True),
        SimpleNamespace(app_key="B", app_id=12, vocab_name="AppB", scope_name="b.scope", prediction_enabled=True),
        SimpleNamespace(app_key="C", app_id=13, vocab_name="AppC", scope_name="c.scope", prediction_enabled=True),
    ]
    return SimpleNamespace(apps=apps)


def bind_config(path: Path) -> Path:
    path.write_text(
        json.dumps({
            "apps": {
                "A": {"domain_id": 101, "memcg_path": "/mock/a"},
                "B": {"domain_id": 102, "memcg_path": "/mock/b"},
                "C": {"domain_id": 103, "memcg_path": "/mock/c"},
            }
        }),
        encoding="utf-8",
    )
    return path


def prediction(**extra: object) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "success",
        "mapped_foreground_app": "AppA",
        "mapped_opened_apps": "AppA|AppB",
        "trigger_type": "foreground_transition",
        "predict_latency_ms": 2.5,
        "history_apps": "AppA|AppC",
        "all_probabilities": [
            {"horizon": 3, "app": "AppA", "probability": 0.9},
            {"horizon": 3, "app": "AppB", "probability": 0.5},
            {"horizon": 3, "app": "AppC", "probability": 0.3},
            {"horizon": 3, "app": "NotWhitelisted", "probability": 1.0},
        ],
    }
    value.update(extra)
    return value


def make_debugfs(root: Path) -> None:
    root.mkdir()
    for name, content in (
        ("app_bind", ""),
        ("app_prior", ""),
        ("stats", "snapshot_generation=7\n"),
    ):
        (root / name).write_text(content, encoding="utf-8")


def read_events(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_shadow_write_uses_current_parp_parser_and_snapshot_marker(tmp_path: Path) -> None:
    debugfs = tmp_path / "parp"
    make_debugfs(debugfs)
    config = bind_config(tmp_path / "bind.json")
    output = tmp_path / "session"
    bridge = PARPDebugfsBridge(
        mode="shadow-write",
        debugfs_root=debugfs,
        runtime_scope=fake_scope(),
        output_dir=output,
        session_id="test2",
        app_bind_config=config,
    )
    preflight = bridge.preflight()
    assert preflight["status"] == "READY"
    assert preflight["app_prior_batch"]["supported"] is False
    bridge.startup_bindings()
    bridge.submit_prediction({}, prediction())
    bridge.close()

    summary = json.loads((output / "parp/parp_bridge_summary.json").read_text(encoding="utf-8"))
    assert summary["app_bind_success"] == 3
    assert summary["app_prior_success"] == 2
    assert summary["snapshot_binding_updates_observed"] == 3
    assert summary["snapshot_prior_updates_observed"] == 2
    assert summary["prediction_to_snapshot_matched"] == 2
    funnel = summary["prediction_funnel"]
    assert funnel["prediction_batch_count"] == 1
    assert funnel["candidate_row_count"] == 4
    assert funnel["target_horizon_row_count"] == 4
    assert funnel["current_app_row_count"] == 1
    assert funnel["non_whitelist_row_count"] == 1
    assert funnel["candidate_row_count_after_filter"] == 2
    assert funnel["prior_command_row_count"] == 2

    events = read_events(output / "parp/parp_bridge_events.csv")
    prior = [row for row in events if row["event_type"] == "app_prior" and row["status"] == "OK"]
    assert {row["candidate_app"] for row in prior} == {"AppB", "AppC"}
    assert all(row["current_app"] != row["candidate_app"] for row in prior)
    assert all(row["snapshot_update_seen"] == "true" for row in prior)
    assert all(len(row["serialized_command"].split()) == 6 for row in prior)
    assert all(len(row["serialized_command"].split()) == 5 for row in events if row["event_type"] == "app_bind" and row["status"] == "OK")


def test_v41_snapshot_ack_requires_a_version_change(tmp_path: Path) -> None:
    debugfs = tmp_path / "parp"
    make_debugfs(debugfs)
    (debugfs / "snapshot").write_text(
        "version=7\ncreated_ns=1\nexpires_ns=2\nnr_priors=1\nnr_bindings=1\n",
        encoding="utf-8",
    )
    bridge = PARPDebugfsBridge(
        mode="dry-run",
        debugfs_root=debugfs,
        runtime_scope=fake_scope(),
        output_dir=tmp_path / "session",
        session_id="v41-snapshot",
    )
    assert bridge._snapshot_generation() == "7"
    assert bridge._snapshot_ack("6", "7")
    assert not bridge._snapshot_ack("7", "7")
    bridge.close()


def test_dry_run_serializes_without_touching_debugfs(tmp_path: Path) -> None:
    debugfs = tmp_path / "missing-parp"
    bridge = PARPDebugfsBridge(
        mode="dry-run",
        debugfs_root=debugfs,
        runtime_scope=fake_scope(),
        output_dir=tmp_path / "session",
        session_id="dry",
        app_bind_config=bind_config(tmp_path / "bind.json"),
    )
    bridge.startup_bindings()
    bridge.submit_prediction({}, prediction())
    bridge.close()
    assert not debugfs.exists()
    events = read_events(tmp_path / "session/parp/parp_bridge_events.csv")
    assert any(row["status"] == "DRY_RUN" and row["serialized_command"] for row in events)
    summary = json.loads((tmp_path / "session/parp/parp_bridge_summary.json").read_text(encoding="utf-8"))
    assert summary["app_bind_success"] == 0
    assert summary["app_prior_success"] == 0


def test_shadow_write_missing_interface_is_fail_closed(tmp_path: Path) -> None:
    bridge = PARPDebugfsBridge(
        mode="shadow-write",
        debugfs_root=tmp_path / "missing-parp",
        runtime_scope=fake_scope(),
        output_dir=tmp_path / "session",
        session_id="blocked",
        app_bind_config=bind_config(tmp_path / "bind.json"),
    )
    assert bridge.preflight()["status"] == "FAIL_CLOSED"
    bridge.submit_prediction({}, prediction())
    bridge.close()
    summary = json.loads((tmp_path / "session/parp/parp_bridge_summary.json").read_text(encoding="utf-8"))
    assert summary["app_prior_failures"] == 2
    assert summary["missing_debugfs"] == 1


def test_invalid_current_nan_and_non_whitelist_candidates_are_dropped(tmp_path: Path) -> None:
    bridge = PARPDebugfsBridge(
        mode="dry-run",
        debugfs_root=tmp_path / "missing-parp",
        runtime_scope=fake_scope(),
        output_dir=tmp_path / "session",
        session_id="invalid",
        app_bind_config=bind_config(tmp_path / "bind.json"),
    )
    bridge.submit_prediction({}, prediction(all_probabilities=[
        {"horizon": 3, "app": "AppA", "probability": 0.9},
        {"horizon": 3, "app": "AppB", "probability": float("nan")},
        {"horizon": 3, "app": "NotWhitelisted", "probability": 1.0},
    ]))
    bridge.close()
    events = read_events(tmp_path / "session/parp/parp_bridge_events.csv")
    assert not any(row["event_type"] == "app_prior" and row["serialized_command"] for row in events)
    assert any(row["fallback_reason"] == "NO_VALID_CANDIDATE" for row in events)


def test_unknown_foreground_is_retained_but_unknown_candidate_is_dropped(tmp_path: Path) -> None:
    bridge = PARPDebugfsBridge(
        mode="dry-run",
        debugfs_root=tmp_path / "missing-parp",
        runtime_scope=fake_scope(),
        output_dir=tmp_path / "session",
        session_id="unknown",
        app_bind_config=bind_config(tmp_path / "bind.json"),
    )
    bridge.submit_prediction(
        {},
        prediction(
            mapped_foreground_app="<UNKNOWN>",
            all_probabilities=[
                {"horizon": 3, "app": "AppA", "probability": 0.9},
                {"horizon": 3, "app": "AppB", "probability": 0.5},
                {"horizon": 3, "app": "NotWhitelisted", "probability": 1.0},
            ],
        ),
    )
    bridge.close()
    summary = json.loads((tmp_path / "session/parp/parp_bridge_summary.json").read_text(encoding="utf-8"))
    funnel = summary["prediction_funnel"]
    assert funnel["unknown_foreground_batch_count"] == 1
    assert funnel["unknown_foreground_candidate_row_count_retained"] == 2
    assert funnel["non_whitelist_row_count"] == 1
    assert funnel["prior_command_row_count"] == 2


def test_app_probability_format_has_no_horizon_filter(tmp_path: Path) -> None:
    bridge = PARPDebugfsBridge(
        mode="dry-run",
        debugfs_root=tmp_path / "missing-parp",
        runtime_scope=fake_scope(),
        output_dir=tmp_path / "session",
        session_id="app-probability",
        app_bind_config=bind_config(tmp_path / "bind.json"),
    )
    bridge.submit_prediction(
        {},
        prediction(
            prediction_format="app_probability",
            all_probabilities=[
                {"app": "AppA", "probability": 0.7},
                {"app": "AppB", "probability": 0.2},
                {"app": "AppC", "probability": 0.1},
            ],
        ),
    )
    bridge.close()
    summary = json.loads((tmp_path / "session/parp/parp_bridge_summary.json").read_text(encoding="utf-8"))
    funnel = summary["prediction_funnel"]
    assert funnel["prediction_format"] == "app_probability"
    assert funnel["probability_row_count"] == 3
    assert funnel["target_horizon_row_count"] == 0
    assert funnel["candidate_row_count_after_filter"] == 2
    assert funnel["prior_command_row_count"] == 2


def test_duplicate_and_stale_predictions_are_suppressed(tmp_path: Path) -> None:
    bridge = PARPDebugfsBridge(
        mode="dry-run",
        debugfs_root=tmp_path / "missing-parp",
        runtime_scope=fake_scope(),
        output_dir=tmp_path / "session",
        session_id="dedupe",
        app_bind_config=bind_config(tmp_path / "bind.json"),
    )
    bridge.submit_prediction({}, prediction())
    bridge.submit_prediction({}, prediction())
    bridge.submit_prediction({}, prediction(valid_until_ns=1))
    bridge.close()
    summary = json.loads((tmp_path / "session/parp/parp_bridge_summary.json").read_text(encoding="utf-8"))
    assert summary["duplicate_predictions_suppressed"] == 1
    assert summary["stale_predictions_suppressed"] == 1
