from __future__ import annotations

import argparse
import hashlib
import json
import platform
import time
from pathlib import Path
from typing import Any, Iterable


RUN_SCHEMA_VERSION = 4
VARIANTS = {"baseline", "candidate"}
CACHE_STATES = {"process-cold", "strict-cold", "warm", "unspecified"}


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def environment_fingerprint(metadata: dict[str, Any]) -> str:
    """Hash only environment fields that must be equal in a paired comparison."""
    stable = {
        "machine": metadata.get("machine"),
        "cpu_model": metadata.get("cpu_model"),
        "cpu_count": metadata.get("cpu_count"),
        "page_size": metadata.get("page_size"),
        "mem_total_kib": metadata.get("mem_total_kib"),
        "swap": metadata.get("swap"),
        "zram": metadata.get("zram"),
        "vm_sysctls": metadata.get("vm_sysctls"),
        "transparent_hugepage": metadata.get("transparent_hugepage"),
        "cpu_governors": metadata.get("cpu_governors"),
        "cpu_frequency_constraints": metadata.get("cpu_frequency_constraints"),
        "numa_nodes_online": metadata.get("numa_nodes_online"),
        "result_filesystem": (metadata.get("result_storage") or {}).get("filesystem"),
    }
    return hashlib.sha256(_canonical(stable)).hexdigest()


def workload_fingerprint(paths: Iterable[str | Path]) -> str | None:
    rows: list[dict[str, str]] = []
    for raw in paths:
        path = Path(raw)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append({"name": path.name, "sha256": digest})
    return hashlib.sha256(_canonical(sorted(rows, key=lambda row: row["name"]))).hexdigest() if rows else None


def validate_manifest(value: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if value.get("schema_version") != RUN_SCHEMA_VERSION:
        reasons.append(f"schema_version must be {RUN_SCHEMA_VERSION}")
    if value.get("variant") not in VARIANTS:
        reasons.append("variant must be baseline or candidate")
    if not value.get("scenario"):
        reasons.append("scenario is required")
    for key in ("seed", "repetition"):
        try:
            parsed = int(value[key])
            minimum = 0 if key == "seed" else 1
            if parsed < minimum:
                reasons.append(f"{key} is out of range")
        except (KeyError, TypeError, ValueError):
            reasons.append(f"{key} must be an integer")
    if value.get("cache_state") not in CACHE_STATES:
        reasons.append(f"cache_state must be one of {sorted(CACHE_STATES)}")
    if not value.get("environment_hash"):
        reasons.append("environment_hash is required for formal paired comparison")
    return reasons


def pair_key(value: dict[str, Any]) -> tuple[Any, ...]:
    return (
        value.get("scenario"),
        value.get("seed"),
        value.get("repetition"),
        value.get("cache_state"),
        value.get("workload_hash"),
    )


def build_manifest(
    *,
    variant: str,
    scenario: str,
    seed: int,
    repetition: int,
    cache_state: str,
    environment_hash: str,
    workload_hash: str | None = None,
    kernel_commit: str | None = None,
    policy_mode: str | None = None,
    apply_compiled: bool | None = None,
    model_provenance: str | None = None,
    policy_state_hash: str | None = None,
) -> dict[str, Any]:
    value = {
        "schema_version": RUN_SCHEMA_VERSION,
        "variant": variant,
        "scenario": scenario,
        "seed": int(seed),
        "repetition": int(repetition),
        "cache_state": cache_state,
        "environment_hash": environment_hash,
        "workload_hash": workload_hash,
        "kernel_release": platform.release(),
        "kernel_commit": kernel_commit,
        "policy": {
            "mode": policy_mode,
            "apply_compiled": apply_compiled,
            "model_provenance": model_provenance,
            "state_hash": policy_state_hash,
        },
        "created_realtime_ns": time.time_ns(),
    }
    reasons = validate_manifest(value)
    if reasons:
        raise ValueError("; ".join(reasons))
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a versioned experiment run manifest")
    parser.add_argument("--variant", choices=sorted(VARIANTS), required=True)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--repetition", type=int, required=True)
    parser.add_argument("--cache-state", choices=sorted(CACHE_STATES), required=True)
    parser.add_argument("--metadata-file")
    parser.add_argument("--workload-file", action="append", default=[])
    parser.add_argument("--kernel-commit")
    parser.add_argument("--policy-mode")
    parser.add_argument("--apply-compiled", choices=("true", "false", "unknown"), default="unknown")
    parser.add_argument("--model-provenance")
    parser.add_argument("--policy-state-file")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.metadata_file:
        metadata = json.loads(Path(args.metadata_file).read_text(encoding="utf-8"))
    else:
        from .snapshot import host_metadata

        metadata = host_metadata(Path(args.output).parent)
    apply_compiled = None if args.apply_compiled == "unknown" else args.apply_compiled == "true"
    policy_state = None
    if args.policy_state_file:
        policy_state = json.loads(Path(args.policy_state_file).read_text(encoding="utf-8"))
        if policy_state.get("valid") is False:
            raise ValueError("policy state verification failed: " + "; ".join(policy_state.get("invalid_reasons", [])))
    value = build_manifest(
        variant=args.variant,
        scenario=args.scenario,
        seed=args.seed,
        repetition=args.repetition,
        cache_state=args.cache_state,
        environment_hash=environment_fingerprint(metadata),
        workload_hash=workload_fingerprint(args.workload_file),
        kernel_commit=args.kernel_commit,
        policy_mode=(policy_state or {}).get("mode", args.policy_mode),
        apply_compiled=(policy_state or {}).get("apply_compiled", apply_compiled),
        model_provenance=(policy_state or {}).get("model_provenance", args.model_provenance),
        policy_state_hash=(policy_state or {}).get("raw_sha256"),
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
