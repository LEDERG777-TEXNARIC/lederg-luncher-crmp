from __future__ import annotations

import unittest
from pathlib import Path


class DirectLauncherRegistryTests(unittest.TestCase):
    def test_direct_helper_registers_selected_game_installation(self):
        source = Path(__file__).resolve().parents[1] / "direct_launcher.cs"
        text = source.read_text(encoding="utf-8")
        self.assertIn('key.SetValue("PlayerName", nickname, RegistryValueKind.String)', text)
        self.assertIn('key.SetValue("gta_sa_exe", game, RegistryValueKind.String)', text)
        self.assertIn('key.SetValue("InstallDir", gameDirectory, RegistryValueKind.String)', text)

    def test_direct_helper_has_extended_loader_timeout_and_actionable_error(self):
        source = Path(__file__).resolve().parents[1] / "direct_launcher.cs"
        text = source.read_text(encoding="utf-8")
        self.assertIn("WaitForSingleObject(remoteThread, 45000)", text)
        self.assertIn("Полный путь к DLL и этап запуска записаны в журнал", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
