from __future__ import annotations

import unittest
from pathlib import Path


class DirectLauncherSourceTests(unittest.TestCase):
    def test_game_is_created_suspended_before_multiplayer_injection(self):
        source = Path(__file__).resolve().parents[1].joinpath("direct_launcher.cs").read_text(encoding="utf-8")
        create = source.index("CreateProcess(game,")
        create_flags = source.index("DETACHED_PROCESS | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT", create)
        inject = source.index("CreateRemoteThread(", create)
        resume = source.index("ResumeThread(pi.hThread)", inject)
        self.assertLess(create, create_flags)
        self.assertLess(create_flags, inject)
        self.assertLess(inject, resume)
        self.assertIn("if (resumeResult == 0xFFFFFFFF)", source)

    def test_nickname_is_sanitized_and_passed_to_native_client(self):
        source = Path(__file__).resolve().parents[1].joinpath("direct_launcher.cs").read_text(encoding="utf-8")
        self.assertIn('"--nickname"', source)
        self.assertIn('key.SetValue("PlayerName", nickname, RegistryValueKind.String)', source)
        self.assertIn('" -c -n " + Quote(nickname)', source)
        self.assertIn('" -h " + host + " -p "', source)

    def test_launcher_ui_has_editable_nickname_field_and_sends_it_on_play(self):
        source = Path(__file__).resolve().parents[1].joinpath("web/index.html").read_text(encoding="utf-8")
        self.assertIn('id="nickname"', source)
        self.assertIn('apiCall("launch_game", nickname)', source)
        self.assertIn('apiCall("set_nickname", nickname)', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
