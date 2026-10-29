"""资源诊断只读取当前安装的有限元数据，不改变游戏或候选状态。"""
from __future__ import annotations

import hashlib
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

    def test_does_not_read_catalog_payload_or_change_source_files(self):
        before = {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                  for path in self.root.rglob("*") if path.is_file()}
        report = resource_diagnostics(self.controller, "test")["text"]
        after = {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertNotIn("catalog contents must not be changed", report)

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


if __name__ == "__main__":
    unittest.main()
