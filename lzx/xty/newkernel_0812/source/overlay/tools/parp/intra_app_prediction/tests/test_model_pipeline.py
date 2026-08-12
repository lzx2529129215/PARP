import json
import unittest
from pathlib import Path

from intra_app_prediction.evaluate import multilabel_metrics
from intra_app_prediction.feature_encoder import FeatureEncoder
from intra_app_prediction.state_cluster import LightweightKMeans


class FeatureEncoderTests(unittest.TestCase):
    def test_vocabulary_is_fitted_from_training_only(self):
        encoder = FeatureEncoder(top_k_files=1).fit([
            {"file_id": "train", "coverage_l10": [1.0, 0.0]},
        ])
        self.assertEqual(encoder.file_vocabulary, ["train"])
        encoded = encoder.transform_one({
            "file_id": "test-only", "coverage_l10": [0.5, 0.5],
            "coverage_l100_summary": [0.5, 0.5, 0.5, 0.0],
            "l1000_topk": [0.5], "file_count": 1,
            "active_file_count": 1, "anon_hot_ratio": 0.25,
            "foreground": 1, "rss_bytes": 4096,
            "operation": "EDIT",
        })
        self.assertEqual(encoded[0], 0.0)  # known-file indicator
        self.assertEqual(encoded[1], 1.0)  # UNK_FILE indicator

    def test_feature_length_is_stable(self):
        train = [{"file_id": "a", "coverage_l10": [1.0] * 10,
                  "operation": "EDIT"}]
        encoder = FeatureEncoder(top_k_files=2).fit(train)
        self.assertEqual(len(encoder.transform_one(train[0])),
                         len(encoder.transform_one({"file_id": "b"})))


class ClusterTests(unittest.TestCase):
    def test_kmeans_is_deterministic_and_serializable(self):
        points = [[0.0, 0.0], [0.1, 0.1], [9.9, 10.0], [10.0, 9.9]]
        first = LightweightKMeans(2, max_iter=20).fit(points)
        second = LightweightKMeans(2, max_iter=20).fit(points)
        self.assertEqual(first.centroids, second.centroids)
        self.assertNotEqual(first.predict_one(points[0]),
                            first.predict_one(points[-1]))
        restored = LightweightKMeans.from_dict(first.to_dict())
        self.assertEqual(restored.predict(points), first.predict(points))

    def test_invalid_k_is_rejected(self):
        with self.assertRaises(ValueError):
            LightweightKMeans(3).fit([[0.0], [1.0]])


class EvaluationTests(unittest.TestCase):
    def test_multilabel_metrics_are_bounded(self):
        metrics = multilabel_metrics(
            [{"a", "b"}, {"c"}],
            [{"a": .9, "x": .8, "b": .1}, {"c": .7}], top_k=2)
        for value in metrics.values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertEqual(metrics["hit_rate"], 1.0)

    def test_false_cold_and_hot_are_explicit(self):
        metrics = multilabel_metrics(
            [{"hot"}], [{"hot": .05, "cold": .95}], top_k=1,
            cold_threshold=.1, hot_threshold=.9)
        self.assertEqual(metrics["false_cold"], 1.0)
        self.assertEqual(metrics["false_hot"], 1.0)


class PredictionSchemaTests(unittest.TestCase):
    def test_prediction_schema_requires_observe_only_contract(self):
        path = Path(__file__).parents[1] / "prediction_schema.json"
        schema = json.loads(path.read_text())
        self.assertIn("kernel_write", schema["required"])
        self.assertEqual(schema["properties"]["kernel_write"]["const"], False)
        segment = schema["properties"]["file_segments"]["items"]
        for field in ("dev_major", "dev_minor", "inode", "file_version",
                      "partition_generation", "probability_10s_q15",
                      "probability_30s_q15", "probability_60s_q15"):
            self.assertIn(field, segment["required"])
        self.assertEqual(segment["properties"]["confidence_q15"]["maximum"],
                         32767)


if __name__ == "__main__":
    unittest.main()
