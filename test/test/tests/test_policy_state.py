import tempfile
import unittest
from pathlib import Path

from memsched_exp.policy_state import capture_policy_state, verify_policy_state


class PolicyStateTest(unittest.TestCase):
    def test_capture_and_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "effective_tier_mode").write_text("apply\n", encoding="utf-8")
            (root / "effective_tier_stats").write_text("apply_compiled=1\n", encoding="utf-8")
            (root / "effective_tier_config").write_text("model_provenance model-v1\n", encoding="utf-8")
            value = verify_policy_state(
                capture_policy_state(root),
                expected_mode="apply",
                expected_apply_compiled=True,
                expected_model_provenance="model-v1",
            )
        self.assertTrue(value["valid"])
        self.assertTrue(value["apply_compiled"])

    def test_mismatch_is_invalid(self):
        value = verify_policy_state(
            {"readable": True, "mode": "shadow", "apply_compiled": False, "model_provenance": None},
            expected_mode="apply",
        )
        self.assertFalse(value["valid"])


if __name__ == "__main__":
    unittest.main()
