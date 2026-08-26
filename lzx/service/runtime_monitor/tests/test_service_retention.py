from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import prune_service_outputs as retention


class ServiceRetentionTests(unittest.TestCase):
    def test_prune_removes_only_old_valid_service_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = []
            for index in range(4):
                session = root / f"service_0123456789ab_2026082{index}_120000"
                session.mkdir()
                (session / "data.csv").write_bytes(b"x" * 32)
                valid.append(session)
            protected = root / "manual_experiment"
            protected.mkdir()
            (protected / "result.csv").write_bytes(b"important")

            removed = retention.prune(
                root,
                max_sessions=3,
                reserve_sessions=1,
                max_bytes=1024,
                min_free_bytes=0,
            )

            self.assertEqual([path.name for path in removed], [path.name for path in valid[:2]])
            self.assertFalse(valid[0].exists())
            self.assertFalse(valid[1].exists())
            self.assertTrue(valid[2].exists())
            self.assertTrue(valid[3].exists())
            self.assertTrue(protected.exists())

    def test_dry_run_does_not_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = root / "service_0123456789ab_20260820_120000"
            session.mkdir()
            removed = retention.prune(
                root,
                max_sessions=0,
                reserve_sessions=0,
                max_bytes=0,
                min_free_bytes=0,
                dry_run=True,
            )
            self.assertEqual(removed, [session.resolve()])
            self.assertTrue(session.exists())


if __name__ == "__main__":
    unittest.main()
