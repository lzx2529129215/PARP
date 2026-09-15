import sys
import unittest
from pathlib import Path

import numpy as np
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'lzx/tool/operation_predictor'))
from v3.models.app_lstm_visit_explicit import explicit_features, AppLSTMVisitExplicit
from v3.models.app_lstm_visit_window import visit_probabilities, masked_loss


class ExplicitTests(unittest.TestCase):
    def test_return_recency_counts_and_absence(self):
        x = explicit_features([0, 1, 0, 2], [0, 20, 30, 45], 50, 4)
        self.assertAlmostEqual(x[0, 0], np.log1p(20)/np.log1p(3600), places=6)
        self.assertAlmostEqual(x[0, 2], np.log1p(5)/np.log1p(3600), places=6)
        self.assertAlmostEqual(x[0, 4], .1)
        self.assertEqual(x[3, 1], 0)
        self.assertEqual(x[3, 3], 0)
        self.assertEqual(x[2, 3], 0)  # Current segment has no future exit.
        self.assertAlmostEqual(x[0, 9], .05)

    def test_zero_elapsed_truncation_and_window_boundary(self):
        x = explicit_features([0, 1], [0, 60], 60, 3)
        self.assertAlmostEqual(x[0, 4], .05)
        self.assertEqual(x[1, 0], 0)
        self.assertEqual(x[1, 8], 0)
        self.assertEqual(x[0, 11], 1)
        short = explicit_features([1], [60], 60, 3)
        self.assertEqual(short[0, 1], 0)
        self.assertEqual(short[0, 4], 0)
        self.assertEqual(short[0, 11], 0)
        with self.assertRaises(ValueError):
            explicit_features([0], [61], 60, 3)

    def test_padding_invariance_monotonicity_and_gradients(self):
        torch.manual_seed(42)
        model = AppLSTMVisitExplicit(num_apps=4, num_user_groups=1, pad_id=3)
        model.eval()
        common = dict(opened_apps=torch.ones(1,4), time_feature=torch.zeros(1,3),
                      current_app=torch.tensor([1]), user_group=torch.tensor([0]),
                      explicit=torch.tensor(explicit_features([0,1],[0,20],25,4)[None]))
        def run(n):
            return model(history_apps=torch.tensor([[3]*n+[0,1]]),
                         history_durations=torch.tensor([[0.]*n+[20.,5.]]),
                         history_mask=torch.tensor([[0.]*n+[1.,1.]]), **common)
        a, b = run(3), run(18)
        torch.testing.assert_close(a,b)
        p = visit_probabilities(a)
        self.assertTrue((p[:,:,0] <= p[:,:,1]).all())
        loss = masked_loss(a,torch.zeros_like(a),torch.ones_like(a))
        loss.backward()
        self.assertTrue(torch.isfinite(loss))
        self.assertTrue(all(torch.isfinite(v.grad).all() for v in model.parameters() if v.grad is not None))


if __name__ == '__main__':
    unittest.main()
