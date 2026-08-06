import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "runtime_monitor/scripts"
sys.path.insert(0, str(SCRIPTS))

from audit_common import event_type, resolve_input_path  # noqa: E402


class PipelineAuditHelpersTest(unittest.TestCase):
    def test_resolve_input_path_is_cwd_independent(self):
        old = os.getcwd()
        try:
            os.chdir("/tmp")
            self.assertEqual(resolve_input_path("runtime_monitor").parent, ROOT)
        finally:
            os.chdir(old)

    def test_no_fallback_is_used_by_input_directories(self):
        source = (SCRIPTS / "run_pipeline_intermediate_audit.py").read_text(encoding="utf-8")
        self.assertNotIn("latest_session", source)
        self.assertNotIn("fallback_share", source)
        self.assertIn("fallback_used", source)

    def test_event_type_compatibility(self):
        self.assertEqual(event_type({"event_type": "workload_update"}), "workload_update")
        self.assertEqual(event_type({"write_type": "markov_set"}), "markov_set")
        self.assertEqual(event_type({"type": "app_bind"}), "app_bind")

    def test_prediction_id_identity_from_real_file(self):
        path = ROOT / "outputs/runtime_monitor/session_unified_pipeline_20260713_115505/model/workload_markov_online_predictions.csv"
        if not path.exists():
            self.skipTest("实验原始数据未提供")
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        ids = [row["prediction_id"] for row in rows]
        self.assertEqual(len(ids), len(set(ids)))

    def test_mapping_keeps_model_and_runtime_spaces(self):
        config = json.loads((ROOT / "configs/runtime/runtime_app_scope.json").read_text(encoding="utf-8"))
        vocab = json.loads((ROOT / "operation_predictor/data/vocab/app_vocab_duration.json").read_text(encoding="utf-8"))
        apps = {item["app_key"]: item for item in config["apps"]}
        self.assertEqual(vocab[apps["QQ"]["vocab_name"]], 2)
        self.assertEqual(apps["QQ"]["app_id"], 2)
        self.assertNotEqual(vocab[apps["FILES"]["vocab_name"]], apps["FILES"]["app_id"])

    def test_scripts_import_from_tmp(self):
        script = SCRIPTS / "run_pipeline_intermediate_audit.py"
        result = subprocess.run([sys.executable, str(script), "--help"], cwd="/tmp", text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
