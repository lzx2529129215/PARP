"""Immutable input hashing."""

import hashlib


def hash_paths(root, paths):
    output = []
    for path in sorted(paths):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        output.append((str(path.relative_to(root)), digest.hexdigest(), path.stat().st_size))
    return output
