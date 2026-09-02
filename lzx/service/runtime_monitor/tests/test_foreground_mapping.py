from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collectors.foreground import _map_foreground_app


class ForegroundMappingTests(unittest.TestCase):
    def test_prefers_specific_keyword_over_overlapping_prefix(self) -> None:
        keywords = {
            "FIREFOX": ["firefox", "mozilla"],
            "LIBREOFFICE": ["libreoffice", "calc"],
            "THUNDERBIRD": ["thunderbird", "mozilla mail"],
            "CALCULATOR": ["calculator", "gnome calculator"],
        }
        self.assertEqual(
            _map_foreground_app(["thunderbird"], "Account Setup - Mozilla Thunderbird", 0, keywords),
            "THUNDERBIRD",
        )
        self.assertEqual(
            _map_foreground_app(["gnome-calculator"], "Calculator", 0, keywords),
            "CALCULATOR",
        )

    def test_evince_is_not_mapped_as_files_from_a_desktop_path(self) -> None:
        keywords = {
            "EVINCE": ["evince", "document viewer", "pdf"],
            "FILES": ["nautilus", "pcmanfm", "files", "主文件夹"],
        }
        self.assertEqual(
            _map_foreground_app(
                ["evince"], "/home/lzx/Desktop/huawei_mem/samples/document_0070.pdf", 0, keywords,
            ),
            "EVINCE",
        )

    def test_gnome_shell_surface_maps_to_desktop_runtime_app(self) -> None:
        keywords = {
            "FIREFOX": ["firefox", "epiphany"],
            "DESKTOP": ["gnome-shell", "org.gnome.Shell"],
        }
        self.assertEqual(
            _map_foreground_app(
                ["gnome-shell", "Gnome-shell"], "Desktop", 0, keywords,
            ),
            "DESKTOP",
        )

    def test_process_identity_wins_over_shared_fixture_filename(self) -> None:
        keywords = {
            "VLC": ["vlc", "audio-test"],
            "AUDACITY": ["audacity", "audio-test"],
            "GIMP": ["gimp", "image-test"],
            "SHOTWELL": ["shotwell", "photo"],
        }
        with patch(
            "collectors.foreground._read_proc_text",
            side_effect=lambda _pid, name: (
                "audacity\n" if name == "comm" else "audacity /fixtures/audio-test.wav\0"
            ),
        ):
            self.assertEqual(
                _map_foreground_app([], "audio-test.wav", 123, keywords),
                "AUDACITY",
            )
        with patch(
            "collectors.foreground._read_proc_text",
            side_effect=lambda _pid, name: (
                "shotwell\n" if name == "comm" else "shotwell /fixtures/image-test.png\0"
            ),
        ):
            self.assertEqual(
                _map_foreground_app([], "image-test.png", 456, keywords),
                "SHOTWELL",
            )


if __name__ == "__main__":
    unittest.main()
