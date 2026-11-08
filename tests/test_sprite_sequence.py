from __future__ import annotations

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from astral_party_auto.modkit import animation, sprite_sequence


def rectangle(x=0, y=0, width=4, height=4):
    return SimpleNamespace(x=x, y=y, width=width, height=height)


class Texture:
    def __init__(self):
        self.m_Name = "shared_atlas"
        self.m_Width = self.m_Height = 12
        self.m_MipCount = self.m_ImageCount = 1
        self.m_TextureDimension = 2
        self.image = Image.new("RGBA", (12, 12), (20, 40, 60, 80))
        self.save = Mock()

    def set_image(self, image, *, target_format, mipmap_count):
        if (target_format, mipmap_count) != (4, 1):
            raise AssertionError("Shared atlas must retain exact RGBA pixels")
        self.image = image.copy()


class SpriteSequenceTests(unittest.TestCase):
    def setUp(self):
        self.file = SimpleNamespace(name="test.assets")
        self.texture = Texture()
        self.texture_reader = self.reader("Texture2D", 1, self.texture)
        self.rows = [self.sprite("Walk_0", 2, 0), self.sprite("Walk_1", 3, 4)]
        self.neighbour = self.sprite("Unrelated", 4, 8)
        self.env = SimpleNamespace(objects=[self.texture_reader, *[row[1] for row in self.rows], self.neighbour[1]])
        self.render_patch = patch.object(sprite_sequence, "_render_data", side_effect=lambda data: data.m_RD)
        self.render_patch.start()
        self.addCleanup(self.render_patch.stop)

    def reader(self, kind, identity, value):
        return SimpleNamespace(type=SimpleNamespace(name=kind), assets_file=self.file,
                               path_id=identity, read=lambda: value, peek_name=lambda: value.m_Name,
                               get_raw_data=lambda: str(identity).encode())

    def sprite(self, name, identity, x):
        pointer = SimpleNamespace(m_PathID=1, deref=lambda: self.texture_reader)
        data = SimpleNamespace(m_Name=name, m_Rect=rectangle(), m_RenderDataKey=(identity,),
                               m_RD=SimpleNamespace(texture=pointer, alphaTexture=None, settingsRaw=2,
                                                    textureRect=rectangle(x=x),
                                                    textureRectOffset=SimpleNamespace(x=0, y=0)))
        return name, self.reader("Sprite", identity, data), data

    def test_rectangular_frames_are_editable_through_normal_inspection(self):
        with patch.object(animation, "_load_bundle", return_value=self.env), \
                patch.object(animation, "_sequence", return_value=self.rows):
            info = animation.inspect_animation("fixture.bundle", "Walk")
        self.assertTrue(info["editable"])
        self.assertEqual(info["affected_textures"], ["shared_atlas"])
        self.assertEqual(info["frame_names"], ["Walk_0", "Walk_1"])

    def test_painting_preserves_neighbours_and_uses_bottom_origin_coordinates(self):
        layout = sprite_sequence.inspect_layout(self.env, self.rows)
        images = sprite_sequence.atlas_images(layout)
        unchanged = sprite_sequence.object_snapshot(self.env, images)
        frames = [Image.new("RGBA", (4, 4), color) for color in ("red", "blue")]
        sprite_sequence.write_frames(layout, images, frames)
        sprite_sequence.validate_write(self.env, self.rows, layout, images, unchanged)
        self.assertEqual(self.texture.image.getpixel((0, 8)), (255, 0, 0, 255))
        self.assertEqual(self.texture.image.getpixel((4, 8)), (0, 0, 255, 255))
        self.assertEqual(self.texture.image.getpixel((0, 0)), (20, 40, 60, 80))
        self.assertEqual(self.texture.image.getpixel((8, 8)), (20, 40, 60, 80))

    def test_restoring_one_group_preserves_other_edits_on_shared_atlas(self):
        original_layout = copy.deepcopy(sprite_sequence.inspect_layout(self.env, self.rows))
        layout = sprite_sequence.inspect_layout(self.env, self.rows)
        self.texture.image.paste("red", (0, 8, 4, 12))
        self.texture.image.paste("green", (8, 8, 12, 12))
        images = sprite_sequence.restore_frames(layout, original_layout)
        self.assertEqual(images[layout["frames"][0]["atlas"]].getpixel((0, 8)), (20, 40, 60, 80))
        self.assertEqual(self.texture.image.getpixel((8, 8)), (0, 128, 0, 255))

    def test_unverified_layouts_are_rejected_before_writing(self):
        for label, change, message in (
            ("rotated", lambda data: setattr(data.m_RD, "settingsRaw", 6), "旋转"),
            ("tight", lambda data: setattr(data.m_RD, "settingsRaw", 0), "网格"),
            ("trimmed", lambda data: setattr(data.m_RD.textureRectOffset, "x", 1), "裁剪"),
            ("alpha", lambda data: setattr(data.m_RD, "alphaTexture", SimpleNamespace(m_PathID=88)), "透明度"),
            ("fractional", lambda data: setattr(data.m_RD.textureRect, "x", 0.5), "非整数"),
            ("outside", lambda data: setattr(data.m_RD.textureRect, "x", 12), "越界"),
        ):
            with self.subTest(label=label):
                original = copy.deepcopy(self.rows[0][2])
                change(self.rows[0][2])
                with self.assertRaisesRegex(ValueError, message):
                    sprite_sequence.inspect_layout(self.env, self.rows)
                self.rows[0][2].__dict__.update(original.__dict__)
        self.texture.save.assert_not_called()

    def test_overlapping_target_and_unrelated_rectangles_are_rejected(self):
        self.rows[1][2].m_RD.textureRect.x = 2
        with self.assertRaisesRegex(ValueError, "重叠"):
            sprite_sequence.inspect_layout(self.env, self.rows)
        self.rows[1][2].m_RD.textureRect.x = 4
        self.neighbour[2].m_RD.textureRect.x = 6
        with self.assertRaisesRegex(ValueError, "共用"):
            sprite_sequence.inspect_layout(self.env, self.rows)

    def test_external_atlas_and_mipmaps_are_rejected(self):
        external = self.reader("Texture2D", 1, self.texture)
        external.assets_file = SimpleNamespace(name="external.assets")
        self.rows[0][2].m_RD.texture.deref = lambda: external
        with self.assertRaisesRegex(ValueError, "其他资源包"):
            sprite_sequence.inspect_layout(self.env, self.rows)
        self.rows[0][2].m_RD.texture.deref = lambda: self.texture_reader
        self.texture.m_MipCount = 2
        with self.assertRaisesRegex(ValueError, "多级纹理"):
            sprite_sequence.inspect_layout(self.env, self.rows)

    def test_roundtrip_checks_pixel_and_unchanged_object_integrity(self):
        layout = sprite_sequence.inspect_layout(self.env, self.rows)
        images = sprite_sequence.atlas_images(layout)
        unchanged = sprite_sequence.object_snapshot(self.env, images)
        self.texture.image.putpixel((11, 11), (1, 2, 3, 4))
        with self.assertRaisesRegex(ValueError, "像素"):
            sprite_sequence.validate_write(self.env, self.rows, layout, images, unchanged)
        self.texture.image = images[next(iter(images))].copy()
        self.neighbour[1].get_raw_data = lambda: b"changed"
        with self.assertRaisesRegex(ValueError, "其它对象"):
            sprite_sequence.validate_write(self.env, self.rows, layout, images, unchanged)

    def test_normal_sequence_replacement_and_restore_routes_use_atlas_checks(self):
        with TemporaryDirectory(prefix="jixing-sprite-check-") as folder:
            source = Path(folder) / "source.gif"
            frames = [Image.new("RGBA", (4, 4), color) for color in ("red", "blue")]
            frames[0].save(source, save_all=True, append_images=frames[1:], duration=100, loop=0)
            def save(env, output, validate):
                validate(env)
            with patch.object(animation, "_load_bundle", return_value=self.env), \
                    patch.object(animation, "_sequence", return_value=self.rows), \
                    patch.object(animation, "_atomic_save", side_effect=save):
                preview = animation.read_adapted_replacement("input.bundle", "Walk", source)
                self.assertEqual(len(preview["frames"]), 2)
                result = animation.replace_bundle_sequence("input.bundle", "Walk", source, "output.bundle")
                self.assertEqual(result["frame_count"], 2)
                self.assertEqual(self.texture.image.getpixel((4, 8)), (0, 0, 255, 255))


if __name__ == "__main__":
    unittest.main()
