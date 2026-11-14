from __future__ import annotations

import copy
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

from astral_party_auto.modkit.visual_animation import VisualAnimationWorkspace, _clear_clip_pixels, animation_identity, fit_animation_frames, pack_frames, plan_card_video, sample_frames
from astral_party_auto.web_app import DesktopApi
from test_fairygui_movieclip import package_fixture


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

    def test_large_card_is_scaled_to_fit_without_covering_official_or_other_frames(self):
        reserved = [[1921, 1, 512, 512]]
        size, placed = fit_animation_frames((1920, 1920), reserved, (8192, 4096), 12)
        self.assertLess(size[0], 8192)
        self.assertAlmostEqual(size[0] / size[1], 2, delta=0.01)
        self.assertGreaterEqual(len(placed), 2)
        for rect in placed:
            self.assertEqual(tuple(rect[2:]), size)
            self.assertFalse(overlaps(rect, [0, 0, 1920, 1920]))
            self.assertTrue(all(not overlaps(rect, prior) for prior in reserved))

    def test_normal_card_keeps_original_resolution_and_static_input_is_rejected(self):
        size, placed = fit_animation_frames((1920, 1920), [], (404, 400), 12)
        self.assertEqual(size, (404, 400))
        self.assertEqual(len(placed), 12)
        with self.assertRaisesRegex(ValueError, "两帧"):
            fit_animation_frames((1920, 1920), [], (404, 400), 1)
        with self.assertRaisesRegex(ValueError, "没有足够空间"):
            fit_animation_frames((4096, 4096), [], (404, 400), 12)

    def test_large_video_card_is_planned_before_decode_within_atlas_and_pixel_budget(self):
        for source_size in ((1340, 1692), (3500, 2200)):
            with self.subTest(source_size=source_size):
                plan = plan_card_video((1920, 1920), [], source_size, 2000)
                self.assertEqual(plan["fps"], 12)
                self.assertLess(plan["width"] * plan["height"], source_size[0] * source_size[1])
                self.assertLessEqual(plan["width"] * plan["height"] * 24, 32_000_000)
                self.assertEqual(len(pack_frames((1920, 1920), [], (plan["width"], plan["height"]), 24)), 24)
                self.assertAlmostEqual(plan["width"] / plan["height"], source_size[0] / source_size[1], delta=0.01)

    def test_long_video_sampling_preserves_full_duration_and_never_exceeds_frame_limit(self):
        plan = plan_card_video((1920, 1920), [], (3500, 2200), 30000)
        self.assertEqual(plan["fps"], 4)
        self.assertEqual(120 * 1000 / plan["fps"], 30000)
        self.assertLessEqual(plan["width"] * plan["height"] * 120, 32_000_000)
        with self.assertRaisesRegex(ValueError, "太短"):
            plan_card_video((1920, 1920), [], (1340, 1692), 10)

    def test_short_video_still_obeys_single_frame_limit_with_an_almost_empty_atlas(self):
        plan = plan_card_video((512, 512), [], (3500, 2200), 100)
        self.assertLessEqual(plan["width"] * plan["height"], 4_194_304)
        self.assertEqual(round(100 * plan["fps"] / 1000), 2)

    def test_video_content_resolution_is_independent_of_target_texture_size(self):
        plans = []
        for reference_size in ((1120, 1740), (420, 684), (128, 308)):
            with self.subTest(reference_size=reference_size):
                plan = plan_card_video((1920, 1920), [], reference_size, 2000,
                                       video_size=(1920, 1080), display_aspect=16 / 9)
                plans.append(plan)
                self.assertGreater(plan["width"], 128)
                self.assertGreater(plan["height"], 72)
                self.assertAlmostEqual(plan["width"] / plan["height"], 16 / 9, delta=0.01)
                self.assertEqual(plan["fps"], 12)
                self.assertLessEqual(plan["width"] * plan["height"] * 24, 32_000_000)
                self.assertEqual(len(pack_frames((1920, 1920), [], (plan["width"], plan["height"]), 24)), 24)
        self.assertEqual(plans[0], plans[1])
        self.assertEqual(plans[1], plans[2])

    def test_video_planning_respects_display_aspect_and_small_source_resolution(self):
        for video_size, display_aspect in (((720, 576), 16 / 9), ((1920, 1080), 9 / 16)):
            with self.subTest(video_size=video_size, display_aspect=display_aspect):
                plan = plan_card_video((1920, 1920), [], (128, 308), 2000,
                                       video_size=video_size, display_aspect=display_aspect)
                self.assertAlmostEqual(plan["width"] / plan["height"], display_aspect, delta=0.01)
                self.assertLessEqual(plan["width"] * plan["height"] * 24, 32_000_000)
        small = plan_card_video((1920, 1920), [], (1120, 1740), 2000,
                                video_size=(96, 64), display_aspect=1.5)
        self.assertEqual((small["width"], small["height"]), (96, 64))

    def test_video_content_planning_keeps_duration_and_reserves_existing_frames(self):
        occupied = [[1921, 1, 512, 512]]
        for duration, expected_count, expected_fps in ((100, 2, 20), (2000, 24, 12), (30000, 120, 4)):
            with self.subTest(duration=duration):
                plan = plan_card_video((1920, 1920), occupied, (128, 308), duration,
                                       video_size=(1920, 1080), display_aspect=16 / 9)
                self.assertEqual(plan["fps"], expected_fps)
                self.assertEqual(round(duration * plan["fps"] / 1000), expected_count)
                self.assertLessEqual(plan["width"] * plan["height"], 4_194_304)
                self.assertLessEqual(plan["width"] * plan["height"] * expected_count, 32_000_000)
                placements = pack_frames((1920, 1920), occupied, (plan["width"], plan["height"]), expected_count)
                self.assertEqual(len(placements), expected_count)
                self.assertTrue(all(not overlaps(rect, occupied[0]) for rect in placements))
        with self.assertRaises(ValueError):
            plan_card_video((4096, 4096), [], (128, 308), 2000,
                            video_size=(1920, 1080), display_aspect=16 / 9)

    def test_video_content_planning_rejects_invalid_source_geometry(self):
        for video_size in ((0, 1080), (1920, 0), (-1, 1080)):
            with self.subTest(video_size=video_size), self.assertRaises(ValueError):
                plan_card_video((1920, 1920), [], (128, 308), 2000, video_size=video_size)
        for display_aspect in (0, -1, float("nan"), float("inf")):
            with self.subTest(display_aspect=display_aspect), self.assertRaises(ValueError):
                plan_card_video((1920, 1920), [], (128, 308), 2000,
                                video_size=(1920, 1080), display_aspect=display_aspect)


