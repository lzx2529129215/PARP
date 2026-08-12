"""Bounded page-state feature encoding with a train-only vocabulary."""

from collections import Counter
import math


OPERATIONS = ("IDLE", "OPEN", "READ", "SCROLL", "EDIT", "SAVE",
              "SEARCH", "MINIMIZE", "RESTORE", "CLOSE", "MIXED")


def _fixed(values, length):
    values = [float(value) for value in (values or ())[:length]]
    return values + [0.0] * (length - len(values))


class FeatureEncoder:
    """Encode one file/window row without learning from validation or test."""

    def __init__(self, top_k_files=16, top_k_l1000=16):
        self.top_k_files = int(top_k_files)
        self.top_k_l1000 = int(top_k_l1000)
        self.file_vocabulary = []
        self.operation_vocabulary = list(OPERATIONS)
        self.fitted = False

    def fit(self, rows):
        rows = list(rows)
        counts = Counter(str(row.get("file_id", "")) for row in rows
                         if row.get("file_id"))
        self.file_vocabulary = [key for key, _ in sorted(
            counts.items(), key=lambda item: (-item[1], item[0]))
            [:self.top_k_files]]
        self.fitted = True
        return self

    def transform_one(self, row):
        if not self.fitted:
            raise RuntimeError("encoder must be fitted on training data")
        file_id = str(row.get("file_id", ""))
        known = file_id in self.file_vocabulary
        file_one_hot = [1.0 if file_id == candidate else 0.0
                        for candidate in self.file_vocabulary]
        file_one_hot += [0.0] * (self.top_k_files - len(file_one_hot))
        operation = str(row.get("operation", "")).upper()
        operation_one_hot = [1.0 if operation == item else 0.0
                             for item in self.operation_vocabulary]
        return [float(known), float(not known)] + file_one_hot + (
            _fixed(row.get("coverage_l10"), 10) +
            _fixed(row.get("coverage_l100_summary"), 4) +
            _fixed(row.get("l1000_topk"), self.top_k_l1000) +
            [float(row.get("file_count", 0)),
             float(row.get("active_file_count", 0)),
             float(row.get("anon_hot_ratio", 0)),
             float(row.get("anon_cooling_ratio", 0)),
             float(bool(row.get("foreground", 0))),
             math.log1p(max(0.0, float(row.get("rss_bytes", 0)))),
             math.log1p(max(0.0, float(row.get("pss_bytes", 0)))),
             math.log1p(max(0.0, float(row.get("swap_bytes", 0))))] +
            operation_one_hot)

    def transform(self, rows):
        return [self.transform_one(row) for row in rows]

    def schema(self):
        return {
            "schema_version": 1,
            "file_vocabulary": list(self.file_vocabulary),
            "unknown_file_bucket": "UNK_FILE",
            "operation_vocabulary": list(self.operation_vocabulary),
            "feature_count": len(self.transform_one({"file_id": ""})),
            "fit_source": "train_only",
        }
