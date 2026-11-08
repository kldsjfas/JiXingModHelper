"""Current Unity discrete Sprite curves must play at their real key times."""
from __future__ import annotations

import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from astral_party_auto.modkit import clip_preview
from selfcheck_clip_preview import _stream, _tree


def discrete_tree():
    tree = _tree()
    tree["m_MuscleClip"]["m_LoopTime"] = True
    stream = tree["m_MuscleClip"]["m_Clip"]["data"]["m_StreamedClip"]
    stream.update(curveCount=0, discreteCurveCount=1)
    return tree


def sprite(name, color):
    return SimpleNamespace(
        m_Name=name, m_PixelsToUnits=100, m_SpriteAtlas=None, m_AtlasTags=[],
        m_Rect=SimpleNamespace(width=16, height=12), m_Pivot=SimpleNamespace(x=0.5, y=0.5),
        m_RD=SimpleNamespace(settingsRaw=2, textureRect=SimpleNamespace(width=16, height=12),
                             textureRectOffset=SimpleNamespace(x=0, y=0)),
        image=Image.new("RGBA", (16, 12), color),
    )


class DiscreteClipPreviewTests(unittest.TestCase):
    def inspect_named_clip(self, names, clip_name):
        sprites = {identity: sprite(name, "red") for identity, name in zip((101, 202), names)}
        readers = {identity: SimpleNamespace(type=SimpleNamespace(name="Sprite"), read=lambda value=value: value)
                   for identity, value in sprites.items()}
        clip = SimpleNamespace(read_typetree=discrete_tree, assets_file=object())
        def pointer(**values):
            return SimpleNamespace(deref=lambda: readers[values["m_PathID"]])
        with patch.object(clip_preview, "_load_bundle", return_value=object()), \
                patch.object(clip_preview, "_clip", return_value=clip), \
                patch.object(clip_preview, "PPtr", side_effect=pointer):
            return clip_preview.inspect_clip_preview("fixture.bundle", clip_name)

    def test_current_discrete_curve_matches_legacy_curve_order_and_timing(self):
        expected = clip_preview._timeline(_tree())
        self.assertEqual(clip_preview._timeline(discrete_tree()), expected)

    def test_holding_last_sprite_preserves_clip_duration(self):
        tree = discrete_tree()
        tree["m_MuscleClip"]["m_StopTime"] = 3.5
        stream = tree["m_MuscleClip"]["m_Clip"]["data"]["m_StreamedClip"]
        stream.update(_stream([(-3.4e38, 0), (0.1, 1)]), curveCount=0, discreteCurveCount=1)
        pointers, durations, duration = clip_preview._timeline(tree)
        self.assertEqual([row["m_PathID"] for row in pointers], [101, 202])
        self.assertAlmostEqual(durations[0], 100, places=5)
        self.assertAlmostEqual(durations[1], 3400, places=5)
        self.assertEqual(sum(durations), 3500)
        self.assertEqual(duration, 3.5)

    def test_mixed_or_invalid_counts_do_not_enable_unknown_tracks(self):
        for continuous, discrete in [(1, 1), (0, 2), (0, -1), (0, True), (0, "1")]:
            with self.subTest(continuous=continuous, discrete=discrete):
                tree = discrete_tree()
                stream = tree["m_MuscleClip"]["m_Clip"]["data"]["m_StreamedClip"]
                stream.update(curveCount=continuous, discreteCurveCount=discrete)
                with self.assertRaises(ValueError):
                    clip_preview._timeline(tree)
        tree = discrete_tree()
        tree["m_PositionCurves"] = [{"path": "root"}]
        with self.assertRaisesRegex(ValueError, "游戏运行时"):
            clip_preview._timeline(tree)

    def test_discrete_stream_still_rejects_broken_or_unmapped_keys(self):
        for values in [[(0, 0.25)], [(0, 9)], [(0.5, 1)], [(0.5, 1), (0.25, 0)]]:
            with self.subTest(values=values):
                tree = discrete_tree()
                stream = tree["m_MuscleClip"]["m_Clip"]["data"]["m_StreamedClip"]
                stream.update(_stream(values), curveCount=0, discreteCurveCount=1)
                with self.assertRaises(ValueError):
                    clip_preview._timeline(tree)

    def test_normal_inspection_and_preview_render_distinct_transparent_frames(self):
        sprites = {101: sprite("Walk_00000", (255, 0, 0, 160)),
                   202: sprite("Walk_00001", (0, 0, 255, 220))}
        readers = {identity: SimpleNamespace(type=SimpleNamespace(name="Sprite"), read=lambda value=value: value)
                   for identity, value in sprites.items()}
        clip = SimpleNamespace(read_typetree=discrete_tree, assets_file=object())
        def pointer(**values):
            return SimpleNamespace(deref=lambda: readers[values["m_PathID"]])
        with patch.object(clip_preview, "_load_bundle", return_value=object()), \
                patch.object(clip_preview, "_clip", return_value=clip), \
                patch.object(clip_preview, "PPtr", side_effect=pointer):
            info = clip_preview.inspect_clip_preview("fixture.bundle", "Walk")
            preview = clip_preview.read_clip_preview("fixture.bundle", "Walk")
        self.assertTrue(info["playable"])
        self.assertTrue(info["loop"])
        self.assertEqual(info["sample_rate"], 30)
        self.assertEqual(preview["names"], ["Walk_00000", "Walk_00001"])
        self.assertEqual(preview["durations"], [250, 750])
        self.assertEqual(len(set(preview["frames"])), 2)
        with Image.open(io.BytesIO(base64.b64decode(preview["frames"][0].split(",", 1)[1]))) as image:
            self.assertEqual(image.size, (16, 12))
            self.assertEqual(image.getpixel((0, 0)), (255, 0, 0, 160))

    def test_full_frame_iterator_keeps_origin_blanks_and_every_frame_beyond_preview_limit(self):
        frame = clip_preview.SpriteFrame("sprite", sprite("sprite", "red"), -0.02, 0, 0.02, 0.04, 100)
        frames = [frame, None] * 75
        info = {"width": 8, "height": 8}
        rendered = clip_preview.iter_clip_canvases(frames, info, (100, -4, 6))
        count = 0
        for index, image in enumerate(rendered):
            self.assertEqual(image.size, (8, 8))
            if index % 2:
                self.assertIsNone(image.getbbox())
            else:
                self.assertEqual(image.getbbox(), (2, 2, 6, 6))
            count += 1
        self.assertEqual(count, 150)

    def test_runtime_action_uses_unique_sprite_prefix_instead_of_clip_name(self):
        for names, expected in [
            (["Fight-effect_00000", "Fight-effect_00001"], "Fight-effect"),
            (["Walk-back_00000", "Walk-back_00001"], "Walk-back"),
            (["Walk_00000", ""], "Walk"),
        ]:
            with self.subTest(names=names):
                info = self.inspect_named_clip(names, "Fight-attack-effect")
                self.assertTrue(info["playable"])
                self.assertEqual(info["runtime_action"], expected)
                self.assertEqual(info["runtime_binding_reason"], "")
                self.assertEqual(info["frame_names"], names)

    def test_uncertain_runtime_prefix_keeps_playback_but_never_guesses_binding(self):
        for names in [
            ["Walk_00000", "Fight_00001"], ["", ""],
            ["动画_00000", "动画_00001"], ["1Walk_00000", "1Walk_00001"],
            ["_00000", "_00001"],
        ]:
            with self.subTest(names=names):
                info = self.inspect_named_clip(names, "Idle")
                self.assertTrue(info["playable"])
                self.assertEqual(info["total"], 2)
                self.assertIsNone(info["runtime_action"])
                self.assertTrue(info["runtime_binding_reason"])


if __name__ == "__main__":
    unittest.main()
