from __future__ import annotations

import unittest
from unittest.mock import patch

from web_runtime import LauncherApi


class LauncherReinstallApiTests(unittest.TestCase):
    def test_reinstall_entry_point_requests_repair_mode(self):
        api = object.__new__(LauncherApi)
        expected = {"ok": True, "message": "repair started"}

        with patch.object(LauncherApi, "_begin_install", return_value=expected) as begin:
            result = api.reinstall_client()

        begin.assert_called_once_with(reinstall=True)
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
