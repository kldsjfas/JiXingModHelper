"""工作台跨步骤操作与贴图写入失败的隔离回归。"""
from __future__ import annotations

import io
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from astral_party_auto.modkit import maker
from astral_party_auto.modkit.manager import ModManager
from astral_party_auto.web_app import DesktopApi


class DraftCropSessionTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory(prefix="jixing-crop-session-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.picture = self.root / "replacement.png"
        Image.new("RGBA", (8, 8), "green").save(self.picture)
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._draft_crop_path = None
        self.api._draft_crop_index = None
        self.api._draft_crop_item = None
        self.api._image_cache = {}
        self.api.controller = Mock()
        self.first = {"kind": "texture", "bundle": "a.bundle", "name": "Portrait"}
        self.second = {"kind": "texture", "bundle": "b.bundle", "name": "Card"}
        self.api.controller.draft_items = [self.first, self.second]
        self.api.controller.original_bundle_path.return_value = None
        self.api.controller.update_draft_texture.side_effect = lambda index, *_a, **_k: self.api.controller.draft_items[index]
        self.api._pick_path = Mock(return_value=self.picture)
        self.api._draft_state = Mock(return_value={"items": []})
        self.api._dashboard_state = Mock(return_value={})

    def choose(self, index=0):
        chosen = self.api.pick_draft_crop_source(index)
        self.assertTrue(chosen["ok"], chosen.get("error"))

    def assert_no_old_crop(self):
        result = self.api.commit_draft_crop([0, 0, 4, 4])
        self.assertFalse(result["ok"])
        self.api.controller.update_draft_texture.assert_not_called()

    def test_removed_or_reordered_item_cannot_receive_another_items_crop(self):
        self.choose()
        self.api.controller.draft_items = [self.second]
        self.assert_no_old_crop()

    def test_changed_item_rejects_stale_crop(self):
        self.choose()
        self.api.controller.draft_items[0] = {**self.first, "note": "newer replacement"}
        self.assert_no_old_crop()

    def test_in_place_change_rejects_stale_crop(self):
        self.choose()
        self.first["note"] = "changed while the crop window was open"
        self.assert_no_old_crop()

    def test_cancelled_second_selection_clears_first_crop_session(self):
        self.choose()
        self.api._pick_path.return_value = None
        self.assertTrue(self.api.pick_draft_crop_source(1)["ok"])
        self.assert_no_old_crop()

    def test_failed_second_selection_clears_first_crop_session(self):
        self.choose()
        self.api._pick_path.return_value = self.root / "missing.png"
        self.assertFalse(self.api.pick_draft_crop_source(1)["ok"])
        self.assert_no_old_crop()

    def test_corrupt_second_image_clears_first_crop_session(self):
        self.choose()
        broken = self.root / "broken.png"
        broken.write_bytes(b"not a picture")
        self.api._pick_path.return_value = broken
        self.assertFalse(self.api.pick_draft_crop_source(1)["ok"])
        self.assert_no_old_crop()

    def test_failed_crop_commit_cannot_be_retried_on_a_different_item(self):
        self.choose()
        self.api.controller.update_draft_texture.side_effect = OSError("file is locked")
        self.assertFalse(self.api.commit_draft_crop([0, 0, 4, 4])["ok"])
        self.api.controller.update_draft_texture.reset_mock()
        self.api.controller.draft_items = [self.second]
        self.assert_no_old_crop()

    def test_unchanged_item_can_be_cropped_once(self):
        self.choose(1)
        result = self.api.commit_draft_crop([1, 2, 7, 8])
        self.assertTrue(result["ok"], result.get("error"))
        self.api.controller.update_draft_texture.assert_called_once_with(1, self.picture, crop_box=(1, 2, 7, 8))
        self.api.controller.update_draft_texture.reset_mock()
        self.assert_no_old_crop()


class TextureWriteFailureTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory(prefix="jixing-texture-write-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.picture = self.root / "replacement.png"
        Image.new("RGBA", (4, 4), "green").save(self.picture)
        self.output = self.root / "draft.bundle"
        self.output.write_bytes(b"previous complete draft")
        self.texture = SimpleNamespace(m_Name="Portrait", m_Width=4, m_Height=4,
                                       image=Image.new("RGBA", (4, 4), "red"), save=Mock())
        reader = SimpleNamespace(type=SimpleNamespace(name="Texture2D"), read=lambda: self.texture)
        self.environment = SimpleNamespace(objects=[reader], file=SimpleNamespace(save=lambda: b"new complete bundle"))

    @staticmethod
    def partial_writer(real_open):
        class FailingWriter:
            def __init__(self, file):
                self.file = file

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.file.close()

            def write(self, data):
                self.file.write(data[:4])
                self.file.flush()
                raise OSError("simulated disk write failure")

        def open_file(file, mode="r", *args, **kwargs):
            stream = real_open(file, mode, *args, **kwargs)
            return FailingWriter(stream) if "w" in mode and "b" in mode else stream

        return open_file

    def test_partial_texture_write_preserves_existing_draft(self):
        before = self.output.read_bytes()
        with patch.object(maker, "_load_bundle", return_value=self.environment), \
                patch("io.open", side_effect=self.partial_writer(io.open)):
            with self.assertRaises(OSError):
                maker.replace_bundle_texture(self.root / "source.bundle", self.picture,
                                             self.output, target_name="Portrait")
        self.assertEqual(self.output.read_bytes(), before)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_partial_cross_bundle_texture_write_preserves_existing_draft(self):
        before = self.output.read_bytes()
        with patch.object(maker, "_load_bundle", return_value=self.environment), \
                patch("io.open", side_effect=self.partial_writer(io.open)):
            with self.assertRaises(OSError):
                maker.replace_bundle_texture_from_bundle(self.root / "target.bundle", self.root / "source.bundle",
                                                         "Portrait", "Portrait", self.output)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_successful_texture_write_replaces_complete_payload(self):
        with patch.object(maker, "_load_bundle", return_value=self.environment):
            maker.replace_bundle_texture(self.root / "source.bundle", self.picture,
                                         self.output, target_name="Portrait")
        self.assertEqual(self.output.read_bytes(), b"new complete bundle")

    def test_locked_output_preserves_existing_draft(self):
        before = self.output.read_bytes()
        with patch.object(maker, "_load_bundle", return_value=self.environment), \
                patch.object(maker.os, "replace", side_effect=PermissionError("draft file is locked")):
            with self.assertRaises(PermissionError):
                maker.replace_bundle_texture(self.root / "source.bundle", self.picture,
                                             self.output, target_name="Portrait")
        self.assertEqual(self.output.read_bytes(), before)
        self.assertFalse(list(self.root.glob("*.tmp")))


class InstalledPreviewTests(unittest.TestCase):
    def test_missing_mod_source_does_not_copy_another_mod_from_game(self):
        with TemporaryDirectory(prefix="jixing-preview-source-") as folder:
            root = Path(folder)
            game = root / "game"
            game.mkdir()
            (game / "example.bundle").write_bytes(b"another mod is active")
            manager = ModManager(game, root / "data")
            store = root / "missing-cache"
            manager._save_state({"mods": {"Theme": {"files": ["example.bundle"], "store": str(store)}}})
            state_before = manager.state_path.read_bytes()
            api = object.__new__(DesktopApi)
            api.controller = SimpleNamespace(manager=manager, has_game=True, bundle_path=manager.bundle_path)
            with self.assertRaisesRegex(RuntimeError, "资源文件缺失"):
                api._load_mod_preview_impl("Theme")
            self.assertFalse(store.exists())
            self.assertEqual(manager.state_path.read_bytes(), state_before)
            self.assertEqual((game / "example.bundle").read_bytes(), b"another mod is active")

    def test_preview_uses_own_nested_source_without_rebuilding_cache(self):
        with TemporaryDirectory(prefix="jixing-preview-source-") as folder:
            root = Path(folder)
            source = root / "source" / "nested"
            source.mkdir(parents=True)
            bundle = source / "example.bundle"
            bundle.write_bytes(b"own mod")
            manager = ModManager(root / "game", root / "data")
            store = root / "missing-cache"
            manager._save_state({"mods": {"Theme": {"files": [bundle.name], "store": str(store), "source": str(source.parent)}}})
            api = object.__new__(DesktopApi)
            api.controller = SimpleNamespace(manager=manager)
            api._mod_preview_title = "已装 · Theme"
            api._set_mod_preview_files = Mock(return_value=[bundle.name])
            api._preview_mod_bundle_impl = Mock(return_value={})
            state_before = manager.state_path.read_bytes()
            with patch("astral_party_auto.web_app.DATA_DIR", root / "data"):
                result = api._load_mod_preview_impl("Theme")
            self.assertEqual(result["bundles"], [bundle.name])
            api._set_mod_preview_files.assert_called_once_with({bundle.name: bundle}, "已装 · Theme")
            self.assertEqual(manager.state_path.read_bytes(), state_before)
            self.assertFalse(store.exists())

    def test_legacy_store_without_recorded_directory_can_be_previewed(self):
        with TemporaryDirectory(prefix="jixing-preview-legacy-") as folder:
            root = Path(folder)
            data = root / "data"
            store = data / "mod_store" / "Theme"
            store.mkdir(parents=True)
            bundle = store / "example.bundle"
            bundle.write_bytes(b"old cached mod")
            manager = ModManager(root / "game", data)
            manager._save_state({"mods": {"Theme": {"files": [bundle.name]}}})
            api = object.__new__(DesktopApi)
            api.controller = SimpleNamespace(manager=manager)
            api._mod_preview_title = "已装 · Theme"
            api._set_mod_preview_files = Mock(return_value=[bundle.name])
            api._preview_mod_bundle_impl = Mock(return_value={})
            with patch("astral_party_auto.web_app.DATA_DIR", data):
                api._load_mod_preview_impl("Theme")
            api._set_mod_preview_files.assert_called_once_with({bundle.name: bundle}, "已装 · Theme")

    def test_install_preview_uses_recorded_store_directory(self):
        with TemporaryDirectory(prefix="jixing-installed-preview-") as folder:
            root = Path(folder)
            source = root / "Theme"
            store = root / "mod_store" / "Theme-staged-id"
            source.mkdir()
            store.mkdir(parents=True)
            (store / "example.bundle").write_bytes(b"installed fixture")
            api = object.__new__(DesktopApi)
            api._controller_lock = threading.RLock()
            api._append_log = Mock()
            api._pending_mod_path = source
            api._mod_preview_title = ""
            api.controller = Mock()
            api.controller.install.return_value = SimpleNamespace(matched=["example.bundle"], unmatched=[])
            api.controller.installed_mods.return_value = [{"name": "Theme", "store": str(store)}]
            api._set_mod_preview_files = Mock(return_value=["example.bundle"])
            api._dashboard_state = Mock(return_value={})
            result = api.install_pending_mod()
            self.assertTrue(result["ok"], result.get("error"))
            self.assertEqual(result["data"]["bundles"], ["example.bundle"])
            api._set_mod_preview_files.assert_called_once_with(
                {"example.bundle": store / "example.bundle"}, "已装 · Theme")


if __name__ == "__main__":
    unittest.main()
