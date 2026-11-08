"""Bounded thumbnail decoding, cache invalidation and selection-independent API."""
from __future__ import annotations

import base64
import io
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from astral_party_auto.modkit import thumbnails
from astral_party_auto.web_app import DesktopApi


class ResourceThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-thumbnail-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.bundle"
        self.source.write_bytes(b"isolated thumbnail fixture")
        self.reader = thumbnails.ResourceThumbnails()

    def resource(self, name, color="red", size=(400, 200)):
        data = SimpleNamespace(m_Width=size[0], m_Height=size[1], image=Image.new("RGBA", size, color))
        return SimpleNamespace(type=SimpleNamespace(name="Texture2D"), peek_name=lambda: name, read=lambda: data)

    def test_thumbnail_is_bounded_and_cache_avoids_reopening_bundle(self):
        env = SimpleNamespace(objects=[self.resource("Card")])
        with patch.object(thumbnails, "_load_bundle", return_value=env) as load:
            first = self.reader.read(self.source, ["Card"])
            second = self.reader.read(self.source, ["Card"])
        self.assertEqual(first, second)
        load.assert_called_once()
        encoded = first["Card"]["thumbnail_data"].split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            self.assertEqual(image.size, (192, 96))

    def test_changed_source_stamp_invalidates_cached_pixels(self):
        with patch.object(thumbnails, "_load_bundle", side_effect=[
            SimpleNamespace(objects=[self.resource("Card", "red")]),
            SimpleNamespace(objects=[self.resource("Card", "blue")]),
        ]) as load:
            first = self.reader.read(self.source, ["Card"])
            before = self.source.stat()
            os.utime(self.source, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            second = self.reader.read(self.source, ["Card"])
        self.assertNotEqual(first, second)
        self.assertEqual(load.call_count, 2)

    def test_decode_failure_is_per_item_and_is_not_cached(self):
        broken = SimpleNamespace(type=SimpleNamespace(name="Texture2D"), peek_name=lambda: "Bad",
                                 read=Mock(side_effect=ValueError("unreadable pixels")))
        env = SimpleNamespace(objects=[broken, self.resource("Card")])
        with patch.object(thumbnails, "_load_bundle", return_value=env):
            result = self.reader.read(self.source, ["Bad", "Card", "Missing"])
            self.reader.read(self.source, ["Bad"])
        self.assertIn("error", result["Bad"])
        self.assertIn("error", result["Missing"])
        self.assertIn("thumbnail_data", result["Card"])
        self.assertEqual(broken.read.call_count, 2)

    def test_cache_entry_count_is_bounded(self):
        env = SimpleNamespace(objects=[self.resource(str(index), size=(2, 2)) for index in range(6)])
        with patch.object(thumbnails, "MAX_CACHED_THUMBNAILS", 3), \
                patch.object(thumbnails, "_load_bundle", return_value=env):
            result = self.reader.read(self.source, [str(index) for index in range(6)])
        self.assertEqual(len(result), 6)
        self.assertEqual(len(self.reader._cache), 3)

    def test_source_change_during_decode_does_not_enter_cache(self):
        original = self.resource("Card")
        def change_during_read():
            self.source.write_bytes(b"different source length")
            return original.read()
        resource = SimpleNamespace(type=original.type, peek_name=original.peek_name, read=change_during_read)
        with patch.object(thumbnails, "_load_bundle", return_value=SimpleNamespace(objects=[resource])):
            with self.assertRaisesRegex(ValueError, "发生变化"):
                self.reader.read(self.source, ["Card"])
        self.assertFalse(self.reader._cache)

    def test_large_texture_is_rejected_before_accessing_pixels(self):
        class LargeTexture:
            m_Width = m_Height = 100_000
            @property
            def image(self):
                raise AssertionError("oversize texture must not be decoded")
        resource = SimpleNamespace(type=SimpleNamespace(name="Texture2D"), peek_name=lambda: "Huge",
                                   read=lambda: LargeTexture())
        with patch.object(thumbnails, "_load_bundle", return_value=SimpleNamespace(objects=[resource])):
            result = self.reader.read(self.source, ["Huge"])
        self.assertIn("过大", result["Huge"]["error"])

    def test_large_sprite_rectangle_is_rejected_before_decoding(self):
        class LargeSprite:
            m_Rect = SimpleNamespace(width=100_000, height=100_000)
            m_RD = SimpleNamespace(texture=SimpleNamespace(read=lambda: SimpleNamespace(m_Width=4, m_Height=4)))
            m_SpriteAtlas = None
            m_AtlasTags = []
            @property
            def image(self):
                raise AssertionError("oversize Sprite must not be decoded")
        resource = SimpleNamespace(type=SimpleNamespace(name="Sprite"), peek_name=lambda: "Huge",
                                   read=lambda: LargeSprite())
        with patch.object(thumbnails, "_load_bundle", return_value=SimpleNamespace(objects=[resource])):
            result = self.reader.read(self.source, ["Huge"])
        self.assertIn("过大", result["Huge"]["error"])

    def test_sprite_atlas_override_is_checked_before_decoding(self):
        huge_atlas = SimpleNamespace(m_Width=100_000, m_Height=100_000)
        real_data = SimpleNamespace(texture=SimpleNamespace(read=lambda: huge_atlas), alphaTexture=None)
        atlas = SimpleNamespace(m_RenderDataMap=[("frame", real_data)])
        class AtlasSprite:
            m_Rect = SimpleNamespace(width=4, height=4)
            m_RenderDataKey = "frame"
            m_SpriteAtlas = SimpleNamespace(deref_parse_as_object=lambda: atlas)
            m_AtlasTags = []
            m_RD = SimpleNamespace(texture=SimpleNamespace(read=lambda: SimpleNamespace(m_Width=4, m_Height=4)))
            @property
            def image(self):
                raise AssertionError("oversize SpriteAtlas must not be decoded")
        resource = SimpleNamespace(type=SimpleNamespace(name="Sprite"), peek_name=lambda: "Huge",
                                   read=lambda: AtlasSprite())
        with patch.object(thumbnails, "_load_bundle", return_value=SimpleNamespace(objects=[resource])):
            result = self.reader.read(self.source, ["Huge"])
        self.assertIn("过大", result["Huge"]["error"])

    def api(self):
        api = object.__new__(DesktopApi)
        api._controller_lock = threading.RLock()
        api._append_log = Mock()
        api.controller = SimpleNamespace(typed_index={"texture": {"source.bundle": ["Card"]}},
                                         original_bundle_path=Mock(return_value=self.source),
                                         selection={"name": "keep selection"})
        return api

    def test_thumbnail_api_keeps_controller_selection_and_rejects_unknown_resources(self):
        api = self.api()
        with patch.object(thumbnails, "_load_bundle", return_value=SimpleNamespace(objects=[self.resource("Card")])):
            result = api.get_resource_thumbnails("texture", [
                {"bundle": "source.bundle", "name": "Card"},
                {"bundle": "../other.bundle", "name": "Card"},
            ])
        self.assertTrue(result["ok"])
        self.assertIn("thumbnail_data", result["data"]["items"][0])
        self.assertIn("error", result["data"]["items"][1])
        self.assertEqual(api.controller.selection, {"name": "keep selection"})
        api.controller.original_bundle_path.assert_called_once_with("source.bundle")

    def test_thumbnail_api_limits_batch_size_and_asset_type(self):
        api = self.api()
        self.assertFalse(api.get_resource_thumbnails("anim", [])["ok"])
        self.assertFalse(api.get_resource_thumbnails("texture", [{}] * 9)["ok"])
        api.controller.original_bundle_path.assert_not_called()


if __name__ == "__main__":
    unittest.main()
