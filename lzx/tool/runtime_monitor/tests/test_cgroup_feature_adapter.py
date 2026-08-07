from __future__ import annotations

from runtime_monitor.region_monitor.cgroup_feature_adapter import CgroupFeatureAdapter


def test_missing_cgroup_fields_are_null_not_zero(tmp_path) -> None:
    cgroup = tmp_path / "automation-wps.scope"
    cgroup.mkdir()
    (cgroup / "memory.current").write_text("123\n", encoding="utf-8")
    (cgroup / "memory.stat").write_text("anon 10\nfile 20\n", encoding="utf-8")
    sample = CgroupFeatureAdapter().sample("automation-wps.scope", cgroup)
    assert sample.status == "ok"
    assert sample.values["memory_current"] == 123
    assert sample.values["anon"] == 10
    assert sample.values["shmem"] is None
    assert sample.availability["shmem"] == "missing"
    assert sample.values["pgscan_delta"] is None