class VideoCanvasTests(unittest.TestCase):
    def test_video_canvas_preserves_card_aspect_and_centers_unscaled_content(self):
        from astral_party_auto.modkit.visual_animation import card_video_canvas

        frame_size = (512, 288)
        for reference_size in ((1120, 1740), (420, 684), (128, 308), (1740, 1120)):
            with self.subTest(reference_size=reference_size):
                canvas_size, rect = card_video_canvas(reference_size, frame_size)
                width, height = canvas_size
                left, top, content_width, content_height = rect
                self.assertEqual((content_width, content_height), frame_size)
                self.assertGreaterEqual(left, 0)
                self.assertGreaterEqual(top, 0)
                self.assertLessEqual(left + content_width, width)
                self.assertLessEqual(top + content_height, height)
                self.assertLessEqual(max(canvas_size), 4096)
                self.assertLessEqual(abs(2 * left + content_width - width), 1)
                self.assertLessEqual(abs(2 * top + content_height - height), 1)
                self.assertLessEqual(abs(width * reference_size[1] - height * reference_size[0]),
                                     max(reference_size))

    def test_matching_aspect_does_not_add_transparent_canvas_padding(self):
        from astral_party_auto.modkit.visual_animation import card_video_canvas

        size, rect = card_video_canvas((1920, 1080), (640, 360))
        self.assertEqual(tuple(size), (640, 360))
        self.assertEqual(tuple(rect), (0, 0, 640, 360))

    def test_transposing_reference_and_content_transposes_the_canvas(self):
        from astral_party_auto.modkit.visual_animation import card_video_canvas

        canvas, rect = card_video_canvas((404, 400), (1280, 720))
        transposed_canvas, transposed_rect = card_video_canvas((400, 404), (720, 1280))
        self.assertEqual(tuple(transposed_canvas), tuple(reversed(canvas)))
        self.assertEqual(tuple(transposed_rect), (rect[1], rect[0], rect[3], rect[2]))

    def test_invalid_or_unrepresentable_canvases_fail_without_scaling_content(self):
        from astral_party_auto.modkit.visual_animation import card_video_canvas

        for reference, frame in (((0, 308), (512, 288)), ((128, -1), (512, 288)),
                                 ((128, 308), (0, 288)), ((128, 308), (512, -1)),
                                 ((1, 4096), (4096, 1)), ((4096, 1), (1, 4096)),
                                 ((1, 1), (4097, 1))):
            with self.subTest(reference=reference, frame=frame), self.assertRaises(ValueError):
                card_video_canvas(reference, frame)
        canvas, rect = card_video_canvas((1, 1), (1, 1))
        self.assertEqual(tuple(canvas), (1, 1))
        self.assertEqual(tuple(rect), (0, 0, 1, 1))

    def test_preview_reads_content_at_its_offset_and_creates_canvases_lazily(self):
        from astral_party_auto.modkit import visual_animation
        from astral_party_auto.modkit.fairygui_movieclip import append_movieclip

        name = "UT_Hero_ThinCard_114"
        descriptor = append_movieclip(
            package_fixture(), item_id=animation_identity(name), atlas_id="atlas0", width=8, height=12,
            frames=[{"rect": [2, 3, 4, 5], "atlas_rect": [0, 32, 4, 5], "duration_ms": 100},
                    {"rect": [2, 3, 4, 5], "atlas_rect": [4, 32, 4, 5], "duration_ms": 200}],
            atlas_width=32, atlas_height=40)
        atlas = Image.new("RGBA", (32, 40))
        atlas.paste((220, 30, 40, 255), (0, 32, 4, 37))
        atlas.paste((20, 50, 230, 255), (4, 32, 8, 37))

        def inspect_frames(frames, names, durations, total, **_options):
            self.assertNotIsInstance(frames, (list, tuple))
            canvases = list(frames)
            self.assertEqual(len(canvases), 2)
            self.assertEqual(durations, [100, 200])
            self.assertEqual(total, 2)
            for frame, color in zip(canvases, ((220, 30, 40, 255), (20, 50, 230, 255))):
                self.assertEqual(frame.size, (8, 12))
                self.assertEqual(frame.getchannel("A").getbbox(), (2, 3, 6, 8))
                self.assertEqual(frame.getpixel((2, 3)), color)
                self.assertEqual(frame.getpixel((0, 0)), (0, 0, 0, 0))
            return {"checked": True}

        with patch.object(visual_animation, "read_descriptor", return_value=descriptor), \
                patch.object(visual_animation, "read_atlas", return_value=atlas), \
                patch.object(visual_animation, "_preview", side_effect=inspect_frames):
            self.assertEqual(VisualAnimationWorkspace(Mock()).preview(name, Path("ui.bundle"), Path("atlas.bundle")),
                             {"checked": True})


