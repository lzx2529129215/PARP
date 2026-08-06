from __future__ import annotations

import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
