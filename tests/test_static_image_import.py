"""静态贴图入口不能把多帧图片悄悄变成单帧。"""
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

from astral_party_auto.modkit.maker import read_static_image, replace_bundle_texture
from astral_party_auto.web_app import DesktopApi


class StaticImageImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-static-images-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.frames = [Image.new("RGBA", (4, 4), color) for color in ("red", "blue")]
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._replacement_path = None
        self.api.controller = Mock()
        self.api.controller.selection = {"asset_type": "texture", "name": "Emoji"}
        self.api._image_data = Mock(return_value="preview")

    def make_animation(self, suffix, image_format):
        path = self.root / ("animated" + suffix)
        self.frames[0].save(path, format=image_format, save_all=True, append_images=self.frames[1:], duration=100, loop=0)
        return path

    def test_multiframe_gif_apng_and_webp_rejected_before_bundle_load(self):
        for suffix, image_format in ((".gif", "GIF"), (".png", "PNG"), (".webp", "WEBP")):
            with self.subTest(format=image_format):
                path = self.make_animation(suffix, image_format)
                with patch("astral_party_auto.modkit.maker._load_bundle") as load:
                    with self.assertRaisesRegex(ValueError, "多帧"):
                        replace_bundle_texture("unused.bundle", path, self.root / "out.bundle")
                    load.assert_not_called()
                self.assertFalse((self.root / "out.bundle").exists())

    def test_single_frame_accepted_and_source_handle_closed(self):
        path = self.root / "static.png"
        self.frames[0].save(path)
        with read_static_image(path) as image:
            self.assertEqual(image.size, (4, 4))
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 255))
        path.unlink()
        self.assertFalse(path.exists())

    def test_cancel_or_bad_candidate_preserves_previous_static_preview(self):
        for path in (None, self.make_animation(".png", "PNG")):
            with self.subTest(path=path):
                self.api._replacement_path = self.root / "older.png"
                self.api._pick_path = Mock(return_value=path)
                result = self.api.choose_replacement()
                self.assertEqual(result["ok"], path is None)
                self.assertEqual(self.api._replacement_path, self.root / "older.png")

    def test_static_candidate_returns_preview(self):
        path = self.root / "static.png"
        self.frames[0].save(path)
        self.api._pick_path = Mock(return_value=path)
        result = self.api.choose_replacement()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["preview_data"], "preview")
        self.assertEqual(self.api._replacement_path, path)


if __name__ == "__main__":
    unittest.main()
