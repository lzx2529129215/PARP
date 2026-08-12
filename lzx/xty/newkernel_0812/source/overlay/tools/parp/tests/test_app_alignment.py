#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
import unittest

from region_schema import align_app_context


class AppAlignmentTest(unittest.TestCase):
    def setUp(self):
        self.now = 1_000_000
        self.owner = {
            "mm_cookie": 7, "domain_id": 11, "app_id": 13,
            "bind_generation": 17, "foreground_epoch_id": 19,
            "bind_expiry_ns": self.now + 100,
            "app_prior_expiry_ns": self.now + 100,
            "domain_online": True, "alive": True,
        }

    def test_exact_shared_mm(self):
        result = align_app_context([self.owner, dict(self.owner)], 7, self.now)
        self.assertEqual(result["alignment_status"], "EXACT")
        self.assertEqual(result["app_id"], 13)

    def test_conflicting_bind_is_ambiguous(self):
        other = dict(self.owner, app_id=99)
        result = align_app_context([self.owner, other], 7, self.now)
        self.assertEqual(result["alignment_status"], "AMBIGUOUS")

    def test_expiry_and_exit_rejected(self):
        expired = dict(self.owner, bind_expiry_ns=self.now)
        self.assertEqual(align_app_context([expired], 7, self.now)["alignment_status"],
                         "UNRESOLVED")
        self.assertEqual(align_app_context([], 7, self.now)["alignment_status"],
                         "STALE")

    def test_epoch_or_generation_conflict(self):
        other = dict(self.owner, bind_generation=18)
        self.assertEqual(align_app_context([self.owner, other], 7, self.now)
                         ["alignment_status"], "AMBIGUOUS")


if __name__ == "__main__":
    unittest.main()
