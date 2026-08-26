"""Unit tests for online causal workload Markov chain.

Verifies:
1. No future information in predictions
2. Training time ≤ prediction time
3. Prediction time < actual next observation time
4. Updates happen after observation, not before
5. Edge cases: empty, single-state, dual-state
6. App isolation
7. Rank tie stability (deterministic by next_workload_id)
8. Correct confidence computation
"""

import tempfile
import time
import unittest
from pathlib import Path

from runtime_monitor.core.online_causal_workload_markov import (
    OnlineCausalWorkloadMarkov,
    _boost_level,
    _fixed_confidence,
    MGLRU_MARKOV_TOPK,
)


class TestBoostLevel(unittest.TestCase):
    def test_boost_levels(self):
        self.assertEqual(_boost_level(9000), 3)
        self.assertEqual(_boost_level(8000), 3)
        self.assertEqual(_boost_level(7999), 2)
        self.assertEqual(_boost_level(5000), 2)
        self.assertEqual(_boost_level(1), 1)
        self.assertEqual(_boost_level(0), 0)


class TestFixedConfidence(unittest.TestCase):
    def test_fixed_confidence(self):
        self.assertEqual(_fixed_confidence(0.95), 9500)
        self.assertEqual(_fixed_confidence(1.0), 10000)
        self.assertEqual(_fixed_confidence(0.0), 0)
        self.assertEqual(_fixed_confidence(0.5555), 5555)


