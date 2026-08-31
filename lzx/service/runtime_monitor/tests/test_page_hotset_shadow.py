from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from runtime_monitor.core.page_hotset_shadow import (
    BASE_ONLY,
    UNKNOWN,
    PageHotsetShadow,
    PageId,
    PageSnapshot,
    jaccard_similarity,
    pages_to_ranges,
    train_page_hotset_model,
)


PAGE_SIZE = 4096


def _page(inode: int, index: int) -> PageId:
    return PageId(8, 1, inode, index)


def _snapshot(index: int, pages: set[PageId], *, epoch: str = "APP:1") -> PageSnapshot:
    return PageSnapshot(
        session_id="session",
        app_id="APP",
        foreground_epoch_id=epoch,
        window_start_ns=index * 1_000_000_000,
        window_end_ns=(index + 1) * 1_000_000_000,
        page_size=PAGE_SIZE,
        pages=frozenset(pages),
        page_access_events=len(pages),
        repeated_page_hits=0,
        valid=True,
    )


def _cluster_snapshots(cluster_count: int, members: int = 12) -> list[PageSnapshot]:
    base = _page(1, 0)
    rows: list[PageSnapshot] = []
    for cluster in range(cluster_count):
        core = {_page(10 + cluster, cluster * 100 + offset) for offset in range(5)}
        for member in range(members):
            rows.append(
                _snapshot(
                    len(rows),
                    {base, *core, _page(100 + member, member)},
                )
            )
    return rows


class PageIdentityTests(unittest.TestCase):
    def test_jaccard_and_range_compression(self) -> None:
        pages = frozenset({_page(7, 1), _page(7, 2), _page(7, 4), _page(8, 0)})
        ranges = pages_to_ranges(pages, PAGE_SIZE)
        self.assertEqual(
            [(row["inode"], row["start_page_index"], row["page_count"]) for row in ranges],
            [(7, 1, 2), (7, 4, 1), (8, 0, 1)],
        )
        self.assertEqual(jaccard_similarity(frozenset(), frozenset()), 1.0)
        self.assertEqual(
            jaccard_similarity(frozenset({_page(1, 0)}), frozenset({_page(1, 0), _page(1, 1)})),
            0.5,
        )


class PageModelTests(unittest.TestCase):
    def test_two_well_separated_clusters_select_k_two(self) -> None:
        model = train_page_hotset_model(
            app_id="APP",
            model_version="app-v1",
            page_size=PAGE_SIZE,
            snapshots=_cluster_snapshots(2),
            base_hot_coverage=0.8,
            bucket_hot_coverage=0.5,
        )
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.selected_k, 2)
        self.assertEqual([bucket.member_count for bucket in model.buckets], [12, 12])

    def test_four_cluster_model_extracts_base_and_bucket_hot_pages(self) -> None:
        rows = _cluster_snapshots(4)
        model = train_page_hotset_model(
            app_id="APP",
            model_version="app-v1",
            page_size=PAGE_SIZE,
            snapshots=rows,
            base_hot_coverage=0.8,
            bucket_hot_coverage=0.5,
        )
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.selected_k, 4)
        self.assertEqual(model.base_hot_pages, frozenset({_page(1, 0)}))
        self.assertEqual([bucket.member_count for bucket in model.buckets], [12] * 4)
        self.assertTrue(all(len(bucket.hot_pages) == 5 for bucket in model.buckets))
        assigned, similarity, reason = model.classify(rows[0].pages)
        self.assertNotEqual(assigned, UNKNOWN)
        self.assertGreater(similarity, 0.7)
        self.assertEqual(reason, "ASSIGNED")
        unknown, similarity, reason = model.classify(
            frozenset({_page(1, 0), _page(999, 999)})
        )
        self.assertEqual(unknown, UNKNOWN)
        self.assertEqual(similarity, 0.0)
        self.assertEqual(reason, "BELOW_REJECTION_THRESHOLD")
        base_only, similarity, _ = model.classify(frozenset({_page(1, 0)}))
        self.assertEqual(base_only, BASE_ONLY)
        self.assertEqual(similarity, 1.0)

    def test_five_well_separated_clusters_select_k_five(self) -> None:
        model = train_page_hotset_model(
            app_id="APP",
            model_version="app-v1",
            page_size=PAGE_SIZE,
            snapshots=_cluster_snapshots(5),
            base_hot_coverage=0.8,
            bucket_hot_coverage=0.5,
        )
        self.assertIsNotNone(model)
        assert model is not None
        self.assertEqual(model.selected_k, 5)

    def test_invalid_and_empty_windows_break_markov_context(self) -> None:
        rows = _cluster_snapshots(4)
        invalid = PageSnapshot(
            session_id="session",
            app_id="APP",
            foreground_epoch_id="APP:1",
            window_start_ns=100_000_000_000,
            window_end_ns=101_000_000_000,
            page_size=PAGE_SIZE,
            pages=frozenset(),
            page_access_events=0,
            repeated_page_hits=0,
            valid=True,
            invalid_reasons=("NO_PAGE_ACCESS",),
        )
        model = train_page_hotset_model(
            app_id="APP",
            model_version="app-v1",
            page_size=PAGE_SIZE,
            snapshots=[*rows[:24], invalid, *rows[24:]],
            base_hot_coverage=0.8,
            bucket_hot_coverage=0.5,
        )
        self.assertIsNotNone(model)
        assert model is not None
        # The empty row is not training-eligible and cannot become a transition state.
        all_next = {
            value
            for counts in model.first_order.values()
            for value in counts
        }
        self.assertNotIn(UNKNOWN, all_next)


