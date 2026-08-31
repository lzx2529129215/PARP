"""Shared file-path privacy helpers; event capture lives in the eBPF collector."""

from __future__ import annotations

import hashlib
from pathlib import Path


TRACKED_EXTS = {
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "pdf",
    "tmp",
    "so",
    "dll",
    "ttf",
    "otf",
    "conf",
    "json",
    "xml",
    "png",
    "jpg",
    "jpeg",
}


def file_ext(path: str) -> str:
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix if suffix in TRACKED_EXTS else suffix


def path_for_mode(path: str, mode: str) -> str:
    if not path:
        return ""
    if mode == "raw":
        return path
    if mode == "basename":
        return Path(path).name
    if mode == "hash":
        return hashlib.sha256(path.encode("utf-8", errors="replace")).hexdigest()
    return path
