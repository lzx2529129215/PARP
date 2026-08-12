#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
from __future__ import annotations

import unittest

from tools.parp.frontier_score.replay_audit import (
    FORBIDDEN_PROXY_FIELDS,
    REQUIRED_CANDIDATE_FIELDS,
    REQUIRED_RECLAIM_FIELDS,
    audit_fields,
)


class ReplayAuditTests(unittest.TestCase):
    def test_proxy_schema_is_rejected(self):
        result = audit_fields({"generation_proxy", "tier_proxy", "age"})
        self.assertFalse(result["supported"])
        self.assertEqual(result["proxy_fields_present_but_not_accepted"],
                         ["generation_proxy", "tier_proxy"])

    def test_complete_real_schema_is_supported(self):
        result = audit_fields(REQUIRED_CANDIDATE_FIELDS |
                              REQUIRED_RECLAIM_FIELDS)
        self.assertTrue(result["supported"])
        self.assertEqual(result["missing_candidate_fields"], [])
        self.assertEqual(result["missing_reclaim_fields"], [])

    def test_proxy_fields_never_satisfy_real_fields(self):
        result = audit_fields(FORBIDDEN_PROXY_FIELDS)
        self.assertEqual(len(result["missing_candidate_fields"]),
                         len(REQUIRED_CANDIDATE_FIELDS))
        self.assertEqual(len(result["missing_reclaim_fields"]),
                         len(REQUIRED_RECLAIM_FIELDS))


if __name__ == "__main__":
    unittest.main()
