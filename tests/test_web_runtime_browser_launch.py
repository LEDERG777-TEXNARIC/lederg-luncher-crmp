from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from web_runtime import LauncherApi


class BrowserLaunchTests(unittest.TestCase):
    @staticmethod
    def make_api(root: Path) -> LauncherApi:
        api = object.__new__(LauncherApi)
        api._lock = threading.RLock()
        api._busy = False
        api._game_root = root
        api._install_path = root
        api._endpoint = "188.127.241.8:1130"
        api._nickname = "LEDERG"
        api._client_health = {}
        api._phase = "ready"
        api._status = ""
        api._error = ""
        api._progress = 0.0
        api._progress_label = ""
        api._current_file = ""
        api._process = None
        api._last_exit_code = None
        api._xpanel_stop = None
        api._repair_failure = {}
        return api

    def test_play_launches_the_browser_from_multiplayer_c_with_server_ip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            browser = root / "multiplayer_c" / "multiplayer_browser_cr.exe"
            browser.parent.mkdir(parents=True)
            browser.write_bytes(b"MZ")
            (root / "gta_sa.exe").write_bytes(b"MZ")
            api = self.make_api(root)
            process = Mock()
            process.pid = 1234
            process.args = [str(browser)]
            process.poll.return_value = None

            with (
                patch.object(LauncherApi, "set_nickname", return_value={"ok": True}),
                patch.object(LauncherApi, "_refresh_server"),
                patch("web_runtime.inspect_client_installation", return_value={
                    "healthy": True, "summary": "ok", "issues": [], "warnings": [],
                    "integrity_mode": "verified", "checked_files": 20,
                }),
                patch("web_runtime.find_client", return_value=browser),
                patch("web_runtime.connect_via_browser_ui", return_value=process) as connect,
                patch.object(LauncherApi, "_start_xpanel_hotkey"),
                patch("web_runtime.threading.Thread") as thread,
            ):
                thread.return_value.start = Mock()
                result = LauncherApi.launch_game(api, "LEDERG")

            self.assertTrue(result["ok"], result)
            connect.assert_called_once_with(browser, "188.127.241.8", 1130, "LEDERG")
            self.assertIs(api._process, process)
            self.assertIn("188.127.241.8:1130", api._status)

    def test_play_reports_missing_multiplayer_browser_instead_of_running_another_exe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            api = self.make_api(root)

            with (
                patch.object(LauncherApi, "set_nickname", return_value={"ok": True}),
                patch.object(LauncherApi, "_refresh_server"),
                patch("web_runtime.inspect_client_installation", return_value={
                    "healthy": True, "summary": "ok", "issues": [], "warnings": [],
                    "integrity_mode": "verified", "checked_files": 20,
                }),
                patch("web_runtime.find_client", return_value=None),
                patch("web_runtime.connect_via_browser_ui") as connect,
            ):
                result = LauncherApi.launch_game(api, "LEDERG")

            self.assertFalse(result["ok"])
            self.assertIn("multiplayer_browser_cr.exe", result["message"])
            connect.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
