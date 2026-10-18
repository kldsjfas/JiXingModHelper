"""固定裁剪界面的 API 回归；所有图片和输出都在隔离目录中。"""
from __future__ import annotations

import hashlib
import math
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

from astral_party_auto.web_app import DesktopApi


class CropApiTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="jixing-crop-api-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.png"
        with Image.new("RGBA", (80, 60)) as image:
            image.putdata([(x, y, x + y, 255) for y in range(60) for x in range(80)])
            image.save(self.source)
        self.source_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.item = {"kind": "texture", "bundle": "card.bundle", "name": "card"}
        self.controller = Mock()
        self.controller.selection = {"asset_type": "texture", "bundle": "card.bundle", "name": "card"}
        self.controller._index_source_key = "isolated-game-a"
        self.controller.draft_items = [dict(self.item)]
        self.controller.original_bundle_path.return_value = None
        self.controller.update_draft_texture.return_value = self.item
        self.controller.installed_mods.return_value = []
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._replacement_path = None
        self.api._image_cache = {}
        self.api._draft_crop_path = None
        self.api._draft_crop_index = None
        self.api._draft_crop_item = None
        self.api._pick_path = Mock(return_value=self.source)
        self.api._draft_state = Mock(return_value={"items": [self.item]})
        self.api._dashboard_state = Mock(return_value={})
        self.api.controller = self.controller
        self.enterContext(patch("astral_party_auto.web_app.DATA_DIR", self.root / "data"))

    def unwrap(self, result):
        self.assertTrue(result["ok"], result.get("error"))
        return result["data"]

    def choose_replacement(self):
        return self.unwrap(self.api.choose_replacement())

    def choose_draft(self):
        return self.unwrap(self.api.pick_draft_crop_source(0))

    def assert_rejected(self, result):
        self.assertFalse(result["ok"])
        self.controller.update_draft_texture.assert_not_called()

    def test_replacement_saves_exact_source_pixels_and_rotates_token(self):
        chosen = self.choose_replacement()
        cropped = self.unwrap(self.api.crop_replacement([11, 7, 43, 31], chosen["crop_token"]))
        with Image.open(cropped["path"]) as image:
            self.assertEqual(image.size, (32, 24))
            self.assertEqual(image.getpixel((0, 0)), (11, 7, 18, 255))
            self.assertEqual(image.getpixel((31, 23)), (42, 30, 72, 255))
        self.assertNotEqual(chosen["crop_token"], cropped["crop_token"])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.source_hash)
        self.assertFalse(self.api.crop_replacement([0, 0, 20, 20], chosen["crop_token"])["ok"])

    def test_replacement_legacy_box_and_integer_float_coordinates_work(self):
        self.choose_replacement()
        result = self.unwrap(self.api.crop_replacement([0.0, 0.0, 20.0, 10.0]))
        with Image.open(result["path"]) as image:
            self.assertEqual(image.size, (20, 10))

    def test_replacement_save_failure_preserves_prior_crop(self):
        chosen = self.choose_replacement()
        first = self.unwrap(self.api.crop_replacement([0, 0, 40, 30], chosen["crop_token"]))
        output = Path(first["path"])
        before = output.read_bytes()
        with patch("PIL.Image.Image.save", side_effect=OSError("模拟写入失败")):
            self.assert_rejected(self.api.crop_replacement([0, 0, 20, 10], first["crop_token"]))
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(self.api._replacement_path, output)
        self.assertEqual(self.api._replacement_crop_token, first["crop_token"])
        self.assertEqual(list(output.parent.glob(".crop-*.png")), [])

    def test_invalid_replacement_boxes_do_not_write_or_change_candidate(self):
        chosen = self.choose_replacement()
        invalid_boxes = [None, [], [0, 0, 1], "0,0,1,1", [-1, 0, 40, 30],
                         [0, -1, 40, 30], [0, 0, 81, 60], [0, 0, 80, 61],
                         [2, 0, 2, 20], [30, 0, 10, 20], [0, 4, 20, 3],
                         [0, 0, 10**9, 10**9], [0, 0, 20.5, 10],
                         [False, 0, 20, 10], ["0", 0, 20, 10],
                         [0, 0, math.nan, 10], [0, 0, math.inf, 10]]
        for box in invalid_boxes:
            with self.subTest(box=box):
                self.assert_rejected(self.api.crop_replacement(box, chosen["crop_token"]))
                self.assertEqual(self.api._replacement_path, self.source)
                self.assertFalse((self.root / "data" / "crop_preview.png").exists())

    def test_stale_replacement_token_cannot_crop_new_candidate(self):
        old = self.choose_replacement()
        current = self.choose_replacement()
        self.assert_rejected(self.api.crop_replacement([0, 0, 20, 10], old["crop_token"]))
        self.assertEqual(self.api._replacement_crop_token, current["crop_token"])
        self.unwrap(self.api.crop_replacement([0, 0, 20, 10], current["crop_token"]))

    def test_replacement_selection_and_external_file_change_are_rejected(self):
        chosen = self.choose_replacement()
        self.controller.selection["name"] = "other-card"
        self.assert_rejected(self.api.crop_replacement([0, 0, 20, 10], chosen["crop_token"]))
        self.controller.selection["name"] = "card"
        with Image.new("RGBA", (80, 60), "red") as image:
            image.save(self.source)
        self.assert_rejected(self.api.crop_replacement([0, 0, 20, 10], chosen["crop_token"]))

    def test_resource_changed_in_file_dialog_does_not_bind_candidate(self):
        def switch_resource(*_args, **_kwargs):
            self.controller.selection["name"] = "other-card"
            return self.source

        self.api._pick_path.side_effect = switch_resource
        self.assert_rejected(self.api.choose_replacement())
        self.assertIsNone(self.api._replacement_path)

    def test_draft_changed_in_file_dialog_does_not_open_wrong_item(self):
        def switch_draft_item(*_args, **_kwargs):
            self.controller.draft_items[0]["name"] = "other-card"
            return self.source

        self.api._pick_path.side_effect = switch_draft_item
        self.assert_rejected(self.api.pick_draft_crop_source(0))
        self.assertIsNone(self.api._draft_crop_path)

    def test_replacement_import_returns_crop_token(self):
        import base64

        expected = dict(self.controller.selection)
        payload = base64.b64encode(self.source.read_bytes()).decode("ascii")
        result = self.unwrap(self.api.import_replacement_file("dropped.png", payload, expected))
        self.assertTrue(result["crop_token"])
        self.unwrap(self.api.crop_replacement([2, 3, 20, 15], result["crop_token"]))
        self.api._clear_replacement()

    def test_exif_orientation_matches_displayed_coordinates_for_both_crop_entries(self):
        # 不依赖被测的 exif_transpose 生成期望值，直接由原始像素坐标换算。
        mappings = {
            2: ((80, 60), lambda x, y: (79 - x, y)),
            3: ((80, 60), lambda x, y: (79 - x, 59 - y)),
            6: ((60, 80), lambda x, y: (y, 59 - x)),
            8: ((60, 80), lambda x, y: (79 - y, x)),
        }
        from astral_party_auto.modkit.maker import read_static_image

        for orientation, (expected_size, raw_position) in mappings.items():
            with self.subTest(orientation=orientation):
                photo = self.root / f"orientation-{orientation}.jpg"
                with Image.open(self.source) as image:
                    exif = Image.Exif()
                    exif[274] = orientation
                    image.convert("RGB").save(photo, quality=100, subsampling=0, exif=exif)
                self.api._pick_path.return_value = photo
                chosen = self.choose_replacement()
                with read_static_image(photo) as normalized:
                    self.assertEqual(normalized.size, expected_size)
                crop = self.unwrap(self.api.crop_replacement([3, 5, 20, 25], chosen["crop_token"]))
                with Image.open(photo) as original, Image.open(crop["path"]) as result:
                    self.assertEqual(result.size, (17, 20))
                    for output_x, output_y in ((0, 0), (16, 0), (0, 19), (16, 19)):
                        expected_pixel = original.getpixel(raw_position(3 + output_x, 5 + output_y))
                        self.assertEqual(result.getpixel((output_x, output_y)), (*expected_pixel, 255))
                draft = self.choose_draft()
                # 旋转后的竖图可以裁到 y=80，而未经方向归一的原始高度只有 60。
                box = [0, expected_size[1] - 10, 10, expected_size[1]]
                self.unwrap(self.api.commit_draft_crop(box, draft["crop_token"]))
                self.controller.update_draft_texture.assert_called_once_with(0, photo, crop_box=tuple(box))
                self.controller.update_draft_texture.reset_mock()

    def test_draft_passes_validated_original_pixel_box_to_controller(self):
        chosen = self.choose_draft()
        self.unwrap(self.api.commit_draft_crop([11, 7, 43, 31], chosen["crop_token"]))
        self.controller.update_draft_texture.assert_called_once_with(0, self.source, crop_box=(11, 7, 43, 31))
        self.assertIsNone(self.api._draft_crop_path)
        self.controller.update_draft_texture.reset_mock()
        self.assert_rejected(self.api.commit_draft_crop([11, 7, 43, 31], chosen["crop_token"]))

    def test_draft_legacy_box_is_supported(self):
        self.choose_draft()
        self.unwrap(self.api.commit_draft_crop([0, 0, 80, 60]))
        self.controller.update_draft_texture.assert_called_once_with(0, self.source, crop_box=(0, 0, 80, 60))

    def test_draft_invalid_boxes_never_write(self):
        for box in (None, [], [-1, 0, 20, 10], [0, 0, 81, 60], [0, 0, 20.5, 10], [0, 0, 0, 0]):
            with self.subTest(box=box):
                chosen = self.choose_draft()
                self.assert_rejected(self.api.commit_draft_crop(box, chosen["crop_token"]))
                self.assertIsNone(self.api._draft_crop_path)

    def test_draft_old_confirm_and_cancel_cannot_consume_new_session(self):
        old = self.choose_draft()
        current = self.choose_draft()
        self.assert_rejected(self.api.commit_draft_crop([0, 0, 20, 10], old["crop_token"]))
        self.assertFalse(self.unwrap(self.api.cancel_draft_crop(old["crop_token"])))
        self.assertEqual(self.api._draft_crop_token, current["crop_token"])
        self.unwrap(self.api.commit_draft_crop([0, 0, 20, 10], current["crop_token"]))

    def test_draft_cancel_prevents_later_save(self):
        chosen = self.choose_draft()
        self.assertTrue(self.unwrap(self.api.cancel_draft_crop(chosen["crop_token"])))
        self.assert_rejected(self.api.commit_draft_crop([0, 0, 20, 10], chosen["crop_token"]))

    def test_draft_changed_item_and_game_cannot_write(self):
        for changed in ("item", "game", "source"):
            with self.subTest(changed=changed):
                self.controller.draft_items = [dict(self.item)]
                self.controller._index_source_key = "isolated-game-a"
                chosen = self.choose_draft()
                if changed == "item":
                    self.controller.draft_items[0]["name"] = "other-card"
                elif changed == "game":
                    self.controller._index_source_key = "isolated-game-b"
                else:
                    with Image.new("RGBA", (80, 60), "blue") as image:
                        image.save(self.source)
                self.assert_rejected(self.api.commit_draft_crop([0, 0, 20, 10], chosen["crop_token"]))
                self.assertIsNone(self.api._draft_crop_path)

    def test_clear_and_remove_draft_invalidate_crop(self):
        for action in (self.api.clear_draft, lambda: self.api.remove_draft_item(0)):
            with self.subTest(action=action):
                chosen = self.choose_draft()
                self.unwrap(action())
                self.assert_rejected(self.api.commit_draft_crop([0, 0, 20, 10], chosen["crop_token"]))


if __name__ == "__main__":
    unittest.main()
