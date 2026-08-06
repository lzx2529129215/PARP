from runtime_monitor.core.app_bind_table import AppBindTable, binding_expired, jiffies_after


def test_bind_first_insert() -> None:
    table = AppBindTable(2)
    assert table.upsert(1, 10, 100, 0) == "insert"
    assert table.stats.insert == 1


def test_bind_same_key_refresh_no_new_slot() -> None:
    table = AppBindTable(2)
    table.upsert(1, 10, 100, 0)
    assert table.upsert(1, 10, 100, 10) == "refresh"
    assert table.stats.refresh == 1
    assert table.active_entries(10) == 1


def test_bind_same_cgroup_replace() -> None:
    table = AppBindTable(2)
    table.upsert(1, 10, 100, 0)
    assert table.upsert(2, 10, 100, 1) == "replace_cgroup"
    assert table.slots[0].app_id == 2


def test_bind_same_app_replace() -> None:
    table = AppBindTable(2)
    table.upsert(1, 10, 100, 0)
    assert table.upsert(1, 20, 100, 1) == "replace_app"
    assert table.slots[0].cgroup_id == 20


def test_bind_expired_slot_reused() -> None:
    table = AppBindTable(1)
    table.upsert(1, 10, 10, 0)
    assert table.upsert(2, 20, 100, 11) == "expired_reuse"


def test_bind_empty_slot_used() -> None:
    table = AppBindTable(2)
    table.upsert(1, 10, 100, 0)
    assert table.upsert(2, 20, 100, 1) == "insert"


def test_bind_enospc_only_all_live_unique() -> None:
    table = AppBindTable(2)
    table.upsert(1, 10, 100, 0)
    table.upsert(2, 20, 100, 0)
    assert table.upsert(3, 30, 100, 1) == "enospc"


def test_bind_ttl_refresh() -> None:
    table = AppBindTable(1)
    table.upsert(1, 10, 10, 0)
    table.upsert(1, 10, 10, 9)
    assert not binding_expired(table.slots[0], 18)


def test_bind_ttl_wrap_safe() -> None:
    assert not jiffies_after(2, 5)
    assert jiffies_after(6, 5)
    table = AppBindTable(1)
    table.upsert(1, 10, 10, (1 << 32) - 5)
    assert not binding_expired(table.slots[0], 3)
    assert binding_expired(table.slots[0], 6)


def test_clear_bind_only() -> None:
    table = AppBindTable(1)
    table.upsert(1, 10, 100, 0)
    table.clear()
    assert table.active_entries(1) == 0
    assert table.stats.insert == 1


def test_clear_all_includes_bind() -> None:
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "MGLRU-test/v0/mglru_kernel_transfer/linux-hwe-6.17-6.17.0/mm/vmscan.c"
    ).read_text(encoding="utf-8")
    clear_all = source[source.index("static void mglru_markov_clear_all_locked"):
                       source.index("static void mglru_markov_clear_histories_locked")]
    assert "mglru_lstm_clear_bindings_locked();" in clear_all


def test_bind_stats_invariant() -> None:
    table = AppBindTable(2)
    table.upsert(1, 10, 100, 0)
    table.upsert(1, 10, 100, 1)
    table.upsert(2, 10, 100, 2)
    assert table.stats.write_calls == 3
    assert table.stats.refresh == 1
    assert table.stats.replace_cgroup == 1


def test_bind_high_watermark() -> None:
    table = AppBindTable(3)
    table.upsert(1, 10, 100, 0)
    table.upsert(2, 20, 100, 1)
    assert table.stats.high_watermark == 2
