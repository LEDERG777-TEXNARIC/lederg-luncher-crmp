from __future__ import annotations

import unittest
from unittest.mock import patch

import web_runtime
from web_runtime import LauncherApi


class LauncherReinstallApiTests(unittest.TestCase):
    def test_partial_client_is_exposed_for_manual_recovery(self):
        import tempfile
        import threading
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp:
            selected = Path(temp)
            partial = selected / "GTA Criminal Russia"
            (partial / "multiplayer_c").mkdir(parents=True)

            api = object.__new__(LauncherApi)
            api._lock = threading.RLock()
            api._game_root = None
            api._install_path = selected
            api._busy = False
            api._checking_client = False
            api._phase = "error"
            api._status = "client files are incomplete"
            api._progress = 0.0
            api._progress_label = "AUTOCHECK FAILED"
            api._current_file = ""
            api._error = api._status
            api._endpoint = "188.127.241.8:1130"
            api._nickname = "LEDERG"
            api._server_online = False
            api._server_ping_ms = None
            api._server_method = "none"
            api._server_detail = "no reply"
            api._server_checked_at = ""
            api._last_exit_code = None
            api._client_health = {
                "healthy": False,
                "summary": "incomplete client",
                "issues": ["missing gta_sa.exe"],
                "warnings": [],
                "integrity_mode": "structural-only",
            }

            with (
                patch("web_runtime.find_direct_game_client", return_value=None),
                patch("web_runtime.find_client", return_value=None),
            ):
                state = api.get_state()

            self.assertTrue(state["client_found"])
            self.assertFalse(state["client_ready"])

    def test_reinstall_entry_point_requests_repair_mode(self):
        api = object.__new__(LauncherApi)
        expected = {"ok": True, "message": "repair started"}

        with patch.object(LauncherApi, "_begin_install", return_value=expected) as begin:
            result = api.reinstall_client()

        begin.assert_called_once_with(reinstall=True)
        self.assertEqual(result, expected)


    def test_reinstall_uses_manifest_file_downloads_and_never_google_drive_archive(self):
        import tempfile
        import threading
        from pathlib import Path
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            selected = base / "install"
            selected.mkdir()
            root = selected / "GTA Criminal Russia"
            (root / "multiplayer_c").mkdir(parents=True)
            (root / "gta_sa.exe").write_bytes(b"MZ")
            browser = root / "multiplayer_c" / "multiplayer_browser_cr.exe"
            browser.write_bytes(b"MZ")
            runtime = root / "multiplayer_c" / "grand_theft_auto_san_andreas.dll"
            runtime.write_bytes(b"MZ")

            manifest = {
                "schema": 1,
                "version": "2026.10.24-test",
                "install_root": "GTA Criminal Russia",
                "files_base_url": "https://example.cloudpub.ru/client-files/",
                "files": [{"path": "gta_sa.exe", "size": 2, "crc32": "00000000"}],
                "file_count": 1,
                "total_bytes": 2,
                "fingerprint": "a" * 64,
            }
            release = {
                "version": "2026.10.24-test",
                "archive_url": "https://drive.usercontent.google.com/download?id=old",
                "files_manifest_url": "https://example.cloudpub.ru/client-manifest.json",
                "client_executable": "multiplayer_c/multiplayer_browser_cr.exe",
                "launch_mode": "direct",
            }

            api = object.__new__(LauncherApi)
            api._lock = threading.RLock()
            api._busy = True
            api._phase = "manifest"
            api._status = ""
            api._progress = 0.0
            api._progress_label = ""
            api._current_file = ""
            api._error = ""
            api._repair_failure = {}
            api._game_root = None
            api._install_path = selected
            api._write_install_path = Mock()

            def fake_download(destination, manifest_url, **kwargs):
                self.assertEqual(destination, selected)
                self.assertEqual(manifest_url, release["files_manifest_url"])
                self.assertEqual(kwargs["manifest_payload"], manifest)
                return root, 1

            with (
                patch("web_runtime.DATA_HOME", base / "data"),
                patch("web_runtime.fetch_release_manifest", return_value=release),
                patch("web_runtime.fetch_files_manifest", return_value=manifest),
                patch("web_runtime.download_client_folder", side_effect=fake_download) as download_folder,
                patch("web_runtime.find_client", return_value=browser),
                patch("web_runtime.game_root_for", return_value=root),
                patch("web_runtime.find_direct_game_client", return_value=runtime),
                patch("web_runtime.apply_successful_update"),
                patch("web_runtime.write_launcher_log"),
            ):
                LauncherApi._install_worker(api, selected, reinstall=True)

            download_folder.assert_called_once()
            self.assertFalse(hasattr(web_runtime, "download_archive"))
            self.assertEqual(api._phase, "ready")
            self.assertFalse(api._error)
            api._write_install_path.assert_called_once_with(root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
