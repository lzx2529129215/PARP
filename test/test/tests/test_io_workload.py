import tempfile
import unittest
from pathlib import Path

from workloads.io_cold_launch import create, verify


class IoWorkloadTest(unittest.TestCase):
    def test_created_file_is_materialized_and_manifested(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.bin"
            create(path, 0.00001)
            data = path.read_bytes()
            self.assertTrue(data)
            self.assertNotEqual(set(data), {0})
            self.assertTrue(verify(path, 0.00001))


if __name__ == "__main__":
    unittest.main()
