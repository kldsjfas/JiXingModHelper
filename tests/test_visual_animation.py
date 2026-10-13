from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

from astral_party_auto.modkit.visual_animation import VisualAnimationWorkspace, _clear_clip_pixels, animation_identity, pack_frames, sample_frames
from astral_party_auto.web_app import DesktopApi


def overlaps(first, second):
    x, y, w, h = first
    other_x, other_y, other_w, other_h = second
    return x < other_x + other_w and x + w > other_x and y < other_y + other_h and y + h > other_y


class AtlasLayoutTests(unittest.TestCase):
    def test_original_and_existing_animation_pixels_are_reserved(self):
        reserved = [[1921, 1, 512, 512], [1, 1921, 900, 600]]
        placed = pack_frames((1920, 1920), reserved, (350, 480), 100)
        self.assertGreater(len(placed), 2)
        for index, rect in enumerate(placed):
            self.assertFalse(overlaps(rect, [0, 0, 1920, 1920]))
            self.assertTrue(all(not overlaps(rect, existing) for existing in reserved))
            self.assertTrue(all(not overlaps(rect, previous) for previous in placed[:index]))
            self.assertLessEqual(rect[0] + rect[2], 4096)
            self.assertLessEqual(rect[1] + rect[3], 4096)

    def test_large_frame_is_rejected_instead_of_overwriting_official_pixels(self):
        self.assertEqual(pack_frames((1920, 1920), [], (4096, 4096), 2), [])

    def test_sampling_keeps_total_time_and_preserves_first_frame(self):
        frames = [Image.new("RGBA", (2, 2), (index, 0, 0, 255)) for index in range(10)]
        selected, durations = sample_frames(frames, [100, 200] * 5, 3)
        self.assertEqual(sum(durations), 1500)
        self.assertEqual([frame.getpixel((0, 0))[0] for frame in selected], [0, 3, 6])
        self.assertEqual(len(selected), 3)


class VisualCandidateApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory(prefix="jixing-visual-api-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.source = self.root / "example.gif"
        self.source.write_bytes(b"mock media")
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._visual_candidate = None
        self.api._visual_candidate_dir = None
        self.api._pick_path = Mock(return_value=self.source)
        self.api._draft_state = Mock(return_value={"items": []})
        self.api.controller = Mock()
        self.api.controller.selection = {"asset_type": "texture", "bundle": "image.bundle", "name": "UT_HandCard_10001"}
        self.api.controller._index_source_key = "game-a"
        self.workspace = self.api.controller.visual_animation_workspace
        self.animation = {"frames": ["a", "b"], "durations": [100, 200]}
        self.workspace.prepare.return_value = {"item": {"name": "UT_HandCard_10001"}, "animation": self.animation, "notice": "2 帧"}
        self.workspace.save.return_value = {"name": "UT_HandCard_10001"}
        self.workspace.draft_preview.return_value = self.animation
        self.enterContext(patch("astral_party_auto.web_app.DATA_DIR", self.root / "data"))

    def choose(self):
        result = self.api.choose_visual_animation()
        self.assertTrue(result["ok"], result.get("error"))
        return result

    def test_cancelled_import_removes_previous_candidate(self):
        self.choose()
        old_folder = Path(self.api._visual_candidate_dir.name)
        self.api._pick_path.return_value = None
        self.assertIsNone(self.choose()["data"])
        self.assertFalse(old_folder.exists())
        self.assertFalse(self.api.save_visual_animation()["ok"])
        self.workspace.save.assert_not_called()

    def test_failed_import_removes_previous_candidate_and_output(self):
        self.choose()
        self.workspace.prepare.side_effect = ValueError("broken media")
        self.assertFalse(self.api.choose_visual_animation()["ok"])
        self.assertIsNone(self.api._visual_candidate)
        self.assertIsNone(self.api._visual_candidate_dir)
        self.assertFalse(self.api.save_visual_animation()["ok"])

    def test_different_selection_cannot_save_old_animation(self):
        self.choose()
        self.api.controller.selection["name"] = "UT_HandCard_10002"
        self.assertFalse(self.api.save_visual_animation()["ok"])
        self.workspace.save.assert_not_called()

    def test_successful_save_returns_actual_draft_preview_and_consumes_candidate(self):
        self.choose()
        result = self.api.save_visual_animation()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["animation"], self.animation)
        self.workspace.draft_preview.assert_called_once()
        self.assertIsNone(self.api._visual_candidate_dir)
        self.assertFalse(self.api.save_visual_animation()["ok"])

    def test_late_result_is_rejected_when_selection_changes_during_prepare(self):
        def changed(*_args):
            self.api.controller.selection["name"] = "UT_HandCard_10002"
            return {"item": {}, "animation": self.animation, "notice": "2 帧"}
        self.workspace.prepare.side_effect = changed
        result = self.api.choose_visual_animation()
        self.assertFalse(result["ok"])
        self.assertIsNone(self.api._visual_candidate_dir)

    def test_preview_failure_after_save_reports_saved_state(self):
        self.choose()
        self.workspace.draft_preview.side_effect = ValueError("preview unavailable")
        result = self.api.save_visual_animation()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIn("已经保存", result["data"]["animation"]["notice"])
        self.workspace.save.assert_called_once()

    def test_saved_item_remains_available_when_its_dynamic_preview_fails(self):
        bundle = self.root / "config.bundle"
        bundle.write_bytes(b"mock bundle")
        item = {"kind": "visual_animation", "bundle": bundle.name, "source_bundle": "image.bundle",
                "name": "UT_HandCard_10001", "frame_count": 2, "duration_ms": 300}
        self.api.controller.draft_items = [item]
        self.api.controller.original_bundle_path.return_value = bundle
        self.api.controller._draft_dir.return_value = self.root
        self.api.controller.preview_bundle.return_value = ("original.png", {})
        self.api._image_data = Mock(return_value="original pixels")
        self.workspace.context.return_value = {"ui_bundle": "ui.bundle", "atlas_bundle": "atlas.bundle"}
        self.workspace.preview.side_effect = ValueError("broken preview")
        result = self.api.get_draft_detail(0)
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["item"], item)
        self.assertEqual(result["data"]["original_data"], "original pixels")
        self.assertEqual(result["data"]["modified_animation"]["frames"], [])
        self.assertIn("移除", result["data"]["visual_info"]["notice"])


