"""Batch pause waits for the game to exit and keeps the existing API error contract."""
import threading
import unittest
from unittest.mock import Mock

from astral_party_auto.mod_controller import ModController
from astral_party_auto.web_app import DesktopApi


class PauseControllerTests(unittest.TestCase):
    def controller(self, *, catalog=False, native=False):
        controller = object.__new__(ModController)
        controller.manager = Mock()
        controller.manager.requires_catalog_reload.return_value = catalog
        controller.manager.requires_native_ui_reload.return_value = native
        controller.manager.installed_mods.return_value = [{"name": "One", "disabled": False}]
        controller.manager.disable_all_mods.return_value = 3
        controller._require_game = Mock()
        controller._require_catalog_game_stopped = Mock()
        controller.log = Mock()
        return controller

    def test_dynamic_and_native_batches_check_game_stopped_before_mutation(self):
        for catalog, native in ((True, False), (False, True)):
            with self.subTest(catalog=catalog, native=native):
                controller = self.controller(catalog=catalog, native=native)
                controller._require_catalog_game_stopped.side_effect = RuntimeError("请先退出游戏")
                with self.assertRaisesRegex(RuntimeError, "退出游戏"):
                    controller.disable_all_mods()
                controller.manager.disable_all_mods.assert_not_called()

    def test_ordinary_batch_requires_game_stopped_and_preserves_result(self):
        controller = self.controller()
        self.assertEqual(controller.disable_all_mods(), 3)
        controller._require_game.assert_called_once_with()
        controller._require_catalog_game_stopped.assert_called_once_with()
        controller.manager.disable_all_mods.assert_called_once_with()
        self.assertIn("记录和备份保留", controller.log.call_args.args[0])

    def test_running_game_blocks_ordinary_batch_before_manager_mutation(self):
        controller = self.controller()
        controller._require_catalog_game_stopped.side_effect = RuntimeError("请先退出游戏")
        with self.assertRaisesRegex(RuntimeError, "退出游戏"):
            controller.disable_all_mods()
        controller.manager.disable_all_mods.assert_not_called()
        controller.log.assert_not_called()

    def test_empty_or_already_paused_library_is_a_no_op_even_with_game_running(self):
        for records in ([], [{"name": "One", "disabled": True}]):
            with self.subTest(records=records):
                controller = self.controller(catalog=True, native=True)
                controller.manager.installed_mods.return_value = records
                controller.manager.disable_all_mods.return_value = 0
                controller._require_catalog_game_stopped.side_effect = RuntimeError("游戏正在运行")
                self.assertEqual(controller.disable_all_mods(), 0)
                controller._require_catalog_game_stopped.assert_not_called()
                controller.manager.disable_all_mods.assert_called_once_with()

    def test_missing_game_never_mutates_manager(self):
        controller = self.controller()
        controller._require_game.side_effect = RuntimeError("未连接游戏")
        with self.assertRaisesRegex(RuntimeError, "未连接"):
            controller.disable_all_mods()
        controller.manager.disable_all_mods.assert_not_called()


class PauseApiTests(unittest.TestCase):
    def setUp(self):
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api.controller = Mock()
        self.api.controller.disable_all_mods.return_value = 3
        self.api.controller.installed_mods.return_value = [{"name": "One", "disabled": True}]
        self.api._dashboard_state = Mock(return_value={"has_game": True, "installed_count": 1})

    def test_pause_exposes_current_installed_records_after_success(self):
        result = self.api.disable_all_mods()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["restored"], 3)
        self.assertEqual(result["data"]["installed"], [{"name": "One", "disabled": True}])
        self.api.controller.disable_all_mods.assert_called_once_with()
        self.assertTrue(DesktopApi.disable_all_mods._exposed)

    def test_failed_pause_does_not_publish_a_false_success_snapshot(self):
        self.api.controller.disable_all_mods.side_effect = RuntimeError("原始备份缺失")
        result = self.api.disable_all_mods()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "原始备份缺失")
        self.api.controller.installed_mods.assert_not_called()
        self.api._dashboard_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
