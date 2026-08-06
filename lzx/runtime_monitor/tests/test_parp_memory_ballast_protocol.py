from __future__ import annotations

import csv
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "tools" / "parp_memory_ballast"


@unittest.skipUnless(BINARY.is_file(), "build runtime_monitor/tools/parp_memory_ballast first")
class BallastProtocolTests(unittest.TestCase):
    def command(self, path: Path, command: str) -> str:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
            stream.settimeout(3); stream.connect(str(path)); stream.sendall((command + "\n").encode())
            return stream.recv(2048).decode().strip()

    def test_foreground_only_allocate_and_deterministic_reaccess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); socket_path = root / "ballast.sock"; events = root / "events.csv"; file_path = root / "data.bin"
            process = subprocess.Popen([
                str(BINARY), "--app-key", "UNIT", "--socket", str(socket_path), "--log", str(events), "--file", str(file_path),
                "--anon-cold", str(1024 * 1024), "--anon-hot", str(256 * 1024),
                "--file-cold", str(1024 * 1024), "--file-hot", str(256 * 1024), "--hot-interval-ms", "20",
            ])
            try:
                for _ in range(50):
                    if socket_path.exists(): break
                    time.sleep(.02)
                self.assertTrue(socket_path.exists())
                self.assertTrue(self.command(socket_path, "ALLOCATE").startswith("ERR reason=ALLOCATE_REQUIRES_FOREGROUND_ACTIVE"))
                self.assertTrue(self.command(socket_path, "ENTER_FOREGROUND").startswith("OK"))
                self.assertTrue(self.command(socket_path, "ALLOCATE").startswith("OK"))
                self.assertTrue(file_path.is_file())
                self.assertTrue(self.command(socket_path, "ENTER_BACKGROUND").startswith("OK"))
                self.assertTrue(self.command(socket_path, "ALLOCATE").startswith("ERR reason=ALLOCATE_REQUIRES_FOREGROUND_ACTIVE"))
                time.sleep(.08)
                self.assertTrue(self.command(socket_path, "ENTER_FOREGROUND").startswith("OK"))
                self.assertTrue(self.command(socket_path, "REACCESS_HOT").startswith("OK"))
                self.assertTrue(self.command(socket_path, "REACCESS_COLD").startswith("OK"))
                self.assertTrue(self.command(socket_path, "VERIFY").startswith("OK"))
                self.assertTrue(self.command(socket_path, "STOP").startswith("OK"))
            finally:
                process.wait(timeout=5)
            with events.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertTrue(any(row["command"] == "ALLOCATE" and row["status"] == "OK" for row in rows))
            self.assertTrue(any(row["command"] == "ALLOCATE" and row["status"] == "REJECTED" for row in rows))
            background = [row for row in rows if row["state_before"] == "BACKGROUND_IDLE"]
            self.assertTrue(all("COLD" not in row["command"] for row in background))


if __name__ == "__main__":
    unittest.main()