class VisualVideoPreparationTests(unittest.TestCase):
    def test_prepare_separates_video_pixels_from_canvas_and_preserves_image_import(self):
        from astral_party_auto.modkit import emote_media, visual_animation, visual_config
        from astral_party_auto.modkit.fairygui_movieclip import inspect_movieclip_package
        from test_visual_config import NAME, fixture

        for kind in ("video", "image"):
            with self.subTest(kind=kind), TemporaryDirectory(prefix="jixing-video-canvas-") as folder:
                root = Path(folder)
                originals = root / "originals"
                originals.mkdir()
                paths = {name: originals / name for name in ("config.bundle", "ui.bundle", "atlas.bundle", "image.bundle")}
                paths["config.bundle"].write_bytes(fixture("Skin"))
                paths["ui.bundle"].write_bytes(package_fixture().replace(b"test1234", b"xuaw6o8j", 1))
                paths["image.bundle"].write_bytes(b"selected original image")
                Image.new("RGBA", (32, 32), (50, 70, 90, 255)).save(paths["atlas.bundle"], format="PNG")
                original_bytes = {name: path.read_bytes() for name, path in paths.items()}
                source = root / "input.media"
                source.write_bytes(b"isolated source fixture")
                draft = root / "draft"
                draft.mkdir()
                candidate_folder = root / "candidate"
                candidate_folder.mkdir()
                selection = {"asset_type": "texture", "bundle": "image.bundle", "name": NAME,
                             "width": 40, "height": 64}
                controller = Mock(selection=selection, draft_items=[])
                controller.original_bundle_path.side_effect = paths.get
                controller._draft_dir.return_value = draft

                def commit(staged, items):
                    for name, path in staged.items():
                        (draft / name).write_bytes(path.read_bytes())
                    controller.draft_items = items

                controller.emote_workspace._commit.side_effect = commit
                workspace = VisualAnimationWorkspace(controller)
                context = {"config_bundle": "config.bundle", "ui_bundle": "ui.bundle", "atlas_bundle": "atlas.bundle",
                           "base_size": (32, 32), "package_id": "xuaw6o8j",
                           "originals": {name: paths[name] for name in ("config.bundle", "ui.bundle", "atlas.bundle")}}
                workspace.context = Mock(return_value=context)
                workspace._selection = Mock(return_value=(selection, {"tables": ["Skin"]}))
                frames = [Image.new("RGBA", (16, 9), color) for color in ((220, 30, 40, 255), (20, 50, 230, 255))]
                video_info = {"width": 16, "height": 9, "fps": 1, "source_width": 1920,
                              "source_height": 1080, "source_duration_ms": 2000}

                def read_atlas(path):
                    with Image.open(path) as image:
                        return image.convert("RGBA")

                def replace_config(source_path, output_path, table, old, new):
                    changed = visual_config.replace_visual_address(source_path.read_bytes(), table, old, new)
                    output_path.write_bytes(changed)

                with ExitStack() as patches:
                    patches.enter_context(patch.object(visual_animation, "MAX_ATLAS_EDGE", 128))
                    patches.enter_context(patch.object(emote_media, "detect_emote_media", return_value=kind))
                    adapted = patches.enter_context(patch.object(emote_media, "adapt_video_source", return_value=(frames, video_info)))
                    patches.enter_context(patch.object(visual_animation, "_read_replacement", return_value=(frames, [120, 240], [])))
                    canvas = patches.enter_context(patch.object(visual_animation, "card_video_canvas", wraps=visual_animation.card_video_canvas))
                    patches.enter_context(patch.object(visual_animation, "read_descriptor", side_effect=lambda path: path.read_bytes()))
                    patches.enter_context(patch.object(visual_animation, "read_atlas", side_effect=read_atlas))
                    patches.enter_context(patch.object(visual_animation, "write_descriptor",
                                                     side_effect=lambda source, output, raw: output.write_bytes(raw)))
                    patches.enter_context(patch.object(visual_animation, "write_atlas",
                                                     side_effect=lambda source, output, image: image.save(output, format="PNG")))
                    patches.enter_context(patch.object(visual_config, "replace_visual_config_bundle", side_effect=replace_config))
                    candidate = workspace.prepare(source, candidate_folder)
                    item = workspace.save(candidate)
                    actual_preview = workspace.draft_preview()
                package = inspect_movieclip_package((draft / "ui.bundle").read_bytes())
                clip = next(row for row in package["movieclips"] if row["id"] == animation_identity(NAME))
                sprites = {row["id"]: row for row in package["sprites"]}
                self.assertEqual(actual_preview, candidate["animation"])
                self.assertEqual((item["width"], item["height"]), (clip["width"], clip["height"]))
                self.assertEqual((item["source_width"], item["source_height"]), (40, 64))
                self.assertEqual(len(controller.draft_items), 1)
                self.assertEqual({name: path.read_bytes() for name, path in paths.items()}, original_bytes)
                if kind == "video":
                    canvas.assert_called_once_with((40, 64), (16, 9))
                    self.assertIn("source_planner", adapted.call_args.kwargs)
                    self.assertNotIn("output_planner", adapted.call_args.kwargs)
                    self.assertEqual((item["content_width"], item["content_height"]), (16, 9))
                    self.assertEqual((item["video_source_width"], item["video_source_height"]), (1920, 1080))
                    self.assertEqual(item["duration_ms"], 2000)
                    self.assertGreater(clip["height"], 9)
                    for frame in clip["frames"]:
                        self.assertEqual(frame["rect"][2:], [16, 9])
                        self.assertGreater(frame["rect"][1], 0)
                        self.assertEqual(sprites[frame["sprite_id"]]["rect"][2:], [16, 9])
                else:
                    canvas.assert_not_called()
                    adapted.assert_not_called()
                    self.assertNotIn("video_source_width", item)
                    self.assertEqual(item["duration_ms"], 360)
                    self.assertEqual((clip["width"], clip["height"]), (40, 64))
                    self.assertTrue(all(frame["rect"] == [0, 0, 40, 64] for frame in clip["frames"]))


