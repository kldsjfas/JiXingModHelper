"""动画编解码/事务校验；传 --game-bundles 路径可额外验证真实包副本。"""
from __future__ import annotations

import argparse
import base64
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from astral_party_auto.modkit import animation
from astral_party_auto.modkit.dynamic import text_asset_bytes


def make_animation(format="GIF", *, default_image=False) -> bytes:
    first = Image.new("RGBA", (16, 12), "red")
    following = [Image.new("RGBA", (16, 12), "blue")]
    if default_image:
        following.append(Image.new("RGBA", (16, 12), "green"))
    buffer = io.BytesIO()
    first.save(buffer, format=format, save_all=True, append_images=following,
               duration=[100, 300], loop=0, default_image=default_image)
    return buffer.getvalue()


class AnimationChecks(unittest.TestCase):
    def test_binary_surrogateescape_roundtrip(self):
        raw = bytes(range(256))
        data = SimpleNamespace(m_Script=raw.decode("utf-8", "surrogateescape"))
        self.assertEqual(text_asset_bytes(data), raw)

    def test_gif_duration_and_composited_frames(self):
        frames, durations, kind = animation._decode_animation(make_animation())
        self.assertEqual(kind, "gif")
        self.assertEqual(durations, [100, 300])
        self.assertEqual(frames[0].getpixel((0, 0)), (255, 0, 0, 255))
        self.assertEqual(frames[1].getpixel((0, 0)), (0, 0, 255, 255))

    def test_apng_poster_is_not_an_animation_frame(self):
        frames, durations, kind = animation._decode_animation(make_animation("PNG", default_image=True))
        self.assertEqual(kind, "apng")
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[0].getpixel((0, 0)), (0, 0, 255, 255))
        self.assertEqual(durations, [100, 300])

    def test_animated_webp_is_readable(self):
        frames, durations, kind = animation._decode_animation(make_animation("WEBP"))
        self.assertEqual(kind, "webp")
        self.assertEqual(len(frames), 2)
        self.assertEqual(durations, [100, 300])

    def test_static_image_rejected(self):
        image = Image.new("RGBA", (8, 8), "red")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        with self.assertRaisesRegex(ValueError, "没有多帧"):
            animation._decode_animation(buffer.getvalue())

    def test_source_limits_reject_before_allocating_all_frames(self):
        with patch.object(animation, "MAX_TOTAL_PIXELS", 250):
            with self.assertRaisesRegex(ValueError, "总像素"):
                animation._decode_animation(make_animation())

    def test_frame_folder_natural_order_and_fps(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            Image.new("RGBA", (8, 8), "blue").save(root / "frame10.png")
            Image.new("RGBA", (8, 8), "red").save(root / "frame2.png")
            frames, durations, kind = animation._read_replacement(root, 20)
            self.assertEqual(frames[0].getpixel((0, 0)), (255, 0, 0, 255))
            self.assertEqual(durations, [50, 50])
            self.assertEqual(kind, "sequence")
            preview = animation.read_replacement_animation(root, fps=20)
            self.assertEqual(preview["fps"], 20)

    def test_preview_sampling_preserves_duration(self):
        durations = [20 + index for index in range(300)]
        indices, merged = animation._preview_indices(len(durations), durations)
        self.assertEqual(len(indices), 120)
        self.assertEqual(sum(merged), sum(durations))
        self.assertEqual(indices[0], 0)

    def test_replacement_uses_time_instead_of_frame_numbers(self):
        self.assertEqual(animation._resample_indices([100, 300], 10), [0, 0, 1, 1, 1, 1, 1, 1, 1, 1])

    def test_failed_validation_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "draft.bundle"
            target.write_bytes(b"old draft")
            env = SimpleNamespace(file=SimpleNamespace(save=lambda: b"new draft"))
            def reject(_):
                raise ValueError("validation failed")
            with patch.object(animation.UnityPy, "load", return_value=None):
                with self.assertRaisesRegex(ValueError, "validation failed"):
                    animation._atomic_save(env, target, reject)
            self.assertEqual(target.read_bytes(), b"old draft")
            self.assertEqual(len(list(Path(folder).iterdir())), 1)


def check_real_bundle_copies(root: Path) -> dict:
    """读官方包，所有写入仅发生在 TemporaryDirectory。"""
    source = root / "013ae6e8eb4823d799bda4799ca36756.bundle"
    sprite_source = root / "014384d605bf10b9bad39dc24ab08425.bundle"
    if not source.is_file() or not sprite_source.is_file():
        raise RuntimeError("这个安装版本未找到测试样本包，请重新选取真实资源验证。")
    original = animation._load_bundle(source)
    original_raw = {obj.path_id: obj.get_raw_data() for obj in original.objects}
    target_rows = animation._sequence(original, "lianxutu_blj")
    edited_ids = {obj.path_id for _, obj, _ in target_rows}
    original_images = {name: data.image.convert("RGBA") for name, _, data in target_rows}
    with tempfile.TemporaryDirectory(prefix="jixing-animation-check-") as folder:
        folder = Path(folder)
        replacement = folder / "input.gif"
        replacement.write_bytes(make_animation())
        draft = folder / "sample.bundle"
        adapted = animation.read_adapted_replacement(source, "lianxutu_blj", replacement, fps=24)
        assert adapted["total"] == 10 and abs(adapted["fps"] - 24) < 0.001
        for index, encoded in enumerate(adapted["frames"]):
            with Image.open(io.BytesIO(base64.b64decode(encoded.split(",", 1)[1]))) as frame:
                expected_preview = original_images[adapted["names"][index]].copy()
                expected_preview.thumbnail((animation.MAX_PREVIEW_EDGE, animation.MAX_PREVIEW_EDGE))
                assert frame.size == expected_preview.size
                assert frame.convert("RGB").getpixel((0, 0)) == ((255, 0, 0) if index < 2 else (0, 0, 255))
        result = animation.replace_bundle_sequence(source, "lianxutu_blj", replacement, draft)
        assert result["frame_count"] == 10
        written = animation._load_bundle(draft)
        rows = animation._sequence(written, "lianxutu_blj")
        for index, (name, _, data) in enumerate(rows):
            assert data.image.size == original_images[name].size
            pixel = data.image.convert("RGB").getpixel((0, 0))
            expected = (255, 0, 0) if index < 2 else (0, 0, 255)
            assert max(abs(a - b) for a, b in zip(pixel, expected)) <= 8, (index, pixel, expected)
        assert all(obj.get_raw_data() == original_raw[obj.path_id] for obj in written.objects if obj.path_id not in edited_ids)
        preview = animation.read_animation_preview(draft, "lianxutu_blj", fps=24)
        assert len(preview["frames"]) == 10 and abs(preview["fps"] - 24) < 0.001
        before_failure = draft.read_bytes()
        bad = folder / "invalid.gif"
        bad.write_bytes(b"invalid")
        try:
            animation.replace_bundle_sequence(draft, "lianxutu_blj", bad, draft)
        except (ValueError, OSError):
            pass
        else:
            raise AssertionError("Invalid animation was accepted")
        assert draft.read_bytes() == before_failure
        animation.restore_bundle_animation(draft, source, "lianxutu_blj")
        restored = animation._load_bundle(draft)
        for name, _, data in animation._sequence(restored, "lianxutu_blj"):
            assert data.image.convert("RGBA").tobytes() == original_images[name].tobytes(), name
        assert all(obj.get_raw_data() == original_raw[obj.path_id] for obj in restored.objects if obj.path_id not in edited_ids)
        sprite_info = animation.inspect_animation(sprite_source, "Walk-back")
        assert not sprite_info["editable"]
        sprite_preview = animation.read_animation_preview(sprite_source, "Walk-back")
        assert len(sprite_preview["frames"]) == 30
        # Reuse a real TextAsset object to test binary container serialization.
        container = animation._load_bundle(source)
        text_objects = [obj for obj in container.objects if obj.type.name == "TextAsset"]
        if not text_objects:
            # This bundle contains Texture2D resources only; use another real
            # package with a TextAsset supplied by the local installation.
            for candidate in root.glob("*.bundle"):
                container = animation._load_bundle(candidate)
                text_objects = [obj for obj in container.objects if obj.type.name == "TextAsset"]
                if text_objects:
                    break
        data = text_objects[0].read()
        asset_name = data.m_Name
        animation._set_text_bytes(data, make_animation("WEBP"))
        text_draft = folder / "container.bundle"
        animation._atomic_save(container, text_draft, lambda _: None)
        wrong_format_before = text_draft.read_bytes()
        try:
            animation.replace_bundle_animated_image(text_draft, asset_name, replacement, text_draft)
        except ValueError:
            pass
        else:
            raise AssertionError("Cross-format replacement was accepted")
        assert text_draft.read_bytes() == wrong_format_before
        webp = folder / "input.webp"
        webp.write_bytes(make_animation("WEBP"))
        animation.replace_bundle_animated_image(text_draft, asset_name, webp, text_draft)
        saved = animation._load_bundle(text_draft)
        assert text_asset_bytes(animation._text_asset(saved, asset_name)[1]) == webp.read_bytes()
    return {"texture_frames": 10, "sprite_preview_frames": 30, "unrelated_objects_preserved": True,
            "failed_import_keeps_draft": True, "binary_container_roundtrip": True, "restore_pixels_exact": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-bundles", type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AnimationChecks))
    if not result.wasSuccessful():
        raise SystemExit(1)
    if args.game_bundles:
        print(check_real_bundle_copies(args.game_bundles))
