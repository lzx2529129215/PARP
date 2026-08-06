from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from runtime_monitor.mapping import AppMapper
from runtime_monitor.state import RuntimeState


class OnlineStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.vocab = base / "app_vocab.json"
        self.mapping = base / "app_mapping.json"
        self.vocab.write_text(
            json.dumps({"WPS": 0, "腾讯QQ": 1, "火狐浏览器": 2}, ensure_ascii=False),
            encoding="utf-8",
        )
        self.mapping.write_text(
            json.dumps(
                {
                    "rules": [
                        {"app": "WPS", "wm_class": ["wpsoffice"], "title_contains": ["WPS Office"]},
                        {"app": "腾讯QQ", "wm_class": ["QQ"], "title_contains": ["QQ"]},
                        {
                            "app": "火狐浏览器",
                            "wm_class": ["firefox", "firefox_firefox"],
                            "title_contains": ["Mozilla Firefox"],
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_window_events_are_coalesced_to_app_events(self) -> None:
        state = RuntimeState(AppMapper(self.mapping, self.vocab))
        events = [
            self._event("Opened", "WPS Office", "wpsoffice", 203733, "wps-main"),
            self._event("Switched", "WPS Office", "wpsoffice", 203733, "wps-main"),
            self._event("Opened", "wpsoffice", "wpsoffice", 203733, "wps-dialog"),
            self._event("Switched", "wpsoffice", "wpsoffice", 203733, "wps-dialog"),
            self._event("Switched", "WPS Office", "wpsoffice", 203733, "wps-main"),
            self._event("Closed", "wpsoffice", "wpsoffice", 203733, "wps-dialog"),
            self._event("Opened", "QQ", "QQ", 203873, "qq-main"),
            self._event("Switched", "QQ", "QQ", 203873, "qq-main"),
            self._event("Opened", "", "", 204059, "firefox-main"),
            self._event("Switched", "Mozilla Firefox", "firefox_firefox", 204059, "firefox-main"),
            self._event("Switched", "QQ", "QQ", 203873, "qq-main"),
            self._event("Switched", "Mozilla Firefox", "firefox_firefox", 204059, "firefox-main"),
            self._event("Closed", "QQ", "QQ", 203873, "qq-main"),
            self._event("Closed", "WPS Office", "wpsoffice", 203733, "wps-main"),
        ]
        rows = [state.handle_event(event).csv_row for event in events]
        event_types = [row["event_type"] for row in rows if row]
        self.assertEqual(
            event_types,
            [
                "Opened",
                "Switched",
                "Opened",
                "Switched",
                "Switched",
                "Switched",
                "Switched",
                "Closed",
                "Closed",
            ],
        )
        self.assertNotIn("WPS", state.opened_apps)
        self.assertNotIn("腾讯QQ", state.opened_apps)
        self.assertIn("火狐浏览器", state.opened_apps)

    @staticmethod
    def _event(event_type: str, title: str, wm_class: str, pid: int, window_id: str) -> dict:
        return {
            "event_type": event_type,
            "timestamp_ms": 1782816031143,
            "window_id": window_id,
            "title": title,
            "wm_class": wm_class,
            "gtk_app_id": "",
            "pid": pid,
            "is_minimized": False,
        }


if __name__ == "__main__":
    unittest.main()
