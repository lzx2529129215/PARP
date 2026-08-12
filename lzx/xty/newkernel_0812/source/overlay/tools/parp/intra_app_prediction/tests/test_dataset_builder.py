import csv
import json
import tempfile
import unittest
from pathlib import Path

from intra_app_prediction.synthetic_fixture import build_fixture


class DatasetBuilderIntegrationTests(unittest.TestCase):
    def test_reproducible_synthetic_dataset_has_all_resolutions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = build_fixture(root, 3, 7)
            self.assertFalse(manifest["real_application_data"])
            metadata = manifest["metadata"]
            self.assertEqual(metadata["windows"], 21)
            self.assertGreater(metadata["level10_entries"], 0)
            self.assertGreater(metadata["level100_entries"], 0)
            self.assertGreater(metadata["level1000_sparse_entries"], 0)
            for name in ("windows_10s.csv", "file_dictionary.csv",
                         "file_segments_l10.csv", "file_segments_l100.csv",
                         "file_segments_l1000.jsonl", "anon_windows_10s.csv",
                         "window_operation_alignment.csv", "schema.json"):
                self.assertTrue((root / "dataset" / name).is_file(), name)
            audit = json.loads((root / "dataset/splits/split_audit.json").read_text())
            self.assertEqual(audit["status"], "PASS")
            with (root / "dataset/file_segments_l10.csv").open() as stream:
                coverages = [float(row["coverage_ratio"])
                             for row in csv.DictReader(stream)]
            self.assertTrue(all(0 <= value <= 1 for value in coverages))


if __name__ == "__main__":
    unittest.main()
