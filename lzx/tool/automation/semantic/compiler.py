"""将高层语义操作编译成既有 app_automation.py 的低层 JSON 场景。"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SIDE_EFFECT_LEVELS = {"NONE", "LOCAL_ONLY", "ACCOUNT_STATE", "EXTERNAL_MESSAGE", "PUBLIC_CONTENT"}
EXTERNAL_LEVELS = {"EXTERNAL_MESSAGE", "PUBLIC_CONTENT"}
VARIABLE_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class CompileError(RuntimeError):
    pass


@dataclass(frozen=True)
class CompileResult:
    scenario_id: str
    compiled: dict[str, Any]
    action_map: list[dict[str, Any]]
    required_apps: list[str]
    optional_apps: list[str]
    side_effect_operations: list[str]


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_operations(operations_dir: Path) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for path in sorted(operations_dir.glob("*_operations.json")):
        data = load_json(path)
        entries = data.get("operations", []) if isinstance(data, dict) else []
        if not isinstance(entries, list):
            raise CompileError(f"{path}: operations 必须是数组")
        for operation in entries:
            if not isinstance(operation, dict):
                raise CompileError(f"{path}: operation 必须是对象")
            operation_id = str(operation.get("operation_id", "")).strip()
            if not operation_id:
                raise CompileError(f"{path}: operation_id 不能为空")
            if operation_id in catalog:
                raise CompileError(f"重复 operation_id: {operation_id}")
            validate_operation(operation, source=path)
            catalog[operation_id] = operation
    if not catalog:
        raise CompileError(f"未在 {operations_dir} 找到 operation 定义")
    return catalog


def validate_operation(operation: dict[str, Any], *, source: Path | None = None) -> None:
    required = ("operation_id", "operation_name_zh", "app_key", "operation_domain", "side_effect_level", "steps")
    missing = [name for name in required if not operation.get(name)]
    if missing:
        prefix = f"{source}: " if source else ""
        raise CompileError(f"{prefix}operation 缺少字段: {','.join(missing)}")
    level = str(operation["side_effect_level"])
    if level not in SIDE_EFFECT_LEVELS:
        raise CompileError(f"{operation['operation_id']}: 未知 side_effect_level={level}")
    if not isinstance(operation["steps"], list):
        raise CompileError(f"{operation['operation_id']}: steps 必须是数组")
    for field in ("prerequisites", "required_assets", "required_variables", "cleanup_steps"):
        value = operation.get(field, [])
        if value not in (None, "") and not isinstance(value, list):
            raise CompileError(f"{operation['operation_id']}: {field} 必须是数组")


def validate_scenario(scenario: dict[str, Any], *, source: Path | None = None) -> None:
    scenario_id = scenario.get("scenario_id")
    phases = scenario.get("phases")
    if not scenario_id or not isinstance(phases, list) or not phases:
        prefix = f"{source}: " if source else ""
        raise CompileError(f"{prefix}semantic scenario 需要 scenario_id 和非空 phases")
    for index, phase in enumerate(phases, start=1):
        if not isinstance(phase, dict):
            raise CompileError(f"{scenario_id}: phase {index} 必须是对象")
        refs = phase.get("operation_ref", [])
        if isinstance(refs, str):
            refs = [refs]
        if not isinstance(refs, list) or not refs:
            raise CompileError(f"{scenario_id}: phase {index} 缺少 operation_ref")


def load_asset_manifest(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    data = load_json(path)
    if not isinstance(data, dict):
        raise CompileError(f"素材 manifest 不是对象: {path}")
    return data


def _substitute(value: Any, variables: dict[str, Any], *, context: str) -> Any:
    if isinstance(value, list):
        return [_substitute(item, variables, context=context) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, variables, context=context) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    full = VARIABLE_RE.fullmatch(value)
    if full:
        name = full.group(1)
        if name not in variables:
            raise CompileError(f"{context}: 缺少变量 {name}")
        return variables[name]
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise CompileError(f"{context}: 缺少变量 {name}")
        return str(variables[name])
    return VARIABLE_RE.sub(replace, value)


def _asset_exists(value: Any) -> bool:
    if isinstance(value, dict):
        candidate = value.get("path", "")
    else:
        candidate = value
    return bool(candidate) and Path(str(candidate)).expanduser().exists()


def _validate_assets(operation: dict[str, Any], manifest: dict[str, Any]) -> None:
    for asset in operation.get("required_assets", []) or []:
        if asset not in manifest:
            raise CompileError(f"{operation['operation_id']}: 素材 manifest 缺少 {asset}")
        if not _asset_exists(manifest[asset]):
            raise CompileError(f"{operation['operation_id']}: 素材不可用 {asset}")


def _action_id(scenario_id: str, phase_id: str, operation_id: str, occurrence: int, index: int) -> str:
    raw = f"{scenario_id}|{phase_id}|{operation_id}|{occurrence}|{index}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _semantic_fields(operation: dict[str, Any], phase_id: str, action_id: str) -> dict[str, Any]:
    return {
        "phase_id": phase_id,
        "action_id": action_id,
        "app_key": operation["app_key"],
        "operation_domain": operation["operation_domain"],
        "operation_id": operation["operation_id"],
        "operation_name": operation["operation_name_zh"],
        "requested_operation": operation["operation_id"],
        "side_effect_level": operation["side_effect_level"],
        "scope_name": operation.get("scope_name", ""),
    }


def compile_scenario(
    scenario_path: Path,
    operations_dir: Path,
    *,
    asset_manifest_path: Path | None = None,
    allow_external_side_effects: bool = False,
    variable_overrides: dict[str, Any] | None = None,
) -> CompileResult:
    scenario = load_json(scenario_path)
    if not isinstance(scenario, dict):
        raise CompileError(f"{scenario_path}: scenario 必须是对象")
    validate_scenario(scenario, source=scenario_path)
    catalog = load_operations(operations_dir)
    manifest = load_asset_manifest(asset_manifest_path)
    variables = dict(scenario.get("variables", {}) or {})
    if not isinstance(variables, dict):
        raise CompileError(f"{scenario_path}: variables 必须是对象")
    if variable_overrides:
        variables.update(variable_overrides)
    scenario_id = str(scenario["scenario_id"])
    actions: list[dict[str, Any]] = []
    action_map: list[dict[str, Any]] = []
    required_apps: set[str] = set()
    optional_apps: set[str] = set()
    side_effect_operations: list[str] = []
    occurrence = 0

    for phase_index, raw_phase in enumerate(scenario["phases"], start=1):
        phase = copy.deepcopy(raw_phase)
        phase_id = str(phase.get("phase_id") or f"phase_{phase_index:02d}")
        refs = phase.get("operation_ref", [])
        if isinstance(refs, str):
            refs = [refs]
        repeat = int(phase.get("repeat", 1) or 1)
        if repeat < 1:
            continue
        raw_params = phase.get("operation_params", {}) or {}
        if not isinstance(raw_params, dict):
            raise CompileError(f"{scenario_id}/{phase_id}: operation_params 必须是对象")
        params = _substitute(raw_params, variables, context=f"{scenario_id}/{phase_id}")
        for repeat_index in range(1, repeat + 1):
            phase_marker_id = _action_id(scenario_id, phase_id, "PHASE", repeat_index, 0)
            actions.append({
                "type": "trace_marker", "event_type": "PHASE_START", "status": "running",
                "op_type": "semantic_phase", "phase_id": phase_id, "action_id": phase_marker_id,
                "metadata": {"repeat_index": repeat_index},
            })
            switch_to_app = str(phase.get("switch_to_app", "")).strip()
            if switch_to_app:
                switch_specs = variables.get("app_switch_specs", {})
                if not isinstance(switch_specs, dict) or not isinstance(switch_specs.get(switch_to_app), dict):
                    raise CompileError(f"{scenario_id}/{phase_id}: switch_to_app={switch_to_app} 缺少 app_switch_specs")
                switch_spec = _substitute(copy.deepcopy(switch_specs[switch_to_app]), variables, context=f"{phase_id}.switch_to_app")
                switch_id = _action_id(scenario_id, phase_id, f"SWITCH_{switch_to_app}", repeat_index, 1)
                switch_fields = {
                    "phase_id": phase_id, "app_key": switch_to_app,
                    "operation_domain": "APP_SWITCH", "operation_id": "COMMON_SWITCH_APP",
                    "operation_name": "应用切换请求", "requested_operation": "COMMON_SWITCH_APP",
                    "side_effect_level": "NONE",
                }
                actions.append({"type": "trace_marker", "event_type": "APP_SWITCH_START", "status": "requested", "op_type": "app_switch", "action_id": switch_id, **switch_fields})
                switch_action = {"type": "switch", **switch_spec, "action_id": _action_id(scenario_id, phase_id, f"SWITCH_{switch_to_app}", repeat_index, 2), **switch_fields}
                actions.append(switch_action)
                wait_s = float(phase.get("wait_after_switch_s", 1.0) or 1.0)
                actions.append({"type": "wait", "seconds": wait_s, "action_id": _action_id(scenario_id, phase_id, f"SWITCH_{switch_to_app}", repeat_index, 3), **switch_fields})
                # This verifies the window only. APP_SWITCH_DONE is deliberately left to
                # the foreground collector, rather than fabricated in automation trace.
                actions.append({"type": "verify_foreground", **switch_spec, "action_id": _action_id(scenario_id, phase_id, f"SWITCH_{switch_to_app}", repeat_index, 4), **switch_fields})
            for reference in refs:
                reference_params: dict[str, Any] = {}
                if isinstance(reference, dict):
                    operation_id = str(reference.get("operation_ref", ""))
                    raw_reference_params = reference.get("operation_params", {}) or {}
                    if not isinstance(raw_reference_params, dict):
                        raise CompileError(f"{scenario_id}/{phase_id}: operation_ref.operation_params 必须是对象")
                    reference_params = _substitute(raw_reference_params, {**variables, **params}, context=f"{scenario_id}/{phase_id}/{operation_id}")
                else:
                    operation_id = str(reference)
                operation = catalog.get(operation_id)
                if operation is None:
                    raise CompileError(f"{scenario_id}/{phase_id}: 未找到 operation_ref={operation_id}")
                occurrence += 1
                local_variables = {**variables, **params, **reference_params, "repeat_index": repeat_index}
                for name in operation.get("required_variables", []) or []:
                    if name not in local_variables:
                        raise CompileError(f"{operation_id}: 缺少 required_variables={name}")
                _validate_assets(operation, manifest)
                level = str(operation["side_effect_level"])
                if level in EXTERNAL_LEVELS:
                    side_effect_operations.append(operation_id)
                    if not allow_external_side_effects:
                        raise CompileError(f"{operation_id}: 外部副作用默认关闭，请显式传入 --allow-external-side-effects")
                    for required in ("test_account", "test_recipient_allowlist"):
                        if not local_variables.get(required):
                            raise CompileError(f"{operation_id}: 外部操作需要变量 {required}")
                if bool(operation.get("optional", False)) or bool(phase.get("optional", False)):
                    optional_apps.add(str(operation["app_key"]))
                else:
                    required_apps.add(str(operation["app_key"]))

                start_id = _action_id(scenario_id, phase_id, operation_id, occurrence, 0)
                start = {
                    "type": "trace_marker",
                    "event_type": "OP_START",
                    "status": "running",
                    "op_type": "semantic_operation",
                    **_semantic_fields(operation, phase_id, start_id),
                    "metadata": {"repeat_index": repeat_index, "scenario_source": str(scenario_path)},
                }
                actions.append(start)
                terminal_event = "OP_DONE"
                terminal_status = "success"
                for step_index, raw_step in enumerate(operation["steps"], start=1):
                    compiled_step = _substitute(copy.deepcopy(raw_step), local_variables, context=operation_id)
                    if not isinstance(compiled_step, dict):
                        raise CompileError(f"{operation_id}: step {step_index} 必须是对象")
                    low_id = _action_id(scenario_id, phase_id, operation_id, occurrence, step_index)
                    compiled_step.update(_semantic_fields(operation, phase_id, low_id))
                    compiled_step.setdefault("optional", bool(operation.get("optional", False)) or bool(phase.get("optional", False)))
                    if compiled_step.get("type") == "trace_marker" and compiled_step.get("event_type") == "OP_FAILED":
                        terminal_event = "OP_FAILED"
                        terminal_status = "not_exercised"
                    actions.append(compiled_step)
                    action_map.append({
                        "scenario_id": scenario_id,
                        "phase_id": phase_id,
                        "operation_id": operation_id,
                        "action_id": low_id,
                        "low_level_index": step_index,
                        "action_type": compiled_step.get("type", ""),
                        "requested_operation": operation_id,
                        "side_effect_level": level,
                    })
                done_id = _action_id(scenario_id, phase_id, operation_id, occurrence, 9999)
                actions.append({
                    "type": "trace_marker",
                    "event_type": terminal_event,
                    "status": terminal_status,
                    "op_type": "semantic_operation",
                    **_semantic_fields(operation, phase_id, done_id),
                    "metadata": {"repeat_index": repeat_index},
                })
                for cleanup_index, raw_cleanup in enumerate(operation.get("cleanup_steps", []) or [], start=1):
                    cleanup = _substitute(copy.deepcopy(raw_cleanup), local_variables, context=operation_id)
                    cleanup.update(_semantic_fields(operation, phase_id, _action_id(scenario_id, phase_id, operation_id, occurrence, 10000 + cleanup_index)))
                    cleanup.setdefault("optional", True)
                    actions.append(cleanup)
            actions.append({
                "type": "trace_marker", "event_type": "PHASE_DONE", "status": "success",
                "op_type": "semantic_phase", "phase_id": phase_id,
                "action_id": _action_id(scenario_id, phase_id, "PHASE", repeat_index, 9999),
                "metadata": {"repeat_index": repeat_index},
            })

    compiled = {
        "scenario_id": scenario_id,
        "description": scenario.get("description", ""),
        "validation_mode": bool(scenario.get("validation_mode", True)),
        "keep_alive_after_s": scenario.get("keep_alive_after_s", 0),
        "semantic_source": str(scenario_path),
        "actions": actions,
    }
    return CompileResult(
        scenario_id=scenario_id,
        compiled=compiled,
        action_map=action_map,
        required_apps=sorted(required_apps),
        optional_apps=sorted(optional_apps),
        side_effect_operations=sorted(set(side_effect_operations)),
    )


def write_compile_result(result: CompileResult, output: Path, action_map_output: Path, report_output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result.compiled, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = ["scenario_id", "phase_id", "operation_id", "action_id", "low_level_index", "action_type", "requested_operation", "side_effect_level"]
    action_map_output.parent.mkdir(parents=True, exist_ok=True)
    with action_map_output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(result.action_map)
    report = {
        "scenario_id": result.scenario_id,
        "compiled_path": str(output),
        "operation_count": len({row["operation_id"] for row in result.action_map}),
        "low_level_action_count": len(result.action_map),
        "required_apps": result.required_apps,
        "optional_apps": result.optional_apps,
        "side_effect_operations": result.side_effect_operations,
        "compile_status": "PASS",
    }
    report_output.parent.mkdir(parents=True, exist_ok=True)
    report_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
