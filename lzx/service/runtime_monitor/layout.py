"""Canonical paths for the post-migration PARP repository layout."""

# lzx-note: Keep service, test automation, and model assets independently rooted.
from __future__ import annotations

import os
from pathlib import Path


RUNTIME_ROOT = Path(__file__).resolve().parent
SERVICE_ROOT = RUNTIME_ROOT.parent
LZX_ROOT = SERVICE_ROOT.parent
REPO_ROOT = LZX_ROOT.parent
TEST_ROOT = Path(os.environ.get("PARP_TEST_ROOT", REPO_ROOT / "test")).resolve()
AUTOMATION_ROOT = Path(os.environ.get("PARP_AUTOMATION_ROOT", TEST_ROOT / "automation")).resolve()
AUTOMATION_CONFIG_ROOT = Path(os.environ.get("PARP_AUTOMATION_CONFIG_ROOT", TEST_ROOT / "configs" / "automation")).resolve()
RUNTIME_CONFIG_ROOT = Path(os.environ.get("PARP_RUNTIME_CONFIG_ROOT", SERVICE_ROOT / "configs" / "runtime")).resolve()
SERVICE_OUTPUT_ROOT = Path(os.environ.get("PARP_SERVICE_OUTPUT_ROOT", SERVICE_ROOT / "outputs" / "runtime_monitor")).resolve()
TEST_OUTPUT_ROOT = Path(os.environ.get("PARP_TEST_OUTPUT_ROOT", TEST_ROOT / "outputs")).resolve()
TOOL_ROOT = LZX_ROOT / "tool"
OPERATION_PREDICTOR_ROOT = Path(
    os.environ.get("OPERATION_PREDICTOR_ROOT", TOOL_ROOT / "operation_predictor")
).resolve()
