"""共存检查只读固定元数据，不把安装痕迹当成游戏内生效证明。"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from astral_party_auto.modkit.coexistence_diagnostics import (
    MAX_CONFIG_BYTES,
    coexistence_diagnostics,
    coexistence_report_lines,
)
from astral_party_auto.modkit.resource_diagnostics import resource_diagnostics


class CoexistenceDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="jixing-coexistence-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.game = self.root / "game"
        self.game.mkdir()
        self.executable = self.game / "AstralParty_CN.exe"
        self.executable.write_bytes(b"game fixture, never executed")

    def write_config(self, relative, value):
        path = self.game / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def inspect(self):
        result = coexistence_diagnostics(self.executable)
        return result, "\n".join(coexistence_report_lines(result))

    def entry(self, result, kind):
        return next(entry for entry in result["entries"] if entry["kind"] == kind)

    def test_unconnected_game_does_not_probe_any_file(self):
        with patch.object(Path, "lstat", side_effect=AssertionError("unexpected file probe")):
            result = coexistence_diagnostics(None)
        self.assertEqual(result["status"], "no_game")
        self.assertEqual(result["checked_paths"], [])
        self.assertIn("未连接游戏", "\n".join(coexistence_report_lines(result)))

    def test_no_traces_reports_exact_bounded_paths_without_recursing(self):
        unrelated = self.game / "private" / "BepInEx"
        unrelated.mkdir(parents=True)
        with patch("os.scandir", side_effect=AssertionError("must not enumerate directories")):
            result, text = self.inspect()
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(len(result["checked_paths"]), 8)
        self.assertTrue(all(Path(path).is_relative_to(self.game) for path in result["checked_paths"]))
        self.assertIn(str(self.game / "version.dll"), text)
        self.assertNotIn(str(unrelated), text)
        self.assertIn("未扫描其他位置", text)

    def test_proxy_dll_is_not_identified_as_cesium_and_is_never_read(self):
        proxy = self.game / "version.dll"
        proxy.write_bytes(b"unknown DLL contents must not be read")
        with patch("os.open", side_effect=AssertionError("DLL content must not be read")):
            result, text = self.inspect()
        self.assertEqual(self.entry(result, "proxy_dll")["status"], "present")
        self.assertFalse(result["runtime_confirmed"])
        self.assertFalse(result["possible_overrides"])
        self.assertIn("仅凭文件名不能确认是 CesiumLoader", text)
        self.assertNotIn("unknown DLL contents", text)

    def test_runtime_directory_alone_does_not_imply_active_overrides(self):
        (self.game / "AstralParty_ModLoader").mkdir()
        (self.game / "BepInEx").mkdir()
        result, text = self.inspect()
        self.assertEqual(result["status"], "found")
        self.assertFalse(result["possible_overrides"])
        self.assertIn("目录存在不代表加载器正在运行", text)
        self.assertIn("未扫描插件", text)

    def test_explicit_skin_settings_keep_enabled_and_selection_separate(self):
        self.write_config("doorstop_config.json", {"enabled": True})
        self.write_config("AstralParty_ModLoader/mods/CharacterSkinMod/config.json",
                          {"Enabled": True, "ActiveSchemeId": "demo", "ActivePackages": []})
        self.write_config("AstralParty_ModLoader/mods/CardSkinMod/config.json",
                          {"Enabled": True, "ActiveSkin": "__game_default__"})
        result, text = self.inspect()
        skin_entries = [entry for entry in result["entries"] if entry["kind"] == "skin_config"]
        self.assertEqual([(entry["enabled"], entry["has_selection"]) for entry in skin_entries],
                         [(True, True), (True, False)])
        self.assertTrue(result["possible_overrides"])
        self.assertIn("配置 enabled：显式启用", text)
        self.assertIn("只表示文件中的设置", text)
        self.assertIn("不能确认游戏内最终生效来源", text)
        self.assertNotIn("demo", text)  # 不回显用户的方案名或任意配置内容。

    def test_disabled_loader_does_not_report_configured_skin_as_override(self):
        self.write_config("doorstop_config.json", {"Enabled": False})
        self.write_config("AstralParty_ModLoader/mods/CardSkinMod/config.json",
                          {"Enabled": True, "ActiveSkin": "custom"})
        result, text = self.inspect()
        self.assertFalse(result["possible_overrides"])
        self.assertIn("配置 enabled：显式停用", text)

    def test_jsonc_comments_preserve_strings_and_do_not_assume_missing_boolean(self):
        path = self.game / "doorstop_config.json"
        path.write_text('{\n// user comment\n"enabled": true, /* one flag */ '
                        '"url": "https://example.invalid/a//b", "quoted": "a\\\"/*b"\n}', encoding="utf-8")
        self.write_config("AstralParty_ModLoader/mods/CardSkinMod/config.json",
                          {"Enabled": "true", "ActiveSkin": "custom", "private": "do not expose"})
        result, text = self.inspect()
        self.assertTrue(self.entry(result, "loader_config")["enabled"])
        self.assertIsNone(self.entry(result, "skin_config")["enabled"])
        self.assertFalse(result["possible_overrides"])
        self.assertNotIn("do not expose", text)
        self.assertNotIn("https://example.invalid", text)

    def test_broken_nested_or_duplicate_json_keeps_other_findings(self):
        (self.game / "version.dll").write_bytes(b"keep")
        invalid_values = ('{"Enabled": true, "enabled": false}', '[1,2]',
                          '[' * 2000 + '0' + ']' * 2000, '{/* incomplete')
        for contents in invalid_values:
            with self.subTest(contents=contents[:30]):
                (self.game / "doorstop_config.json").write_text(contents, encoding="utf-8")
                result, text = self.inspect()
                self.assertEqual(self.entry(result, "loader_config")["status"], "invalid")
                self.assertEqual(self.entry(result, "proxy_dll")["status"], "present")
                self.assertIn("格式不明确", text)

    def test_oversized_config_is_rejected_before_open(self):
        path = self.game / "doorstop_config.json"
        path.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
        with patch("os.open", side_effect=AssertionError("oversized config was opened")):
            result, text = self.inspect()
        self.assertEqual(self.entry(result, "loader_config")["status"], "oversized")
        self.assertIn("读取上限", text)

    def test_permission_failure_is_local_and_does_not_read_another_path(self):
        self.write_config("doorstop_config.json", {"enabled": True})
        (self.game / "BepInEx").mkdir()
        with patch("os.open", side_effect=PermissionError("not copied into report")):
            result, text = self.inspect()
        self.assertEqual(self.entry(result, "loader_config")["status"], "unreadable")
        self.assertEqual(self.entry(result, "bepinex")["status"], "present")
        self.assertNotIn("not copied into report", text)

    def test_reparse_directory_is_not_followed_even_with_a_readable_target(self):
        config = self.write_config("AstralParty_ModLoader/mods/CardSkinMod/config.json",
                                   {"Enabled": True, "ActiveSkin": "private"})
        actual_lstat = Path.lstat
        blocked = self.game / "AstralParty_ModLoader"

        def fake_lstat(path, *args, **kwargs):
            actual = actual_lstat(path, *args, **kwargs)
            if path == blocked:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return actual

        with patch.object(Path, "lstat", fake_lstat), \
                patch("os.open", side_effect=AssertionError("reparse point followed")):
            result, text = self.inspect()
        self.assertEqual(self.entry(result, "skin_config")["status"], "linked")
        self.assertIn(str(config), text)
        self.assertIn("重解析点，未跟随", text)
        self.assertFalse(result["possible_overrides"])

    def test_file_swap_between_probe_and_open_is_not_read(self):
        config = self.write_config("doorstop_config.json", {"enabled": True})
        actual_fstat = os.fstat

        def different_identity(descriptor):
            actual = actual_fstat(descriptor)
            return SimpleNamespace(st_mode=actual.st_mode, st_dev=actual.st_dev,
                                   st_ino=actual.st_ino + 1, st_file_attributes=0)

        with patch("os.fstat", different_identity):
            result, text = self.inspect()
        self.assertEqual(self.entry(result, "loader_config")["status"], "changed")
        self.assertIn("检查期间文件已变化", text)
        self.assertEqual(json.loads(config.read_text())["enabled"], True)

    def test_resource_mod_record_reports_counts_without_following_source_paths(self):
        self.write_config("AstralParty_ResourceMods/installed.json", {
            "Mods": [{"Id": "a", "Enabled": True, "SourceFile": "C:/private/archive.zip"},
                     {"Id": "b", "Enabled": False}],
            "Applied": {"Redirects": 3, "Messages": ["private log must not be exposed"]},
        })
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unexpected whole-file read")):
            result, text = self.inspect()
        manifest = self.entry(result, "resource_manifest")
        self.assertEqual((manifest["recorded_count"], manifest["enabled_count"], manifest["applied_redirects"]),
                         (2, 1, 3))
        self.assertTrue(result["possible_overrides"])
        self.assertIn("未比对当前 catalog", text)
        self.assertNotIn("C:/private", text)
        self.assertNotIn("private log", text)

    def test_resource_mod_counts_are_bounded_and_unknown_schema_stays_unknown(self):
        path = self.write_config("AstralParty_ResourceMods/installed.json",
                                 {"mods": [{"enabled": True}] * 5})
        with patch("astral_party_auto.modkit.coexistence_diagnostics.MAX_RESOURCE_MODS", 2):
            result, text = self.inspect()
        manifest = self.entry(result, "resource_manifest")
        self.assertEqual(manifest["enabled_count"], 2)
        self.assertEqual(manifest["recorded_count"], 5)
        self.assertTrue(manifest["count_limited"])
        self.assertIn("仅检查前 2 条", text)
        path.write_text('{"mods": "unknown shape"}', encoding="utf-8")
        result, _ = self.inspect()
        self.assertEqual(self.entry(result, "resource_manifest")["status"], "invalid")
        self.assertFalse(result["possible_overrides"])

    def test_diagnostics_include_coexistence_even_when_catalog_is_unavailable(self):
        self.write_config("AstralParty_ModLoader/mods/CardSkinMod/config.json",
                          {"Enabled": True, "ActiveSkin": "custom"})
        controller = SimpleNamespace(game_executable=self.executable, aa_dirs=(), selection=None)
        before = {str(path): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                  for path in self.game.rglob("*") if path.is_file()}
        with patch("astral_party_auto.modkit.resource_diagnostics.local_low_dir", return_value=self.root / "low"):
            result = resource_diagnostics(controller, "1.3.9-test")
        after = {str(path): (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
                 for path in self.game.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result["checks"]["catalog"], "unavailable")
        self.assertTrue(result["checks"]["coexistence"]["possible_overrides"])
        self.assertIn("外部加载器与资源 Mod", result["text"])
        self.assertIn("安装后没有效果", result["text"])


if __name__ == "__main__":
    unittest.main()
