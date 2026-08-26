import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memsched_exp import cache_state


class CacheStateTest(unittest.TestCase):
    def test_evict_requires_proven_low_residency(self):
        before = {"supported": True, "resident_ratio": 0.8}
        after = {"supported": True, "resident_ratio": 0.005}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.bin"
            path.write_bytes(b"x" * 4096)
            with (
                patch.object(cache_state, "file_residency", side_effect=[before, after]),
                patch.object(cache_state.os, "name", "posix"),
                patch.object(cache_state.os, "posix_fadvise", create=True) as advise,
                patch.object(cache_state.os, "POSIX_FADV_DONTNEED", 4, create=True),
                patch.object(cache_state.time, "sleep"),
            ):
                result = cache_state.evict_file_cache(path, max_resident_ratio=0.01)
        self.assertTrue(result["valid"])
        advise.assert_called_once()


if __name__ == "__main__":
    unittest.main()
