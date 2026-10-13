"""动态界面资源的重载标记须跟随安装记录，同时兼容旧作品。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astral_party_auto.modkit.manager import ModManager


class NativeUiAnimationManagerTests(unittest.TestCase):
    def setUp(self):
        cache = Path(__file__).resolve().parents[1] / "qa-evidence" / "static-dynamic-20261013" / "work-cache"
        cache.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="manager-marker-", dir=cache)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.game = self.root / "game"
        self.game.mkdir()
        self.originals = {"a.bundle": b"ORIGINAL_A", "b.bundle": b"ORIGINAL_B"}
        self.replacements = {"a.bundle": b"ANIMATION_A", "b.bundle": b"ANIMATION_B"}
        for name, content in self.originals.items():
            (self.game / name).write_bytes(content)
        self.data = self.root / "data"
        self.manager = ModManager(self.game, self.data)

    def make_mod(self, name, *, native=False):
        folder = self.root / name
        folder.mkdir()
        for bundle_name, content in self.replacements.items():
            (folder / bundle_name).write_bytes(content)
        if native:
            self.write_metadata(folder, {"name": name, "items": [{"kind": "visual_animation", "name": "UT_Test"}]})
        return folder

    @staticmethod
    def write_metadata(folder, value):
        (folder / "mod_info.json").write_text(json.dumps(value), encoding="utf-8")

    def game_contents(self):
        return {name: (self.game / name).read_bytes() for name in self.originals}

    def test_native_marker_survives_disable_enable_and_clears_on_uninstall(self):
        source = self.make_mod("native", native=True)
        self.assertTrue(self.manager.has_native_ui_animation(source))
        self.assertFalse(self.manager.requires_native_ui_reload())
        self.manager.install_mod(source, "Native")
        self.assertEqual(self.game_contents(), self.replacements)
        self.assertTrue(self.manager.requires_native_ui_reload("Native"))
        self.assertTrue(self.manager.requires_native_ui_reload())
        self.assertTrue(json.loads(self.manager.state_path.read_text(encoding="utf-8"))["mods"]["Native"]["native_ui_animation"])

        self.assertEqual(self.manager.disable_mod("Native"), 2)
        self.assertEqual(self.game_contents(), self.originals)
        # 禁用的作品仍须在重新启用时触发客户端重载保护。
        self.manager = ModManager(self.game, self.data)
        self.assertTrue(self.manager.requires_native_ui_reload("Native"))
        self.assertTrue(self.manager.requires_native_ui_reload())
        self.assertEqual(self.manager.enable_mod("Native"), 2)
        self.assertEqual(self.game_contents(), self.replacements)
        self.assertTrue(self.manager.requires_native_ui_reload("Native"))
        self.assertEqual(self.manager.uninstall_mod("Native"), 2)
        self.assertEqual(self.game_contents(), self.originals)
        self.assertFalse(self.manager.requires_native_ui_reload("Native"))
        self.assertFalse(self.manager.requires_native_ui_reload())

    def test_installed_marker_does_not_depend_on_source_metadata_remaining(self):
        source = self.make_mod("native", native=True)
        self.manager.install_mod(source, "Native")
        (source / "mod_info.json").unlink()
        self.assertFalse(self.manager.has_native_ui_animation(source))
        self.manager = ModManager(self.game, self.data)
        self.assertTrue(self.manager.requires_native_ui_reload("Native"))
        self.assertEqual(self.manager.disable_mod("Native"), 2)
        self.assertEqual(self.manager.enable_mod("Native"), 2)
        self.assertTrue(self.manager.requires_native_ui_reload("Native"))

    def test_ordinary_mod_without_metadata_remains_compatible(self):
        source = self.make_mod("old_mod")
        self.assertFalse(self.manager.has_native_ui_animation(source))
        self.manager.install_mod(source, "Legacy")
        state = json.loads(self.manager.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("native_ui_animation", state["mods"]["Legacy"])
        self.manager = ModManager(self.game, self.data)
        self.assertFalse(self.manager.requires_native_ui_reload("Legacy"))
        self.assertFalse(self.manager.requires_native_ui_reload())
        self.assertEqual(self.manager.disable_mod("Legacy"), 2)
        self.assertEqual(self.game_contents(), self.originals)
        self.assertEqual(self.manager.enable_mod("Legacy"), 2)
        self.assertEqual(self.game_contents(), self.replacements)
        self.assertEqual(self.manager.uninstall_mod("Legacy"), 2)
        self.assertEqual(self.game_contents(), self.originals)

    def test_named_lookup_distinguishes_ordinary_native_and_missing_mods(self):
        native = self.make_mod("native", native=True)
        ordinary = self.make_mod("ordinary")
        self.manager.install_mod(native, "Native")
        self.manager.install_mod(ordinary, "Ordinary")
        self.assertTrue(self.manager.requires_native_ui_reload())
        self.assertTrue(self.manager.requires_native_ui_reload("Native"))
        self.assertFalse(self.manager.requires_native_ui_reload("Ordinary"))
        self.assertFalse(self.manager.requires_native_ui_reload("Missing"))
        self.manager.uninstall_mod("Native")
        self.assertFalse(self.manager.requires_native_ui_reload())
        self.assertEqual(self.game_contents(), self.replacements)

    def test_same_name_reinstall_replaces_the_marker_with_new_work_type(self):
        native = self.make_mod("native", native=True)
        ordinary = self.make_mod("ordinary")
        self.manager.install_mod(native, "Shared name")
        self.assertTrue(self.manager.requires_native_ui_reload("Shared name"))
        self.manager.install_mod(ordinary, "Shared name")
        self.assertFalse(self.manager.requires_native_ui_reload("Shared name"))
        self.manager.install_mod(native, "Shared name")
        self.assertTrue(self.manager.requires_native_ui_reload("Shared name"))

    def test_failed_install_cannot_publish_a_native_marker(self):
        ordinary = self.make_mod("ordinary")
        native = self.make_mod("native", native=True)
        self.manager.install_mod(ordinary, "Ordinary")
        before = self.manager.state_path.read_bytes()
        with patch.object(self.manager, "_save_state", side_effect=OSError("full disk")), self.assertRaises(OSError):
            self.manager.install_mod(native, "Native")
        self.assertEqual(self.manager.state_path.read_bytes(), before)
        self.assertEqual(self.game_contents(), self.replacements)
        self.assertFalse(self.manager.requires_native_ui_reload())
        self.assertFalse(self.manager.requires_native_ui_reload("Native"))

    def test_restore_all_removes_marker_and_restores_original_bytes(self):
        self.manager.install_mod(self.make_mod("native", native=True), "Native")
        self.assertEqual(self.manager.restore_all(), 2)
        self.assertEqual(self.game_contents(), self.originals)
        self.assertFalse(self.manager.requires_native_ui_reload())

    def test_only_visual_animation_item_kind_requests_native_ui_reload(self):
        source = self.make_mod("metadata")
        cases = [None, [], "visual_animation", {"items": None}, {"items": {}},
                 {"items": [None, "visual_animation", [], {"kind": "emote"}, {"kind": "texture"}]},
                 {"items": [{"kind": "Visual_Animation"}]}, {"items": [{"kind": True}]}]
        for value in cases:
            with self.subTest(metadata=value):
                self.write_metadata(source, value)
                self.assertFalse(self.manager.has_native_ui_animation(source))
        self.write_metadata(source, {"items": [None, {"kind": "emote"}, {"kind": "visual_animation"}]})
        self.assertTrue(self.manager.has_native_ui_animation(source))

    def test_metadata_accepts_bom_and_rejects_missing_invalid_or_unreadable_content(self):
        source = self.make_mod("metadata")
        info = source / "mod_info.json"
        self.assertFalse(self.manager.has_native_ui_animation(source))
        info.mkdir()
        self.assertFalse(self.manager.has_native_ui_animation(source))
        info.rmdir()
        for payload in (b"", b"{", b"\xff", b'{"items":[{"kind":"visual_animation"}]} trailing'):
            with self.subTest(payload=payload):
                info.write_bytes(payload)
                self.assertFalse(self.manager.has_native_ui_animation(source))
        info.write_text(json.dumps({"items": [{"kind": "visual_animation"}]}), encoding="utf-8-sig")
        self.assertTrue(self.manager.has_native_ui_animation(source))
        with patch.object(Path, "read_text", side_effect=PermissionError("locked")):
            self.assertFalse(self.manager.has_native_ui_animation(source))

    def test_metadata_size_is_bounded_before_reading(self):
        source = self.make_mod("metadata")
        info = source / "mod_info.json"
        body = json.dumps({"items": [{"kind": "visual_animation"}]}).encode("utf-8")
        limit = 2 * 1024 * 1024
        info.write_bytes(body + b" " * (limit - len(body)))
        self.assertTrue(self.manager.has_native_ui_animation(source))
        info.write_bytes(body + b" " * (limit + 1 - len(body)))
        with patch.object(Path, "read_text", side_effect=AssertionError("oversized metadata must not be read")):
            self.assertFalse(self.manager.has_native_ui_animation(source))

    def test_deeply_nested_invalid_metadata_is_ignored(self):
        source = self.make_mod("metadata")
        (source / "mod_info.json").write_bytes(b"[" * 1200 + b"0" + b"]" * 1200)
        self.assertFalse(self.manager.has_native_ui_animation(source))


if __name__ == "__main__":
    unittest.main()
