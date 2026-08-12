#!/usr/bin/env python3
"""Freeze Phase2.10B input provenance without touching any source output."""

import argparse
import hashlib
import json
from pathlib import Path
import time


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def project_root(start):
    cursor = Path(start).resolve()
    for candidate in (cursor, *cursor.parents):
        if all((candidate / name).exists() for name in ("MGLRU-test/v4-parp/work", "automation", "outputs")):
            return candidate
    raise RuntimeError("PROJECT_ROOT_NOT_FOUND")


def json_status(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--pilot", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    project = project_root(args.project)
    pilot = args.pilot.resolve()
    output = args.output.resolve()
    state = json_status(pilot / "state/collection.json")
    if state.get("status") != "PARP_PHASE210_POSITIVE_SUPPORT_PILOT_INSUFFICIENT":
        raise RuntimeError("PILOT_STATUS_NOT_TRUSTED")
    session = state.get("sessions", [{}])[0]
    if session.get("trace_lost") != 0 or not state.get("apply") is False or state.get("pressure") is not False:
        raise RuntimeError("PILOT_SAFETY_GATE_FAILED")
    files = {
        "pilot_collection_state": pilot / "state/collection.json",
        "pilot_session_state": pilot / "raw/qq_positive_support_pilot/state/session.json",
        "pilot_trace": pilot / "raw/qq_positive_support_pilot/trace/parp_region_evidence.filtered",
        "pilot_final_support": pilot / "raw/qq_positive_support_pilot/state/positive_support_final.json",
        "pilot_horizon_support": pilot / "raw/qq_positive_support_pilot/state/horizon_support_diagnostics.json",
        "pilot_selection_diagnostic": pilot / "raw/qq_positive_support_pilot/state/candidate_selection_diagnostics.json",
        "pilot_cleanup": pilot / "cleanup/cleanup.json",
        "pilot_report": pilot / "final/POSITIVE_SUPPORT_PILOT_REPORT.json",
        "phase29_decisions": project / "outputs/parp_phase29a_workload_expert_20260803_102327/candidate_reconstruction/decisions_generation_tail_128.jsonl.gz",
        "phase29_schema": project / "outputs/parp_phase29a_workload_expert_20260803_102327/candidate_reconstruction/candidate_schema.json",
    }
    for name, path in files.items():
        if not path.is_file():
            raise FileNotFoundError("%s: %s" % (name, path))
    old_candidates = sorted(project.glob("outputs/parp_phase29a_workload_expert_*/candidate_reconstruction/decisions_generation_tail_128.jsonl.gz"))
    if files["phase29_decisions"] not in old_candidates:
        raise RuntimeError("PHASE29_INPUT_NOT_IN_PROJECT")
    manifests = sorted(project.glob("outputs/parp_phase2*/*/manifest*.json"))
    manifest_refs = [path for path in manifests if path.is_file()][:32]
    output.mkdir(parents=True, exist_ok=True)
    provenance = {
        "schema_version": 1,
        "timestamp_ns": time.time_ns(),
        "project_root": str(project),
        "phase210_pilot_root": str(pilot),
        "pilot_status": state.get("status"),
        "kernel_release": session.get("kernel_release"),
        "session_id": session.get("session_id"),
        "boot_id_not_read_from_runtime": True,
        "root_used_in_this_phase": False,
        "runtime_actions_started": False,
        "files": {name: {"path": str(path), "sha256": sha256(path), "size": path.stat().st_size} for name, path in files.items()},
        "related_manifests": [{"path": str(path), "sha256": sha256(path), "size": path.stat().st_size} for path in manifest_refs],
    }
    (output / "input/input_provenance.json").write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    inventory = {
        "schema_version": 1,
        "sessions": [{"session_id": session.get("session_id"), "app": session.get("app"), "role": "TEST_QQ_PILOT", "source": str(pilot)}],
        "train_sessions": ["wps_01", "files_01"],
        "validation_sessions": ["wps_02"],
        "test_sessions": ["wps_03", "files_02", session.get("session_id")],
        "qq_pilot_used_for_selector_tuning": False,
    }
    (output / "input/session_inventory.json").write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    manifest = {
        "schema_version": 1,
        "input_manifest_hash": hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest(),
        "input_names": sorted(files),
        "selector_inputs": ["pilot_trace", "phase29_decisions", "phase29_schema"],
        "label_inputs": ["pilot_trace"],
        "future_not_read_by_selector": True,
        "created_before_candidate_build": True,
    }
    (output / "input/input_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    for filename in ("input_manifest_before.json", "old_output_hashes_before.sha256"):
        target = output / "validation" / filename
        if filename.endswith(".json"):
            target.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        else:
            target.write_text("\n".join("%s  %s" % (entry["sha256"], entry["path"]) for entry in provenance["files"].values()) + "\n")
    (output / "validation/raw_hashes_before.sha256").write_text(
        "\n".join("%s  %s" % (entry["sha256"], entry["path"]) for entry in provenance["files"].values()) + "\n"
    )
    state_payload = {
        "stage": "INPUT_FREEZE",
        "status": "INPUT_FROZEN",
        "timestamp_ns": time.time_ns(),
        "current_head": "UNKNOWN_UNTIL_DRIVER_WRITES",
        "input_manifest_hash": manifest["input_manifest_hash"],
        "completed_outputs": sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()),
        "decision_count": 0,
        "candidate_count": 0,
        "failure_reason": None,
        "resume_supported": True,
    }
    (output / "state/state.json").write_text(json.dumps(state_payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"project_root": str(project), "pilot": str(pilot), "output": str(output), "input_manifest_hash": manifest["input_manifest_hash"]}, sort_keys=True))


if __name__ == "__main__":
    main()
