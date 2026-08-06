from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .stable_region_key import key_hash


@dataclass
class RegionVocab:
    path: Path
    regions: dict[str, dict[str, Any]] = field(default_factory=dict)
    canonical_regions: dict[str, int] = field(default_factory=dict)
    next_id: int = 1
    next_canonical_id: int = 1
    corrupted: bool = False
    error: str = ""

    @classmethod
    def load(cls, path: str | Path) -> "RegionVocab":
        vocab = cls(Path(path))
        if not vocab.path.exists():
            return vocab
        try:
            data = json.loads(vocab.path.read_text(encoding="utf-8"))
            for item in data.get("regions", []):
                key = str(item["stable_key"])
                vocab.regions[key] = item
                vocab.next_id = max(vocab.next_id, int(item["region_id"]) + 1)
            for item in data.get("canonical_regions", []):
                key = str(item["canonical_key"])
                cid = int(item["canonical_region_id"])
                vocab.canonical_regions[key] = cid
                vocab.next_canonical_id = max(vocab.next_canonical_id, cid + 1)
        except Exception as exc:
            vocab.corrupted = True
            vocab.error = str(exc)
        return vocab

    def get_or_create(
        self,
        *,
        stable_key: str,
        canonical_key: str,
        region_type: str,
        app_id: str,
        process_role: str,
        path_metadata: dict[str, str],
        identity_confidence: str,
        now_ns: int | None = None,
    ) -> tuple[int, int]:
        if self.corrupted:
            raise ValueError(f"region vocab is corrupted: {self.error}")
        now_ns = now_ns or time.time_ns()
        canonical_id = self.canonical_regions.get(canonical_key)
        if canonical_id is None:
            canonical_id = self.next_canonical_id
            self.next_canonical_id += 1
            self.canonical_regions[canonical_key] = canonical_id
        item = self.regions.get(stable_key)
        if item is None:
            item = {
                "region_id": self.next_id,
                "region_type": region_type,
                "stable_key": stable_key,
                "stable_key_hash": key_hash(stable_key),
                "canonical_key": canonical_key,
                "canonical_key_hash": key_hash(canonical_key),
                "canonical_region_id": canonical_id,
                "app_id": str(app_id),
                "process_role": process_role,
                "path_metadata": path_metadata,
                "identity_confidence": identity_confidence,
                "first_seen_ns": now_ns,
                "last_seen_ns": now_ns,
                "observation_count": 0,
            }
            self.next_id += 1
            self.regions[stable_key] = item
        item["last_seen_ns"] = now_ns
        item["observation_count"] = int(item.get("observation_count", 0)) + 1
        return int(item["region_id"]), canonical_id

    def save(self) -> None:
        if self.corrupted:
            raise ValueError(f"region vocab is corrupted: {self.error}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "regions": sorted(self.regions.values(), key=lambda item: int(item["region_id"])),
            "canonical_regions": [
                {"canonical_region_id": cid, "canonical_key": key, "canonical_key_hash": key_hash(key)}
                for key, cid in sorted(self.canonical_regions.items(), key=lambda item: item[1])
            ],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

