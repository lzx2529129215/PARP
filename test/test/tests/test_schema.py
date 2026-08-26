import unittest

from memsched_exp.schema import RUN_SCHEMA_VERSION, build_manifest, validate_manifest


class SchemaTest(unittest.TestCase):
    def test_formal_manifest(self):
        value = build_manifest(
            variant="baseline",
            scenario="demo",
            seed=1,
            repetition=1,
            cache_state="warm",
            environment_hash="abc",
        )
        self.assertEqual(value["schema_version"], RUN_SCHEMA_VERSION)
        self.assertEqual(validate_manifest(value), [])

    def test_missing_environment_hash_is_invalid(self):
        value = {
            "schema_version": RUN_SCHEMA_VERSION,
            "variant": "candidate",
            "scenario": "demo",
            "seed": 1,
            "repetition": 1,
            "cache_state": "warm",
        }
        self.assertIn("environment_hash is required for formal paired comparison", validate_manifest(value))


if __name__ == "__main__":
    unittest.main()
