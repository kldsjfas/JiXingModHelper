"""Compatibility checks for saved atlas drafts without reading game files."""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from astral_party_auto.web_app import DesktopApi


class DraftPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-draft-preview-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundle = self.root / "character.bundle"
        self.bundle.write_bytes(b"fake draft bundle")
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api.controller = Mock()
        self.api.controller.draft_items = [{
            "kind": "texture", "bundle": self.bundle.name,
            "name": "Cry-001", "from_anim": "Cry",
        }]
        self.api.controller.original_bundle_path.return_value = self.root / "original.bundle"
        self.api.controller._draft_dir.return_value = self.root
        self.api.controller.draft_preview_paths.return_value = (
            self.root / "original.png", self.root / "modified.png",
        )
        self.api._image_data = Mock(side_effect=lambda path: f"image:{path.name}")
        self.api._animation_preview = Mock()

    def detail(self):
        response = self.api.get_draft_detail(0)
        self.assertTrue(response["ok"], response.get("error"))
        return response["data"]

    def test_unsupported_legacy_atlas_keeps_both_images_and_reason(self):
        self.api._animation_preview.side_effect = ValueError("骨骼动画需要游戏运行时。")
        detail = self.detail()
        self.assertEqual(detail["original_data"], "image:original.png")
        self.assertEqual(detail["modified_data"], "image:modified.png")
        for side in ("original", "modified"):
            self.assertEqual(detail[f"{side}_animation"]["frames"], [])
            self.assertIn("骨骼动画", detail[f"{side}_animation"]["notice"])
        self.api.controller.draft_preview_paths.assert_called_once_with(0)

    def test_playable_atlas_stays_animated_without_static_fallback(self):
        self.api._animation_preview.return_value = {"frames": ["frame 1", "frame 2"]}
        detail = self.detail()
        for side in ("original", "modified"):
            self.assertEqual(len(detail[f"{side}_animation"]["frames"]), 2)
            self.assertEqual(detail[f"{side}_data"], "")
        self.api.controller.draft_preview_paths.assert_not_called()

    def test_failed_modified_preview_does_not_replace_playable_original(self):
        self.api._animation_preview.side_effect = [
            {"frames": ["original frame"]}, ValueError("修改后的动画不支持独立播放。"),
        ]
        detail = self.detail()
        self.assertEqual(detail["original_animation"]["frames"], ["original frame"])
        self.assertEqual(detail["original_data"], "")
        self.assertEqual(detail["modified_data"], "image:modified.png")
        self.assertIn("不支持独立播放", detail["modified_animation"]["notice"])


if __name__ == "__main__":
    unittest.main()
