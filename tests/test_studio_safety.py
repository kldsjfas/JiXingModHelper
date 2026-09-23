"""Studio regressions use a fake game directory and never discover a real game."""
from __future__ import annotations

import json
import os
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

import astral_party_auto.mod_controller as controller_module
import astral_party_auto.modkit.animation as animation_module
from astral_party_auto.modkit.bundles import TextureInfo
from astral_party_auto.modkit.manager import ModManager


class StudioSafetyTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.root = Path(self.resources.enter_context(TemporaryDirectory(prefix="jixing-studio-safety-")))
        self.game = self.root / "fake-game"
        self.data = self.root / "data"
        self.made = self.root / "made"
        self.previews = self.data / "previews"
        for folder in (self.game, self.data, self.made, self.previews):
            folder.mkdir(parents=True, exist_ok=True)
        self.bundle = self.game / "shared.bundle"
        self.bundle.write_text('{"original": true}', encoding="utf-8")
        self.image = self.root / "replacement.png"
        Image.new("RGBA", (3, 2), "green").save(self.image)
        self.resources.enter_context(patch.multiple(
            controller_module, DATA_DIR=self.data, MADE_DIR=self.made,
            DRAFT_META=self.data / "draft.json", PREVIEW_DIR=self.previews,
        ))
        self.controller = object.__new__(controller_module.ModController)
        self.controller.aa_dirs = (self.game,)
        self.controller.manager = ModManager((self.game,), self.data)
        self.controller.draft_items = []
        self.controller.draft_name = "Local test"
        self.controller.log = Mock()

    @property
    def draft_bundle(self):
        return self.made / "_draft" / self.bundle.name

    @staticmethod
    def update_fake_bundle(source, destination, key, value):
        contents = json.loads(Path(source).read_text(encoding="utf-8"))
        contents[key] = value
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_text(json.dumps(contents), encoding="utf-8")

    def stub_replacements(self):
        def text(source, name, value, destination, **_kwargs):
            self.update_fake_bundle(source, destination, "text:" + name, value)
            return name

        def texture(source, _image, destination, target_name=None, **_kwargs):
            self.update_fake_bundle(source, destination, "texture:" + target_name, "changed")
            return target_name

        def sequence(source, name, _replacement, destination, **_kwargs):
            self.update_fake_bundle(source, destination, "sequence:" + name, "animated")
            return {"name": name}

        self.resources.enter_context(patch.object(controller_module, "replace_bundle_text", side_effect=text))
        texture_mock = self.resources.enter_context(patch.object(controller_module, "replace_bundle_texture", side_effect=texture))
        self.resources.enter_context(patch.object(animation_module, "inspect_animation", return_value={
            "editable": True, "frame_names": ["Walk-001", "Walk-002", "Walk-003"],
        }))
        sequence_mock = self.resources.enter_context(patch.object(animation_module, "replace_bundle_sequence", side_effect=sequence))
        return texture_mock, sequence_mock

    def add_sequence(self):
        self.controller.add_dynamic_to_draft(
            self.bundle, "Walk", self.image, bundle_name=self.bundle.name,
            animation_kind="sequence", fps=24,
        )

    def test_same_named_hot_cache_files_do_not_share_preview(self):
        first = self.root / "hot-a" / "__data"
        second = self.root / "hot-b" / "__data"
        first.parent.mkdir()
        second.parent.mkdir()
        first.write_bytes(b"red")
        second.write_bytes(b"blue")

        def extract(source, destination, target_name=None):
            Image.new("RGB", (2, 2), Path(source).read_bytes().decode()).save(destination)
            return TextureInfo(target_name, 2, 2)

        with patch.object(controller_module, "extract_texture_png", side_effect=extract) as extraction:
            first_preview, _ = self.controller.preview_bundle(first, "SharedTexture")
            second_preview, _ = self.controller.preview_bundle(second, "SharedTexture")
            cached_preview, _ = self.controller.preview_bundle(first, "SharedTexture")
            self.assertNotEqual(first_preview, second_preview)
            self.assertEqual(first_preview, cached_preview)
            self.assertEqual(extraction.call_count, 2)
            with Image.open(first_preview) as image:
                self.assertEqual(image.getpixel((0, 0)), (255, 0, 0))
            with Image.open(second_preview) as image:
                self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))

    def test_preview_changes_when_source_is_replaced(self):
        def extract(source, destination, target_name=None):
            Image.new("RGB", (1, 1), "red" if Path(source).read_bytes() == b"first" else "blue").save(destination)
            return TextureInfo(target_name, 1, 1)

        self.bundle.write_bytes(b"first")
        with patch.object(controller_module, "extract_texture_png", side_effect=extract) as extraction:
            before, _ = self.controller.preview_bundle(self.bundle, "Same", tag="draft")
            old_stat = self.bundle.stat()
            self.bundle.write_bytes(b"other")
            os.utime(self.bundle, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000_000))
            after, _ = self.controller.preview_bundle(self.bundle, "Same", tag="draft")
            self.assertNotEqual(before, after)
            self.assertEqual(extraction.call_count, 2)

    def test_texture_edits_preserve_text_and_animation_in_same_bundle(self):
        texture_mock, _ = self.stub_replacements()
        self.controller.add_text_to_draft(self.bundle, "Caption", "new words", bundle_name=self.bundle.name)
        self.add_sequence()
        self.controller.add_texture_to_draft(self.bundle, self.image, "Portrait", bundle_name=self.bundle.name)
        self.controller.add_texture_to_draft(self.bundle, self.image, "Portrait", bundle_name=self.bundle.name)
        contents = json.loads(self.draft_bundle.read_text(encoding="utf-8"))
        self.assertEqual(contents, {
            "original": True, "text:Caption": "new words",
            "sequence:Walk": "animated", "texture:Portrait": "changed",
        })
        self.assertEqual({item["kind"] for item in self.controller.draft_items}, {"text", "dynamic", "texture"})
        self.assertEqual(len(self.controller.draft_items), 3)
        self.assertTrue(all(Path(call.args[0]) == self.draft_bundle for call in texture_mock.call_args_list))
        self.assertEqual(json.loads(self.bundle.read_text()), {"original": True})

    def test_repeated_text_edits_use_original_format_reference(self):
        self.stub_replacements()
        for text in ('{"message":"changed"}', "plain text again"):
            self.controller.add_text_to_draft(self.bundle, "Caption", text, bundle_name=self.bundle.name)
        replacement = controller_module.replace_bundle_text
        self.assertEqual(replacement.call_count, 2)
        self.assertEqual(Path(replacement.call_args.args[0]), self.draft_bundle)
        self.assertTrue(all(Path(call.kwargs["reference_bundle"]) == self.bundle for call in replacement.call_args_list))

    def test_sequence_then_overlapping_single_frame_is_rejected(self):
        texture_mock, _ = self.stub_replacements()
        self.add_sequence()
        before = self.draft_bundle.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "共用帧图"):
            self.controller.add_texture_to_draft(self.bundle, self.image, "Walk-002", bundle_name=self.bundle.name)
        texture_mock.assert_not_called()
        self.assertEqual(self.draft_bundle.read_bytes(), before)
        self.assertEqual(len(self.controller.draft_items), 1)

    def test_single_frame_then_overlapping_sequence_is_rejected(self):
        _, sequence_mock = self.stub_replacements()
        self.controller.add_texture_to_draft(self.bundle, self.image, "Walk-002", bundle_name=self.bundle.name)
        before = self.draft_bundle.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "共用帧图"):
            self.add_sequence()
        sequence_mock.assert_not_called()
        self.assertEqual(self.draft_bundle.read_bytes(), before)
        self.assertEqual(len(self.controller.draft_items), 1)

    def install_fake_draft(self):
        self.stub_replacements()
        self.controller.add_text_to_draft(self.bundle, "Caption", "new words", bundle_name=self.bundle.name)
        self.controller.add_texture_to_draft(self.bundle, self.image, "Portrait", bundle_name=self.bundle.name)
        # This is a real file-copy install, exclusively inside our temporary game.
        manager = self.controller.manager
        manager.install_mod(self.draft_bundle.parent, self.controller.draft_name)
        installed = manager.installed_mods()
        state = manager.state_path.read_bytes()
        game_bytes = self.bundle.read_bytes()
        saved_mod = self.made / "older-export" / "keep.txt"
        saved_mod.parent.mkdir()
        saved_mod.write_text("keep", encoding="utf-8")
        watched = []
        for owner, name in (
            (self.controller, "install"), (self.controller, "install_draft"),
            (self.controller, "uninstall"), (manager, "install_mod"),
            (manager, "uninstall_mod"), (manager, "disable_mod"),
            (manager, "restore_all"),
        ):
            watched.append(self.resources.enter_context(patch.object(owner, name, side_effect=AssertionError("Draft editing must not change installed mods"))))
        return installed, state, game_bytes, saved_mod, watched

    def assert_install_untouched(self, snapshot):
        installed, state, game_bytes, saved_mod, watched = snapshot
        manager = self.controller.manager
        self.assertEqual(manager.installed_mods(), installed)
        self.assertEqual(manager.state_path.read_bytes(), state)
        self.assertEqual(self.bundle.read_bytes(), game_bytes)
        self.assertEqual((manager._store_dir(self.controller.draft_name) / self.bundle.name).read_bytes(), game_bytes)
        self.assertEqual((manager.backup_dir / self.bundle.name).read_text(), '{"original": true}')
        self.assertEqual(saved_mod.read_text(), "keep")
        for call in watched:
            call.assert_not_called()

    def test_remove_installed_draft_items_only_changes_local_draft(self):
        snapshot = self.install_fake_draft()

        def restore(_item, draft, _original):
            contents = json.loads(Path(draft).read_text())
            del contents["text:Caption"]
            Path(draft).write_text(json.dumps(contents))

        with patch.object(self.controller, "_restore_removed_draft_item", side_effect=restore) as restoration:
            self.controller.remove_draft_item(0)
            restoration.assert_called_once()
        self.assertEqual(len(self.controller.draft_items), 1)
        self.assertEqual(json.loads(self.draft_bundle.read_text()), {"original": True, "texture:Portrait": "changed"})
        self.assert_install_untouched(snapshot)
        self.controller.remove_draft_item(0)
        self.assertFalse(self.draft_bundle.exists())
        self.assertEqual(self.controller.draft_items, [])
        self.assert_install_untouched(snapshot)

    def test_clear_installed_draft_does_not_uninstall_or_delete_saved_exports(self):
        snapshot = self.install_fake_draft()
        (self.previews / "draft_old.png").write_bytes(b"old")
        (self.previews / "browse_keep.png").write_bytes(b"keep")
        self.controller.clear_draft()
        self.assertEqual(self.controller.draft_items, [])
        self.assertEqual(list(self.draft_bundle.parent.iterdir()), [])
        self.assertEqual(json.loads((self.data / "draft.json").read_text(encoding="utf-8"))["items"], [])
        self.assertFalse((self.previews / "draft_old.png").exists())
        self.assertTrue((self.previews / "browse_keep.png").exists())
        self.assert_install_untouched(snapshot)


if __name__ == "__main__":
    unittest.main()
