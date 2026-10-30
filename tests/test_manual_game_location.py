"""手动客户端选择应保留旧状态，并明确隔离国服和国际服。"""
from __future__ import annotations

import json
import hashlib
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from astral_party_auto.core.detector import manual_game_install
from astral_party_auto import mod_controller
from astral_party_auto.web_app import DesktopApi


class ManualGameLocationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-manual-location-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.data = self.root / "helper-data"
        self.enterContext(patch.multiple(mod_controller, DATA_DIR=self.data,
            INDEX_CACHE=self.data / "index.json", DYNAMIC_VALID_PATH=self.data / "dynamic.json"))
        self.enterContext(patch.object(mod_controller, "load_asset_index", return_value={
            "texture": {}, "text": {}, "mesh": {}, "anim": {}, "dynamic": {}}))
        self.enterContext(patch("astral_party_auto.modkit.unity_paths._local_low_dir_from_windows",
                                return_value=self.root / "redirected-low"))

    def executable(self, folder="手动游戏/客户端", *, region="CN", filename=None, company="feimo", product=None):
        target = self.root / folder / (filename or f"AstralParty_{region}.exe")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"read-only fixture executable")
        data = target.parent / f"{target.stem}_Data"
        bundles = data / "StreamingAssets" / "aa" / "StandaloneWindows64"
        bundles.mkdir(parents=True)
        (bundles / f"original-{region}.bundle").write_bytes(b"fixture")
        (data / "app.info").write_text(f"{company}\n{product or 'AstralParty_' + region}\n", encoding="utf-8")
        return target

    def controller(self):
        controller = object.__new__(mod_controller.ModController)
        controller.log = Mock()
        controller.game_executable = None
        controller.game_install = None
        controller.aa_dirs = ()
        controller.aa_dir = None
        controller.manager = None
        controller._index_source_key = ""
        controller.typed_index = {"texture": {}, "text": {}, "mesh": {}, "anim": {}, "dynamic": {}}
        controller.selection = {"name": "old selection"}
        return controller

    def test_manual_chinese_path_is_saved_and_restored_without_auto_detector(self):
        target = self.executable()
        controller = self.controller()
        with patch.object(mod_controller, "find_game_install") as automatic:
            controller.set_manual_game_location(target.parent)
            self.assertEqual(controller.game_executable, target)
            self.assertTrue(controller.has_game)
            self.assertEqual(controller.game_location_mode, "manual")
            restart = self.controller()
            restart.refresh_detection()
            self.assertEqual(restart.game_executable, target)
            automatic.assert_not_called()
        self.assertEqual(json.loads((self.data / "game_location.json").read_text(encoding="utf-8"))["executable"], str(target))

    def test_dual_install_requires_explicit_executable_and_keeps_int_identity(self):
        cn = self.executable("dual/8vJXn6CN")
        international = self.executable("dual/8vJXnINT", region="INT")
        with self.assertRaisesRegex(ValueError, "多个客户端"):
            manual_game_install(self.root / "dual")
        controller = self.controller()
        controller.set_manual_game_location(international)
        self.assertIsNone(controller.game_install.cn_exe)
        self.assertEqual(controller.game_install.int_exe, international)
        self.assertEqual(controller.game_executable, international)
        self.assertTrue(all("8vJXn6CN" not in str(folder) for folder in controller.aa_dirs))
        self.assertTrue(cn.is_file())

    def test_identity_is_read_from_app_info_even_for_renamed_executable(self):
        target = self.executable(filename="我的客户端.exe")
        self.assertEqual(manual_game_install(target).cn_exe, target)
        other = self.executable("other", company="OtherCompany")
        with self.assertRaisesRegex(ValueError, "不是已识别"):
            manual_game_install(other)
        launcher = self.root / "launcher.exe"
        launcher.write_bytes(b"launcher")
        with self.assertRaisesRegex(ValueError, "安装信息"):
            manual_game_install(launcher)

    def test_data_directory_and_non_game_file_are_rejected(self):
        target = self.executable()
        with self.assertRaises(ValueError):
            manual_game_install(target.parent / f"{target.stem}_Data")
        with self.assertRaises(ValueError):
            manual_game_install(target.with_suffix(".bundle"))

    def test_folder_scan_is_bounded(self):
        folder = self.root / "not-game"
        folder.mkdir()
        for number in range(257):
            (folder / f"file-{number}.txt").write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "内容太多"):
            manual_game_install(folder)

    def test_failed_validation_and_atomic_save_keep_previous_connection_and_setting(self):
        previous = self.executable("previous")
        replacement = self.executable("replacement", region="INT")
        controller = self.controller()
        controller.set_manual_game_location(previous)
        controller.selection = {"name": "selected"}
        old_setting = (self.data / "game_location.json").read_bytes()
        with self.assertRaises(ValueError):
            controller.set_manual_game_location(self.root / "unknown.exe")
        with patch("astral_party_auto.modkit.catalog_mods.atomic_bytes", side_effect=OSError("controlled save failure")):
            with self.assertRaises(OSError):
                controller.set_manual_game_location(replacement)
        self.assertEqual(controller.game_executable, previous)
        self.assertEqual(controller.selection, {"name": "selected"})
        self.assertEqual((self.data / "game_location.json").read_bytes(), old_setting)

    def test_missing_saved_client_does_not_silently_switch_to_auto_or_launch_steam(self):
        target = self.executable()
        controller = self.controller()
        controller.set_manual_game_location(target)
        target.unlink()
        with patch.object(mod_controller, "find_game_install") as automatic, \
                patch.object(mod_controller, "launch_with_steam") as launcher:
            controller.refresh_detection()
            self.assertFalse(controller.has_game)
            self.assertIn("手动游戏位置暂不可用", controller.game_detection_note)
            with self.assertRaises(RuntimeError):
                controller.launch_game()
            automatic.assert_not_called()
            launcher.assert_not_called()

    def test_restore_auto_reselects_detected_install_and_persists_mode(self):
        manual = self.executable("manual", region="INT")
        detected = self.executable("steam")
        controller = self.controller()
        controller.set_manual_game_location(manual)
        with patch.object(mod_controller, "find_game_install", return_value=manual_game_install(detected)):
            controller.restore_automatic_game_location()
            self.assertEqual(controller.game_executable, detected)
            self.assertEqual(controller.game_location_mode, "auto")
            self.assertIsNone(controller._read_game_location())

    def test_existing_installed_mods_prevent_moving_backups_to_other_client(self):
        previous = self.executable("previous")
        replacement = self.executable("replacement", region="INT")
        controller = self.controller()
        controller.set_manual_game_location(previous)
        controller.manager.installed_mods = Mock(return_value=[{"name": "existing mod"}])
        with self.assertRaisesRegex(ValueError, "先在原客户端卸载或还原"):
            controller.set_manual_game_location(replacement)
        self.assertEqual(controller.game_executable, previous)

    def test_stale_manual_path_after_restart_cannot_bypass_installed_mod_guard(self):
        previous = self.executable("previous")
        other = self.executable("other", region="INT")
        first = self.controller()
        first.set_manual_game_location(previous)
        setting = (self.data / "game_location.json").read_bytes()
        (self.data / "mods_state.json").write_text('{"mods":{"original":{"files":["original.bundle"]}}}', encoding="utf-8")
        previous.unlink()
        restarted = self.controller()
        restarted.refresh_detection()
        self.assertIsNone(restarted.manager)
        with self.assertRaisesRegex(ValueError, "原客户端仍有"):
            restarted.set_manual_game_location(other)
        self.assertEqual((self.data / "game_location.json").read_bytes(), setting)
        previous.write_bytes(b"repaired original executable")
        restarted.set_manual_game_location(previous)
        self.assertEqual(restarted.game_executable, previous)

    def test_unknown_auto_origin_with_old_records_is_refused_without_deleting_them(self):
        target = self.executable()
        self.data.mkdir()
        (self.data / "game_location.json").write_text('{"version":1,"mode":"auto"}', encoding="utf-8")
        original = b'{"mods":{"original":{"files":["original.bundle"]}}}'
        state_path = self.data / "mods_state.json"
        state_path.write_bytes(original)
        controller = self.controller()
        with self.assertRaisesRegex(ValueError, "单独解压"):
            controller.set_manual_game_location(target)
        self.assertEqual(state_path.read_bytes(), original)
        self.assertIsNone(controller.game_executable)

    def test_remaining_backups_are_not_reused_for_another_client(self):
        previous = self.executable("previous")
        other = self.executable("other", region="INT")
        controller = self.controller()
        controller.set_manual_game_location(previous)
        backup = self.data / "backups" / "original.bundle"
        backup.parent.mkdir()
        backup.write_bytes(b"original backup must survive")
        with self.assertRaisesRegex(ValueError, "原客户端的备份"):
            controller.set_manual_game_location(other)
        self.assertEqual(backup.read_bytes(), b"original backup must survive")
        self.assertEqual(controller.game_executable, previous)

    def test_optional_validation_cache_failure_does_not_fail_committed_connection(self):
        target = self.executable()
        for failure in (OSError("cache unreadable"), ValueError("bad cache"), RecursionError("nested cache")):
            controller = self.controller()
            with patch.object(controller, "_load_dynamic_validation", side_effect=failure):
                controller.set_manual_game_location(target)
            self.assertTrue(controller.has_game)
            self.assertEqual(controller.game_executable, target)
            self.assertEqual(controller._valid_dynamic, {})
            self.assertEqual(controller._read_game_location(), target)

    def test_manual_to_missing_auto_does_not_drop_origin_or_allow_later_int(self):
        original = self.executable("original")
        international = self.executable("international", region="INT")
        controller = self.controller()
        controller.set_manual_game_location(original)
        (self.data / "mods_state.json").write_text('{"mods":{"old":{"files":["original-CN.bundle"]}}}', encoding="utf-8")
        before = (self.data / "game_location.json").read_bytes()
        with patch.object(mod_controller, "find_game_install", return_value=None):
            with self.assertRaisesRegex(ValueError, "原游戏位置和备份记录保持不变"):
                controller.restore_automatic_game_location()
        self.assertEqual(controller.game_executable, original)
        self.assertEqual((self.data / "game_location.json").read_bytes(), before)
        with patch.object(mod_controller, "find_game_install", return_value=manual_game_install(international)):
            controller.refresh_detection()
        self.assertEqual(controller.game_executable, original)
        self.assertEqual(controller.game_location_mode, "manual")

    def test_auto_refresh_cannot_switch_existing_cn_mods_to_int(self):
        original = self.executable("original")
        international = self.executable("international", region="INT")
        controller = self.controller()
        with patch.object(mod_controller, "find_game_install", return_value=manual_game_install(original)):
            controller.refresh_detection()
        self.assertEqual(controller._read_last_game_executable(), original)
        (self.data / "mods_state.json").write_text('{"mods":{"old":{"files":["original-CN.bundle"]}}}', encoding="utf-8")
        before = (self.data / "game_location.json").read_bytes()
        with patch.object(mod_controller, "find_game_install", return_value=manual_game_install(international)):
            controller.refresh_detection()
        self.assertEqual(controller.game_executable, original)
        self.assertIn("自动检测没有切换", controller.game_detection_note)
        self.assertEqual((self.data / "game_location.json").read_bytes(), before)

    def test_auto_origin_survives_restart_when_original_executable_disappears(self):
        original = self.executable("original")
        international = self.executable("international", region="INT")
        initial = self.controller()
        with patch.object(mod_controller, "find_game_install", return_value=manual_game_install(original)):
            initial.refresh_detection()
        (self.data / "mods_state.json").write_text('{"mods":{"old":{"files":["original-CN.bundle"]}}}', encoding="utf-8")
        original.unlink()
        restarted = self.controller()
        with patch.object(mod_controller, "find_game_install", return_value=manual_game_install(international)):
            restarted.refresh_detection()
        self.assertIsNone(restarted.game_executable)
        self.assertIsNone(restarted.manager)
        self.assertEqual(restarted._read_last_game_executable(), original)
        self.assertIn("原客户端仍有", restarted.game_detection_note)

    def test_auto_without_installed_data_can_change_install_and_remember_it(self):
        original = self.executable("original")
        international = self.executable("international", region="INT")
        controller = self.controller()
        with patch.object(mod_controller, "find_game_install", return_value=manual_game_install(original)):
            controller.refresh_detection()
        with patch.object(mod_controller, "find_game_install", return_value=manual_game_install(international)):
            controller.refresh_detection()
        self.assertEqual(controller.game_executable, international)
        self.assertEqual(controller._read_last_game_executable(), international)

    def legacy_mod(self, target, *, match=True):
        self.data.mkdir(exist_ok=True)
        store = self.data / "mod_store" / "old"
        store.mkdir(parents=True)
        filename = "original-CN.bundle"
        actual = target.parent / f"{target.stem}_Data" / "StreamingAssets" / "aa" / "StandaloneWindows64" / filename
        actual.write_bytes(b"installed legacy mod")
        (store / filename).write_bytes(b"installed legacy mod" if match else b"wrong legacy content")
        backups = self.data / "backups"
        backups.mkdir()
        (backups / filename).write_bytes(b"original game bytes")
        state = {"mods": {"old": {"files": [filename], "store": str(store), "disabled": False}}}
        (self.data / "mods_state.json").write_text(json.dumps(state), encoding="utf-8")
        return actual

    def test_old_auto_without_origin_can_bind_after_all_managed_bytes_match(self):
        original = self.executable("legacy")
        actual = self.legacy_mod(original)
        before = actual.read_bytes()
        controller = self.controller()
        with patch.object(mod_controller, "find_game_install", return_value=manual_game_install(original)):
            controller.refresh_detection()
        self.assertEqual(controller.game_executable, original)
        self.assertEqual(controller._read_last_game_executable(), original)
        self.assertEqual(actual.read_bytes(), before)
        self.assertEqual((self.data / "backups" / actual.name).read_bytes(), b"original game bytes")

    def test_old_auto_without_origin_cannot_bind_by_bundle_name_alone(self):
        original = self.executable("legacy")
        actual = self.legacy_mod(original, match=False)
        before = (self.data / "mods_state.json").read_bytes()
        controller = self.controller()
        with patch.object(mod_controller, "find_game_install", return_value=manual_game_install(original)):
            controller.refresh_detection()
        self.assertIsNone(controller.manager)
        self.assertIsNone(controller.game_executable)
        self.assertFalse((self.data / "game_location.json").exists())
        self.assertEqual((self.data / "mods_state.json").read_bytes(), before)
        self.assertEqual(actual.read_bytes(), b"installed legacy mod")

    def test_legacy_overlay_verification_enforces_shared_file_and_byte_budget(self):
        original = self.executable("legacy")
        controller = self.controller()
        connection = controller._prepare_game_connection(original)
        target = self.root / "catalog.json"
        target.write_bytes(b"{}")
        digest = hashlib.sha256(b"{}").hexdigest()
        baseline = self.data / "catalog_backups" / f"catalog-{digest}.json"
        baseline.parent.mkdir(parents=True)
        baseline.write_bytes(b"{}")
        overlay = {"bundle_root": str(connection["directories"][0]), "target": str(target),
            "applied_sha256": digest, "official_hash": "version", "baseline_sha256": digest,
            "bundles": {f"jixing_emote_{number}.bundle": "0" * 64 for number in range(257)}}
        with patch("astral_party_auto.modkit.catalog_mods.active_catalog_path", return_value=target), \
                patch("astral_party_auto.modkit.catalog_mods._official_hash", return_value="version"), \
                patch("astral_party_auto.modkit.catalog_mods.file_sha256", return_value=digest) as checksum:
            self.assertFalse(controller._legacy_game_data_matches(connection, {"mods": {}, "catalog_overlay": overlay}))
            self.assertEqual(checksum.call_count, 2, "oversized owned list started hashing resource bundles")
            checksum.reset_mock()
            with patch.object(Path, "stat", return_value=SimpleNamespace(st_size=513 * 1024 * 1024)):
                self.assertFalse(controller._legacy_game_data_matches(connection, {"mods": {}, "catalog_overlay": overlay}))
            checksum.assert_not_called()


