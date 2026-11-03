#!/usr/bin/env python3
"""Load the supplied four-operation lookup ABI. No synthetic fallback data."""
import ctypes
from dataclasses import dataclass
import json
from pathlib import Path

U32_MAX = (1 << 32) - 1
U64_MAX = (1 << 64) - 1
UNITS = {"bytes": 1, "kib": 1024, "mib": 1024**2, "gib": 1024**3}


def integer(value, minimum=0, maximum=U64_MAX):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"integer in [{minimum}, {maximum}] required: {value!r}")
    return value


@dataclass(frozen=True)
class LookupResult:
    raw_wss: tuple
    raw_pagefault: tuple
    wss_bytes: tuple
    pagefault: tuple
    source_kind: str = "external_library_unreported"
    wss_unit: str = "bytes"

    @property
    def valid_count(self):
        return len(self.wss_bytes)


def decode_result(wss, pagefault, unit):
    if unit not in UNITS or len(wss) != 4 or len(pagefault) != 4:
        raise ValueError("explicit WSS unit and two signed arrays of length four required")
    valid_wss, valid_faults = [], []
    ended = False
    for w, f in zip(wss, pagefault):
        integer(w, -1, (1 << 63) - 1)
        integer(f, -1, (1 << 63) - 1)
        if w == -1 or f == -1:
            if (w, f) != (-1, -1):
                raise ValueError("missing WSS and pagefault must both be -1")
            ended = True
        else:
            if ended:
                raise ValueError("missing operations must be a trailing suffix")
            valid_wss.append(integer(w * UNITS[unit]))
            valid_faults.append(f)
    integer(sum(valid_faults))
    return LookupResult(tuple(wss), tuple(pagefault), tuple(valid_wss), tuple(valid_faults),
                        wss_unit=unit)


class OperationLookup:
    def __init__(self, library, wss_unit, success_code=0):
        if wss_unit not in UNITS:
            raise ValueError("WSS unit must be specified explicitly")
        self.library = ctypes.CDLL(str(Path(library).resolve(strict=True)))
        self.function = self.library.get_wss_predictor
        self.function.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_uint64, ctypes.POINTER(ctypes.c_int64),
                                  ctypes.POINTER(ctypes.c_int64)]
        self.function.restype = ctypes.c_int
        self.unit = wss_unit
        self.success_code = integer(success_code, -(1 << 31), (1 << 31) - 1)

    def predict(self, app_name, sequence_num, current_op_id, pressure_gib):
        if not isinstance(app_name, str) or not app_name or "\0" in app_name:
            raise ValueError("nonempty application name without NUL required")
        integer(sequence_num, 0, U32_MAX)
        integer(current_op_id, 0, U32_MAX)
        if integer(pressure_gib) not in (0, 8, 12, 14):
            raise ValueError("pressure_gib is a lookup bucket: 0, 8, 12 or 14")
        # An untouched slot remains missing, never uninitialized or unsigned.
        wss = (ctypes.c_int64 * 4)(-1, -1, -1, -1)
        faults = (ctypes.c_int64 * 4)(-1, -1, -1, -1)
        ret = self.function(app_name.encode("utf-8"), sequence_num, current_op_id,
                            pressure_gib, wss, faults)
        if ret != self.success_code:
            raise RuntimeError(f"get_wss_predictor failed: ret={ret}")
        return decode_result(list(wss), list(faults), self.unit)


class TableOperationLookup:
    """Python equivalent of the supplied API v3: exact bytes, custom statuses."""
    def __init__(self, table):
        self.table = json.loads(Path(table).read_text(encoding="utf-8"))
        if (self.table.get("schema_version") != 2 or self.table.get("api_version") != 3
                or self.table.get("interface_wss_unit") != "bytes"):
            raise ValueError("unsupported operation table")
        self.rows = {}
        for row in self.table["records"]:
            key = (integer(row["sequence_num"], 1, 30), integer(row["op_id"], 1, 8),
                   integer(row["pressure_gib"]))
            if key[2] not in (0, 8, 12, 14) or key in self.rows:
                raise ValueError("invalid/duplicate operation table key")
            integer(row["wss_bytes"], 0, (1 << 63)-1)
            if row["pagefault"] is not None:
                integer(row["pagefault"], 0, (1 << 63)-1)
            if row["source_kind"] not in ("MEASURED", "SIMULATED"):
                raise ValueError("missing table provenance")
            self.rows[key] = row
        if len(self.rows) != 960:
            raise ValueError("complete 30 x 8 x 4 operation table required")

    def predict(self, app_name, sequence_num, current_op_id, pressure_gib):
        if not isinstance(app_name, str) or not app_name or "\0" in app_name:
            raise RuntimeError("get_wss_predictor failed: ret=-1 (invalid argument)")
        integer(sequence_num, 0, U32_MAX)
        integer(current_op_id, 0, U32_MAX)
        integer(pressure_gib)
        if not sequence_num or not current_op_id or pressure_gib not in (0, 8, 12, 14):
            raise RuntimeError("get_wss_predictor failed: ret=-1 (invalid argument)")
        if app_name.lower() != "gimp":
            raise RuntimeError("get_wss_predictor failed: ret=-6 (unsupported application)")
        if current_op_id > 8:
            raise RuntimeError("get_wss_predictor failed: ret=-1 (invalid operation)")
        if (sequence_num, current_op_id, pressure_gib) not in self.rows:
            raise RuntimeError("get_wss_predictor failed: ret=-2 (current record not found)")
        wss, faults, origins = [], [], set()
        for op in range(current_op_id+1, min(current_op_id+4, 8)+1):
            row = self.rows.get((sequence_num, op, pressure_gib))
            if row is None:
                raise RuntimeError("get_wss_predictor failed: ret=-4 (missing future record)")
            if row["pagefault"] is None:
                raise RuntimeError("get_wss_predictor failed: ret=-5 (data unavailable)")
            wss.append(row["wss_bytes"])
            faults.append(row["pagefault"])
            origins.add(row["source_kind"])
        result = decode_result(wss+[-1]*(4-len(wss)), faults+[-1]*(4-len(faults)), "bytes")
        kind = "+".join(sorted(origins)) if origins else "END_OF_SEQUENCE"
        return LookupResult(result.raw_wss, result.raw_pagefault, result.wss_bytes,
                            result.pagefault, kind)
