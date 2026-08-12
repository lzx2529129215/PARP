"""Small deterministic KMeans used when scikit-learn is unavailable."""


def _distance_squared(left, right):
    return sum((float(a) - float(b)) ** 2 for a, b in zip(left, right))


class LightweightKMeans:
    def __init__(self, clusters=8, max_iter=100):
        if clusters <= 0 or max_iter <= 0:
            raise ValueError("clusters and max_iter must be positive")
        self.clusters = int(clusters)
        self.max_iter = int(max_iter)
        self.centroids = []

    def _initialize(self, rows):
        centroids = [list(rows[0])]
        while len(centroids) < self.clusters:
            candidate = max(rows, key=lambda row: min(
                _distance_squared(row, center) for center in centroids))
            centroids.append(list(candidate))
        return centroids

    def fit(self, rows):
        rows = [list(map(float, row)) for row in rows]
        if len(rows) < self.clusters:
            raise ValueError("clusters cannot exceed training row count")
        if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
            raise ValueError("training rows must be a nonempty rectangular matrix")
        centroids = self._initialize(rows)
        for _ in range(self.max_iter):
            assignments = [min(range(self.clusters), key=lambda index:
                               (_distance_squared(row, centroids[index]), index))
                           for row in rows]
            updated = []
            for index in range(self.clusters):
                members = [row for row, assigned in zip(rows, assignments)
                           if assigned == index]
                if not members:
                    updated.append(list(centroids[index]))
                    continue
                updated.append([sum(column) / len(members)
                                for column in zip(*members)])
            if updated == centroids:
                break
            centroids = updated
        self.centroids = centroids
        return self

    def predict_one(self, row):
        if not self.centroids:
            raise RuntimeError("cluster model is not fitted")
        if len(row) != len(self.centroids[0]):
            raise ValueError("feature length mismatch")
        return min(range(len(self.centroids)), key=lambda index:
                   (_distance_squared(row, self.centroids[index]), index))

    def predict(self, rows):
        return [self.predict_one(row) for row in rows]

    def inertia(self, rows):
        return sum(_distance_squared(row, self.centroids[self.predict_one(row)])
                   for row in rows)

    def to_dict(self):
        return {"schema_version": 1, "algorithm": "lightweight_kmeans",
                "clusters": self.clusters, "max_iter": self.max_iter,
                "centroids": self.centroids, "fit_source": "train_only"}

    @classmethod
    def from_dict(cls, payload):
        result = cls(int(payload["clusters"]), int(payload.get("max_iter", 100)))
        result.centroids = [list(map(float, row)) for row in payload["centroids"]]
        return result
