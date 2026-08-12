#!/usr/bin/env python3
import unittest

from reference_model import (Q15_ONE, anon_cold_score, assign_state,
                             file_future_score, q15_mul)
from schema import APP_BIND, APP_PRIOR, pack_app_bind, pack_app_prior


class ReferenceModelTest(unittest.TestCase):
    def test_q15(self):
        self.assertEqual(q15_mul(Q15_ONE, Q15_ONE), 32766)

    def test_state_and_unknown(self):
        self.assertEqual(assign_state([10, 12], [[9, 11], [100, 100]], 16), 0)
        self.assertEqual(assign_state([10, 12], [[9, 11], [100, 100]], 1), -1)

    def test_file_score(self):
        self.assertGreater(file_future_score(*([Q15_ONE] * 5)), 32000)

    def test_anon_needs_evidence(self):
        self.assertEqual(anon_cold_score(0, 0, 0, 0, 0, False), 0)
        self.assertEqual(anon_cold_score(0, 0, 0, 0, 0), 32766)

    def test_control_schema(self):
        prior = pack_app_prior(7, 30000, 1, 30000, 60_000_000_000, 9, 11)
        bind = pack_app_bind(13, 7, 2, 17, 19, 23, 9)
        self.assertEqual(len(prior), APP_PRIOR.size)
        self.assertEqual(len(bind), APP_BIND.size)
        self.assertEqual(APP_PRIOR.unpack(prior)[-2:], (9, 11))
        self.assertEqual(APP_BIND.unpack(bind)[-2:], (9, True))


if __name__ == "__main__":
    unittest.main()
