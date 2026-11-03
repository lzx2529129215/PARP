#!/usr/bin/env python3
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

from operation_lookup import OperationLookup, TableOperationLookup

HERE = Path(__file__).resolve().parent
TABLE = HERE/"data/operation_table.json"
EXAMPLE_WSS = (289120256, 485830656, 475205632, 553238528)


class WorkbookTableTest(unittest.TestCase):
    def setUp(self):
        self.lookup = TableOperationLookup(TABLE)

    def test_source_totals_and_example(self):
        table = self.lookup.table
        self.assertEqual(table["source_kind_counts"], {"MEASURED":192, "SIMULATED":768})
        self.assertEqual(table["missing_pagefault_records"], 24)
        r = self.lookup.predict("gimp", 1, 1, 0)
        self.assertEqual(r.raw_wss, EXAMPLE_WSS)
        self.assertEqual(r.wss_bytes, EXAMPLE_WSS)
        self.assertEqual(r.wss_unit, "bytes")
        self.assertEqual(r.raw_pagefault, (64992, 120144, 25773, 45476))
        self.assertEqual(r.source_kind, "MEASURED")
        self.assertEqual(self.lookup.predict("gimp", 7, 1, 0).source_kind, "SIMULATED")
        self.assertEqual(self.lookup.predict("GIMP", 1, 1, 0).wss_bytes, EXAMPLE_WSS)

    def test_all_future_slices_are_exact_bytes(self):
        for sequence in range(1, 31):
            for pressure in (0, 8, 12, 14):
                for current in range(1, 9):
                    r = self.lookup.predict("gimp", sequence, current, pressure)
                    rows = [self.lookup.rows[sequence, op, pressure] for op in
                            range(current+1, min(current+4, 8)+1)]
                    self.assertEqual(r.valid_count, len(rows))
                    self.assertEqual(r.wss_bytes, tuple(x["wss_bytes"] for x in rows))
                    self.assertEqual(r.raw_pagefault[len(rows):], (-1,)*(4-len(rows)))
        self.assertEqual(self.lookup.predict("gimp", 1, 7, 0).raw_wss, (48627712, -1, -1, -1))
        self.assertEqual(self.lookup.predict("gimp", 1, 8, 0).raw_pagefault, (-1,)*4)

    def test_api_v3_errors_and_no_zero_filling(self):
        for sequence in range(1, 31):
            for pressure in (0, 8, 12, 14):
                with self.assertRaisesRegex(RuntimeError, "ret=-1 "):
                    self.lookup.predict("gimp", sequence, 0, pressure)
        cases = (("unknown", 1, 1, 0, -6), ("gimp", 31, 1, 0, -2),
                 ("gimp", 1, 9, 0, -1), ("gimp", 1, 1, 1, -1),
                 ("gimp", 0, 1, 0, -1), ("", 1, 1, 0, -1))
        for app, seq, op, pressure, expected in cases:
            with self.assertRaisesRegex(RuntimeError, f"ret={expected} "):
                self.lookup.predict(app, seq, op, pressure)
        self.lookup.rows[1, 2, 0]["pagefault"] = None
        with self.assertRaisesRegex(RuntimeError, "ret=-5 "):
            self.lookup.predict("gimp", 1, 1, 0)
        del self.lookup.rows[1, 2, 0]
        with self.assertRaisesRegex(RuntimeError, "ret=-4 "):
            self.lookup.predict("gimp", 1, 1, 0)

    def test_supplied_c_data_matches_all_json_records(self):
        pattern = r"\{(\d+)U, (\d+)U, (\d+)U, (\d+)ULL, (\d+)ULL, (\d+)U\}"
        rows = [tuple(map(int, m)) for m in re.findall(pattern, (HERE/"gimp_lookup_data.h").read_text())]
        self.assertEqual(len(rows), 960)
        self.assertEqual(rows, sorted(rows, key=lambda r:r[:3]))
        for seq, pressure, op, wss, fault, mask in rows:
            source = self.lookup.rows[seq, op, pressure]
            self.assertEqual(wss, source["wss_bytes"])
            self.assertEqual((fault, mask), (0, 1) if source["pagefault"] is None
                             else (source["pagefault"], 3))

    def test_imported_owner_files_unchanged_except_newlines(self):
        manifest = json.loads((HERE/"data/legacy_lookup_import.json").read_text())
        for entry in manifest["files"]:
            self.assertEqual(hashlib.sha256((HERE/entry["destination"]).read_bytes()).hexdigest(),
                             entry["imported_sha256"])

    def test_no_compiler_query_cli(self):
        result = subprocess.run([sys.executable, str(HERE/"operation_controller.py"), "--query",
                                 "gimp", "1", "1", "0", "--rss-bytes", "1073741824"],
                                capture_output=True, text=True, check=True)
        record = json.loads(result.stdout)
        self.assertEqual(record["raw_wss"], list(EXAMPLE_WSS))
        self.assertEqual(record["raw_wss_unit"], "bytes")
        self.assertFalse(record["executed"])
        rejected = subprocess.run([sys.executable, str(HERE/"operation_controller.py"),
                                   "--wss-unit", "mib", "--query", "gimp", "1", "1", "0",
                                   "--rss-bytes", "1073741824"], capture_output=True, text=True)
        self.assertNotEqual(rejected.returncode, 0)


