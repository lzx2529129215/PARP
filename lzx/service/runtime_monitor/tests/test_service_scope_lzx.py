import json
import unittest
from pathlib import Path
import sys


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RUNTIME_ROOT))

from core.app_mapper import AppMapper, ProcessIdentity
from collectors.cgroup import _exact_cgroup_paths
from collectors.process import ProcessSample


class ResidentServiceScopeTest(unittest.TestCase):
    def test_resident_scope_is_cross_slice_and_has_expanded_apps(self) -> None:
        # lzx-note: The boot service must observe both test and automation slices.
        path = Path(__file__).resolve().parents[2] / "configs" / "runtime" / "runtime_app_scope.service.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["slice"], "")
        self.assertEqual(len(data["apps"]), 15)
        self.assertIn("lzx-note", data["implementation_note"])

    def test_solitaire_short_name_does_not_match_systemd_resolved(self) -> None:
        # lzx-note: Resident discovery must not contaminate Solitaire metrics.
        mapper = AppMapper(
            {"apps": {"SOLITAIRE": {"keywords": ["sol", "aisleriot"]}}},
            target_app="SOLITAIRE",
        )
        resolved = ProcessIdentity(
            pid=1,
            tgid=1,
            comm="systemd-resolve",
            exe_path="/usr/lib/systemd/systemd-resolved",
        )
        solitaire = ProcessIdentity(
            pid=2,
            tgid=2,
            comm="sol",
            exe_path="/usr/games/sol",
        )
        self.assertEqual(mapper.map_process(resolved), "")
        self.assertEqual(mapper.map_process(solitaire), "SOLITAIRE")

    def test_app_resources_keep_distinct_leaf_cgroups(self) -> None:
        # lzx-note: Evince and evinced must not collapse to their shared parent slice.
        def sample(pid: int, cgroup: str) -> ProcessSample:
            identity = ProcessIdentity(
                pid=pid, tgid=pid, comm="evince", exe_path="/usr/bin/evince",
                cgroup_path=cgroup,
            )
            return ProcessSample(identity=identity, app_id="EVINCE", io={}, status={}, stat={})

        paths = _exact_cgroup_paths([
            sample(1, "/user.slice/test.slice/automation-evince.scope"),
            sample(2, "/user.slice/app.slice/dbus-evince.service"),
            sample(3, "/user.slice/test.slice/automation-evince.scope"),
        ])
        rendered = [str(path) for path in paths]
        self.assertEqual(len(rendered), 2)
        self.assertFalse(any(path.endswith("/user.slice") for path in rendered))


if __name__ == "__main__":
    unittest.main()
