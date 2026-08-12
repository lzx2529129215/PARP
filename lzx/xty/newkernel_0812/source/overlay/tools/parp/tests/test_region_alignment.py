#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
import json
from pathlib import Path
import unittest

from region_schema import (MAX_SPLITS, VMA, align_interval, anon_page_range,
                           file_page_range, split_region, vma_signature)
from region_decode import decode_trace_line


class RegionAlignmentTest(unittest.TestCase):
    def test_runtime_trace_timestamp_is_preserved_for_windows(self):
        line = ("worker-1 [000] .... 123.456789: parp_region_evidence: "
                "sample=1 pid=2 tgid=2 domain=3 app=4 mm_cookie=5 "
                "type=0 align=1 logical_start=6 nr_pages=7 "
                "access_evidence=8 age=9 confidence_q15=10 reasons=0x0")
        record = decode_trace_line(line)
        self.assertEqual(record["timestamp_ns"], 123456789000)

    def test_shared_vectors(self):
        vectors = json.loads((Path(__file__).parents[1] / "configs" /
                              "region_vectors.json").read_text())
        for vector in vectors["align"]:
            self.assertEqual(align_interval(*vector["input"]),
                             tuple(vector["output"]))
        file_vector = vectors["file_offset"]
        file_vma = VMA(file_vector["vma_start"], 0x20000, "file",
                       vm_pgoff=file_vector["vm_pgoff"])
        self.assertEqual(file_page_range(file_vma, *file_vector["region"]),
                         tuple(file_vector["output"]))
        anon_vector = vectors["anon_offset"]
        anon_vma = VMA(anon_vector["vma_start"], 0x30000, "anon")
        self.assertEqual(anon_page_range(anon_vma, *anon_vector["region"]),
                         tuple(anon_vector["output"]))

    def test_three_vmas_and_hole_conserve_length(self):
        vmas = [VMA(0x1000, 0x3000, "file", vm_pgoff=2,
                    inode=1, file_version=1, backing_class="REGULAR_FILE"),
                VMA(0x4000, 0x6000, "anon"),
                VMA(0x6000, 0x8000, "stack")]
        parts = split_region(0x1000, 0x8000, vmas)
        self.assertEqual(sum(part["end"] - part["start"] for part in parts),
                         0x7000)
        self.assertEqual(parts[1]["region_type"], "UNRESOLVED")

    def test_boundary_and_unmapped(self):
        parts = split_region(0x2000, 0x4000,
                             [VMA(0x1000, 0x2000, "anon")])
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["region_type"], "UNRESOLVED")

    def test_split_limit(self):
        vmas = [VMA(index * 0x2000, index * 0x2000 + 0x1000, "anon")
                for index in range(MAX_SPLITS + 2)]
        parts = split_region(0x1000, (MAX_SPLITS + 2) * 0x2000,
                             vmas, max_splits=MAX_SPLITS)
        self.assertTrue(parts[-1].get("truncated"))

    def test_signature_stability_and_invalidation(self):
        vma = VMA(0x1000, 0x9000, "anon", flags=3)
        self.assertEqual(vma_signature(vma), vma_signature(vma))
        self.assertNotEqual(vma_signature(vma),
                            vma_signature(VMA(0x1000, 0x9000, "anon", flags=7)))


if __name__ == "__main__":
    unittest.main()
