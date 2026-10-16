"""选中贴图即可对照当前作品集；所有资源与缓存都位于隔离 fixture。"""
from __future__ import annotations

import base64
import io
import json
import os
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

import astral_party_auto.mod_controller as controller_module
from astral_party_auto.modkit.bundles import TextureInfo
from astral_party_auto.modkit.manager import ModManager
from astral_party_auto.web_app import DesktopApi


class SelectionComparisonTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        fixture_parent = Path(__file__).resolve().parents[1] / "qa-evidence" / "issue19" / "comparison-fixtures"
        fixture_parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(self.resources.enter_context(TemporaryDirectory(prefix="case-", dir=fixture_parent)))
        self.game = self.root / "game"
        self.data = self.root / "data"
        self.made = self.root / "made"
        self.previews = self.data / "previews"
        for folder in (self.game, self.data, self.made, self.previews):
            folder.mkdir(parents=True, exist_ok=True)
        self.bundle = self.game / "shared.bundle"
        self.write_bundle(self.bundle, Portrait="red", Other="yellow", **{"Walk-001": "orange"})
        self.resources.enter_context(patch.multiple(
            controller_module, DATA_DIR=self.data, MADE_DIR=self.made,
            DRAFT_META=self.data / "draft.json", PREVIEW_DIR=self.previews,
        ))
        self.controller = object.__new__(controller_module.ModController)
        self.controller.aa_dirs = (self.game,)
        self.controller.manager = ModManager((self.game,), self.data)
        self.controller.draft_items = []
        self.controller.draft_name = "隔离测试"
        self.controller.selection = None
        self.controller.log = Mock()
        self.extraction = self.resources.enter_context(patch.object(
            controller_module, "extract_texture_png", side_effect=self.extract_texture,
        ))
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._image_cache = {}
        self.api._append_log = Mock()
        self.api.controller = self.controller

    @property
    def draft_bundle(self):
        return self.made / "_draft" / self.bundle.name

    @staticmethod
    def write_bundle(path, **textures):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(textures), encoding="utf-8")

    @staticmethod
    def extract_texture(source, destination, target_name=None):
        textures = json.loads(Path(source).read_text(encoding="utf-8"))
        color = textures.get(target_name)
        if color is None:
            return None
        Image.new("RGB", (3, 2), color).save(destination)
        return TextureInfo(target_name, 3, 2)

    def add_draft_texture(self, name="Portrait", color="green"):
        self.write_bundle(self.draft_bundle, **{name: color})
        self.controller.draft_items = [{"kind": "texture", "bundle": self.bundle.name, "name": name}]

    def select(self, name="Portrait", force=False):
        response = self.api.select_asset("texture", self.bundle.name, name, force=force)
        self.assertTrue(response["ok"], response.get("error"))
        return response["data"]

    def studio(self):
        response = self.api.get_studio_state()
        self.assertTrue(response["ok"], response.get("error"))
        return response["data"]

    def assert_pixel(self, data_url, expected):
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        with Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1]))) as image:
            self.assertEqual(image.getpixel((0, 0)), expected)

    def test_unmodified_selection_keeps_original_and_has_no_modified_image(self):
        selected = self.select()
        self.assert_pixel(selected["preview_data"], (255, 0, 0))
        self.assertIsNone(selected["comparison"]["modified"])
        self.assertIn("尚未修改", selected["comparison"]["notice"])

    def test_same_bundle_other_texture_is_not_reported_as_modified(self):
        self.add_draft_texture("Other")
        selected = self.select()
        self.assertIsNone(selected["comparison"]["modified"])
        self.assertEqual(self.extraction.call_count, 1)
        self.assertEqual(Path(self.extraction.call_args.args[0]), self.bundle)

    def test_same_name_in_another_bundle_is_not_reported_as_modified(self):
        self.add_draft_texture()
        self.controller.draft_items[0]["bundle"] = "different.bundle"
        self.assertIsNone(self.select()["comparison"]["modified"])

    def test_exact_texture_returns_original_and_draft_data_without_mutating_files(self):
        self.add_draft_texture()
        game_before, draft_before = self.bundle.read_bytes(), self.draft_bundle.read_bytes()
        selected = self.select()
        modified = selected["comparison"]["modified"]
        self.assert_pixel(selected["preview_data"], (255, 0, 0))
        self.assert_pixel(modified["preview_data"], (0, 128, 0))
        self.assertEqual((modified["width"], modified["height"]), (3, 2))
        self.assertEqual(modified["draft_index"], 0)
        self.assertEqual(modified["source"], "draft")
        self.assertEqual(modified["label"], "作品集修改图")
        self.assertNotEqual(selected["preview"], modified["preview"])
        self.assertEqual(self.bundle.read_bytes(), game_before)
        self.assertEqual(self.draft_bundle.read_bytes(), draft_before)

    def test_original_uses_backup_when_game_has_an_installed_mod(self):
        self.add_draft_texture()
        backup = self.data / "backups" / self.bundle.name
        self.write_bundle(backup, Portrait="red")
        self.write_bundle(self.bundle, Portrait="blue")
        selected = self.select()
        self.assertEqual(Path(selected["original_path"]), backup)
        self.assert_pixel(selected["preview_data"], (255, 0, 0))
        self.assert_pixel(selected["comparison"]["modified"]["preview_data"], (0, 128, 0))
        self.assertEqual(json.loads(self.bundle.read_text())["Portrait"], "blue")

    def test_draft_state_is_read_again_in_studio_instead_of_caching_match(self):
        self.select()
        self.add_draft_texture()
        self.assertIsNotNone(self.studio()["comparison"]["modified"])
        self.controller.remove_draft_item(0)
        selected = self.studio()
        self.assertIsNone(selected["comparison"]["modified"])
        self.assert_pixel(selected["preview_data"], (255, 0, 0))

    def test_clear_draft_removes_comparison_and_draft_cache(self):
        self.add_draft_texture()
        self.select()
        self.controller.clear_draft()
        self.assertIsNone(self.studio()["comparison"]["modified"])
        self.assertEqual(list(self.previews.glob("draft_*")), [])
        self.assertEqual(len(list(self.previews.glob("orig_*"))), 1)

    def test_loading_saved_draft_replaces_current_comparison(self):
        self.select()
        self.add_draft_texture()
        self.controller._save_draft()
        self.controller.draft_items = []
        self.controller._load_draft()
        self.assertIsNotNone(self.studio()["comparison"]["modified"])
        (self.data / "draft.json").write_text('{"items": []}', encoding="utf-8")
        self.controller._load_draft()
        self.assertIsNone(self.studio()["comparison"]["modified"])

    def test_missing_draft_bundle_reports_problem_and_keeps_original(self):
        self.add_draft_texture()
        self.draft_bundle.unlink()
        selected = self.select()
        self.assertIsNone(selected["comparison"]["modified"])
        self.assertIn("草稿资源包缺失", selected["comparison"]["notice"])
        self.assert_pixel(selected["preview_data"], (255, 0, 0))

    def test_unreadable_draft_reports_problem_and_keeps_original(self):
        self.add_draft_texture()
        self.draft_bundle.write_bytes(b"invalid bundle")
        selected = self.select()
        self.assertIsNone(selected["comparison"]["modified"])
        self.assertIn("预览失败", selected["comparison"]["notice"])
        self.assert_pixel(selected["preview_data"], (255, 0, 0))

    def test_missing_texture_in_draft_never_uses_an_unrelated_first_texture(self):
        self.add_draft_texture()
        self.write_bundle(self.draft_bundle, Other="green")
        selected = self.select()
        self.assertIsNone(selected["comparison"]["modified"])
        self.assertIn("找不到这张贴图", selected["comparison"]["notice"])

    def test_refresh_reextracts_both_sides_even_if_bundle_timestamps_are_unchanged(self):
        self.add_draft_texture()
        selected = self.select()
        self.assert_pixel(selected["comparison"]["modified"]["preview_data"], (0, 128, 0))
        timestamp = self.draft_bundle.stat()
        self.write_bundle(self.draft_bundle, Portrait="black")
        os.utime(self.draft_bundle, ns=(timestamp.st_atime_ns, timestamp.st_mtime_ns))
        response = self.api.refresh_selection()
        self.assertTrue(response["ok"], response.get("error"))
        self.assert_pixel(response["data"]["comparison"]["modified"]["preview_data"], (0, 0, 0))
        self.assertEqual(self.extraction.call_count, 4)

    def test_known_sequence_frame_is_compared_as_that_frame(self):
        self.write_bundle(self.draft_bundle, **{"Walk-001": "green"})
        self.controller.draft_items = [{
            "kind": "dynamic", "animation_kind": "sequence", "bundle": self.bundle.name,
            "name": "Walk", "frame_names": ["Walk-001", "Walk-002"],
        }]
        modified = self.select("Walk-001")["comparison"]["modified"]
        self.assertEqual(modified["label"], "作品集序列帧")
        self.assert_pixel(modified["preview_data"], (0, 128, 0))

    def test_dynamic_mapping_is_not_presented_as_modified_static_pixels(self):
        for kind in ("emote", "visual_animation"):
            with self.subTest(kind=kind):
                self.controller.draft_items = [{
                    "kind": kind, "bundle": "config.bundle", "source_bundle": self.bundle.name,
                    "name": "Portrait",
                }]
                selected = self.select()
                self.assertIsNone(selected["comparison"]["modified"])
                self.assertIn("静态原图未改动", selected["comparison"]["notice"])

    def test_ambiguous_or_invalid_records_do_not_leak_a_modified_image(self):
        self.add_draft_texture()
        self.controller.draft_items.extend([None, "invalid", dict(self.controller.draft_items[0])])
        selected = self.select()
        self.assertIsNone(selected["comparison"]["modified"])
        self.assertIn("多项修改", selected["comparison"]["notice"])

    def test_nontexture_and_empty_selections_do_not_extract_a_modified_image(self):
        self.add_draft_texture()
        self.assertEqual(self.controller.selection_comparison({"asset_type": "text"}), {"modified": None, "notice": ""})
        self.assertEqual(self.controller.selection_comparison({}), {"modified": None, "notice": ""})
        self.extraction.assert_not_called()

    def test_invalid_bundle_name_is_rejected_before_reading_draft(self):
        selection = {"asset_type": "texture", "bundle": "../outside.bundle", "name": "Portrait"}
        result = self.controller.selection_comparison(selection)
        self.assertIsNone(result["modified"])
        self.assertIn("名称无效", result["notice"])
        self.extraction.assert_not_called()


if __name__ == "__main__":
    unittest.main()