class PageWindowAndPredictionTests(unittest.TestCase):
    def _make_shadow(self, root: Path, **kwargs: object) -> PageHotsetShadow:
        defaults: dict[str, object] = {
            "session_id": "session",
            "model_dir": root / "model",
            "prediction_dir": root / "prediction",
            "review_dir": root / "review",
            "window_ms": 1000,
            "lateness_ms": 0,
            "warmup_windows": 40,
            "retrain_windows": 20,
            "history_windows": 100,
            "base_hot_coverage": 0.8,
            "bucket_hot_coverage": 0.5,
            "background_training": False,
            "page_size": PAGE_SIZE,
            "minimum_resolved_predictions": 1,
        }
        defaults.update(kwargs)
        return PageHotsetShadow(**defaults)  # type: ignore[arg-type]

    @staticmethod
    def _event(timestamp_ns: int, app: str, inode: int, offset: int, size: int) -> dict[str, object]:
        return {
            "event": "page_access",
            "ts_ns": timestamp_ns,
            "app": app,
            "device_major": 8,
            "device_minor": 1,
            "inode": inode,
            "offset": offset,
            "size": size,
        }

    def test_event_time_window_cross_page_dedup_and_empty_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow = self._make_shadow(root)
            start = 10_000_000_000
            shadow.observe_foreground(start, "APP")
            shadow.set_source_available(start, True, "READY")
            self.assertTrue(shadow.observe_page_access(
                self._event(start + 100, "APP", 7, PAGE_SIZE - 1, 2)
            ))
            self.assertTrue(shadow.observe_page_access(
                self._event(start + 200, "APP", 7, 0, PAGE_SIZE)
            ))
            shadow.observe_page_access(self._event(start + 300, "BACKGROUND", 9, 0, PAGE_SIZE))
            shadow.advance(start + 1_000_000_000)
            shadow.advance(start + 2_000_000_000)
            shadow.close(start + 2_000_000_000)

            rows = [
                json.loads(line)
                for line in (root / "model" / "page_snapshots.jsonl").read_text().splitlines()
            ]
            snapshots = [row for row in rows if row["record_type"] == "SNAPSHOT"]
            self.assertEqual(snapshots[0]["page_count"], 2)
            self.assertEqual(snapshots[0]["page_access_events"], 2)
            self.assertEqual(snapshots[0]["repeated_page_hits"], 1)
            self.assertTrue(snapshots[0]["valid"])
            self.assertEqual(snapshots[1]["page_count"], 0)
            self.assertIn("NO_PAGE_ACCESS", snapshots[1]["invalid_reasons"])
            self.assertFalse(snapshots[1]["model_eligible"])

    def test_compressed_page_window_preserves_ranges_and_audit_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow = self._make_shadow(root)
            start = 11_000_000_000
            shadow.observe_foreground(start, "APP")
            shadow.set_source_available(start, True, "READY")
            self.assertTrue(shadow.observe_page_window({
                "event": "page_access_window",
                "app": "APP",
                "window_start_ns": start,
                "window_end_ns": start + 1_000_000_000,
                "page_size": PAGE_SIZE,
                "page_access_events": 7,
                "repeated_page_hits": 4,
                "page_ranges": [{
                    "device_major": 8,
                    "device_minor": 1,
                    "inode": 77,
                    "start_page_index": 3,
                    "page_count": 3,
                }],
            }))
            shadow.advance(start + 1_000_000_000)
            shadow.close(start + 1_000_000_000)
            rows = [
                json.loads(line)
                for line in (root / "model/page_snapshots.jsonl").read_text().splitlines()
                if json.loads(line).get("record_type") == "SNAPSHOT"
            ]
            self.assertEqual(rows[0]["page_count"], 3)
            self.assertEqual(rows[0]["page_access_events"], 7)
            self.assertEqual(rows[0]["repeated_page_hits"], 4)
            self.assertTrue(rows[0]["model_eligible"])

    def test_foreground_switch_and_source_gap_invalidate_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow = self._make_shadow(root)
            start = 20_000_000_000
            shadow.observe_foreground(start, "APP")
            shadow.set_source_available(start, True, "READY")
            shadow.observe_foreground(start + 500_000_000, "OTHER")
            shadow.observe_page_access(self._event(start + 100, "APP", 7, 0, PAGE_SIZE))
            shadow.mark_source_gap(start + 1_100_000_000, "DELIVERY_GAP")
            shadow.observe_page_access(
                self._event(start + 1_200_000_000, "OTHER", 8, 0, PAGE_SIZE)
            )
            shadow.advance(start + 2_000_000_000)
            shadow.close(start + 2_000_000_000)
            snapshots = [
                json.loads(line)
                for line in (root / "model" / "page_snapshots.jsonl").read_text().splitlines()
                if json.loads(line).get("record_type") == "SNAPSHOT"
            ]
            self.assertIn("FOREGROUND_CHANGED", snapshots[0]["invalid_reasons"])
            self.assertIn("DELIVERY_GAP", snapshots[1]["invalid_reasons"])
            self.assertFalse(snapshots[0]["valid"])
            self.assertFalse(snapshots[1]["valid"])

    def test_online_prediction_is_causal_and_resolves_on_next_distinct_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow = self._make_shadow(root)
            start = 100_000_000_000
            shadow.observe_foreground(start, "APP")
            shadow.set_source_available(start, True, "READY")
            base = _page(1, 0)
            cores = [
                {_page(10 + cluster, cluster * 100 + offset) for offset in range(5)}
                for cluster in range(4)
            ]
            for window in range(43):
                pages = {base, *cores[window % 4]}
                for page in pages:
                    shadow.observe_page_access(self._event(
                        start + window * 1_000_000_000 + 100,
                        "APP",
                        page.inode,
                        page.page_index * PAGE_SIZE,
                        PAGE_SIZE,
                    ))
                shadow.advance(start + (window + 1) * 1_000_000_000)
            shadow.close(start + 43_000_000_000)

            predictions = [
                json.loads(line)
                for line in (root / "prediction" / "page_hotset_predictions.jsonl").read_text().splitlines()
            ]
            outcomes = [
                json.loads(line)
                for line in (root / "prediction" / "page_hotset_outcomes.jsonl").read_text().splitlines()
            ]
            resolved = [row for row in outcomes if row.get("resolution_status") == "RESOLVED"]
            self.assertGreaterEqual(len(predictions), 2)
            self.assertGreaterEqual(len(resolved), 1)
            self.assertTrue(all(row["causal_valid"] for row in resolved))
            self.assertTrue(all(row["bucket_hit"] for row in resolved))
            self.assertTrue(all(row["page_recall"] >= 0.8 for row in resolved))
            self.assertTrue((root / "review" / "page_hotset_summary.md").exists())
            self.assertTrue(any((root / "model" / "page_hotset_models").rglob("*.json")))

    def test_background_training_process_publishes_versioned_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow = self._make_shadow(root, background_training=True)
            start = 200_000_000_000
            shadow.observe_foreground(start, "APP")
            shadow.set_source_available(start, True, "READY")
            for window in range(40):
                cluster = window % 4
                for offset in range(5):
                    shadow.observe_page_access(self._event(
                        start + window * 1_000_000_000 + offset + 1,
                        "APP",
                        20 + cluster,
                        (cluster * 100 + offset) * PAGE_SIZE,
                        PAGE_SIZE,
                    ))
                shadow.advance(start + (window + 1) * 1_000_000_000)
            shadow.close(start + 40_000_000_000)
            models = list((root / "model" / "page_hotset_models").rglob("*.json"))
            self.assertEqual(len(models), 1)
            payload = json.loads(models[0].read_text())
            self.assertEqual(payload["app_id"], "APP")
            self.assertEqual(payload["selected_k"], 4)


if __name__ == "__main__":
    unittest.main()
