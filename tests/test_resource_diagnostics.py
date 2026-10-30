"""资源诊断只读取当前安装的有限元数据，不改变游戏或候选状态。"""
from __future__ import annotations

import hashlib
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astral_party_auto.modkit.resource_diagnostics import resource_diagnostics
from astral_party_auto.web_app import DesktopApi


class ResourceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="jixing-diagnostics-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.executable = self.root / "game" / "RenamedClient.exe"
        self.data = self.executable.parent / "RenamedClient_Data"
        self.data.mkdir(parents=True)
        (self.data / "app.info").write_text("feimo\nAstralParty_CN\n", encoding="utf-8")
        self.local_low = self.root / "redirected-low"
        self.user_metadata = self.local_low / "feimo" / "AstralParty_CN" / "com.unity.addressables"
        self.user_metadata.mkdir(parents=True)
        self.catalog = self.user_metadata / "catalog_3.3.0.json"
        self.catalog.write_bytes(b"catalog contents must not be changed")
        self.controller = SimpleNamespace(game_executable=self.executable, aa_dirs=(self.data / "cache",),
                                          selection=None)
        self.enterContext(patch("astral_party_auto.modkit.resource_diagnostics.local_low_dir",
                                return_value=self.local_low))
        self.enterContext(patch("astral_party_auto.modkit.bundles.unity_catalog_roots_for_exe",
                                return_value=(self.data / "com.unity.addressables", self.user_metadata)))

    def write_valid_catalog(self, *, video_type="UnityEngine.Material", omit=()):
        from tests.test_visual_catalog import fixture
        self.catalog.write_text(json.dumps(fixture(video_type=video_type)), encoding="utf-8")
        self.bundle_root = self.data / "com.unity.addressables" / "AssetBundles"
        self.bundle_root.mkdir(parents=True, exist_ok=True)
        self.controller.aa_dirs = (self.bundle_root,)
        for name in ("config.bundle", "ui.bundle", "atlas.bundle", "video.bundle"):
            if name not in omit:
                (self.bundle_root / name).write_bytes(b"bundle must not be decoded")

    def api(self):
        api = object.__new__(DesktopApi)
        api._controller_lock = threading.RLock()
        api._append_log = Mock()
        api.controller = self.controller
        api._pick_path = Mock(return_value=self.root / "report.txt")
        return api

    def test_uses_actual_executable_app_identity_and_system_local_low(self):
        self.controller.game_install = SimpleNamespace(exe=self.root / "launcher.exe")
        result = resource_diagnostics(self.controller, "1.3.8-test")
        text = result["text"]
        self.assertIn(str(self.executable), text)
        self.assertIn("Unity 公司：feimo", text)
        self.assertIn("Unity 产品：AstralParty_CN", text)
        self.assertIn(str(self.user_metadata), text)
        self.assertIn("catalog_3.3.0.json", text)
        self.assertNotIn("launcher.exe", text)
        self.assertNotIn(str(self.local_low / "feimo" / "RenamedClient"), text)
        self.assertIn("1.3.8-test", text)
        self.assertIn(result["checked_at"], text)

    def test_lists_metadata_when_assetbundles_directory_does_not_exist(self):
        report = resource_diagnostics(self.controller, "test")["text"]
        self.assertIn("catalog_3.3.0.json", report)
        self.assertIn(str(self.user_metadata / "AssetBundles") + "（不存在", report)

    def test_does_not_disclose_catalog_payload_or_change_source_files(self):
        before = {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                  for path in self.root.rglob("*") if path.is_file()}
        report = resource_diagnostics(self.controller, "test")["text"]
        after = {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertNotIn("catalog contents must not be changed", report)

    def test_fresh_check_finds_metadata_without_resource_subdirectory(self):
        self.write_valid_catalog()
        self.catalog.with_suffix(".hash").write_bytes(b"official-version")
        result = resource_diagnostics(self.controller, "1.3.8")
        checks = result["checks"]
        self.assertEqual(checks["catalog_path"], str(self.catalog))
        self.assertEqual(checks["emote"], "available")
        self.assertEqual(checks["visual"], "available")
        self.assertEqual(checks["emote_downloaded"], 1)
        self.assertEqual(checks["emote_total"], 1)
        self.assertEqual(checks["independent"], "version_verified")
        self.assertIn("UnityEngine.Material 1 项", result["text"])
        self.assertFalse((self.user_metadata / "AssetBundles").exists())

    def test_missing_configuration_is_distinct_from_missing_dynamic_dependency(self):
        for missing, expected in (("config.bundle", "missing_configuration"),
                                  ("video.bundle", "missing_dependencies")):
            with self.subTest(missing=missing):
                self.write_valid_catalog()
                (self.bundle_root / missing).unlink()
                result = resource_diagnostics(self.controller, "test")
                self.assertEqual(result["checks"]["emote"], expected)
                self.assertIn(missing, result["checks"]["missing_bundles"])
                self.assertIn(missing, result["text"])

    def test_missing_hash_does_not_hide_available_native_resources(self):
        self.write_valid_catalog()
        result = resource_diagnostics(self.controller, "test")
        self.assertEqual(result["checks"]["emote"], "available")
        self.assertEqual(result["checks"]["independent"], "missing_hash")
        self.assertIn("独立自定义导入不可用", result["text"])

    def test_unknown_resource_type_does_not_hide_card_dependency_result(self):
        self.write_valid_catalog(video_type="UnityEngine.Texture2D")
        result = resource_diagnostics(self.controller, "test")
        self.assertEqual(result["checks"]["emote"], "unknown_type")
        self.assertEqual(result["checks"]["visual"], "available")

    def test_oversized_catalog_is_rejected_before_payload_read(self):
        self.write_valid_catalog()
        with patch("astral_party_auto.modkit.visual_catalog.MAX_CATALOG_BYTES", 32), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("oversized payload read")):
            result = resource_diagnostics(self.controller, "test")
        self.assertEqual(result["checks"]["catalog"], "invalid_catalog")
        self.assertIn("超过 32 MB", result["text"])

    def test_excessive_json_nesting_keeps_the_path_report_available(self):
        self.catalog.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
        result = resource_diagnostics(self.controller, "test")
        self.assertEqual(result["checks"]["catalog"], "invalid_catalog")
        self.assertIn("JSON 嵌套过深", result["text"])
        self.assertIn(str(self.user_metadata), result["text"])
        self.assertIn("当前资源搜索目录", result["text"])

    def test_missing_catalog_does_not_borrow_international_install(self):
        self.catalog.unlink()
        other = self.local_low / "feimo" / "AstralParty_INT" / "com.unity.addressables"
        other.mkdir(parents=True)
        (other / "catalog_international_only.json").write_bytes(b"{}")
        result = resource_diagnostics(self.controller, "test")
        self.assertEqual(result["checks"]["catalog"], "unavailable")
        self.assertNotIn("catalog_international_only", result["text"])

    def test_assistant_entry_distinguishes_frozen_and_source_execution(self):
        with patch("astral_party_auto.modkit.resource_diagnostics.sys.frozen", True, create=True), \
                patch("astral_party_auto.modkit.resource_diagnostics.sys.executable", str(self.root / "helper.exe")):
            result = resource_diagnostics(self.controller, "test")
        self.assertIn("助手程序：" + str(self.root / "helper.exe"), result["text"])
        with patch("astral_party_auto.modkit.resource_diagnostics.sys.frozen", False, create=True):
            self.assertIn("源码运行", resource_diagnostics(self.controller, "test")["text"])

    def test_identity_or_system_path_failure_still_returns_diagnostics(self):
        (self.data / "app.info").write_text("invalid", encoding="utf-8")
        with patch("astral_party_auto.modkit.resource_diagnostics.local_low_dir",
                   side_effect=ValueError("known-folder unavailable")):
            text = resource_diagnostics(self.controller, "test")["text"]
        self.assertIn("known-folder unavailable", text)
        self.assertIn("Unity 身份读取失败", text)
        self.assertIn(str(self.data / "com.unity.addressables"), text)
        self.assertNotIn("用户元数据：", text)

    def test_unconnected_game_does_not_guess_another_install(self):
        self.controller.game_executable = None
        text = resource_diagnostics(self.controller, "test")["text"]
        self.assertIn("未连接实际游戏 exe", text)
        self.assertNotIn("catalog_3.3.0", text)

    def test_reports_selected_feature_errors_without_private_logs(self):
        self.controller.selection = {"name": "UT_HandCard_20022", "bundle": "hand.bundle",
                                     "emote": {"editable": False, "reason": "表情配置未找到\n已检查：C:\\example",
                                               "independent_supported": False, "independent_reason": "独立导入缺少 hash"},
                                     "visual_animation": {"editable": False, "reason": "当前目录身份未知"}}
        self.controller.token = "not-for-diagnostics"
        self.controller.logs = ["unrelated private log"]
        text = resource_diagnostics(self.controller, "test")["text"]
        self.assertIn("表情配置未找到\n已检查：C:\\example", text)
        self.assertIn("当前目录身份未知", text)
        self.assertIn("独立导入缺少 hash", text)
        self.assertIn("最近一次读取", text)
        self.assertNotIn("not-for-diagnostics", text)
        self.assertNotIn("unrelated private log", text)

    def test_catalog_listing_is_bounded_and_does_not_recurse(self):
        nested = self.user_metadata / "deep"
        nested.mkdir()
        (nested / "catalog_private.json").write_text("not included", encoding="utf-8")
        for number in range(6):
            (self.user_metadata / f"catalog_{number}.json").write_bytes(b"{}")
        with patch("astral_party_auto.modkit.resource_diagnostics.MAX_CATALOG_FILES", 2):
            text = resource_diagnostics(self.controller, "test")["text"]
        self.assertIn("元数据文件超过 2 项", text)
        self.assertNotIn("catalog_private.json", text)

    def test_directory_listing_failure_is_reported(self):
        with patch("astral_party_auto.modkit.resource_diagnostics.os.scandir",
                   side_effect=PermissionError("directory denied")):
            text = resource_diagnostics(self.controller, "test")["text"]
        self.assertIn("无法列出元数据：directory denied", text)

    def test_api_remains_exposed_and_preserves_current_candidates(self):
        api = object.__new__(DesktopApi)
        api._controller_lock = threading.RLock()
        api._append_log = Mock()
        api.controller = self.controller
        api._emote_replacement = {"candidate": "keep"}
        api._visual_candidate = {"candidate": "keep"}
        result = api.get_resource_diagnostics()
        self.assertTrue(result["ok"], result)
        self.assertIn("资源诊断", result["data"]["text"])
        self.assertEqual(api._emote_replacement, {"candidate": "keep"})
        self.assertEqual(api._visual_candidate, {"candidate": "keep"})
        self.assertTrue(DesktopApi.get_resource_diagnostics._exposed)

    def test_export_writes_the_displayed_snapshot_without_rescanning(self):
        api = self.api()
        report = api.get_resource_diagnostics()["data"]
        self.controller.selection = {"name": "changed after checking"}
        exported = api.export_resource_diagnostics(report["report_token"])
        self.assertTrue(exported["ok"], exported)
        saved = Path(exported["data"]["path"]).read_text(encoding="utf-8-sig")
        self.assertEqual(saved, report["text"])
        self.assertNotIn("changed after checking", saved)
        self.assertTrue(DesktopApi.export_resource_diagnostics._exposed)

    def test_cancel_export_preserves_the_current_report(self):
        api = self.api()
        report = api.get_resource_diagnostics()["data"]
        api._pick_path.return_value = None
        self.assertEqual(api.export_resource_diagnostics(report["report_token"]), {"ok": True, "data": None})
        self.assertEqual(api._resource_diagnostic_report["text"], report["text"])
        self.assertFalse((self.root / "report.txt").exists())

    def test_export_rejects_stale_report_or_changed_game_before_dialog(self):
        api = self.api()
        old = api.get_resource_diagnostics()["data"]
        self.controller.selection = {"name": "new selected resource"}
        latest = api.get_resource_diagnostics()["data"]
        self.assertNotEqual(old["report_token"], latest["report_token"])
        self.assertFalse(api.export_resource_diagnostics(old["report_token"])["ok"])
        self.controller.game_executable = self.root / "another" / "game.exe"
        self.assertFalse(api.export_resource_diagnostics(latest["report_token"])["ok"])
        api._pick_path.assert_not_called()

    def test_game_change_during_dialog_cannot_write_another_installs_report(self):
        api = self.api()
        report = api.get_resource_diagnostics()["data"]
        output = self.root / "report.txt"
        def choose(*_args, **_kwargs):
            self.controller.game_executable = self.root / "another" / "game.exe"
            return output
        api._pick_path.side_effect = choose
        result = api.export_resource_diagnostics(report["report_token"])
        self.assertFalse(result["ok"])
        self.assertFalse(output.exists())

    def test_export_cannot_overwrite_a_program_or_broken_existing_report(self):
        api = self.api()
        report = api.get_resource_diagnostics()["data"]
        executable = self.root / "keep.exe"
        executable.write_bytes(b"keep program")
        api._pick_path.return_value = executable
        self.assertFalse(api.export_resource_diagnostics(report["report_token"])["ok"])
        self.assertEqual(executable.read_bytes(), b"keep program")
        existing = self.root / "existing.txt"
        existing.write_bytes(b"keep previous report")
        api._pick_path.return_value = existing
        with patch("astral_party_auto.modkit.catalog_mods.atomic_bytes", side_effect=OSError("disk full")):
            self.assertFalse(api.export_resource_diagnostics(report["report_token"])["ok"])
        self.assertEqual(existing.read_bytes(), b"keep previous report")


if __name__ == "__main__":
    unittest.main()
