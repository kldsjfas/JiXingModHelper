"""表情 API 会话边界回归，文件框与游戏工作区全部隔离。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from wsgiref.util import setup_testing_defaults

from astral_party_auto.web_app import DesktopApi, _build_server


class EmoteApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-emote-api-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original = self.root / "native-video.bundle"
        self.original.write_bytes(b"fake original resource")
        self.movie = self.root / "replacement.usm"
        self.movie.write_bytes(b"isolated candidate")
        self.destination = self.root / "export.usm"
        self.selection = {"asset_type": "texture", "bundle": "emoji.bundle", "name": "UT_Item_Emoji_101",
                          "original_path": self.root / "cover-source.bundle"}
        self.item = {"kind": "emote", "name": self.selection["name"], "bundle": "character.bundle",
                     "source_bundle": "emoji.bundle", "emote_id": 101, "video_name": "VEmoji_A",
                     "custom_video": False}
        self.workspace = Mock()
        self.workspace._selected_id.return_value = 101
        self.workspace.video_bundle.side_effect = self.video_bundle
        self.controller = Mock()
        self.controller.selection = dict(self.selection)
        self.controller._index_source_key = "isolated-game-a"
        self.controller.emote_workspace = self.workspace
        self.controller.draft_items = []
        self.controller.add_emote_to_draft.return_value = self.item
        self.controller.set_selection.side_effect = self.set_selection
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._replacement_path = None
        self.api._emote_replacement = None
        self.api._emote_candidate_dir = None
        self.api._emote_preview_cache = {}
        self.api._draft_crop_path = None
        self.api._draft_crop_index = None
        self.api._draft_crop_item = None
        self.api._image_cache = {}
        self.api.controller = self.controller
        self.api._pick_path = Mock(return_value=self.movie)
        self.api._draft_state = Mock(return_value={"name": "测试套装", "items": [self.item]})
        self.validator = self.enterContext(patch(
            "astral_party_auto.modkit.cri_emote.validate_usm_file", side_effect=self.video_info))
        self.exporter = self.enterContext(patch(
            "astral_party_auto.modkit.cri_emote.export_cri_emote_bundle", side_effect=self.export_video))
        self.enterContext(patch("astral_party_auto.web_app.DATA_DIR", self.root / "data"))
        self.preparer = self.enterContext(patch(
            "astral_party_auto.modkit.emote_media.prepare_emote_media", side_effect=self.prepare_media))
        self.animation = {"frames": ["data:image/png;base64,frame-a", "data:image/png;base64,frame-b"],
                          "durations": [100, 100], "total": 2, "preview_available": True,
                          "notice": "从成品双流实际解码。"}
        self.previewer = self.enterContext(patch(
            "astral_party_auto.modkit.emote_media.preview_emote_media", return_value=self.animation))
        self.workspace.context.return_value = {"records": [{"emote_id": 101, "video_name": ""}]}

    def video_bundle(self, video_name):
        if not isinstance(video_name, str) or video_name not in ("VEmoji_A", "VEmoji_B"):
            raise ValueError("请选择当前游戏目录中已有的动态视频。")
        return "native-video.bundle", self.original

    def video_info(self, path, *_args):
        raw = Path(path).read_bytes()
        return {"format": "usm", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                "codec": "MPEG-2", "streams": [{"stream": "@SFV", "codec_id": 1}],
                "preview_available": False, "notice": "模拟兼容性校验；没有解码播放。"}

    def export_video(self, _source, _name, destination):
        Path(destination).write_bytes(b"isolated exported movie")
        return destination

    def prepare_media(self, source, reference, name, output, **_options):
        info = self.validator(source, reference, name)
        Path(output).write_bytes(Path(source).read_bytes())
        return {"path": str(output), "source_name": Path(source).name, "size": info["size"],
                "video_info": {**info, "preview_available": True}, "animation": self.animation,
                "notice": "已自动识别转换。"}

    def set_selection(self, bundle, name, *, asset_type, force=False):
        self.controller.selection = {"asset_type": asset_type, "bundle": bundle, "name": name}
        return self.controller.selection

    def choose(self, video_name="VEmoji_A"):
        result = self.api.choose_emote_file(video_name)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIsNotNone(result["data"])
        return result["data"]

    def seed_candidate(self, video_name="VEmoji_A"):
        self.api._emote_replacement = {"path": self.movie, "video_name": video_name,
                                       "selection": self.api._emote_selection_key(),
                                       "sha256": hashlib.sha256(self.movie.read_bytes()).hexdigest()}

    def assert_rejected_without_write(self, result):
        self.assertFalse(result["ok"])
        self.assertIsNone(self.api._emote_replacement)
        self.controller.add_emote_to_draft.assert_not_called()

    def http_request(self, method_name, arguments):
        app = _build_server(self.api, "emote-api-test-token")
        environment = {}
        setup_testing_defaults(environment)
        payload = json.dumps(arguments).encode("utf-8")
        environment.update(REQUEST_METHOD="POST", PATH_INFO=f"/api/{method_name}",
                           HTTP_HOST="127.0.0.1:12345", HTTP_X_JIXING_TOKEN="emote-api-test-token",
                           CONTENT_TYPE="application/json", CONTENT_LENGTH=str(len(payload)),
                           **{"wsgi.input": io.BytesIO(payload)})
        response = {}

        def start_response(status, headers, _exc_info=None):
            response.update(status=int(status.split()[0]), headers=dict(headers))

        chunks = app(environment, start_response)
        try:
            response["data"] = json.loads(b"".join(chunks).decode("utf-8"))
        finally:
            if hasattr(chunks, "close"):
                chunks.close()
        return response

    def test_import_returns_real_metadata_and_binds_selection_slot_hash(self):
        data = self.choose()
        self.assertNotEqual(data["path"], str(self.movie))
        self.assertEqual(data["name"], self.movie.name)
        self.assertEqual(data["size"], self.movie.stat().st_size)
        self.assertTrue(data["video_info"]["preview_available"])
        self.assertEqual(data["animation"], self.animation)
        self.assertEqual(data["video_info"]["streams"][0]["codec_id"], 1)
        self.validator.assert_called_once_with(self.movie, self.original, "VEmoji_A")
        candidate = self.api._emote_replacement
        self.assertEqual(candidate["selection"], self.api._emote_selection_key())
        self.assertEqual(candidate["video_name"], "VEmoji_A")
        self.assertEqual(candidate["sha256"], hashlib.sha256(self.movie.read_bytes()).hexdigest())
        self.assertIsNone(self.api._replacement_path, "USM import occupied the ordinary image candidate")

    def test_cancel_import_to_other_slot_clears_previous_candidate(self):
        self.seed_candidate()
        self.api._pick_path.return_value = None
        result = self.api.choose_emote_file("VEmoji_B")
        self.assertEqual(result, {"ok": True, "data": None})
        self.assertIsNone(self.api._emote_replacement)
        self.validator.assert_not_called()

    def test_bad_slot_clears_previous_candidate_before_dialog(self):
        for invalid in (None, "", "unknown", "../VEmoji_A", [], {}):
            with self.subTest(video_name=invalid):
                self.seed_candidate()
                self.assert_rejected_without_write(self.api.choose_emote_file(invalid))
        self.api._pick_path.assert_not_called()
        self.validator.assert_not_called()

    def test_non_emote_selection_clears_candidate_before_dialog(self):
        self.seed_candidate()
        self.workspace._selected_id.side_effect = ValueError("请选择可修改的静态表情。")
        self.assert_rejected_without_write(self.api.choose_emote_file("VEmoji_A"))
        self.api._pick_path.assert_not_called()
        self.validator.assert_not_called()

    def test_invalid_media_clears_previous_candidate(self):
        self.seed_candidate()
        self.validator.side_effect = ValueError("需要兼容的透明双流 USM。")
        result = self.api.choose_emote_file("VEmoji_A")
        self.assert_rejected_without_write(result)
        self.assertIn("透明双流", result["error"])

    def test_selection_changed_in_dialog_does_not_bind_old_import_to_new_resource(self):
        self.seed_candidate()

        def switch_resource(*_args, **_kwargs):
            self.controller.selection = {**self.selection, "name": "UT_Item_Emoji_102"}
            return self.movie

        self.api._pick_path.side_effect = switch_resource
        self.assert_rejected_without_write(self.api.choose_emote_file("VEmoji_A"))

    def test_selection_changed_during_validation_does_not_keep_candidate(self):
        def validate_and_switch(path, *_args):
            information = self.video_info(path)
            self.controller.selection = {**self.selection, "bundle": "another.bundle"}
            return information

        self.validator.side_effect = validate_and_switch
        self.assert_rejected_without_write(self.api.choose_emote_file("VEmoji_A"))

    def test_native_save_needs_no_file_and_clears_prior_custom_candidate(self):
        self.seed_candidate()
        result = self.api.save_emote("VEmoji_B", False)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"], {"item": self.item, "draft": self.api._draft_state.return_value})
        self.assertIsNone(self.api._emote_replacement)
        self.controller.add_emote_to_draft.assert_called_once_with("VEmoji_B", None)
        self.validator.assert_not_called()
        self.api._pick_path.assert_not_called()

    def test_custom_save_forwards_verified_path_and_consumes_candidate(self):
        chosen = self.choose()
        converted = Path(chosen["path"])
        result = self.api.save_emote("VEmoji_A", True)
        self.assertTrue(result["ok"], result.get("error"))
        self.controller.add_emote_to_draft.assert_called_once_with("VEmoji_A", converted, independent=True)
        self.assertFalse(converted.exists(), "owned candidate remains after save")
        self.assertTrue(self.movie.is_file(), "user source was deleted")
        self.assertIsNone(self.api._emote_replacement)
        self.controller.add_emote_to_draft.reset_mock()
        self.assert_rejected_without_write(self.api.save_emote("VEmoji_A", True))

    def test_custom_save_rejects_missing_candidate(self):
        self.assert_rejected_without_write(self.api.save_emote("VEmoji_A", True))

    def test_custom_save_rejects_changed_resource_identity(self):
        for field, replacement in (("asset_type", "dynamic"), ("bundle", "another.bundle"),
                                   ("name", "UT_Item_Emoji_102")):
            with self.subTest(field=field):
                self.controller.selection = dict(self.selection)
                self.seed_candidate()
                self.controller.selection[field] = replacement
                self.assert_rejected_without_write(self.api.save_emote("VEmoji_A", True))

    def test_custom_save_rejects_changed_video_slot(self):
        self.seed_candidate()
        self.assert_rejected_without_write(self.api.save_emote("VEmoji_B", True))

    def test_same_named_resource_in_different_game_source_rejects_custom_candidate(self):
        self.choose()
        previous_selection = dict(self.controller.selection)
        self.controller._index_source_key = "isolated-game-b"
        self.assertEqual(self.controller.selection, previous_selection, "Resource names changed in the fixture")
        self.assert_rejected_without_write(self.api.save_emote("VEmoji_A", True))

    def test_same_named_resource_at_different_original_path_rejects_custom_candidate(self):
        self.choose()
        self.controller.selection["original_path"] = self.root / "another-cover-source.bundle"
        for field in ("asset_type", "bundle", "name"):
            self.assertEqual(self.controller.selection[field], self.selection[field])
        self.assert_rejected_without_write(self.api.save_emote("VEmoji_A", True))

    def test_game_source_changed_during_validation_does_not_keep_candidate(self):
        def validate_and_switch_source(path, *_args):
            information = self.video_info(path)
            self.controller._index_source_key = "isolated-game-b"
            return information

        self.validator.side_effect = validate_and_switch_source
        self.assert_rejected_without_write(self.api.choose_emote_file("VEmoji_A"))

    def test_custom_save_checks_bytes_when_length_and_timestamp_stay_equal(self):
        self.seed_candidate()
        previous = self.movie.stat()
        self.movie.write_bytes(b"x" * previous.st_size)
        os.utime(self.movie, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        self.assert_rejected_without_write(self.api.save_emote("VEmoji_A", True))

    def test_custom_save_rejects_missing_or_oversized_file(self):
        self.seed_candidate()
        self.movie.unlink()
        self.assert_rejected_without_write(self.api.save_emote("VEmoji_A", True))
        self.movie.write_bytes(b"isolated candidate")
        self.seed_candidate()
        with patch.object(Path, "stat", return_value=type("FileStat", (), {"st_size": 64 * 1024 * 1024 + 1})()), \
             patch.object(Path, "is_file", return_value=True):
            self.assert_rejected_without_write(self.api.save_emote("VEmoji_A", True))

    def test_save_requires_strict_boolean_and_consumes_candidate_on_bad_mode(self):
        for invalid in (0, 1, "true", "false", None, [], {}):
            with self.subTest(use_custom=invalid):
                self.seed_candidate()
                self.assert_rejected_without_write(self.api.save_emote("VEmoji_A", invalid))

    def test_workspace_save_failure_consumes_candidate(self):
        self.seed_candidate()
        self.controller.add_emote_to_draft.side_effect = OSError("写入失败")
        result = self.api.save_emote("VEmoji_A", True)
        self.assertFalse(result["ok"])
        self.assertIsNone(self.api._emote_replacement)
        self.assertIn("写入失败", result["error"])

    def test_select_and_refresh_selection_clear_custom_candidate(self):
        self.seed_candidate()
        result = self.api.select_asset("texture", "other.bundle", "UT_Item_Emoji_102")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIsNone(self.api._emote_replacement)
        self.seed_candidate()
        result = self.api.refresh_selection()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIsNone(self.api._emote_replacement)

    def test_refresh_detection_clears_both_candidates_even_when_selection_stays_same(self):
        self.seed_candidate()
        self.api._replacement_path = self.movie
        self.controller.has_game = False
        self.api._dashboard_state = Mock(return_value={"has_game": False})
        result = self.api.refresh_detection()
        self.assertEqual(result, {"ok": True, "data": {"has_game": False}})
        self.assertEqual(self.controller.selection, self.selection)
        self.assertIsNone(self.api._emote_replacement)
        self.assertIsNone(self.api._replacement_path)
        self.controller.refresh_detection.assert_called_once_with()

    def test_refresh_detection_failure_does_not_keep_stale_candidates(self):
        self.seed_candidate()
        self.api._replacement_path = self.movie
        self.controller.refresh_detection.side_effect = OSError("模拟检测失败")
        result = self.api.refresh_detection()
        self.assertFalse(result["ok"])
        self.assertIsNone(self.api._emote_replacement)
        self.assertIsNone(self.api._replacement_path)

    def test_export_cancel_does_not_write_or_consume_import_candidate(self):
        self.seed_candidate()
        expected = dict(self.api._emote_replacement)
        self.api._pick_path.return_value = None
        self.assertEqual(self.api.export_emote_video("VEmoji_A"), {"ok": True, "data": None})
        self.assertEqual(self.api._emote_replacement, expected)
        self.exporter.assert_not_called()

    def test_export_forwards_original_bundle_and_reports_written_file(self):
        self.api._pick_path.return_value = self.destination
        result = self.api.export_emote_video("VEmoji_A")
        self.assertTrue(result["ok"], result.get("error"))
        self.exporter.assert_called_once_with(self.original, "VEmoji_A", self.destination)
        self.assertEqual(result["data"], {"path": str(self.destination), "name": self.destination.name,
                                          "size": self.destination.stat().st_size})
        self.assertEqual(self.original.read_bytes(), b"fake original resource")

    def test_export_invalid_selection_or_slot_does_not_open_dialog(self):
        result = self.api.export_emote_video("unknown")
        self.assertFalse(result["ok"])
        self.workspace._selected_id.side_effect = ValueError("请选择表情。")
        self.assertFalse(self.api.export_emote_video("VEmoji_A")["ok"])
        self.api._pick_path.assert_not_called()
        self.exporter.assert_not_called()

    def test_export_failure_returns_error(self):
        self.api._pick_path.return_value = self.destination
        self.exporter.side_effect = OSError("导出目标不可写")
        result = self.api.export_emote_video("VEmoji_A")
        self.assertFalse(result["ok"])
        self.assertIn("不可写", result["error"])
        self.assertFalse(self.destination.exists())

    def test_http_methods_keep_positional_arguments_and_json_boolean(self):
        imported = self.http_request("choose_emote_file", ["VEmoji_A"])
        self.assertEqual(imported["status"], 200)
        self.assertTrue(imported["data"]["ok"], imported["data"].get("error"))
        saved = self.http_request("save_emote", ["VEmoji_A", True])
        self.assertEqual(saved["status"], 200)
        self.assertTrue(saved["data"]["ok"], saved["data"].get("error"))
        self.controller.add_emote_to_draft.assert_called_once_with("VEmoji_A", Path(imported["data"]["data"]["path"]), independent=True)
        self.api._pick_path.return_value = self.destination
        exported = self.http_request("export_emote_video", ["VEmoji_A"])
        self.assertEqual(exported["status"], 200)
        self.assertTrue(exported["data"]["ok"], exported["data"].get("error"))
        self.exporter.assert_called_once_with(self.original, "VEmoji_A", self.destination)

    def test_http_string_boolean_is_rejected_and_consumes_candidate(self):
        self.seed_candidate()
        result = self.http_request("save_emote", ["VEmoji_A", "false"])
        self.assertEqual(result["status"], 200)
        self.assert_rejected_without_write(result["data"])

    def prepare_detail(self, *, source_available=True):
        self.controller.draft_items = [self.item]
        draft_bundle = self.root / self.item["bundle"]
        draft_bundle.write_bytes(b"fake draft configuration")
        self.controller._draft_dir.return_value = self.root
        self.controller.original_bundle_path.side_effect = lambda name: (
            self.original if name == self.item["bundle"] else self.root / "cover.bundle" if source_available else None)
        self.controller.preview_bundle.return_value = (self.root / "cover.png", None)
        self.api._image_data = Mock(return_value="data:image/png;base64,isolated-cover")
        self.api._animation_preview = Mock()

    def test_emote_detail_returns_actual_animation_and_static_original_cover(self):
        self.prepare_detail()
        result = self.api.get_draft_detail(0)
        self.assertTrue(result["ok"], result.get("error"))
        data = result["data"]
        self.assertEqual(data["original_data"], data["modified_data"])
        self.assertEqual(data["original_data"], "data:image/png;base64,isolated-cover")
        self.assertEqual(data["emote_info"]["video_name"], "VEmoji_A")
        self.assertFalse(data["emote_info"]["custom_video"])
        self.assertEqual(data["emote_animation"], self.animation)
        self.assertIn("实际解码", data["emote_info"]["preview_notice"])
        self.assertNotIn("original_animation", data)
        self.assertNotIn("modified_animation", data)
        self.api._animation_preview.assert_not_called()
        self.controller.draft_preview_paths.assert_not_called()
        self.controller.preview_bundle.assert_called_once_with(
            self.root / "cover.bundle", self.item["name"], tag="emote-cover")

    def test_emote_detail_without_source_cover_still_decodes_actual_animation(self):
        self.prepare_detail(source_available=False)
        result = self.api.get_draft_detail(0)
        self.assertTrue(result["ok"], result.get("error"))
        data = result["data"]
        self.assertEqual(data["original_data"], "")
        self.assertEqual(data["modified_data"], "")
        self.assertEqual(data["emote_animation"], self.animation)
        self.controller.preview_bundle.assert_not_called()
        self.api._animation_preview.assert_not_called()

    def test_emote_detail_preserves_custom_mode_and_rejects_invalid_index(self):
        self.item["custom_video"] = True
        self.prepare_detail()
        self.assertTrue(self.api.get_draft_detail(0)["data"]["emote_info"]["custom_video"])
        for invalid in (-1, 1):
            with self.subTest(index=invalid):
                self.assertFalse(self.api.get_draft_detail(invalid)["ok"])

    def test_preview_uses_original_and_saved_bundle_and_caches_separately(self):
        self.controller._draft_dir.return_value = self.root / "draft"
        first = self.api.get_emote_preview("VEmoji_A", "original")
        self.assertTrue(first["ok"], first.get("error"))
        self.api.get_emote_preview("VEmoji_A", "original")
        self.previewer.assert_called_once()
        draft = self.root / "draft" / "native-video.bundle"
        draft.parent.mkdir()
        draft.write_bytes(b"saved custom video")
        self.controller.draft_items = [{**self.item, "custom_video": True}]
        saved = self.api.get_emote_preview("VEmoji_A", "draft")
        self.assertTrue(saved["ok"], saved.get("error"))
        self.assertEqual(self.previewer.call_args.args, (draft, "VEmoji_A"))
        saved["data"]["frames"].clear()
        self.assertEqual(len(self.api.get_emote_preview("VEmoji_A", "draft")["data"]["frames"]), 2)

    def test_custom_draft_missing_video_does_not_show_official_video_as_result(self):
        self.controller._draft_dir.return_value = self.root / "missing-draft"
        self.controller.draft_items = [{**self.item, "custom_video": True}]
        result = self.api.get_emote_preview("VEmoji_A", "draft")
        self.assertFalse(result["ok"])
        self.assertIn("缺失", result["error"])
        self.previewer.assert_not_called()

    def test_preview_decode_failure_can_be_retried(self):
        self.previewer.side_effect = [ValueError("坏视频"), self.animation]
        self.assertFalse(self.api.get_emote_preview("VEmoji_A")["ok"])
        self.assertTrue(self.api.get_emote_preview("VEmoji_A")["ok"])
        self.assertEqual(self.previewer.call_count, 2)

    def test_preview_cache_detects_changed_bytes_even_if_stat_is_unchanged(self):
        self.previewer.side_effect = [self.animation, {**self.animation, "notice": "new video"}]
        self.assertTrue(self.api.get_emote_preview("VEmoji_A")["ok"])
        before = self.original.stat()
        self.original.write_bytes(b"x" * before.st_size)
        os.utime(self.original, ns=(before.st_atime_ns, before.st_mtime_ns))
        result = self.api.get_emote_preview("VEmoji_A")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["notice"], "new video")
        self.assertEqual(self.previewer.call_count, 2)

    def test_preview_rejects_invalid_choice_variant_or_selection(self):
        for name, variant in (("unknown", "original"), ("VEmoji_A", "fake")):
            self.assertFalse(self.api.get_emote_preview(name, variant)["ok"])
        self.workspace._selected_id.side_effect = ValueError("请选择表情")
        self.assertFalse(self.api.get_emote_preview("VEmoji_A")["ok"])
        self.previewer.assert_not_called()

    def test_owned_conversion_is_cleaned_on_cancel_switch_and_failure(self):
        for action in ("cancel", "switch", "failure"):
            with self.subTest(action=action):
                self.controller.add_emote_to_draft.side_effect = None
                self.api._pick_path.return_value = self.movie
                chosen = self.choose()
                output = Path(chosen["path"])
                self.assertTrue(output.exists())
                if action == "cancel":
                    self.api._pick_path.return_value = None
                    self.api.choose_emote_file("VEmoji_A")
                elif action == "switch":
                    self.api.refresh_selection()
                else:
                    self.controller.add_emote_to_draft.side_effect = OSError("save failed")
                    self.api.save_emote("VEmoji_A", True)
                self.assertFalse(output.parent.exists())
                self.assertTrue(self.movie.exists())

    def test_custom_save_checks_both_original_source_and_converted_bytes(self):
        for changed in ("source", "converted", "reference"):
            with self.subTest(changed=changed):
                self.controller.add_emote_to_draft.reset_mock()
                self.movie.write_bytes(b"isolated candidate")
                self.original.write_bytes(b"fake original resource")
                chosen = self.choose()
                target = {"source": self.movie, "converted": Path(chosen["path"]), "reference": self.original}[changed]
                before = target.stat()
                target.write_bytes(b"z" * before.st_size)
                os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
                self.assert_rejected_without_write(self.api.save_emote("VEmoji_A", True))
                self.assertFalse(Path(chosen["path"]).parent.exists())

    def test_detail_previews_native_original_and_reports_decode_failure(self):
        self.prepare_detail()
        self.workspace.context.return_value = {"records": [{"emote_id": 101, "video_name": "VEmoji_B"}]}
        self.previewer.side_effect = [ValueError("saved broken"), self.animation]
        response = self.api.get_draft_detail(0)
        self.assertTrue(response["ok"], response.get("error"))
        self.assertEqual(response["data"]["emote_animation"]["frames"], [])
        self.assertIn("预览失败", response["data"]["emote_info"]["preview_notice"])
        self.assertEqual(response["data"]["emote_original_animation"], self.animation)

    def test_emote_draft_rejects_image_replacement_before_dialog(self):
        self.controller.draft_items = [self.item]
        result = self.api.replace_draft_image(0)
        self.assertFalse(result["ok"])
        self.api._pick_path.assert_not_called()
        self.controller.update_draft_texture.assert_not_called()

    def test_emote_draft_rejects_crop_before_dialog_and_clears_old_session(self):
        self.controller.draft_items = [self.item]
        self.api._draft_crop_path = self.movie
        self.api._draft_crop_index = 0
        self.api._draft_crop_item = dict(self.item)
        result = self.api.pick_draft_crop_source(0)
        self.assertFalse(result["ok"])
        self.api._pick_path.assert_not_called()
        self.assertIsNone(self.api._draft_crop_path)
        self.assertIsNone(self.api._draft_crop_index)
        self.assertIsNone(self.api._draft_crop_item)

    def test_emote_draft_rejects_forged_crop_commit_without_writing(self):
        self.controller.draft_items = [self.item]
        self.api._draft_crop_path = self.movie
        self.api._draft_crop_index = 0
        self.api._draft_crop_item = dict(self.item)
        result = self.api.commit_draft_crop([0, 0, 10, 10])
        self.assertFalse(result["ok"])
        self.controller.update_draft_texture.assert_not_called()
        self.assertIsNone(self.api._draft_crop_path)


if __name__ == "__main__":
    unittest.main()
