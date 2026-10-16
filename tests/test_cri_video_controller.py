"""CRI 技能影片的浏览、只读预览、导出及旧校验缓存集成回归。"""
from __future__ import annotations

import json
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import astral_party_auto.mod_controller as controller_module
import astral_party_auto.web_app as web_module
from astral_party_auto.modkit import animation, cri_video


class CriVideoControllerTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.root = Path(self.resources.enter_context(TemporaryDirectory(prefix="jixing-cri-controller-")))
        self.original = self.root / "original.bundle"
        self.installed = self.root / "game.bundle"
        self.original.write_bytes(b"isolated original fixture")
        self.installed.write_bytes(b"isolated installed fixture")
        self.validation = self.root / "dynamic-validation.json"
        self.resources.enter_context(patch.object(controller_module, "DYNAMIC_VALID_PATH", self.validation))
        self.resources.enter_context(patch.object(web_module, "DATA_DIR", self.root / "data"))

        self.controller = object.__new__(controller_module.ModController)
        self.controller.selection = None
        self.controller.log = Mock()
        self.controller.original_bundle_path = Mock(return_value=self.original)
        self.controller.bundle_path = Mock(return_value=self.installed)
        self.controller.aa_dirs = (self.root,)
        self.controller.typed_index = {}
        self.controller._dynamic_sequence_bases = {}
        self.controller._valid_dynamic = {}
        self.controller.draft_items = []
        self.api = object.__new__(web_module.DesktopApi)
        self.api.controller = self.controller
        self.api._controller_lock = threading.RLock()
        self.api._animation_cache = {}
        self.api._append_log = Mock()
        self.api._pick_path = Mock(return_value=self.root / "export.usm")

    def select_video(self, info=None, error=None):
        if info is None:
            info = {"playable": True, "editable": True, "exportable": True,
                    "width": 1500, "height": 1080, "encoded_width": 1504,
                    "frames": 40, "fps": 30, "reason": "原始影片只读浏览"}
        with patch.object(controller_module, "read_bundle_asset_names", return_value={"cri_usm": ["VSkill_Hero124"]}), \
                patch.object(cri_video, "inspect_cri_video_bundle", return_value=info, side_effect=error) as inspect:
            selected = self.controller.set_selection("movie.bundle", "VSkill_Hero124", asset_type="dynamic")
            inspect.assert_called_once_with(self.original, "VSkill_Hero124")
        return selected

    def test_selection_uses_actual_cri_object_and_stays_readonly_even_if_inspector_says_editable(self):
        selected = self.select_video()
        self.assertEqual(selected["animation_kind"], "cri_usm")
        self.assertEqual(selected["asset_type"], "dynamic")
        self.assertEqual(selected["category"], "cri_usm")
        self.assertEqual(selected["category_label"], "视频 / 技能演出")
        self.assertFalse(selected["editable"])
        self.assertTrue(selected["playable"])
        self.assertTrue(selected["exportable"])
        self.assertEqual((selected["width"], selected["height"], selected["encoded_width"]), (1500, 1080, 1504))
        self.assertEqual(selected["original_path"], str(self.original))
        self.assertEqual(selected["bundle_path"], str(self.installed))
        self.assertIn("只读", selected["text_preview"])

    def test_unsupported_cri_layout_has_clear_reason_and_cannot_replace(self):
        selected = self.select_video(error=ValueError("外部视频暂不支持"))
        self.assertFalse(selected["editable"])
        self.assertFalse(selected["playable"])
        self.assertFalse(selected["exportable"])
        self.assertIn("暂不支持此 CRI 视频", selected["reason"])
        self.assertIn("外部视频", selected["text_preview"])
        response = self.api.choose_replacement()
        self.assertFalse(response["ok"])
        self.assertIn("外部视频", response["error"])
        self.api._pick_path.assert_not_called()

    def test_selection_does_not_trust_name_or_private_index_without_fresh_object_check(self):
        self.controller.typed_index = {"cri_usm": {"movie.bundle": ["VSkill_Hero124"]}}
        with patch.object(controller_module, "read_bundle_asset_names", return_value={}), \
                patch.object(cri_video, "inspect_cri_video_bundle") as inspect, \
                patch("UnityPy.load", return_value=SimpleNamespace(objects=[])):
            selected = self.controller.set_selection("movie.bundle", "VSkill_Hero124", asset_type="dynamic")
        self.assertEqual(selected["animation_kind"], "unsupported")
        self.assertFalse(selected["editable"])
        inspect.assert_not_called()

    def test_unsupported_video_does_not_open_a_save_dialog(self):
        self.select_video(error=ValueError("外部视频暂不支持"))
        result = self.api.export_selection()
        self.assertFalse(result["ok"])
        self.assertIn("外部视频", result["error"])
        self.api._pick_path.assert_not_called()

    def test_cri_filter_counts_only_real_private_mapping_and_preserves_non_skill_names(self):
        self.controller.typed_index = {
            "dynamic": {"movie.bundle": ["VSkill_Hero124", "UnexpectedMovie", "VSkill_not_video", "Main_fui"]},
            "cri_usm": {"movie.bundle": ["VSkill_Hero124", "UnexpectedMovie"]},
        }
        counts = {name: count for name, _label, count in self.controller.categories(asset_type="dynamic")}
        self.assertEqual(counts["all"], 4)
        self.assertEqual(counts["cri_usm"], 2)
        self.assertEqual(self.controller._dynamic_kind("movie.bundle", "VSkill_not_video"), "other")
        self.assertEqual(self.controller._dynamic_kind("movie.bundle", "UnexpectedMovie"), "cri_usm")
        self.assertEqual(list(self.controller._iter_browse_rows("dynamic", "cri_usm", "")), [
            ("movie.bundle", "UnexpectedMovie"), ("movie.bundle", "VSkill_Hero124"),
        ])
        self.assertEqual(list(self.controller._iter_browse_rows("dynamic", "cri_usm", "unexpected")), [
            ("movie.bundle", "UnexpectedMovie"),
        ])
        self.controller._valid_dynamic = {"movie.bundle": {"UnexpectedMovie", "VSkill_not_video"}}
        counts = {name: count for name, _label, count in self.controller.categories(asset_type="dynamic")}
        self.assertEqual((counts["all"], counts["cri_usm"]), (2, 1))
        self.assertEqual(list(self.controller._iter_browse_rows("dynamic", "cri_usm", "")), [
            ("movie.bundle", "UnexpectedMovie"),
        ])

    def test_default_filename_and_controller_export_use_native_usm_exporter(self):
        self.select_video()
        self.assertEqual(self.controller.default_export_filename(), "VSkill_Hero124.usm")
        output = self.root / "chosen.usm"
        with patch.object(cri_video, "export_cri_video_bundle", return_value=output) as exporter, \
                patch.object(controller_module, "export_by_type") as generic:
            self.assertEqual(self.controller.export_selection(output), output)
            exporter.assert_called_once_with(self.original, "VSkill_Hero124", output)
            generic.assert_not_called()

    def test_web_export_keeps_usm_name_and_refuses_alternative_format_before_dialog(self):
        self.select_video()
        destination = self.root / "export.usm"
        with patch.object(cri_video, "export_cri_video_bundle", return_value=destination) as exporter, \
                patch.object(controller_module, "export_by_type") as generic:
            response = self.api.export_selection()
            self.assertTrue(response["ok"], response.get("error"))
            self.assertEqual(response["data"]["path"], str(destination))
            self.assertEqual(self.api._pick_path.call_args.kwargs["save_filename"], "VSkill_Hero124.usm")
            exporter.assert_called_once_with(self.original, "VSkill_Hero124", destination)
            generic.assert_not_called()
            self.api._pick_path.reset_mock()
            rejected = self.api.export_selection("secondary")
            self.assertFalse(rejected["ok"])
            self.assertIn("原始 USM", rejected["error"])
            self.api._pick_path.assert_not_called()

    def test_dynamic_validation_rejects_missing_or_old_index_version(self):
        for version in (None, 8):
            with self.subTest(version=version):
                payload = {"source": "current-source", "valid": {"movie.bundle": ["old"]}}
                if version is not None:
                    payload["index_version"] = version
                self.validation.write_text(json.dumps(payload), encoding="utf-8")
                self.controller._valid_dynamic = {"movie.bundle": {"stale in memory"}}
                self.controller._load_dynamic_validation("current-source")
                self.assertEqual(self.controller._valid_dynamic, {})

    def test_current_dynamic_validation_saves_version_9_and_roundtrips(self):
        self.assertEqual(controller_module.INDEX_VERSION, 9)
        self.controller._valid_dynamic = {"movie.bundle": {"VSkill_Hero124", "UnexpectedMovie"}}
        with patch.object(controller_module, "bundle_source_key", return_value="current-source"):
            self.controller._save_dynamic_validation()
        payload = json.loads(self.validation.read_text(encoding="utf-8"))
        self.assertEqual(payload["index_version"], 9)
        self.assertEqual(payload["source"], "current-source")
        self.controller._valid_dynamic = {}
        self.controller._load_dynamic_validation("current-source")
        self.assertEqual(self.controller._valid_dynamic, {"movie.bundle": {"VSkill_Hero124", "UnexpectedMovie"}})
        self.controller._load_dynamic_validation("different-source")
        self.assertEqual(self.controller._valid_dynamic, {})

    def test_web_preview_uses_cri_reader_and_cache_instead_of_generic_animation(self):
        result = {"animation_kind": "cri_usm", "frames": ["decoded-frame"], "durations": [33.33], "total": 1}
        with patch.object(cri_video, "read_cri_video_preview", return_value=result) as reader, \
                patch.object(animation, "read_animation_preview") as generic:
            self.assertIs(self.api._animation_preview(self.original, "VSkill_Hero124", "cri_usm"), result)
            self.assertIs(self.api._animation_preview(self.original, "VSkill_Hero124", "cri_usm"), result)
            reader.assert_called_once_with(self.original, "VSkill_Hero124", work_dir=self.root / "data" / "skill-video-preview")
            generic.assert_not_called()

    def test_public_preview_resolves_selected_cri_kind_and_original_bundle(self):
        self.select_video()
        result = {"animation_kind": "cri_usm", "frames": [], "durations": [], "total": 0}
        with patch.object(cri_video, "read_cri_video_preview", return_value=result) as reader, \
                patch.object(animation, "read_animation_preview") as generic:
            response = self.api.get_animation_preview("movie.bundle", "VSkill_Hero124")
            self.assertTrue(response["ok"], response.get("error"))
            self.assertEqual(response["data"]["animation_kind"], "cri_usm")
            reader.assert_called_once_with(self.original, "VSkill_Hero124", work_dir=self.root / "data" / "skill-video-preview")
            generic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
