"""只有会改变独立表情目录的操作要求游戏已退出，不关闭用户进程。"""
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astral_party_auto.mod_controller import ModController


class EmoteGameProcessTests(unittest.TestCase):
    def setUp(self):
        self.controller = object.__new__(ModController)
        self.controller.game_install = SimpleNamespace(cn_exe=Path("D:/game/AstralParty_CN.exe"), int_exe=None)

    def test_running_game_refuses_catalog_change_without_stopping_process(self):
        response = SimpleNamespace(returncode=0, stdout=b'"AstralParty_CN.exe","123","Console","1","123 K"\r\n')
        with patch("astral_party_auto.mod_controller.subprocess.run", return_value=response) as runner:
            with self.assertRaisesRegex(RuntimeError, "先退出游戏"):
                self.controller._require_catalog_game_stopped()
            self.assertEqual(runner.call_args.args[0][0], "tasklist")

    def test_stopped_game_and_no_detected_install_are_allowed(self):
        with patch("astral_party_auto.mod_controller.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b"INFO: no tasks")):
            self.controller._require_catalog_game_stopped()
        self.controller.game_install = None
        with patch("astral_party_auto.mod_controller.subprocess.run") as runner:
            self.controller._require_catalog_game_stopped()
            runner.assert_not_called()

    def test_query_failure_does_not_proceed_on_unknown_process_state(self):
        for failure in (OSError("missing"), subprocess.TimeoutExpired("tasklist", 10)):
            with patch("astral_party_auto.mod_controller.subprocess.run", side_effect=failure):
                with self.assertRaisesRegex(RuntimeError, "无法确认"):
                    self.controller._require_catalog_game_stopped()
