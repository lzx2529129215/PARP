import unittest

from memsched_exp.compare import compare_rows, confidence_conclusion


def row(variant, pair_id, value, environment="same"):
    return {
        "variant": variant,
        "pair_id": pair_id,
        "scenario": "demo",
        "environment_hash": environment,
        "measurement_valid": True,
        "launch_latency_ms": value,
    }


class CompareTest(unittest.TestCase):
    def test_paired_statistics_and_improvement(self):
        value = compare_rows(
            [
                row("baseline", "p1", 100),
                row("candidate", "p1", 80),
                row("baseline", "p2", 120),
                row("candidate", "p2", 90),
            ],
            iterations=100,
            bootstrap_seed=1,
        )
        metric = next(item for item in value["metrics"] if item["metric"] == "launch_latency_ms")
        self.assertEqual(value["paired_runs"], 2)
        self.assertEqual(metric["baseline"]["median"], 110)
        self.assertAlmostEqual(metric["improvement_percent_from_means"], 22.7272727)
        self.assertTrue(metric["statistically_significant_95ci"])
        self.assertTrue(metric["directionally_favorable_95ci"])
        self.assertEqual(metric["confidence_conclusion"], "significant_improvement")

    def test_environment_mismatch_is_not_paired(self):
        value = compare_rows(
            [row("baseline", "p1", 100, "a"), row("candidate", "p1", 80, "b")],
            iterations=10,
        )
        self.assertFalse(value["valid"])
        self.assertEqual(value["skipped"]["environment_mismatch"], 1)

    def test_confidence_interval_conclusions_are_direction_aware(self):
        self.assertEqual(
            confidence_conclusion([-3.0, -1.0], "lower")["confidence_conclusion"],
            "significant_improvement",
        )
        self.assertEqual(
            confidence_conclusion([1.0, 3.0], "lower")["confidence_conclusion"],
            "significant_regression",
        )
        self.assertEqual(
            confidence_conclusion([-1.0, 1.0], "lower")["confidence_conclusion"],
            "no_significant_difference",
        )


if __name__ == "__main__":
    unittest.main()
