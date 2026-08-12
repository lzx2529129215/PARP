#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Run the existing Runtime Monitor suite on Python 3.8 without source edits."""

import argparse
import __future__
import importlib.machinery
import os
from pathlib import Path
import sys
import types
import unittest


class BooleanOptionalAction(argparse.Action):
    """Small Python 3.9 argparse.BooleanOptionalAction compatibility shim."""

    def __init__(self, option_strings, dest, default=None, **kwargs):
        positive = list(option_strings)
        negative = ["--no-" + item[2:] for item in positive
                    if item.startswith("--")]
        super().__init__(option_strings=positive + negative, dest=dest,
                         nargs=0, default=default, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        del parser, values
        setattr(namespace, self.dest,
                not option_string.startswith("--no-"))


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("runtime_monitor_root", type=Path)
    args = parser.parse_args(argv)
    root = args.runtime_monitor_root.resolve()
    if not (root / "tests").is_dir():
        parser.error("runtime monitor tests directory is missing")

    if not hasattr(argparse, "BooleanOptionalAction"):
        argparse.BooleanOptionalAction = BooleanOptionalAction

    # PEP 585 annotations in two pre-existing files are valid syntax on 3.8,
    # but are evaluated eagerly.  Compile imported sources with the future
    # annotations flag in memory; no Runtime Monitor file is rewritten.
    original = importlib.machinery.SourceFileLoader.source_to_code

    def source_to_code(self, data, path, *, _optimize=-1):
        return compile(data, path, "exec",
                       flags=__future__.annotations.compiler_flag,
                       dont_inherit=True, optimize=_optimize)

    importlib.machinery.SourceFileLoader.source_to_code = source_to_code
    try:
        os.chdir(str(root))
        sys.path.insert(0, str(root.parent))
        sys.path.insert(0, str(root))
        suites = []
        for test_path in sorted((root / "tests").glob("test*.py")):
            name = test_path.stem
            module = types.ModuleType(name)
            module.__file__ = str(test_path)
            sys.modules[name] = module
            source = test_path.read_text(encoding="utf-8")
            code = compile(source, str(test_path), "exec",
                           flags=__future__.annotations.compiler_flag,
                           dont_inherit=True)
            exec(code, module.__dict__)
            suites.append(unittest.defaultTestLoader.loadTestsFromModule(module))
        suite = unittest.TestSuite(suites)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    finally:
        importlib.machinery.SourceFileLoader.source_to_code = original
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