class SharedVideoWorkspaceTests(unittest.TestCase):
    def setUp(self):
        from astral_party_auto.modkit import emote_media, visual_animation, visual_config
        from test_visual_config import card, table

        temporary = TemporaryDirectory(prefix="jixing-shared-video-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        originals = self.root / "originals"
        originals.mkdir()
        self.paths = {name: originals / name for name in ("config.bundle", "ui.bundle", "atlas.bundle", "image.bundle")}
        self.targets = {"UT_HandCard_20018": (40, 64), "UT_HandCard_21001": (42, 64), "UT_HandCard_21007": (32, 64)}
        rows = [(number, card(number, {6: name})) for number, name in enumerate(self.targets, 20000)]
        self.paths["config.bundle"].write_bytes(table(rows))
        self.paths["ui.bundle"].write_bytes(package_fixture().replace(b"test1234", b"xuaw6o8j", 1))
        self.paths["image.bundle"].write_bytes(b"isolated source texture")
        Image.new("RGBA", (32, 32), (50, 70, 90, 255)).save(self.paths["atlas.bundle"], format="PNG")
        self.original_bytes = {name: path.read_bytes() for name, path in self.paths.items()}
        self.source = self.root / "first-container.mkv"
        self.alias = self.root / "same-frames-another-container.mov"
        self.source.write_bytes(b"first source container")
        self.alias.write_bytes(b"different source bytes with identical decoded frames")
        self.frames = [Image.new("RGBA", (28, 28), color) for color in ((220, 30, 40, 255), (20, 50, 230, 255))]
        self.draft = self.root / "draft"
        self.draft.mkdir()
        self.controller = Mock(selection=None, draft_items=[])
        self.controller.original_bundle_path.side_effect = self.paths.get
        self.controller._draft_dir.return_value = self.draft
        self.controller.emote_workspace._commit.side_effect = self.commit
        self.written_atlases = []
        self.decoded_sizes = []

        @contextmanager
        def prepare_folder(prefix):
            with TemporaryDirectory(prefix=prefix, dir=self.root) as folder:
                yield Path(folder)

        self.controller.emote_workspace._prepare_folder.side_effect = prepare_folder
        self.workspace = VisualAnimationWorkspace(self.controller)
        self.context = {"config_bundle": "config.bundle", "ui_bundle": "ui.bundle", "atlas_bundle": "atlas.bundle",
                        "base_size": (32, 32), "package_id": "xuaw6o8j",
                        "originals": {name: self.paths[name] for name in ("config.bundle", "ui.bundle", "atlas.bundle")},
                        "tables": {"Card": visual_config.inspect_visual_config(table(rows), "Card")}}
        self.workspace.context = Mock(return_value=self.context)
        self.workspace._source_key = ("isolated bundles", "isolated catalog")

        def read_atlas(path):
            with Image.open(path) as image:
                return image.convert("RGBA")

        def write_atlas(source, output, image):
            self.written_atlases.append(image.copy())
            image.save(output, format="PNG")

        def replace_config(source, output, table_name, old, new, *, restore_from=None):
            original = restore_from.read_bytes() if restore_from else None
            output.write_bytes(visual_config.replace_visual_address(source.read_bytes(), table_name, old, new,
                                                                   original_raw=original))

        def adapt_video(source, reference, *, source_planner, **_options):
            plan = source_planner({"width": 28, "height": 28, "display_aspect": 1, "duration_ms": 100})
            size = (plan["width"], plan["height"])
            self.decoded_sizes.append(size)
            return [emote_media._fit_frame(frame, *size) for frame in self.frames], {
                "width": size[0], "height": size[1], "fps": plan["fps"], "source_width": 28,
                "source_height": 28, "source_duration_ms": 100,
            }

        self.enterContext(patch.object(visual_animation, "MAX_ATLAS_EDGE", 64))
        self.enterContext(patch.object(emote_media, "detect_emote_media", return_value="video"))
        self.enterContext(patch.object(emote_media, "adapt_video_source", side_effect=adapt_video))
        self.enterContext(patch.object(visual_animation, "read_descriptor", side_effect=lambda path: path.read_bytes()))
        self.enterContext(patch.object(visual_animation, "read_atlas", side_effect=read_atlas))
        self.enterContext(patch.object(visual_animation, "write_descriptor",
                                     side_effect=lambda source, output, raw: output.write_bytes(raw)))
        self.enterContext(patch.object(visual_animation, "write_atlas", side_effect=write_atlas))
        self.enterContext(patch.object(visual_config, "replace_visual_config_bundle", side_effect=replace_config))
        self.enterContext(patch.object(visual_animation, "_config_has_address", side_effect=lambda path, tables, address: all(
            any(target["name"] == address for target in visual_config.inspect_visual_config(path.read_bytes(), name)["targets"])
            for name in tables)))

    def commit(self, staged, items):
        for name, source in staged.items():
            destination = self.draft / name
            if source is None:
                destination.unlink(missing_ok=True)
            else:
                destination.write_bytes(source.read_bytes())
        self.controller.draft_items = copy.deepcopy(items)

    def select(self, name):
        width, height = self.targets[name]
        self.controller.selection = {"asset_type": "texture", "bundle": "image.bundle", "name": name,
                                     "width": width, "height": height}

    def save_video(self, name, source=None):
        self.select(name)
        with TemporaryDirectory(prefix="candidate-", dir=self.root) as folder:
            candidate = self.workspace.prepare(source or self.source, Path(folder))
            item = self.workspace.save(candidate)
            self.assertEqual(self.workspace.draft_preview(), candidate["animation"])
        return item

    def package(self):
        from astral_party_auto.modkit.fairygui_movieclip import inspect_movieclip_package

        return inspect_movieclip_package((self.draft / "ui.bundle").read_bytes())

    def save_legacy_video(self, name, size):
        from astral_party_auto.modkit import visual_animation

        with patch.object(visual_animation, "plan_card_video", return_value={"width": size[0], "height": size[1], "fps": 20}):
            self.save_video(name)
        self.controller.draft_items[-1].pop("frame_content_sha256", None)
        package = self.package()
        sprites = {row["id"]: row for row in package["sprites"]}
        clip = next(row for row in package["movieclips"] if row["id"] == animation_identity(name))
        return [sprites[frame["sprite_id"]]["rect"] for frame in clip["frames"]]

    def assert_saved_frames(self, names, expected_rects=None):
        package = self.package()
        sprites = {row["id"]: row for row in package["sprites"]}
        with Image.open(self.draft / "atlas.bundle") as image:
            atlas = image.convert("RGBA")
        self.assertEqual(atlas.crop((0, 0, 32, 32)).tobytes(), Image.new("RGBA", (32, 32), (50, 70, 90, 255)).tobytes())
        self.assertEqual(len(package["movieclips"]), len(names))
        for name in names:
            identity = animation_identity(name)
            clip = next(row for row in package["movieclips"] if row["id"] == identity)
            rects = [sprites[frame["sprite_id"]]["rect"] for frame in clip["frames"]]
            if expected_rects is not None:
                self.assertEqual(rects, expected_rects)
            for index, (frame, rect) in enumerate(zip(clip["frames"], rects)):
                self.assertEqual(frame["sprite_id"], f"{identity}_{index}")
                self.assertEqual(frame["rect"][2:], [28, 28])
                x, y, width, height = rect
                self.assertEqual(atlas.crop((x, y, x + width, y + height)).tobytes(), self.frames[index].tobytes())
        return [sprites[frame["sprite_id"]]["rect"] for frame in package["movieclips"][0]["frames"]]

    def test_same_video_and_container_alias_share_pixels_across_one_draft_lifecycle(self):
        names = list(self.targets)
        first = self.save_video(names[0])
        first_rects = self.assert_saved_frames(names[:1])
        self.assertLess(len(pack_frames((32, 32), first_rects, (28, 28), 2)), 2)
        for name in names[1:]:
            item = self.save_video(name, self.alias)
            self.assertEqual((item["content_width"], item["content_height"]), (28, 28))
            self.assertEqual(item["frame_content_sha256"], first["frame_content_sha256"])
            self.assert_saved_frames([row["name"] for row in self.controller.draft_items], first_rects)
        self.save_video(names[0], self.alias)
        self.assert_saved_frames(names, first_rects)
        for name in names[:-1]:
            index = next(index for index, item in enumerate(self.controller.draft_items) if item["name"] == name)
            self.workspace.remove(index)
            remaining = [item["name"] for item in self.controller.draft_items]
            self.assert_saved_frames(remaining, first_rects)
        self.workspace.remove(0)
        self.assertEqual(self.controller.draft_items, [])
        self.assertFalse(list(self.draft.glob("*.bundle")))
        last_atlas = self.written_atlases[-1]
        for x, y, width, height in first_rects:
            self.assertEqual(last_atlas.crop((x - 1, y - 1, x + width + 1, y + height + 1)).getbbox(), None)
        self.assertEqual({name: path.read_bytes() for name, path in self.paths.items()}, self.original_bytes)

    def test_card_added_by_game_update_can_be_animated_with_an_older_draft_config(self):
        from astral_party_auto.modkit import visual_config
        from test_visual_config import card, table

        names = list(self.targets)
        first = self.save_video(names[0])
        # 草稿配置来自更新前的游戏：只有第一张卡，后两张是新版本新增的。
        older = visual_config.replace_visual_address(table([(20000, card(20000, {6: names[0]}))]), "Card",
                                                     names[0], first["movieclip_url"])
        (self.draft / "config.bundle").write_bytes(older)
        second = self.save_video(names[1], self.alias)
        saved = {row["name"] for row in visual_config.inspect_visual_config(
            (self.draft / "config.bundle").read_bytes(), "Card")["targets"]}
        self.assertEqual(saved, {first["movieclip_url"], second["movieclip_url"], names[2]})
        self.controller.log.assert_any_call(
            "作品集中的游戏配置与当前游戏版本不一致，已按当前版本重建并保留已有的表情和动态作品。")

    def test_legacy_item_without_content_hash_reuses_verified_pixels(self):
        names = list(self.targets)
        self.save_video(names[0])
        first_rects = self.assert_saved_frames(names[:1])
        self.controller.draft_items[0].pop("frame_content_sha256", None)
        item = self.save_video(names[1], self.alias)
        self.assertEqual((item["content_width"], item["content_height"]), (28, 28))
        self.assert_saved_frames(names[:2], first_rects)

    def test_different_video_fails_before_reducing_quality_or_changing_the_draft(self):
        names = list(self.targets)
        self.save_video(names[0])
        previous_items = copy.deepcopy(self.controller.draft_items)
        previous_files = {path.name: path.read_bytes() for path in self.draft.glob("*.bundle")}
        self.frames[0] = Image.new("RGBA", (28, 28), (5, 200, 80, 255))
        self.select(names[1])
        with TemporaryDirectory(prefix="candidate-", dir=self.root) as folder, self.assertRaises(ValueError):
            self.workspace.prepare(self.alias, Path(folder))
        self.assertEqual(self.controller.draft_items, previous_items)
        self.assertEqual({path.name: path.read_bytes() for path in self.draft.glob("*.bundle")}, previous_files)

    def test_matching_metadata_cannot_reuse_changed_atlas_pixels(self):
        names = list(self.targets)
        self.save_video(names[0])
        first_rects = self.assert_saved_frames(names[:1])
        with Image.open(self.draft / "atlas.bundle") as image:
            atlas = image.convert("RGBA")
        atlas.putpixel(tuple(first_rects[0][:2]), (1, 2, 3, 255))
        atlas.save(self.draft / "atlas.bundle", format="PNG")
        previous_files = {path.name: path.read_bytes() for path in self.draft.glob("*.bundle")}
        self.select(names[1])
        with TemporaryDirectory(prefix="candidate-", dir=self.root) as folder, self.assertRaises(ValueError):
            self.workspace.prepare(self.alias, Path(folder))
        self.assertEqual(len(self.controller.draft_items), 1)
        self.assertEqual({path.name: path.read_bytes() for path in self.draft.glob("*.bundle")}, previous_files)

    def test_matching_hash_cannot_bypass_actual_frame_timing_or_sprite_geometry(self):
        from astral_party_auto.modkit.visual_animation import _find_shared_frames, _frame_content_hash

        self.save_video(next(iter(self.targets)))
        package = self.package()
        with Image.open(self.draft / "atlas.bundle") as image:
            atlas = image.convert("RGBA")
        changed_timing = [25, 75]
        forged_hash = _frame_content_hash(self.frames, changed_timing)
        self.controller.draft_items[0]["frame_content_sha256"] = forged_hash
        self.assertEqual(_find_shared_frames(package, atlas, self.frames, changed_timing, (32, 32),
                                             self.controller.draft_items, forged_hash), [])
        content_hash = _frame_content_hash(self.frames, [50, 50])
        self.controller.draft_items[0]["frame_content_sha256"] = content_hash
        sprite_id = package["movieclips"][0]["frames"][0]["sprite_id"]
        for changes in ({"rect": [1, 1, 28, 28]}, {"rotated": True}, {"trimmed": True}, {"atlas_id": "other"}):
            with self.subTest(changes=changes):
                altered = copy.deepcopy(package)
                sprite = next(row for row in altered["sprites"] if row["id"] == sprite_id)
                sprite.update(changes)
                self.assertEqual(_find_shared_frames(altered, atlas, self.frames, [50, 50], (32, 32),
                                                     self.controller.draft_items, content_hash), [])

    def test_legacy_profiles_redecode_and_reuse_the_highest_verified_resolution(self):
        names = list(self.targets)
        highest_rects = self.save_legacy_video(names[0], (20, 20))
        self.save_legacy_video(names[1], (14, 14))
        self.decoded_sizes.clear()
        item = self.save_video(names[2], self.alias)
        self.assertEqual(self.decoded_sizes, [(28, 28), (20, 20)])
        self.assertEqual((item["content_width"], item["content_height"]), (20, 20))
        package = self.package()
        sprites = {row["id"]: row for row in package["sprites"]}
        clip = next(row for row in package["movieclips"] if row["id"] == animation_identity(names[2]))
        self.assertEqual([sprites[frame["sprite_id"]]["rect"] for frame in clip["frames"]], highest_rects)
        self.assertEqual(len(package["sprites"]), 7)

    def test_legacy_profile_pixels_are_checked_before_trying_a_lower_matching_profile(self):
        names = list(self.targets)
        self.save_legacy_video(names[0], (20, 20))
        self.frames[0] = Image.new("RGBA", (28, 28), (5, 200, 80, 255))
        matching_rects = self.save_legacy_video(names[1], (14, 14))
        self.decoded_sizes.clear()
        item = self.save_video(names[2], self.alias)
        self.assertEqual(self.decoded_sizes, [(28, 28), (20, 20), (14, 14)])
        self.assertEqual((item["content_width"], item["content_height"]), (14, 14))
        package = self.package()
        sprites = {row["id"]: row for row in package["sprites"]}
        clip = next(row for row in package["movieclips"] if row["id"] == animation_identity(names[2]))
        self.assertEqual([sprites[frame["sprite_id"]]["rect"] for frame in clip["frames"]], matching_rects)

    def test_legacy_profile_over_current_pixel_budget_does_not_block_a_valid_profile(self):
        from astral_party_auto.modkit import emote_media

        names = list(self.targets)
        self.save_legacy_video(names[0], (20, 20))
        matching_rects = self.save_legacy_video(names[1], (14, 14))
        self.controller.draft_items.insert(0, dict(self.controller.draft_items[0], name=None))
        adapt_video = emote_media.adapt_video_source.side_effect

        def enforce_output_budget(source, reference, *, source_planner, **options):
            def checked_plan(info):
                plan = source_planner(info)
                pixels = plan["width"] * plan["height"]
                if pixels > emote_media.MAX_VIDEO_FRAME_PIXELS or pixels * 2 > emote_media.MAX_OUTPUT_PIXELS:
                    raise ValueError("转码后的帧数据超过像素限制。")
                return plan

            return adapt_video(source, reference, source_planner=checked_plan, **options)

        for frame_budget, total_budget in ((1000, 600), (300, 2000)):
            with self.subTest(frame_budget=frame_budget, total_budget=total_budget):
                self.decoded_sizes.clear()
                with patch.object(emote_media, "MAX_VIDEO_FRAME_PIXELS", frame_budget), \
                        patch.object(emote_media, "MAX_OUTPUT_PIXELS", total_budget), \
                        patch.object(emote_media, "adapt_video_source", side_effect=enforce_output_budget):
                    item = self.save_video(names[2], self.alias)
                self.assertEqual(self.decoded_sizes, [(17, 17), (14, 14)])
                self.assertEqual((item["content_width"], item["content_height"]), (14, 14))
        package = self.package()
        sprites = {row["id"]: row for row in package["sprites"]}
        clip = next(row for row in package["movieclips"] if row["id"] == animation_identity(names[2]))
        self.assertEqual([sprites[frame["sprite_id"]]["rect"] for frame in clip["frames"]], matching_rects)

    def test_unidentified_legacy_item_does_not_block_verified_frame_reuse(self):
        names = list(self.targets)
        self.save_video(names[0])
        first_rects = self.assert_saved_frames(names[:1])
        for bad_name in (None, 123, ""):
            with self.subTest(name=bad_name):
                legacy_item = dict(self.controller.draft_items[-1], name=bad_name)
                legacy_item.pop("frame_content_sha256", None)
                self.controller.draft_items.insert(0, legacy_item)
                try:
                    item = self.save_video(names[1], self.alias)
                    self.assertEqual((item["content_width"], item["content_height"]), (28, 28))
                    self.assertEqual(self.controller.draft_items[0], legacy_item)
                    self.assert_saved_frames(names[:2], first_rects)
                finally:
                    self.controller.draft_items.pop(0)
        legacy_item = dict(self.controller.draft_items[-1])
        legacy_item.pop("name")
        legacy_item.pop("frame_content_sha256", None)
        self.controller.draft_items.insert(0, legacy_item)
        self.save_video(names[1], self.alias)
        self.assertEqual(self.controller.draft_items[0], legacy_item)
        self.assert_saved_frames(names[:2], first_rects)

    def test_unknown_overlapping_sprite_cannot_be_used_as_a_shared_frame_donor(self):
        from astral_party_auto.modkit import fairygui_movieclip

        names = list(self.targets)
        self.save_video(names[0])
        ui_path = self.draft / "ui.bundle"
        package = fairygui_movieclip._parse(ui_path.read_bytes())
        donor = package.sprites[1]
        unknown_index = package.strings.index("unused duplicate")
        unknown_record = unknown_index.to_bytes(2, "big") + donor["_record"][2:]
        ui_path.write_bytes(fairygui_movieclip._write(
            package, [item["_record"] for item in package.items],
            [sprite["_record"] for sprite in package.sprites] + [unknown_record]))
        before_items = copy.deepcopy(self.controller.draft_items)
        before_files = {path.name: path.read_bytes() for path in self.draft.glob("*.bundle")}
        self.select(names[1])
        with TemporaryDirectory(prefix="candidate-", dir=self.root) as folder, self.assertRaises(ValueError):
            self.workspace.prepare(self.alias, Path(folder))
        self.assertEqual(self.controller.draft_items, before_items)
        self.assertEqual({path.name: path.read_bytes() for path in self.draft.glob("*.bundle")}, before_files)

    def test_decoded_frames_are_closed_after_prepare_and_after_write_failure(self):
        from astral_party_auto.modkit import emote_media, visual_animation

        adapt_video = emote_media.adapt_video_source.side_effect
        decoded = []

        def track_frames(*args, **options):
            frames, info = adapt_video(*args, **options)
            decoded.extend(frames)
            return frames, info

        names = list(self.targets)
        with patch.object(emote_media, "adapt_video_source", side_effect=track_frames):
            self.save_video(names[0])
            for frame in decoded:
                with self.assertRaises(ValueError):
                    frame.getpixel((0, 0))
            decoded.clear()
            self.select(names[1])
            with patch.object(visual_animation, "write_descriptor", side_effect=OSError("isolated write failure")), \
                    TemporaryDirectory(prefix="candidate-", dir=self.root) as folder, self.assertRaises(OSError):
                self.workspace.prepare(self.alias, Path(folder))
            for frame in decoded:
                with self.assertRaises(ValueError):
                    frame.getpixel((0, 0))

    def test_legacy_retry_frames_are_closed_when_content_validation_raises(self):
        from astral_party_auto.modkit import emote_media, visual_animation

        names = list(self.targets)
        self.save_legacy_video(names[0], (20, 20))
        adapt_video = emote_media.adapt_video_source.side_effect
        content_hash = visual_animation._frame_content_hash
        decoded = []

        def track_frames(*args, **options):
            frames, info = adapt_video(*args, **options)
            decoded.extend(frames)
            return frames, info

        def fail_retry_hash(frames, durations):
            if len(decoded) > 2:
                raise ValueError("isolated retry validation failure")
            return content_hash(frames, durations)

        self.select(names[1])
        before_items = copy.deepcopy(self.controller.draft_items)
        before_files = {path.name: path.read_bytes() for path in self.draft.glob("*.bundle")}
        with patch.object(emote_media, "adapt_video_source", side_effect=track_frames), \
                patch.object(visual_animation, "_frame_content_hash", side_effect=fail_retry_hash), \
                TemporaryDirectory(prefix="candidate-", dir=self.root) as folder, self.assertRaisesRegex(ValueError, "retry"):
            self.workspace.prepare(self.alias, Path(folder))
        self.assertEqual(len(decoded), 4)
        for frame in decoded:
            with self.assertRaises(ValueError):
                frame.getpixel((0, 0))
        self.assertEqual(self.controller.draft_items, before_items)
        self.assertEqual({path.name: path.read_bytes() for path in self.draft.glob("*.bundle")}, before_files)

    def test_switching_one_shared_video_to_gif_preserves_the_other_clip(self):
        from astral_party_auto.modkit import emote_media, visual_animation

        names = list(self.targets)
        self.save_video(names[0])
        first_rects = self.assert_saved_frames(names[:1])
        self.save_video(names[1])
        gif_frames = [Image.new("RGBA", (8, 8), color) for color in ((10, 200, 30, 255), (180, 20, 210, 255))]
        with patch.object(emote_media, "detect_emote_media", return_value="gif"), \
                patch.object(visual_animation, "_read_replacement", return_value=(gif_frames, [70, 130], "gif")):
            item = self.save_video(names[0])
        self.assertNotIn("frame_content_sha256", item)
        self.assertNotIn("video_source_width", item)
        with Image.open(self.draft / "atlas.bundle") as image:
            before = image.convert("RGBA")
        package = self.package()
        sprites = {row["id"]: row for row in package["sprites"]}
        clip = next(row for row in package["movieclips"] if row["id"] == animation_identity(names[0]))
        gif_rects = [sprites[frame["sprite_id"]]["rect"] for frame in clip["frames"]]
        self.workspace.remove(next(index for index, row in enumerate(self.controller.draft_items) if row["name"] == names[1]))
        with Image.open(self.draft / "atlas.bundle") as image:
            after = image.convert("RGBA")
        for x, y, width, height in gif_rects:
            box = (x, y, x + width, y + height)
            self.assertEqual(after.crop(box).tobytes(), before.crop(box).tobytes())
        for x, y, width, height in first_rects:
            self.assertIsNone(after.crop((x, y, x + width, y + height)).getbbox())


class FrameContentIdentityTests(unittest.TestCase):
    def test_content_hash_uses_rgba_pixels_geometry_order_and_timing(self):
        from astral_party_auto.modkit.visual_animation import _frame_content_hash

        frames = [Image.new("RGBA", (4, 2), (200, 20, 40, 255)), Image.new("RGBA", (4, 2), (20, 40, 200, 255))]
        original = _frame_content_hash(frames, [100, 200])
        self.assertEqual(original, _frame_content_hash([frame.convert("RGB") for frame in frames], [100, 200]))
        self.assertNotEqual(original, _frame_content_hash(frames, [200, 100]))
        self.assertNotEqual(original, _frame_content_hash(list(reversed(frames)), [100, 200]))
        reshaped = [Image.frombytes("RGBA", (2, 4), frame.tobytes()) for frame in frames]
        self.assertNotEqual(original, _frame_content_hash(reshaped, [100, 200]))

    def test_content_hash_rejects_missing_or_invalid_timing(self):
        from astral_party_auto.modkit.visual_animation import _frame_content_hash

        frame = Image.new("RGBA", (2, 2))
        for frames, durations in (([], []), ([frame], []), ([frame], [0]), ([frame], [True]),
                                  ([frame], [1.5]), ([frame], [60001])):
            with self.subTest(durations=durations), self.assertRaises(ValueError):
                _frame_content_hash(frames, durations)


class VisualSelectionTests(unittest.TestCase):
    def test_saved_candidate_cannot_cross_a_catalog_source_change(self):
        selection = {"asset_type": "texture", "bundle": "card.bundle", "name": "UT_HandCard_1"}
        controller = Mock(selection=selection)
        workspace = VisualAnimationWorkspace(controller)
        workspace._selection = Mock(return_value=(selection, {}))
        def changed_context():
            workspace._source_key = ("current-bundles", "new-catalog")
            return {}
        workspace.context = Mock(side_effect=changed_context)
        candidate = {"item": {"name": selection["name"], "source_bundle": selection["bundle"]},
                     "catalog_source_key": ("current-bundles", "old-catalog")}
        with self.assertRaisesRegex(ValueError, "目录来源.*变化"):
            workspace.save(candidate)
        controller.emote_workspace._commit.assert_not_called()

    def test_configuration_reference_is_editable_even_with_a_new_resource_name(self):
        controller = Mock(draft_items=[])
        workspace = VisualAnimationWorkspace(controller)
        workspace.context = Mock(return_value={"tables": {"Card": {"targets": [{"name": "NewVersionCardImage"}]},
                                                          "Skin": {"targets": []}}})
        self.assertTrue(workspace.selection("NewVersionCardImage")["editable"])
        self.assertFalse(workspace.selection("UT_Ordinary_Icon")["recognized"])

    def test_known_card_variants_keep_explanations_when_resources_are_unavailable(self):
        workspace = VisualAnimationWorkspace(Mock())
        workspace.context = Mock(side_effect=ValueError("请加载当前卡面资源"))
        for name in ("UT_HandCard_1", "UT_Hero_Card2_101", "UT_Hero_ThinCard_101", "UT_CardFront_9", "UT_IAltArt_1"):
            with self.subTest(name=name):
                result = workspace.selection(name)
                self.assertTrue(result["recognized"])
                self.assertFalse(result["editable"])
                self.assertIn("加载", result["reason"])


class StaleDraftConfigTests(unittest.TestCase):
    """作品集配置早于当前游戏版本时（新卡牌），需按当前版本重建，不能报找不到资源名。"""

    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.folder = root / "work"
        self.folder.mkdir()
        self.original = root / "original" / "config.bundle"
        self.draft = root / "draft" / "config.bundle"
        for path in (self.original, self.draft):
            path.parent.mkdir()
            path.write_bytes(b"bundle")
        self.context = {"config_bundle": "config.bundle", "originals": {"config.bundle": self.original}}
        self.controller = Mock(draft_items=[
            {"kind": "emote", "bundle": "config.bundle", "name": "Emote", "emote_id": 7, "video_name": "Video_7"},
            {"kind": "visual_animation", "bundle": "config.bundle", "name": "UT_HandCard_Old",
             "extra_bundles": ["ui.bundle", "atlas.bundle"], "tables": ["Card"], "movieclip_url": "ui://xuaw6o8jjmold"},
            {"kind": "texture", "bundle": "other.bundle", "name": "UT_Other"},
        ])
        self.workspace = VisualAnimationWorkspace(self.controller)
        self.workspace._source = Mock(return_value=self.draft)
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def _replay(self, kind):
        def write(source, output, *args):
            self.calls.append((kind, Path(source), *args))
            Path(output).write_bytes(b"rebuilt")
        return write

    def _run(self, has_address):
        with patch("astral_party_auto.modkit.visual_animation._config_has_address", side_effect=has_address), \
                patch("astral_party_auto.modkit.emote_config.replace_emote_config_bundle", side_effect=self._replay("emote")), \
                patch("astral_party_auto.modkit.visual_config.replace_visual_config_bundle", side_effect=self._replay("visual")):
            return self.workspace._config_base(self.context, ["Card"], "UT_HandCard_New", self.folder)

    def test_current_draft_config_is_used_directly(self):
        self.assertEqual(self._run(lambda path, tables, address: True), self.draft)
        self.assertEqual(self.calls, [])

    def test_stale_draft_is_rebuilt_from_current_game_and_keeps_existing_edits(self):
        result = self._run(lambda path, tables, address: path != self.draft)
        self.assertNotEqual(result, self.draft)
        self.assertEqual(self.calls[0], ("emote", self.original, 7, "Video_7"))
        self.assertEqual(self.calls[1][0], "visual")
        self.assertEqual(self.calls[1][2:], ("Card", "UT_HandCard_Old", "ui://xuaw6o8jjmold"))
        self.assertTrue(result.is_relative_to(self.folder))
        self.controller.log.assert_called_once()

    def test_unknown_config_edit_reports_clear_reason_instead_of_dropping_it(self):
        self.controller.draft_items.append({"kind": "text", "bundle": "config.bundle", "name": "Language"})
        with self.assertRaisesRegex(ValueError, "旧版本游戏.*Language.*另建作品集"):
            self._run(lambda path, tables, address: path != self.draft)

    def test_name_missing_from_current_game_too_is_reported(self):
        with self.assertRaisesRegex(ValueError, "重建后的配置仍找不到"):
            self._run(lambda path, tables, address: False)


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

    def test_draft_list_failure_after_save_keeps_committed_response(self):
        self.choose()
        candidate_folder = Path(self.api._visual_candidate_dir.name)
        self.api._draft_state.side_effect = OSError("draft list unavailable")
        result = self.api.save_visual_animation()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["item"], self.workspace.save.return_value)
        self.assertEqual(result["data"]["animation"], self.animation)
        self.assertIsNone(result["data"]["draft"])
        self.assertIn("已经保存", result["data"]["notice"])
        self.assertIn("draft list unavailable", result["data"]["notice"])
        self.assertFalse(candidate_folder.exists())
        self.assertIsNone(self.api._visual_candidate)
        self.assertFalse(self.api.save_visual_animation()["ok"])
        self.workspace.save.assert_called_once()

    def test_draft_list_and_preview_failures_after_save_keep_both_notices(self):
        self.choose()
        self.workspace.draft_preview.side_effect = ValueError("preview unavailable")
        self.api._draft_state.side_effect = TypeError("invalid draft metadata")
        result = self.api.save_visual_animation()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertIsNone(result["data"]["draft"])
        self.assertEqual(result["data"]["animation"]["frames"], [])
        self.assertIn("preview unavailable", result["data"]["animation"]["notice"])
        self.assertIn("invalid draft metadata", result["data"]["notice"])
        self.workspace.save.assert_called_once()

    def test_commit_failure_stays_failed_without_reading_response_state(self):
        self.choose()
        self.workspace.save.side_effect = OSError("draft write failed")
        result = self.api.save_visual_animation()
        self.assertFalse(result["ok"])
        self.assertIn("draft write failed", result["error"])
        self.workspace.draft_preview.assert_not_called()
        self.api._draft_state.assert_not_called()
        self.assertIsNone(self.api._visual_candidate)
        self.assertIsNone(self.api._visual_candidate_dir)

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

    def test_identical_rect_alias_keeps_pixels_until_the_final_clip_is_removed(self):
        self.package["sprites"][0]["id"] = "jmowner_0"
        owner = {"id": "jmowner", "path": "/JiXingModHelper/Animations/", "frames": [{"sprite_id": "jmowner_0"}]}
        alias = {"id": "jmalias", "path": "/JiXingModHelper/Animations/", "frames": [{"sprite_id": "jmalias_0"}]}
        alias_sprite = dict(self.package["sprites"][0], id="jmalias_0")
        self.package["sprites"].append(alias_sprite)
        self.package["movieclips"] = [owner, alias]
        before = self.atlas.tobytes()
        _clear_clip_pixels(self.atlas, self.package, owner, (8, 8))
        self.assertEqual(self.atlas.tobytes(), before)
        self.package["sprites"] = [row for row in self.package["sprites"] if row["id"] != "jmowner_0"]
        self.package["movieclips"] = [alias]
        _clear_clip_pixels(self.atlas, self.package, alias, (8, 8))
        self.assertIsNone(self.atlas.crop((11, 11, 17, 17)).getbbox())
        self.assertEqual(self.atlas.getpixel((0, 0)), (45, 90, 120, 255))
        self.assertEqual(self.atlas.getpixel((20, 20)), (45, 90, 120, 255))

    def test_self_reimport_can_retain_its_exact_verified_rectangles(self):
        before = self.atlas.tobytes()
        _clear_clip_pixels(self.atlas, self.package, self.clip, (8, 8), retained_rects=[[12, 12, 4, 4]])
        self.assertEqual(self.atlas.tobytes(), before)

    def test_alias_cleanup_rejects_partial_overlap_unreferenced_and_modified_sprites(self):
        cases = [({"rect": [13, 12, 4, 4]}, True, "/JiXingModHelper/Animations/"),
                 ({}, False, "/JiXingModHelper/Animations/"), ({}, True, "/External/"),
                 ({"rotated": True}, True, "/JiXingModHelper/Animations/"),
                 ({"trimmed": True}, True, "/JiXingModHelper/Animations/"),
                 ({"atlas_id": "other"}, True, "/JiXingModHelper/Animations/")]
        for changes, referenced, path in cases:
            with self.subTest(changes=changes, referenced=referenced, path=path):
                package = copy.deepcopy(self.package)
                alias = dict(package["sprites"][0], id="jmalias_0")
                alias.update(changes)
                package["sprites"].append(alias)
                package["movieclips"] = ([{"id": "jmalias", "path": path, "frames": [{"sprite_id": "jmalias_0"}]}]
                                         if referenced else [])
                before = self.atlas.tobytes()
                with self.assertRaises(ValueError):
                    _clear_clip_pixels(self.atlas, package, self.clip, (8, 8))
                self.assertEqual(self.atlas.tobytes(), before)

    def test_allowed_alias_does_not_hide_another_partially_overlapping_resource(self):
        package = copy.deepcopy(self.package)
        package["sprites"].append(dict(package["sprites"][0], id="jmalias_0"))
        package["sprites"].append(dict(package["sprites"][0], id="unknown", rect=[16, 12, 4, 4]))
        package["movieclips"] = [{"id": "jmalias", "path": "/JiXingModHelper/Animations/",
                                  "frames": [{"sprite_id": "jmalias_0"}]}]
        before = self.atlas.tobytes()
        with self.assertRaises(ValueError):
            _clear_clip_pixels(self.atlas, package, self.clip, (8, 8))
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