@unittest.skipUnless(os.environ.get("PARP_TEST_REAL_LOOKUP_LIBRARY"), "C table built by run_operation_tests.py")
class CWorkbookTableTest(unittest.TestCase):
    def test_every_c_lookup_matches_python(self):
        py = TableOperationLookup(TABLE)
        c = OperationLookup(os.environ["PARP_TEST_REAL_LOOKUP_LIBRARY"], "bytes")
        for sequence in range(1, 31):
            for pressure in (0, 8, 12, 14):
                for current in range(1, 9):
                    a, b = c.predict("gimp", sequence, current, pressure), py.predict("gimp", sequence, current, pressure)
                    self.assertEqual(a.raw_wss, b.raw_wss)
                    self.assertEqual(a.raw_pagefault, b.raw_pagefault)

    def test_c_errors_leave_outputs_unchanged(self):
        c = OperationLookup(os.environ["PARP_TEST_REAL_LOOKUP_LIBRARY"], "bytes")
        for name, sequence, current, pressure, expected in (
                (b"gimp", 1, 0, 0, -1), (b"gimp", 31, 1, 0, -2),
                (b"unknown", 1, 1, 0, -6), (b"gimp", 1, 9, 0, -1),
                (b"gimp", 1, 1, 1, -1)):
            w = (ctypes.c_int64*4)(111, 112, 113, 114)
            f = (ctypes.c_int64*4)(221, 222, 223, 224)
            self.assertEqual(c.function(name, sequence, current, pressure, w, f), expected)
            self.assertEqual(list(w), [111, 112, 113, 114])
            self.assertEqual(list(f), [221, 222, 223, 224])

    def test_actual_library_cli_uses_bytes(self):
        result = subprocess.run([sys.executable, str(HERE/"operation_controller.py"),
                                 "--library", os.environ["PARP_TEST_REAL_LOOKUP_LIBRARY"],
                                 "--wss-unit", "bytes", "--query", "gimp", "1", "1", "0",
                                 "--rss-bytes", "1073741824"], check=True, capture_output=True, text=True)
        record = json.loads(result.stdout)
        self.assertEqual(record["wss_bytes"], list(EXAMPLE_WSS))


@unittest.skipUnless(os.environ.get("PARP_TEST_LEGACY_FIXTURE_LIBRARY"), "owner fixtures built by run_operation_tests.py")
class OwnerErrorFixtureTest(unittest.TestCase):
    def test_early_late_gaps_missing_fields_and_genuine_zero(self):
        c = OperationLookup(os.environ["PARP_TEST_LEGACY_FIXTURE_LIBRARY"], "bytes")
        for seq, expected in ((1, -4), (2, -5), (4, -4), (5, -5)):
            w = (ctypes.c_int64*4)(111, 112, 113, 114)
            f = (ctypes.c_int64*4)(221, 222, 223, 224)
            self.assertEqual(c.function(b"gimp", seq, 1, 0, w, f), expected)
            self.assertEqual(list(w), [111, 112, 113, 114])
            self.assertEqual(list(f), [221, 222, 223, 224])
        result = c.predict("gimp", 3, 1, 0)
        self.assertEqual(result.raw_wss, (20, 30, 40, 50))
        self.assertEqual(result.raw_pagefault, (0, 2, 3, 4))


if __name__ == "__main__":
    unittest.main()
