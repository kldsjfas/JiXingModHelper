"""角色动画工作台的真实帧、兼容清单与失败保留回归。"""
from __future__ import annotations

import hashlib
import io
import json
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from astral_party_auto.modkit import character_animation as module


class CharacterAnimationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-character-animation-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = module.CharacterAnimationWorkspace(self.root / "workspace")
        self.frames = self.root / "frames"
        self.frames.mkdir()
        for number, color in [(1, "red"), (2, "green"), (10, "blue")]:
            Image.new("RGBA", (8, 12), color).save(self.frames / f"frame-{number}.png")

    def project(self):
        return json.loads((self.root / "workspace/hero-115/project.json").read_text(encoding="utf-8"))

    def test_three_actions_export_actual_lossless_frames_and_pascal_case_manifest(self):
        for action in ["Idle", "Walk", "Fight"]:
            self.workspace.import_action(115, action, self.frames, fps=20, loop=action != "Fight")
        result = self.workspace.export(115, "三动作测试", "作者")
        self.assertEqual((result["action_count"], result["frame_count"]), (3, 9))
        with zipfile.ZipFile(result["path"]) as archive:
            manifest = json.loads(archive.read("character-skin.json"))
            self.assertEqual(manifest["HeroId"], 115)
            self.assertEqual(manifest["SkinItemId"], 0)
            self.assertEqual(manifest["SchemaVersion"], 1)
            self.assertFalse(manifest["UseOriginalSpriteLayout"])
            self.assertEqual(manifest["Layout"], module.DEFAULT_LAYOUT)
            self.assertEqual(manifest["Images"], {})
            self.assertEqual(manifest["SpriteMappings"], {})
            animation = manifest["Animations"]["Walk"]
            self.assertEqual(animation["DurationsMs"], [50, 50, 50])
            self.assertEqual(animation["Fps"], 20)
            self.assertTrue(animation["Loop"])
            self.assertFalse(manifest["Animations"]["Fight"]["Loop"])
            expected_colors = [(255, 0, 0, 255), (0, 128, 0, 255), (0, 0, 255, 255)]
            for relative, color in zip(animation["Frames"], expected_colors):
                with Image.open(io.BytesIO(archive.read(relative))) as image:
                    self.assertEqual(image.size, (8, 12))
                    self.assertEqual(image.convert("RGBA").getpixel((0, 0)), color)
            self.assertIn("未填写动作", archive.read("README.txt").decode("utf-8"))
            self.assertFalse(any(name.lower().endswith(".dll") for name in archive.namelist()))
            self.assertEqual(archive.testzip(), None)
        self.assertEqual(hashlib.sha256(Path(result["path"]).read_bytes()).hexdigest(), result["sha256"])

    def test_gif_and_apng_keep_native_frame_durations(self):
        images = [Image.new("RGBA", (8, 8), color) for color in ("red", "blue")]
        for filename, format_name, durations in [("source.gif", "GIF", [60, 130]), ("source.png", "PNG", [5, 17])]:
            with self.subTest(filename=filename):
                source = self.root / filename
                images[0].save(source, format=format_name, save_all=True, append_images=images[1:], duration=durations, loop=0)
                self.workspace.import_action(115, "Idle", source, fps=30)
                self.assertEqual(self.project()["manifest"]["Animations"]["Idle"]["DurationsMs"], durations)

    def test_static_jpeg_and_one_png_directory_create_single_frame_actions(self):
        source = self.root / "still.jpg"
        Image.new("RGB", (11, 13), "orange").save(source)
        result = self.workspace.import_action(115, "Idle", source, fps=10)
        self.assertEqual(result["preview"]["frame_count"], 1)
        self.assertEqual(result["preview"]["duration_ms"], 100)
        self.assertTrue(Path(result["preview"]["preview_path"]).is_file())
        folder = self.root / "single"
        folder.mkdir()
        Image.new("RGBA", (8, 8), (10, 20, 30, 90)).save(folder / "one.png")
        self.workspace.import_action(115, "Walk", folder, fps=25)
        self.assertEqual(self.project()["manifest"]["Animations"]["Walk"]["DurationsMs"], [40])

    def test_fractional_frame_intervals_preserve_total_duration(self):
        self.workspace.import_action(115, "Walk", self.frames, fps=30)
        durations = self.project()["manifest"]["Animations"]["Walk"]["DurationsMs"]
        self.assertEqual(durations, [33, 34, 33])
        self.assertEqual(sum(durations), 100)

    def test_failed_action_save_keeps_all_previous_actions_export_and_frame_files(self):
        self.workspace.import_action(115, "Idle", self.frames)
        previous = self.project()
        package = self.workspace.export(115)
        package_bytes = Path(package["path"]).read_bytes()
        with patch.object(module, "_atomic_json", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.workspace.import_action(115, "Idle", self.frames, fps=20)
        self.assertEqual(self.project(), previous)
        self.assertEqual(Path(package["path"]).read_bytes(), package_bytes)
        role_folder = self.root / "workspace/hero-115"
        self.assertEqual(len(list((role_folder / "animations").glob("*/*.png"))), 3)
        self.assertFalse(list(role_folder.glob(".action-*")))

    def test_remove_action_keeps_other_action_and_previous_export(self):
        self.workspace.import_action(115, "Idle", self.frames)
        self.workspace.import_action(115, "Walk", self.frames)
        old_package = self.workspace.export(115)
        result = self.workspace.remove_action(115, "idle")
        self.assertEqual([row["action"] for row in result["actions"]], ["Walk"])
        self.assertEqual(len(list((self.root / "workspace/hero-115/animations").glob("*/*.png"))), 3)
        self.assertTrue(Path(old_package["path"]).is_file())
        new_package = self.workspace.export(115)
        self.assertNotEqual(new_package["path"], old_package["path"])

    def test_failed_remove_preserves_project_and_frames(self):
        self.workspace.import_action(115, "Idle", self.frames)
        previous = self.project()
        with patch.object(module, "_atomic_json", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                self.workspace.remove_action(115, "Idle")
        self.assertEqual(self.project(), previous)
        self.assertEqual(len(list((self.root / "workspace/hero-115/animations").glob("*/*.png"))), 3)

    def test_failed_export_preserves_existing_package_and_removes_temporary_zip(self):
        self.workspace.import_action(115, "Idle", self.frames)
        package = self.workspace.export(115)
        original = Path(package["path"]).read_bytes()
        with patch.object(zipfile.ZipFile, "write", side_effect=OSError("simulated packaging failure")):
            with self.assertRaises(OSError):
                self.workspace.export(115)
        self.assertEqual(Path(package["path"]).read_bytes(), original)
        self.assertFalse(list((self.root / "workspace/exports").glob(".export-*")))

    def test_corrupt_or_missing_frames_cannot_export_over_existing_package(self):
        self.workspace.import_action(115, "Idle", self.frames)
        package = self.workspace.export(115)
        previous = Path(package["path"]).read_bytes()
        frame = Path(self.workspace.status(115)["actions"][0]["preview_path"])
        frame.write_bytes(b"corrupt PNG")
        with self.assertRaises(Exception):
            self.workspace.export(115)
        self.assertEqual(Path(package["path"]).read_bytes(), previous)

    def test_project_limits_reject_without_changing_saved_action(self):
        self.workspace.import_action(115, "Idle", self.frames)
        previous = self.project()
        with patch.object(module, "MAX_PROJECT_PIXELS", 1):
            with self.assertRaises(ValueError):
                self.workspace.import_action(115, "Walk", self.frames)
        self.assertEqual(self.project(), previous)
        self.assertEqual(len(list((self.root / "workspace/hero-115/animations").glob("*/*.png"))), 3)

    def test_role_and_action_validation_does_not_guess_characters_or_allow_path_escape(self):
        for role_id in [0, -1, True, 2_147_483_648, "主人", "../115"]:
            with self.subTest(role_id=role_id), self.assertRaises(ValueError):
                self.workspace.import_action(role_id, "Idle", self.frames)
        for action in ["../Idle", "Idle/Walk", "", "待机", "Idle:1"]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.workspace.import_action(115, action, self.frames)
        self.workspace.import_action(115, "idle", self.frames)
        self.workspace.import_action(115, "IDLE", self.frames, fps=20)
        self.assertEqual(list(self.project()["manifest"]["Animations"]), ["Idle"])

    def test_multiple_workspace_instances_serialize_imports_for_the_same_root(self):
        another = module.CharacterAnimationWorkspace(self.root / "workspace")
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(workspace.import_action, 115, action, self.frames)
                       for workspace, action in [(self.workspace, "Idle"), (another, "Walk"), (self.workspace, "Fight")]]
            for future in futures:
                future.result()
        self.assertEqual({row["action"] for row in another.status(115)["actions"]}, {"Idle", "Walk", "Fight"})

    def test_empty_project_cannot_be_exported_as_a_supported_animation(self):
        self.assertEqual(self.workspace.status()["actions"], [])
        with self.assertRaisesRegex(ValueError, "真实图片帧"):
            self.workspace.export(115)

    def test_video_reuses_checked_decoder_and_keeps_declared_timing(self):
        source = self.root / "movie.mp4"
        source.write_bytes(b"isolated video fixture")
        images = [Image.new("RGBA", (16, 16), color) for color in ("red", "blue")]
        with patch.object(module, "detect_emote_media", return_value="video"), \
                patch.object(module, "adapt_video_source", return_value=(images, {"duration_ms": 200})) as adapt:
            result = self.workspace.import_action(115, "Fight", source, fps=10, loop=False)
        self.assertEqual(result["preview"]["duration_ms"], 200)
        self.assertEqual(adapt.call_args.args[1], {"width": 512, "height": 512, "fps": 10.0})
        self.assertFalse(self.project()["manifest"]["Animations"]["Fight"]["Loop"])

    def test_playable_preview_preserves_timing_and_does_not_write_project_or_frames(self):
        self.workspace.import_action(115, "Idle", self.frames, fps=20, loop=False)
        project_path = self.root / "workspace/hero-115/project.json"
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in project_path.parent.rglob("*") if path.is_file()}
        preview = self.workspace.preview(115, "Idle")
        self.assertEqual(preview["durations"], [50, 50, 50])
        self.assertEqual(preview["original_frame_count"], 3)
        self.assertEqual(len(preview["frames"]), 3)
        self.assertTrue(all(frame.startswith("data:image/png;base64,") for frame in preview["frames"]))
        self.assertFalse(preview["loop"])
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in project_path.parent.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_preview_sampling_preserves_full_duration_and_rejects_changes_during_read(self):
        folder = self.root / "many-frames"
        folder.mkdir()
        for index in range(129):
            Image.new("RGBA", (2, 2), (index, 0, 0, 255)).save(folder / f"{index:04d}.png")
        self.workspace.import_action(115, "Walk", folder, fps=10)
        preview = self.workspace.preview(115, "Walk")
        self.assertEqual(preview["original_frame_count"], 129)
        self.assertEqual(len(preview["frames"]), 120)
        self.assertEqual(sum(preview["durations"]), 12900)
        self.assertTrue(preview["truncated"])
        original_preview = module._preview
        frame = Path(self.workspace.status(115)["actions"][0]["preview_path"])

        def changed_preview(*args, **kwargs):
            result = original_preview(*args, **kwargs)
            Image.new("RGBA", (2, 2), "yellow").save(frame)
            return result

        with patch.object(module, "_preview", side_effect=changed_preview):
            with self.assertRaisesRegex(ValueError, "变化"):
                self.workspace.preview(115, "Walk")


if __name__ == "__main__":
    unittest.main()
