"""通过正常公开 API 验证角色动画制作与现有资源草稿互相独立。"""
from __future__ import annotations

import base64
import io
import json
import threading
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

import astral_party_auto.web_app as web_module


class CharacterAnimationApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-character-api-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = patch.object(web_module, "MADE_DIR", self.root / "made")
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.api = object.__new__(web_module.DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._pick_path = Mock()
        self.api.controller = Mock()
        self.api.controller.selection = {"asset_type": "texture", "bundle": "existing.bundle", "name": "existing"}
        self.api.controller.draft_items = [{"kind": "texture", "bundle": "existing.bundle", "name": "existing"}]
        self.api._replacement_path = self.root / "existing-replacement.png"
        self.api._emote_replacement = {"candidate": "existing-emote"}
        self.api._visual_candidate = {"candidate": "existing-visual"}
        self.api._clear_replacement = Mock()
        self.api._clear_emote_candidate = Mock()
        self.api._clear_visual_candidate = Mock()
        self.original_state = deepcopy(self.api.controller.selection)
        self.original_draft = deepcopy(self.api.controller.draft_items)
        self.png = self.root / "source.png"
        Image.new("RGBA", (13, 17), (20, 30, 40, 99)).save(self.png)
        self.api._pick_path.return_value = self.png

    def assert_resource_state_preserved(self):
        self.assertEqual(self.api.controller.selection, self.original_state)
        self.assertEqual(self.api.controller.draft_items, self.original_draft)
        self.assertEqual(self.api._replacement_path, self.root / "existing-replacement.png")
        self.assertEqual(self.api._emote_replacement, {"candidate": "existing-emote"})
        self.assertEqual(self.api._visual_candidate, {"candidate": "existing-visual"})
        self.api._clear_replacement.assert_not_called()
        self.api._clear_emote_candidate.assert_not_called()
        self.api._clear_visual_candidate.assert_not_called()
        self.api.controller.assert_not_called()

    def project_path(self):
        return self.root / "made/character-animations/hero-115/project.json"

    def test_public_png_import_persists_real_frames_and_preview_is_playable(self):
        response = self.api.character_animation_import(115, "Idle", {"fps": 10, "loop": False})
        self.assertTrue(response["ok"], response.get("error"))
        self.assertTrue(self.project_path().is_file())
        row = response["data"]["actions"][0]
        self.assertEqual((row["action"], row["frame_count"], row["duration_ms"]), ("Idle", 1, 100))
        self.assertTrue(Path(row["preview_path"]).is_file())
        preview = self.api.character_animation_preview(115, "Idle")
        self.assertTrue(preview["ok"], preview.get("error"))
        self.assertEqual(preview["data"]["durations"], [100])
        self.assertFalse(preview["data"]["loop"])
        encoded = preview["data"]["frames"][0].split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            self.assertEqual(image.size, (13, 17))
            self.assertEqual(image.convert("RGBA").getpixel((0, 0)), (20, 30, 40, 99))
        self.assertEqual(self.api._pick_path.call_args.args[0], "open")
        self.assert_resource_state_preserved()

    def test_public_gif_import_and_export_keep_all_real_frames_and_native_durations(self):
        gif = self.root / "source.gif"
        images = [Image.new("RGBA", (9, 9), color) for color in ("red", "blue")]
        images[0].save(gif, save_all=True, append_images=images[1:], duration=[80, 120], loop=0)
        self.api._pick_path.return_value = gif
        imported = self.api.character_animation_import(115, "Walk", {"fps": 30, "loop": True})
        self.assertTrue(imported["ok"], imported.get("error"))
        preview = self.api.character_animation_preview(115, "Walk")
        self.assertTrue(preview["ok"], preview.get("error"))
        self.assertEqual(preview["data"]["durations"], [80, 120])
        exported = self.api.character_animation_export(115, "公开入口测试", "测试作者")
        self.assertTrue(exported["ok"], exported.get("error"))
        with zipfile.ZipFile(exported["data"]["path"]) as archive:
            manifest = json.loads(archive.read("character-skin.json"))
            self.assertEqual(manifest["Name"], "公开入口测试")
            self.assertEqual(manifest["Author"], "测试作者")
            self.assertEqual(manifest["HeroId"], 115)
            self.assertEqual(manifest["SkinItemId"], 0)
            self.assertEqual(manifest["Animations"]["Walk"]["DurationsMs"], [80, 120])
            self.assertEqual(len(manifest["Animations"]["Walk"]["Frames"]), 2)
            self.assertTrue(all(frame in archive.namelist() for frame in manifest["Animations"]["Walk"]["Frames"]))
            self.assertIn("CharacterSkinMod", archive.read("README.txt").decode("utf-8"))
            self.assertEqual(archive.testzip(), None)
        self.assert_resource_state_preserved()

    def test_public_folder_import_uses_folder_picker_and_requested_fps(self):
        folder = self.root / "sequence"
        folder.mkdir()
        for index, color in enumerate(("red", "blue", "green")):
            Image.new("RGBA", (7, 7), color).save(folder / f"{index}.png")
        self.api._pick_path.return_value = folder
        imported = self.api.character_animation_import(115, "Fight", {"fps": 30, "loop": False}, True)
        self.assertTrue(imported["ok"], imported.get("error"))
        self.api._pick_path.assert_called_once_with("folder")
        preview = self.api.character_animation_preview(115, "Fight")
        self.assertEqual(preview["data"]["durations"], [33, 34, 33])
        self.assertFalse(preview["data"]["loop"])
        self.assert_resource_state_preserved()

    def test_cancelled_import_keeps_project_frames_and_existing_resource_candidates(self):
        self.assertTrue(self.api.character_animation_import(115, "Idle", {})["ok"])
        original_project = self.project_path().read_bytes()
        self.api._pick_path.return_value = None
        response = self.api.character_animation_import(115, "Walk", {"fps": 20})
        self.assertEqual(response, {"ok": True, "data": None})
        self.assertEqual(self.project_path().read_bytes(), original_project)
        state = self.api.character_animation_state(115)
        self.assertTrue(state["ok"])
        self.assertEqual([row["action"] for row in state["data"]["actions"]], ["Idle"])
        self.assert_resource_state_preserved()

    def test_invalid_import_uses_api_error_contract_and_keeps_project_and_candidates(self):
        self.assertTrue(self.api.character_animation_import(115, "Idle", {})["ok"])
        previous = self.project_path().read_bytes()
        for action, options in [("../Walk", {}), ("Walk", {"fps": 0}), ("Walk", {"loop": "false"})]:
            with self.subTest(action=action, options=options):
                response = self.api.character_animation_import(115, action, options)
                self.assertFalse(response["ok"])
                self.assertTrue(response["error"])
                self.assertEqual(self.project_path().read_bytes(), previous)
                self.assert_resource_state_preserved()
        self.api._append_log.assert_called()

    def test_remove_only_requested_action_and_export_failure_is_reported(self):
        self.api.character_animation_import(115, "Idle", {})
        self.api.character_animation_import(115, "Walk", {})
        self.api.character_animation_import(115, "Fight", {})
        removed = self.api.character_animation_remove(115, "Walk")
        self.assertTrue(removed["ok"], removed.get("error"))
        self.assertEqual({row["action"] for row in removed["data"]["actions"]}, {"Idle", "Fight"})
        exported = self.api.character_animation_export(115)
        self.assertEqual(exported["data"]["action_count"], 2)
        missing = self.api.character_animation_preview(115, "Walk")
        self.assertFalse(missing["ok"])
        empty = self.api.character_animation_export(116)
        self.assertFalse(empty["ok"])
        self.assertIn("真实图片帧", empty["error"])
        self.assert_resource_state_preserved()

    def test_all_role_animation_operations_are_exposed(self):
        for name in ["character_animation_state", "character_animation_import", "character_animation_preview",
                     "character_animation_remove", "character_animation_export"]:
            self.assertTrue(getattr(web_module.DesktopApi, name)._exposed)


if __name__ == "__main__":
    unittest.main()
