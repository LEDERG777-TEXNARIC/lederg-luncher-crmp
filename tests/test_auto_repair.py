from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import web_runtime

from web_runtime import LauncherApi


class AutomaticRepairGuardTests(unittest.TestCase):
    def make_api_stub(self, root: Path):
        api = object.__new__(LauncherApi)
        api._lock = threading.RLock()
        api._busy = False
        api._process = None
        api._game_root = root
        api._install_path = root
        api._status = ""
        api._error = ""
        api._phase = "ready"
        return api

    def test_allows_one_automatic_repair_then_blocks_repeat_within_cooldown(self):
        with tempfile.TemporaryDirectory() as temp:
            data_home = Path(temp)
            root = data_home / "GTA Criminal Russia"
            root.mkdir()
            guard = data_home / "auto-repair-state.json"
            api = self.make_api_stub(root)

            with (
                patch("web_runtime.AUTO_REPAIR_STATE_FILE", guard),
                patch("web_runtime.write_launcher_log"),
            ):
                first = api._auto_repair_allowed(root, "startup integrity")
                second = api._auto_repair_allowed(root, "same error again")

            self.assertTrue(first)
            self.assertFalse(second)
            state = json.loads(guard.read_text(encoding="utf-8"))
            self.assertEqual(state["root"], str(root.resolve()))
            self.assertEqual(state["reason"], "startup integrity")
            self.assertIn("24 часа", api._error)
            self.assertEqual(state["result"], "in_progress")

    def test_failed_repair_uses_short_retry_cooldown_instead_of_full_day(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            guard = Path(temp) / "auto-repair-state.json"
            now = 1_800_000_000.0
            guard.write_text(json.dumps({
                "root": str(root.resolve()),
                "attempted_at": now - 30,
                "reason": "archive download failed",
                "result": "failed",
            }), encoding="utf-8")
            api = self.make_api_stub(root)

            with (
                patch("web_runtime.AUTO_REPAIR_STATE_FILE", guard),
                patch("web_runtime.time.time", return_value=now),
                patch("web_runtime.write_launcher_log"),
            ):
                blocked = api._auto_repair_allowed(root, "same damaged files")
                self.assertFalse(blocked)
                self.assertIn("мин", api._error)

                guard_payload = json.loads(guard.read_text(encoding="utf-8"))
                guard_payload["attempted_at"] = now - web_runtime.AUTO_REPAIR_FAILURE_RETRY_SECONDS - 1
                guard.write_text(json.dumps(guard_payload), encoding="utf-8")

                allowed = api._auto_repair_allowed(root, "retry failed repair")

            self.assertTrue(allowed)
            self.assertEqual(json.loads(guard.read_text(encoding="utf-8"))["result"], "in_progress")

    def test_repair_is_not_started_while_game_process_is_running(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            api = self.make_api_stub(root)
            api._process = type("RunningProcess", (), {"poll": lambda self: None})()

            with (
                patch("web_runtime.AUTO_REPAIR_STATE_FILE", root / "guard.json"),
                patch("web_runtime.write_launcher_log"),
            ):
                started = api._auto_repair_allowed(root, "startup integrity")

            self.assertFalse(started)
            self.assertFalse((root / "guard.json").exists())

    def test_requests_full_restore_once_when_guard_allows_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "GTA Criminal Russia"
            root.mkdir()
            (root / "gta_sa.exe").write_bytes(b"fake executable")
            api = self.make_api_stub(root)
            expected = {"ok": True, "message": "started"}

            with (
                patch.object(LauncherApi, "_auto_repair_allowed", return_value=True),
                patch.object(LauncherApi, "_begin_install", return_value=expected) as begin,
                patch("web_runtime.write_launcher_log"),
            ):
                started = LauncherApi._request_auto_repair(api, root, "integrity mismatch")

            self.assertTrue(started)
            begin.assert_called_once_with(reinstall=True, auto_repair_root=root)


if __name__ == "__main__":
    unittest.main(verbosity=2)
