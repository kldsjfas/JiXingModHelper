"""静态源图的色彩、透明度和高位深像素应与归一预览一致。"""
from __future__ import annotations

import math
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image, ImageCms

from astral_party_auto.modkit.maker import read_static_image


def linear_rgb_profile() -> bytes:
    """用 LCMS 自己生成的 sRGB 原色和线性曲线构造独立测试配置。"""
    profile = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    count = struct.unpack_from(">I", profile, 128)[0]
    for index in range(count):
        tag, offset, length = struct.unpack_from(">4sII", profile, 132 + index * 12)
        if tag in {b"rTRC", b"gTRC", b"bTRC"}:
            profile[offset:offset + length] = (
                b"para" + bytes(8) + struct.pack(">i", 65536) + bytes(length - 16)
            )
    return bytes(profile)


def linear_channel_to_srgb(value: int) -> int:
    channel = value / 255
    encoded = channel * 12.92 if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055
    return math.floor(encoded * 255 + 0.5)


class StaticImageColorTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="jixing-static-color-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = linear_rgb_profile()

    def assert_color_near(self, actual, expected):
        self.assertEqual(len(actual), len(expected))
        for actual_channel, expected_channel in zip(actual, expected):
            self.assertLessEqual(abs(actual_channel - expected_channel), 1)

    def test_valid_rgb_profile_is_converted_for_png_jpeg_and_webp(self):
        for suffix, options in (("png", {}), ("jpg", {"quality": 100, "subsampling": 0}),
                                ("webp", {"lossless": True})):
            with self.subTest(suffix=suffix):
                path = self.root / f"linear.{suffix}"
                with Image.new("RGB", (8, 6), (64, 128, 192)) as image:
                    image.save(path, icc_profile=self.profile, **options)
                with Image.open(path) as encoded:
                    source_pixel = encoded.getpixel((4, 3))
                expected = tuple(linear_channel_to_srgb(value) for value in source_pixel) + (255,)
                with read_static_image(path) as image:
                    self.assert_color_near(image.getpixel((4, 3)), expected)
                    self.assertNotIn("icc_profile", image.info)
                    self.assertGreater(image.getpixel((4, 3))[0], source_pixel[0] + 50)

    def test_color_conversion_keeps_every_alpha_value_and_hidden_rgb(self):
        for suffix, options in (("png", {}), ("webp", {"lossless": True, "exact": True})):
            with self.subTest(suffix=suffix):
                path = self.root / f"alpha.{suffix}"
                pixels = [(64, 128, 192, alpha) for alpha in (0, 1, 127, 255)]
                with Image.new("RGBA", (4, 1)) as image:
                    image.putdata(pixels)
                    image.save(path, icc_profile=self.profile, **options)
                expected_rgb = tuple(linear_channel_to_srgb(value) for value in (64, 128, 192))
                with read_static_image(path) as image:
                    for index, (_, _, _, alpha) in enumerate(pixels):
                        result = image.getpixel((index, 0))
                        self.assert_color_near(result[:3], expected_rgb)
                        self.assertEqual(result[3], alpha)

    def test_palette_color_profile_and_transparency_are_both_applied(self):
        path = self.root / "palette.png"
        with Image.new("P", (2, 1)) as image:
            image.putpalette([64, 128, 192, 128, 64, 32] + [0] * 762)
            image.putdata([0, 1])
            image.save(path, transparency=bytes([0, 127]), icc_profile=self.profile)
        with read_static_image(path) as image:
            for index, rgb in enumerate(((64, 128, 192), (128, 64, 32))):
                self.assert_color_near(image.getpixel((index, 0))[:3],
                                       tuple(linear_channel_to_srgb(value) for value in rgb))
            self.assertEqual([image.getpixel((index, 0))[3] for index in range(2)], [0, 127])

    def test_no_profile_preserves_ordinary_rgba_pixels_exactly(self):
        path = self.root / "ordinary.png"
        pixels = [(1, 2, 3, 0), (45, 67, 89, 1), (250, 130, 7, 127), (0, 255, 123, 255)]
        with Image.new("RGBA", (4, 1)) as image:
            image.putdata(pixels)
            image.save(path)
        with read_static_image(path) as image:
            self.assertEqual([image.getpixel((index, 0)) for index in range(4)], pixels)

    def test_broken_or_incompatible_profile_uses_plain_pixel_fallback(self):
        lab = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
        for index, profile in enumerate((b"not an ICC profile", lab)):
            with self.subTest(profile=index):
                path = self.root / f"unsupported-{index}.png"
                with Image.new("RGBA", (2, 2), (64, 128, 192, 87)) as image:
                    image.save(path, icc_profile=profile)
                with read_static_image(path) as image:
                    self.assertEqual(image.getpixel((0, 0)), (64, 128, 192, 87))
                    self.assertNotIn("icc_profile", image.info)

    def test_cmyk_without_compatible_profile_keeps_pillow_fallback(self):
        for index, profile in enumerate((None, self.profile)):
            with self.subTest(profile=index):
                path = self.root / f"cmyk-{index}.jpg"
                options = {"icc_profile": profile} if profile is not None else {}
                with Image.new("CMYK", (4, 3), (20, 130, 200, 30)) as image:
                    image.save(path, quality=100, **options)
                with Image.open(path) as source, source.convert("RGBA") as fallback:
                    expected = fallback.getpixel((0, 0))
                with read_static_image(path) as image:
                    self.assertEqual(image.getpixel((0, 0)), expected)
                    self.assertNotIn("icc_profile", image.info)

    def test_sixteen_bit_grayscale_uses_high_eight_bits_instead_of_clipping(self):
        values = [0, 1, 127, 128, 255, 256, 257, 32767, 32768, 65534, 65535]
        path = self.root / "grayscale16.png"
        with Image.new("I;16", (len(values), 1)) as image:
            image.putdata(values)
            image.save(path)
        with read_static_image(path) as image:
            self.assertEqual([image.getpixel((index, 0)) for index in range(len(values))],
                             [(value >> 8,) * 3 + (255,) for value in values])

    def test_normalized_pixels_do_not_apply_color_or_rotation_twice(self):
        path = self.root / "rotated.png"
        exif = Image.Exif()
        exif[274] = 6
        with Image.new("RGBA", (8, 4), (64, 128, 192, 73)) as image:
            image.save(path, exif=exif, icc_profile=self.profile)
        normalized_path = self.root / "normalized.png"
        with read_static_image(path) as image:
            expected_pixel = image.getpixel((0, 0))
            self.assertEqual(image.size, (4, 8))
            self.assertIsNone(image.getexif().get(274))
            image.save(normalized_path)
        with read_static_image(normalized_path) as image:
            self.assertEqual(image.size, (4, 8))
            self.assertEqual(image.getpixel((0, 0)), expected_pixel)


if __name__ == "__main__":
    unittest.main()