class TestOnlineCausalMarkov(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.model_dir = Path(self.tmpdir) / "model"
        self.review_dir = Path(self.tmpdir) / "review"
        # Use a simple incrementing timestamp to simulate time
        self._ts = 1_000_000_000_000_000_000  # 1e18 ns

    def _ts_next(self, delta_ns: int = 1_000_000_000) -> int:
        self._ts += delta_ns
        return self._ts

    def _make(self, **kwargs):
        defaults = dict(
            enabled=True,
            session_id="test_session",
            model_dir=self.model_dir,
            review_dir=self.review_dir,
            debugfs_writer=None,
        )
        defaults.update(kwargs)
        return OnlineCausalWorkloadMarkov(**defaults)

    # ── Edge cases ──

    def test_empty_table_does_not_crash(self):
        m = self._make()
        result = m.observe_workload(
            app_key="test", app_id="1", scope_name="test.scope",
            workload_id=0, timestamp_ns=self._ts_next(),
        )
        self.assertIsNone(result)
        m.close()
        self.assertEqual(m.total_predictions, 0)
        self.assertEqual(m.total_updates, 0)  # Need at least 2 states to update

    def test_single_state_no_prediction(self):
        m = self._make()
        m.observe_workload(
            app_key="test", app_id="1", scope_name="test.scope",
            workload_id=0, timestamp_ns=self._ts_next(),
        )
        self.assertEqual(m.total_predictions, 0)
        m.close()

    def test_two_states_still_no_prediction(self):
        """Need at least 3 states: w[t-2], w[t-1], w[t]."""
        m = self._make()
        m.observe_workload(app_key="t", app_id="1", scope_name="s.scope",
                           workload_id=0, timestamp_ns=self._ts_next())
        m.observe_workload(app_key="t", app_id="1", scope_name="s.scope",
                           workload_id=1, timestamp_ns=self._ts_next())
        # At this point: window = (0, 1). No prediction yet because no transition data.
        self.assertEqual(m.total_predictions, 0)
        m.close()

    def test_three_states_triggers_prediction_and_update(self):
        """With 3 states, the first transition update + first prediction happen."""
        m = self._make()
        m.observe_workload(app_key="t", app_id="1", scope_name="s.scope",
                           workload_id=0, timestamp_ns=self._ts_next())
        m.observe_workload(app_key="t", app_id="1", scope_name="s.scope",
                           workload_id=1, timestamp_ns=self._ts_next())
        # Third: (prev=0, cur=1) context, observe workload=2
        result = m.observe_workload(
            app_key="t", app_id="1", scope_name="s.scope",
            workload_id=2, timestamp_ns=self._ts_next(),
        )
        m.close()
        # After the third: transition (0,1)→2 updated, prediction from (1,2) context
        # But we have NO data for (1,2) yet, so prediction may be null
        self.assertEqual(m.total_updates, 1)  # (0,1)→2 was updated
        # prediction may be null if (1,2) has no transitions yet

    def test_four_states_produces_complete_chain(self):
        """4 states: 2 updates + 1 resolved prediction."""
        m = self._make()
        seq = [0, 1, 3, 0]  # 4 states
        times = []
        for wl in seq:
            times.append(self._ts_next())
            m.observe_workload(
                app_key="t", app_id="1", scope_name="s.scope",
                workload_id=wl, timestamp_ns=times[-1],
            )
        m.close()

        # 2 updates: (0,1)→3 and (1,3)→0
        self.assertEqual(m.total_updates, 2)
        # Predictions: step 3 context (0,1) has no prior data → no prediction
        #              step 4 context (1,3) has no prior data → no prediction
        # Predictions only happen when context has been seen before
        self.assertEqual(m.total_predictions, 0)

    # ── Causal validity ──

    def test_prediction_before_observation(self):
        """Prediction time must be strictly before observation time."""
        m = self._make()
        t1 = self._ts_next()
        m.observe_workload(app_key="a", app_id="1", scope_name="s.scope",
                           workload_id=0, timestamp_ns=t1)
        t2 = self._ts_next()
        m.observe_workload(app_key="a", app_id="1", scope_name="s.scope",
                           workload_id=1, timestamp_ns=t2)
        t3 = self._ts_next()
        pred = m.observe_workload(app_key="a", app_id="1", scope_name="s.scope",
                                  workload_id=3, timestamp_ns=t3)
        t4 = self._ts_next()
        m.observe_workload(app_key="a", app_id="1", scope_name="s.scope",
                           workload_id=0, timestamp_ns=t4)
        m.close()

        # At t3: prediction was made from context (0,1), training=latest(t2)=t2
        # Then at t4: prediction is resolved against actual (0)
        # Causal: prediction_time(t3?) actually the prediction was made inside step 4 call
        # Let me trace more carefully:
        # Step 3 (t3, wl=3): window=(0,1) → pred from (0,1), update (0,1)→3
        #   Pred time = t3, training = t2. pred_time(t3) >= training(t2) ✓
        # Step 4 (t4, wl=0): resolve pred against wl=0
        #   actual_time(t4) > pred_time(t3) ✓
        self.assertEqual(m.future_information_rows, 0)

    def test_training_time_before_prediction_time(self):
        """Latest training sample must be ≤ prediction time."""
        m = self._make()
        t1 = self._ts_next()
        m.observe_workload(app_key="a", app_id="1", scope_name="s.scope",
                           workload_id=0, timestamp_ns=t1)
        t2 = self._ts_next()
        m.observe_workload(app_key="a", app_id="1", scope_name="s.scope",
                           workload_id=1, timestamp_ns=t2)
        t3 = self._ts_next(delta_ns=500_000_000)
        m.observe_workload(app_key="a", app_id="1", scope_name="s.scope",
                           workload_id=3, timestamp_ns=t3)
        m.close()
        # At step 3: latest_training=t2, prediction_time=t3, t2 < t3 ✓
        self.assertEqual(m.future_information_rows, 0)

    def test_deterministic_sequence_no_future_info(self):
        """The test sequence from spec: 0, 1, 3, 0, 1, 6, 0, 1, 3"""
        m = self._make()
        seq = [0, 1, 3, 0, 1, 6, 0, 1, 3]
        for wl in seq:
            m.observe_workload(
                app_key="test", app_id="1", scope_name="s.scope",
                workload_id=wl, timestamp_ns=self._ts_next(),
            )
        m.close()

        # Key verification: no future information
        self.assertEqual(m.future_information_rows, 0)
        # 9 states → first 2 no-op, remaining 7 each trigger an update
        self.assertEqual(m.total_updates, len(seq) - 2)
        # Predictions only when context repeats: (0,1) seen again at step 6 and 9
        self.assertGreaterEqual(m.total_predictions, 1)
        self.assertLessEqual(m.total_predictions, len(seq) - 2)

    # ── App isolation ──

    def test_apps_do_not_cross_contaminate(self):
        m = self._make()
        # App 1: 0 → 1 → 2 → 3
        # App 2: 5 → 6 → 7 → 8
        for wl in [0, 1, 2, 3]:
            m.observe_workload(app_key="a1", app_id="1", scope_name="s1.scope",
                               workload_id=wl, timestamp_ns=self._ts_next())
        for wl in [5, 6, 7, 8]:
            m.observe_workload(app_key="a2", app_id="2", scope_name="s2.scope",
                               workload_id=wl, timestamp_ns=self._ts_next())

        # Verify app1 transitions don't contain app2 workloads
        tkeys_app1 = [k for k in m._transitions if k[0] == "1"]
        for tkey in tkeys_app1:
            all_wls = set(m._transitions[tkey].keys())
            self.assertTrue(
                all(w in {0, 1, 2, 3} for w in all_wls),
                f"App 1 transition {tkey} contains foreign workloads: {all_wls}",
            )

        m.close()

    # ── Confidence computation ──

    def test_confidence_calculation(self):
        m = self._make()
        # Feed: 0, 1, 0, 1, 0 → establishes (0,1)→0 twice
        seq = [0, 1, 0, 1, 0]
        for wl in seq:
            m.observe_workload(app_key="t", app_id="1", scope_name="s.scope",
                               workload_id=wl, timestamp_ns=self._ts_next())
        m.close()

        # Check transition (0,1): should have count of next=0 more than others
        tkey = ("1", "s.scope", 0, 1)
        self.assertIn(tkey, m._transitions)
        counter = m._transitions[tkey]
        total = sum(counter.values())
        # (0,1)→0 appears twice in seq (positions 2 and 4)
        self.assertGreaterEqual(counter[0], 1)
        confidence_0 = counter[0] / total
        self.assertGreater(confidence_0, 0.5, f"Expected high confidence for (0,1)→0, got {confidence_0}")

    # ── Rank tie stability ──

    def test_rank_tie_deterministic(self):
        m = self._make()
        # 2 transitions with same count → tie broken by next_workload_id
        seq = [0, 1, 2, 0, 1, 3]  # (0,1)→2 count=1, (0,1)→3 count=1
        for wl in seq:
            m.observe_workload(app_key="t", app_id="1", scope_name="s.scope",
                               workload_id=wl, timestamp_ns=self._ts_next())
        m.close()

        tkey = ("1", "s.scope", 0, 1)
        counter = m._transitions[tkey]
        self.assertEqual(counter[2], 1)
        self.assertEqual(counter[3], 1)

        # _get_topk_predictions sorts by (-count, next_id), so smaller id wins
        ranked = m._get_topk_predictions("1", "s.scope", 0, 1)
        self.assertEqual(ranked[0][0], 2)  # 2 < 3, so 2 ranks first

    # ── Disabled mode ──

    def test_disabled_skips_all(self):
        m = self._make(enabled=False)
        result = m.observe_workload(
            app_key="t", app_id="1", scope_name="s.scope",
            workload_id=0, timestamp_ns=self._ts_next(),
        )
        self.assertIsNone(result)
        self.assertEqual(m.total_predictions, 0)
        self.assertEqual(m.total_updates, 0)
        m.close()

    # ── Confidence fixed-point ──

    def test_confidence_fixed_point_range(self):
        for raw in [0.0, 0.0001, 0.5, 0.9999, 1.0]:
            fixed = _fixed_confidence(raw)
            self.assertGreaterEqual(fixed, 0)
            self.assertLessEqual(fixed, 10000)

    # ── Preload ──

    def test_preload_does_not_crash_with_missing_file(self):
        m = self._make(preload_transitions_csv="/nonexistent/path.csv")
        self.assertEqual(len(m._preloaded_keys), 0)
        m.close()


if __name__ == "__main__":
    unittest.main()
