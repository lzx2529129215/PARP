#!/usr/bin/env python3
"""Build both C lookup libraries, then compare the real table and controller."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default="cc")
    args = parser.parse_args()
    command = [str(Path(args.cc).resolve()) if Path(args.cc).exists() else args.cc]
    zig = Path(args.cc).name.lower() in ("zig", "zig.exe")
    if zig:
        command.append("cc")
    windows = sys.platform == "win32"
    root = (Path.cwd()/".parp-test-build").resolve()
    root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="operation-", dir=root) as temporary:
        build = Path(temporary)
        assert build.resolve().parent == root
        env = os.environ.copy()
        for name in ("ZIG_GLOBAL_CACHE_DIR", "ZIG_LOCAL_CACHE_DIR", "TEMP", "TMP"):
            directory = build/name.lower()
            directory.mkdir()
            env[name] = str(directory)
        base = command+["-std=c11", "-Wall", "-Wextra", "-Werror", "-I"+str(HERE)]
        common = base+["-shared"]
        if windows:
            common += ["-DWSS_PREDICTOR_BUILD_DLL"]
        elif not windows:
            common += ["-fPIC"]
        extension = ".dll" if windows else ".so"
        for source, variable in (("test_operation_fixture", "PARP_TEST_LOOKUP_LIBRARY"),
                                 ("get_wss_predictor", "PARP_TEST_REAL_LOOKUP_LIBRARY")):
            library = build/(source+extension)
            subprocess.run(common+[str(HERE/(source+".c")), "-o", str(library)], check=True, env=env)
            env[variable] = str(library)
        fixture_flag = '-DWSS_PREDICTOR_DATA_HEADER="legacy_fixtures/fixture_data.h"'
        library = build/("legacy_error_fixture"+extension)
        subprocess.run(common+[fixture_flag, str(HERE/"get_wss_predictor.c"), "-o", str(library)],
                       check=True, env=env)
        env["PARP_TEST_LEGACY_FIXTURE_LIBRARY"] = str(library)
        # Run the owner's original contract and missing-data tests as supplied.
        for test, extra in (("legacy_contract.c", []),
                            ("legacy_fixtures/test_fixture.c", [fixture_flag])):
            executable = build/(Path(test).stem+(".exe" if windows else ""))
            subprocess.run(base+extra+[str(HERE/"get_wss_predictor.c"), str(HERE/test),
                                       "-o", str(executable)], check=True, env=env)
            subprocess.run([str(executable)], check=True, env=env)
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(HERE),
                        "-p", "test_*.py", "-v"], check=True, env=env)


if __name__ == "__main__":
    main()
