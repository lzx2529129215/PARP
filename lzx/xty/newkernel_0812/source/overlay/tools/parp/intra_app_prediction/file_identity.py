"""File identity and partition-generation tracking."""

from dataclasses import dataclass
from typing import Dict, Tuple

from .schemas import FileIdentity

__all__ = ["FileIdentity", "PartitionTracker", "PartitionRecord"]


@dataclass(frozen=True)
class PartitionRecord:
    identity: FileIdentity
    file_page_count: int
    file_size_bytes: int
    partition_generation: int


class PartitionTracker:
    def __init__(self):
        self._partitions: Dict[FileIdentity, PartitionRecord] = {}

    def observe(self, identity: FileIdentity, file_page_count: int,
                file_size_bytes: int) -> int:
        if file_page_count <= 0 or file_size_bytes < 0:
            raise ValueError("nonempty file page count and size are required")
        old = self._partitions.get(identity)
        generation = 1
        if old is not None:
            generation = old.partition_generation
            if (old.file_page_count, old.file_size_bytes) != (
                    file_page_count, file_size_bytes):
                generation += 1
        self._partitions[identity] = PartitionRecord(
            identity, file_page_count, file_size_bytes, generation)
        return generation

    def record(self, identity: FileIdentity) -> PartitionRecord:
        return self._partitions[identity]