class VisualRemovalGuardTests(unittest.TestCase):
    def setUp(self):
        self.atlas = Image.new("RGBA", (32, 32), (45, 90, 120, 255))
        self.clip = {"frames": [{"sprite_id": "jm_owned_0"}]}
        self.package = {"sprites": [
            {"id": "jm_owned_0", "atlas_id": "atlas0", "rect": [12, 12, 4, 4], "rotated": False, "trimmed": False},
            {"id": "other", "atlas_id": "atlas0", "rect": [20, 20, 4, 4], "rotated": False, "trimmed": False},
        ]}

    def test_removal_clears_owned_frame_and_border_only(self):
        before = self.atlas.copy()
        _clear_clip_pixels(self.atlas, self.package, self.clip, (8, 8))
        for y in range(32):
            for x in range(32):
                expected = (0, 0, 0, 0) if 11 <= x < 17 and 11 <= y < 17 else before.getpixel((x, y))
                self.assertEqual(self.atlas.getpixel((x, y)), expected)

    def test_overlap_or_changed_layout_fails_before_any_pixel_is_cleared(self):
        for changes in ({"rect": [7, 7, 4, 4]}, {"rect": [17, 17, 4, 4]},
                        {"rect": [0, 12, 4, 4]}, {"rect": [28, 12, 4, 4]},
                        {"atlas_id": "another"}, {"trimmed": True}, {"rotated": True}):
            with self.subTest(changes=changes):
                package = {"sprites": [self.package["sprites"][0] | changes, self.package["sprites"][1]]}
                before = self.atlas.tobytes()
                with self.assertRaises(ValueError):
                    _clear_clip_pixels(self.atlas, package, self.clip, (8, 8))
                self.assertEqual(self.atlas.tobytes(), before)

    def test_all_rectangles_are_checked_before_clearing_first_frame(self):
        self.clip["frames"].append({"sprite_id": "jm_owned_1"})
        self.package["sprites"].append(self.package["sprites"][0] | {"id": "jm_owned_1", "rect": [19, 19, 4, 4]})
        before = self.atlas.tobytes()
        with self.assertRaises(ValueError):
            _clear_clip_pixels(self.atlas, self.package, self.clip, (8, 8))
        self.assertEqual(self.atlas.tobytes(), before)

    def test_incomplete_or_forged_table_list_cannot_start_removal(self):
        controller = Mock()
        workspace = VisualAnimationWorkspace(controller)
        name = "UT_Shared_Image"
        context = {"config_bundle": "config.bundle", "ui_bundle": "ui.bundle", "atlas_bundle": "atlas.bundle",
                   "package_id": "test1234", "tables": {table: {"targets": [{"name": name}]} for table in ("Card", "Skin")}}
        workspace.context = Mock(return_value=context)
        for tables in ([], ["Card"], ["Skin"], ["Card", "Skin", "Skin"], ["Card", "Unknown"]):
            with self.subTest(tables=tables):
                controller.draft_items = [{"kind": "visual_animation", "name": name, "bundle": "config.bundle",
                                          "extra_bundles": ["ui.bundle", "atlas.bundle"], "tables": tables,
                                          "movieclip_url": "ui://test1234" + animation_identity(name)}]
                with self.assertRaises(ValueError):
                    workspace.remove(0)
                controller.emote_workspace._prepare_folder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
