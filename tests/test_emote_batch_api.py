"""批量 GIF API 的文件框取消、单行预览与保存结果。"""
from __future__ import annotations

import base64
import hashlib
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from astral_party_auto.web_app import DesktopApi


class EmoteBatchApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-batch-api-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "120102001.gif"
        self.write_gif(("red", "blue"))
        self.original = self.root / "original-static.png"
        Image.new("RGBA", (4, 4), "white").save(self.original)
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._image_cache = {}
        self.api._emote_replacement = {"path": "previous single candidate"}
        self.api._emote_candidate_dir = Mock()
        self.api._emote_preview_cache = {("old",): {"frames": ["cached"]}}
        self.api.controller = Mock()
        self.api._emit = Mock()
        self.api._draft_state = Mock(return_value={"name": "当前作品", "items": [{"kind": "emote"}]})
        self.api._pick_emote_gif_paths = Mock(return_value=[self.source])
        self.source_info = {"source_id": "source-1", "name": self.source.name, "path": str(self.source),
                            "sha256": self.digest(self.source), "frames": 2, "duration_ms": 200}
        self.target = {"emote_id": 120102001, "character_id": 102, "label": "芬妮 · 表情01"}
        self.inspection = {"token": "batch-token", "sources": [self.source_info], "targets": [self.target]}
        self.api.controller.inspect_emote_gifs.return_value = self.inspection
        self.api.controller.emote_gif_batch_preview.return_value = {
            "source_path": str(self.source), "original_path": str(self.original),
            "source": self.source_info, "target": self.target}
        self.mappings = [{"source_id": "source-1", "emote_id": 120102001}]

    def write_gif(self, colors):
        frames = [Image.new("RGBA", (4, 4), color) for color in colors]
        frames[0].save(self.source, format="GIF", save_all=True, append_images=frames[1:],
                       duration=100, loop=0, disposal=2, optimize=False)

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_canceling_native_multiselect_preserves_previous_single_candidate(self):
        self.api._pick_emote_gif_paths.return_value = []
        candidate = self.api._emote_replacement
        result = self.api.choose_emote_gifs()
        self.assertEqual(result, {"ok": True, "data": None})
        self.assertIs(self.api._emote_replacement, candidate)
        self.api.controller.inspect_emote_gifs.assert_not_called()
        self.api._emote_candidate_dir.cleanup.assert_not_called()

    def test_selection_passes_native_paths_to_preflight_once(self):
        result = self.api.choose_emote_gifs()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"], self.inspection)
        self.api.controller.inspect_emote_gifs.assert_called_once_with([self.source])
        self.api._emote_candidate_dir.cleanup.assert_not_called()

    def test_preflight_failure_does_not_clear_existing_candidate_or_report_success(self):
        candidate = self.api._emote_replacement
        self.api.controller.inspect_emote_gifs.side_effect = ValueError("target is not static")
        result = self.api.choose_emote_gifs()
        self.assertFalse(result["ok"])
        self.assertIn("target is not static", result["error"])
        self.assertIs(self.api._emote_replacement, candidate)

    def test_native_picker_is_mocked_and_restores_its_previous_window_state(self):
        del self.api._pick_emote_gif_paths

        class OwnerWindow:
            topmost = False

            def attributes(self, name, value=None):
                if value is not None:
                    self.topmost = value
                return self.topmost

            def update_idletasks(self):
                pass

        self.api._tk_root = OwnerWindow()
        self.api._run_on_ui = lambda action: action()
        with patch("tkinter.filedialog.askopenfilenames", return_value=(str(self.source),)) as picker:
            self.assertEqual(self.api._pick_emote_gif_paths(), [self.source])
        self.assertFalse(self.api._tk_root.topmost)
        self.assertEqual(picker.call_args.kwargs["filetypes"], [("GIF 动图", "*.gif")])

    def test_preview_forwards_authorization_tuple_and_sends_only_one_source_and_original(self):
        result = self.api.preview_emote_gif_batch("batch-token", "source-1", 120102001)
        self.assertTrue(result["ok"], result)
        self.api.controller.emote_gif_batch_preview.assert_called_once_with("batch-token", "source-1", 120102001)
        data = result["data"]
        self.assertEqual(set(data), {"source_data", "original_data", "source", "target"})
        self.assertTrue(data["source_data"].startswith("data:image/gif;base64,"))
        self.assertTrue(data["original_data"].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(data["source_data"].split(",", 1)[1]), self.source.read_bytes())
        self.assertNotIn("sources", data)
        self.assertNotIn("targets", data)
        self.assertNotIn("source_path", data)

    def test_unauthorized_token_or_source_stops_before_image_reads(self):
        self.api.controller.emote_gif_batch_preview.side_effect = ValueError("preflight token is stale")
        with patch.object(self.api, "_image_data") as image:
            result = self.api.preview_emote_gif_batch("other-token", "outside-source", 120102001)
        self.assertFalse(result["ok"])
        self.assertIn("stale", result["error"])
        image.assert_not_called()

    def test_source_change_before_preview_is_rejected(self):
        self.write_gif(("green", "yellow"))
        result = self.api.preview_emote_gif_batch("batch-token", "source-1", 120102001)
        self.assertFalse(result["ok"])
        self.assertIn("变化", result["error"])

    def test_source_change_while_encoding_original_image_is_rejected(self):
        image_data = self.api._image_data

        def encode(path):
            value = image_data(path)
            if Path(path) == self.original:
                self.write_gif(("green", "yellow"))
            return value

        with patch.object(self.api, "_image_data", side_effect=encode):
            result = self.api.preview_emote_gif_batch("batch-token", "source-1", 120102001)
        self.assertFalse(result["ok"])
        self.assertIn("预览期间", result["error"])

    def test_new_preflight_cannot_receive_an_old_gif_from_a_size_and_mtime_cache_hit(self):
        first = self.api.preview_emote_gif_batch("batch-token", "source-1", 120102001)
        self.assertTrue(first["ok"], first)
        previous_stat = self.source.stat()
        self.write_gif(("green", "yellow"))
        self.assertEqual(self.source.stat().st_size, previous_stat.st_size)
        os.utime(self.source, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        self.source_info["sha256"] = self.digest(self.source)
        current = self.api.preview_emote_gif_batch("new-token", "source-1", 120102001)
        self.assertTrue(current["ok"], current)
        actual = base64.b64decode(current["data"]["source_data"].split(",", 1)[1])
        self.assertEqual(actual, self.source.read_bytes())
        self.assertNotEqual(actual, base64.b64decode(first["data"]["source_data"].split(",", 1)[1]))

    def test_source_over_preview_budget_is_not_base64_encoded(self):
        with self.source.open("r+b") as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        with patch.object(self.api, "_image_data") as image:
            result = self.api.preview_emote_gif_batch("batch-token", "source-1", 120102001)
        self.assertFalse(result["ok"])
        self.assertIn("16 MB", result["error"])
        image.assert_not_called()

    def test_save_forwards_explicit_mapping_and_progress_then_returns_current_draft(self):
        update = {"done": 1, "total": 1, "stage": "verify", "label": "核对成品"}

        def commit(token, mappings, *, progress):
            self.assertEqual(token, "batch-token")
            self.assertEqual(mappings, self.mappings)
            progress(update)
            return {"count": 1, "items": [{"kind": "emote", "emote_id": 120102001}]}

        self.api.controller.commit_emote_gifs.side_effect = commit
        result = self.api.save_emote_gif_batch("batch-token", self.mappings)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["count"], 1)
        self.assertEqual(result["data"]["draft"], self.api._draft_state.return_value)
        self.api._emit.assert_called_once_with("emote_batch_progress", {**update, "token": "batch-token"})
        self.assertEqual(self.api._emote_preview_cache, {})
        self.assertIsNone(self.api._emote_replacement)

    def test_commit_failure_cannot_return_a_successful_count_or_refresh_draft(self):
        self.api.controller.commit_emote_gifs.side_effect = OSError("batch not committed")
        result = self.api.save_emote_gif_batch("batch-token", self.mappings)
        self.assertFalse(result["ok"])
        self.assertIn("not committed", result["error"])
        self.api._draft_state.assert_not_called()
        self.assertNotIn("data", result)

    def test_workshop_analysis_summarizes_independent_emotes_per_character(self):
        manager = Mock()
        manager.analyze_mod.return_value = SimpleNamespace(
            matched=["config.bundle", "one.bundle"], unmatched=[],
            catalog_patches=[{"emote_id": 120102001}, {"emote_id": 120102002}, {"emote_id": 120115001}])
        manager._load_state.return_value = {"mods": {}}
        self.api.controller.manager = manager
        result = self.api._workshop_analysis(self.root, {"id": "sample-emotes"})
        self.assertEqual(result["emote_count"], 3)
        self.assertEqual({row["emote_id"] for row in result["emote_targets"]}, {120102001, 120102002, 120115001})
        counts = {row["label"].split("（", 1)[0]: row["count"] for row in result["emote_characters"]}
        self.assertEqual(counts, {"芬妮": 2, "美咲": 1})
        self.assertTrue(result["can_install"])

    def test_non_list_workshop_patch_metadata_does_not_break_plain_texture_packages(self):
        manager = Mock()
        manager._load_state.return_value = {"mods": {}}
        self.api.controller.manager = manager
        for patches in (Mock(), None, "not a patch list", {}):
            manager.analyze_mod.return_value = SimpleNamespace(matched=["texture.bundle"], unmatched=[], catalog_patches=patches)
            with self.subTest(patches=type(patches).__name__):
                result = self.api._workshop_analysis(self.root, {"id": "plain-texture"})
                self.assertEqual(result["emote_count"], 0)
                self.assertEqual(result["emote_targets"], [])
                self.assertTrue(result["can_install"])


if __name__ == "__main__":
    unittest.main()