class ManualGameLocationApiTests(unittest.TestCase):
    def setUp(self):
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api.controller = Mock()
        self.api.controller.installed_mods.return_value = []
        self.api._pick_path = Mock(return_value=Path("D:/fixture/AstralParty_CN.exe"))
        self.api._dashboard_state = Mock(return_value={"has_game": True, "game_location_mode": "manual"})
        self.clearers = []
        for name in ("_clear_workshop_pending", "_clear_emote_candidate", "_clear_visual_candidate",
                     "_clear_replacement", "_clear_draft_crop"):
            method = Mock()
            setattr(self.api, name, method)
            self.clearers.append(method)

    def test_cancel_and_invalid_selection_preserve_current_candidates(self):
        self.api._pick_path.return_value = None
        self.assertEqual(self.api.choose_game_location("program"), {"ok": True, "data": None})
        self.api.controller.set_manual_game_location.assert_not_called()
        self.api._pick_path.return_value = Path("D:/wrong.exe")
        self.api.controller.set_manual_game_location.side_effect = ValueError("wrong client")
        self.assertFalse(self.api.choose_game_location("program")["ok"])
        for clearer in self.clearers:
            clearer.assert_not_called()

    def test_success_clears_old_candidates_after_connection_and_keeps_http_contract(self):
        result = self.api.choose_game_location("program")
        self.assertTrue(result["ok"], result.get("error"))
        self.api.controller.set_manual_game_location.assert_called_once_with(Path("D:/fixture/AstralParty_CN.exe"))
        self.assertEqual(result["data"]["dashboard"]["game_location_mode"], "manual")
        for clearer in self.clearers:
            clearer.assert_called_once_with()
        self.assertIsNone(self.api.controller.selection)

    def test_invalid_dialog_kind_does_not_open_dialog_or_change_settings(self):
        self.assertFalse(self.api.choose_game_location("execute")["ok"])
        self.api._pick_path.assert_not_called()
        self.api.controller.set_manual_game_location.assert_not_called()

    def test_restore_auto_failure_preserves_candidates(self):
        self.api.controller.restore_automatic_game_location.side_effect = OSError("locked setting")
        self.assertFalse(self.api.restore_automatic_game_location()["ok"])
        for clearer in self.clearers:
            clearer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
