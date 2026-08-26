import re
import unittest
from unittest.mock import patch

from memsched_exp.launch import _wmctrl_window


class LaunchWindowTest(unittest.TestCase):
    @patch("memsched_exp.launch._descendants", return_value={10})
    @patch(
        "memsched_exp.launch._wmctrl_windows",
        return_value=[
            {"window_id": "0x1", "pid": 99, "title": "QQ"},
            {"window_id": "0x2", "pid": 100, "title": "QQ"},
        ],
    )
    def test_existing_title_match_is_not_reused(self, _windows, _descendants):
        result = _wmctrl_window(10, re.compile("QQ"), {"0x1"})
        self.assertEqual(result["window_id"], "0x2")

    @patch("memsched_exp.launch._descendants", return_value={10})
    @patch(
        "memsched_exp.launch._wmctrl_windows",
        return_value=[{"window_id": "0x1", "pid": 99, "title": "QQ"}],
    )
    def test_only_existing_title_match_returns_none(self, _windows, _descendants):
        self.assertIsNone(_wmctrl_window(10, re.compile("QQ"), {"0x1"}))

    @patch("memsched_exp.launch._descendants", return_value={10})
    @patch(
        "memsched_exp.launch._wmctrl_windows",
        return_value=[{"window_id": "0x2", "pid": 100, "title": "QQ"}],
    )
    def test_cgroup_filter_rejects_unrelated_new_window(self, _windows, _descendants):
        self.assertIsNone(_wmctrl_window(10, re.compile("QQ"), set(), {200}))


if __name__ == "__main__":
    unittest.main()
