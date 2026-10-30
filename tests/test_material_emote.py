"""序列图表情的时间采样、透明留边和独立资源边界。"""
from __future__ import annotations

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

from astral_party_auto.modkit import material_emote as material


def sequence_tree():
    return {"m_SavedProperties": {
        "m_Colors": [("_Sequence", {"r": 2.0, "g": 3.0, "b": 5.0, "a": 5.0})],
        "m_Floats": [("_FramePaddingPixels", 1.0)]}}


class MaterialEmoteTests(unittest.TestCase):
    def test_sequence_vector_uses_rows_then_columns_and_excludes_padding(self):
        profile = material._sequence_layout(sequence_tree(), 36, 20)
        self.assertEqual((profile["rows"], profile["columns"]), (2, 3))
        self.assertEqual((profile["width"], profile["height"]), (10, 8))
        self.assertEqual((profile["frames"], profile["fps"], profile["duration_ms"]), (5, 5, 1000))

    def test_invalid_sequence_cannot_escape_atlas_or_duration_limits(self):
        cases = [("r", 1.5), ("g", 0), ("a", 7), ("b", float("nan")), ("b", 0),
                 ("b", 0.01), ("a", 1), ("g", 129)]
        for key, value in cases:
            tree = sequence_tree()
            tree["m_SavedProperties"]["m_Colors"][0][1][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                material._sequence_layout(tree, 36, 20)
        with self.assertRaises(ValueError):
            material._sequence_layout(sequence_tree(), 35, 20)
        with self.assertRaises(ValueError):
            material._sequence_layout(sequence_tree(), 8192, 8192)

    def test_output_plan_keeps_duration_and_bounds_tall_wide_and_long_sources(self):
        for size in ((160, 160), (1, 4096), (4096, 1), (4096, 4096), (320, 180)):
            for duration in (100, 1000, 30_000):
                plan = material._output_plan(*size, duration)
                with self.subTest(size=size, duration=duration):
                    self.assertLessEqual(max(plan["atlas_width"], plan["atlas_height"]), 4096)
                    self.assertLessEqual(plan["atlas_width"] * plan["atlas_height"], material.MAX_ATLAS_PIXELS)
                    self.assertLessEqual(plan["frames"], 120)
                    self.assertAlmostEqual(plan["frames"] * 1000 / plan["fps"], duration)
                    self.assertLessEqual(plan["frames"], plan["rows"] * plan["columns"])

    def test_invalid_output_duration_and_size_are_rejected(self):
        for duration in (0, -1, float("nan"), float("inf"), 30_001, 1):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                material._output_plan(160, 160, duration)
        for size in ((0, 160), (160, -1), (True, 160), (4097, 160)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                material._output_plan(*size, 1000)

    def test_variable_duration_resampling_follows_timeline(self):
        red, blue = Image.new("RGBA", (2, 2), "red"), Image.new("RGBA", (2, 2), "blue")
        plan = {"frames": 10, "width": 2, "height": 2}
        output = material._resample([red, blue], [100, 900], plan)
        self.assertEqual([frame.getpixel((0, 0)) for frame in output],
                         [(255, 0, 0, 255)] + [(0, 0, 255, 255)] * 9)
        self.assertEqual(red.size, (2, 2))

    def test_resampling_preserves_alpha_and_adds_transparent_aspect_padding(self):
        source = Image.new("RGBA", (4, 2), (255, 0, 0, 128))
        output = material._resample([source, source], [100, 100], {"frames": 2, "width": 4, "height": 4})
        self.assertEqual(output[0].getpixel((2, 0)), (0, 0, 0, 0))
        self.assertEqual(output[0].getpixel((2, 1)), (255, 0, 0, 128))
        self.assertEqual(output[0].getpixel((2, 3)), (0, 0, 0, 0))

    def test_tile_positions_start_at_top_left_and_keep_last_frame(self):
        colors = [(255, 0, 0, 255), (0, 255, 0, 128), (0, 0, 255, 0),
                  (100, 100, 0, 255), (100, 0, 100, 255)]
        plan = {"atlas_width": 12, "atlas_height": 8, "columns": 3, "rows": 2,
                "width": 2, "height": 2, "padding": 1, "frames": 5}
        atlas = material._sprite_sheet([Image.new("RGBA", (2, 2), color) for color in colors], plan)
        self.assertEqual(atlas.getpixel((1, 1)), colors[0])
        self.assertEqual(atlas.getpixel((5, 1)), colors[1])
        self.assertEqual(atlas.getpixel((1, 5)), colors[3])
        self.assertEqual(atlas.getpixel((5, 5)), colors[4])
        self.assertEqual(atlas.getpixel((9, 5)), (0, 0, 0, 0))
        self.assertEqual([frame.getpixel((0, 0)) for frame in material._frames_from_atlas(atlas, plan)], colors)

    def test_invalid_timing_does_not_reuse_frames(self):
        frames = [Image.new("RGBA", (2, 2)) for _ in range(2)]
        for durations in ([0, 1], [float("nan"), 1], [1], [-2, 1]):
            with self.subTest(durations=durations), self.assertRaises(ValueError):
                material._resample(frames, durations, {"frames": 2, "width": 2, "height": 2})

    def test_unknown_shader_is_rejected_even_if_named_like_original(self):
        shader = Mock()
        shader.type.name = "Shader"
        shader.get_raw_data.return_value = b"unverified shader"
        with self.assertRaisesRegex(ValueError, "Shader.*验证"):
            material._shader_profile(shader)

    def test_existing_output_is_preserved_before_reading_source(self):
        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "jixing_emote_120101008.bundle"
            destination.write_bytes(b"existing independent work")
            with patch.object(material, "_checked_environment") as load:
                with self.assertRaises(ValueError):
                    material.prepare_material_emote("missing", "missing", "VEmoji_104_11", destination,
                        emote_id=120101008, work_dir=temporary)
            load.assert_not_called()
            self.assertEqual(destination.read_bytes(), b"existing independent work")

    def test_sequence_properties_are_never_edited_during_inspection(self):
        tree = sequence_tree()
        baseline = copy.deepcopy(tree)
        material._sequence_layout(tree, 36, 20)
        self.assertEqual(tree, baseline)


if __name__ == "__main__":
    unittest.main()
