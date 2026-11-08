"""Original character references keep raw clips separate from runtime action bindings."""
from __future__ import annotations

import os
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from astral_party_auto import mod_controller
from astral_party_auto.modkit import clip_atlas, clip_frame_export, clip_preview
from astral_party_auto.web_app import DesktopApi


class CharacterComparisonApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-character-comparison-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bundles = {}
        for name in ("default304.bundle", "walk304.bundle", "skin304.bundle", "hero101.bundle", "monster.bundle"):
            path = self.root / name
            path.write_bytes(b"isolated animation source")
            self.bundles[name] = path
        self.index = {"anim": {
            "default304.bundle": ["Idle", "Fight-attack-effect"], "walk304.bundle": ["Walk"],
            "skin304.bundle": ["Fight-attack-effect", "Cry"], "hero101.bundle": ["Walk", "Fight-attack-effect"],
            "monster.bundle": ["MonsterOnly"],
        }}
        self.locations = [
            ("Hero304_PC", "Core.CharacterAsset", {"default304.bundle", "walk304.bundle"}),
            ("Hero304_Walk_PC", "UnityEngine.AnimationClip", {"walk304.bundle"}),
            ("Hero304_02_PC", "Core.CharacterAsset", {"skin304.bundle"}),
            ("Hero101_PC", "Core.CharacterAsset", {"hero101.bundle"}),
            ("Monster304_PC", "Core.CharacterAsset", {"monster.bundle"}),
        ]
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._animation_preview = Mock(return_value={
            "frames": ["first", "second"], "durations": [100, 200], "total": 2,
            "runtime_action": "Fight-effect", "runtime_binding_reason": "", "sample_rate": 30, "loop": True,
        })
        self.api._clear_replacement = Mock()
        self.api._clear_emote_candidate = Mock()
        self.api._clear_visual_candidate = Mock()
        self.api.controller = Mock()
        self.api.controller.aa_dirs = (self.root,)
        self.api.controller.typed_index = self.index
        self.api.controller.original_bundle_path.side_effect = self.bundles.get
        self.api.controller.selection = {"asset_type": "texture", "bundle": "personal.bundle", "name": "saved portrait"}
        self.api.controller.draft_items = [{"kind": "texture", "bundle": "personal.bundle", "name": "saved portrait"}]
        self.original_selection = deepcopy(self.api.controller.selection)
        self.original_draft = deepcopy(self.api.controller.draft_items)
        patcher = patch.object(clip_frame_export, "_character_locations", return_value=self.locations)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_selection_preserved(self):
        self.assertEqual(self.api.controller.selection, self.original_selection)
        self.assertEqual(self.api.controller.draft_items, self.original_draft)
        self.api.controller.set_selection.assert_not_called()
        self.api._clear_replacement.assert_not_called()
        self.api._clear_emote_candidate.assert_not_called()
        self.api._clear_visual_candidate.assert_not_called()

    def clip_binding(self, source, name):
        runtime_action = "Fight-effect" if name == "Fight-attack-effect" else name
        return {"playable": True, "runtime_action": runtime_action, "runtime_binding_reason": ""}

    def test_original_choices_keep_all_raw_actions_and_prefer_verified_skin_source(self):
        result = self.api.character_animation_originals(304, "Fight-attack-effect", "skin304.bundle")
        self.assertTrue(result["ok"], result.get("error"))
        data = result["data"]
        self.assertEqual({row["action"] for row in data["action_choices"]}, {"Idle", "Walk", "Fight-attack-effect", "Cry"})
        self.assertNotIn("Fight-effect", {row["action"] for row in data["action_choices"]})
        self.assertEqual(data["items"][0]["bundle"], "skin304.bundle")
        self.assertEqual({row["name"] for row in data["items"]}, {"Fight-attack-effect"})
        self.assertTrue(all(row["role_id"] == 304 for row in data["items"]))
        self.assertEqual(data["reason"], "")
        self.assert_selection_preserved()

    def test_preferred_bundle_from_another_role_does_not_add_foreign_candidates(self):
        result = self.api.character_animation_originals(304, "Walk", "hero101.bundle")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual([row["bundle"] for row in result["data"]["items"]], ["walk304.bundle"])
        self.assert_selection_preserved()

    def test_runtime_alias_fallback_finds_exact_sprite_binding_and_keeps_raw_preview_names(self):
        with patch.object(clip_preview, "inspect_clip_preview", side_effect=self.clip_binding):
            result = self.api.character_animation_originals(304, "Fight-effect", "skin304.bundle")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual([row["bundle"] for row in result["data"]["items"]], ["skin304.bundle", "default304.bundle"])
        self.assertTrue(all(row["action"] == "Fight-attack-effect" and row["name"] == "Fight-attack-effect"
                            for row in result["data"]["items"]))
        self.assert_selection_preserved()

    def test_runtime_alias_binding_cache_reuses_unchanged_sources_and_invalidates_mtime(self):
        with patch.object(clip_preview, "inspect_clip_preview", side_effect=self.clip_binding) as inspect:
            first = self.api.character_animation_originals(304, "Fight-effect")
            first_calls = inspect.call_count
            second = self.api.character_animation_originals(304, "Fight-effect")
            self.assertEqual(first, second)
            self.assertEqual(inspect.call_count, first_calls)
            source = self.bundles["default304.bundle"]
            stamp = source.stat()
            os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000_000))
            self.api.character_animation_originals(304, "Fight-effect")
            self.assertGreater(inspect.call_count, first_calls)
        self.assert_selection_preserved()

    def test_runtime_alias_binding_cache_invalidates_changed_size_and_drops_old_binding(self):
        changed = False
        def binding(source, name):
            if changed and Path(source).name == "default304.bundle":
                return {"playable": True, "runtime_action": "ChangedAction"}
            return self.clip_binding(source, name)
        with patch.object(clip_preview, "inspect_clip_preview", side_effect=binding):
            first = self.api.character_animation_originals(304, "Fight-effect")
            self.assertEqual(len(first["data"]["items"]), 2)
            changed = True
            self.bundles["default304.bundle"].write_bytes(b"updated source with different size")
            second = self.api.character_animation_originals(304, "Fight-effect")
        self.assertEqual([row["bundle"] for row in second["data"]["items"]], ["skin304.bundle"])

    def test_runtime_binding_cache_stays_bounded_when_old_sources_fill_it(self):
        self.api._character_clip_bindings = {("old-source", index): "OldAction" for index in range(256)}
        with patch.object(clip_preview, "inspect_clip_preview", side_effect=self.clip_binding):
            result = self.api.character_animation_originals(304, "Fight-effect")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(len(result["data"]["items"]), 2)
        self.assertLessEqual(len(self.api._character_clip_bindings), 256)
        self.assert_selection_preserved()

    def test_uncertain_binding_does_not_guess_an_original_runtime_alias(self):
        with patch.object(clip_preview, "inspect_clip_preview", return_value={
            "playable": True, "runtime_action": None, "runtime_binding_reason": "多个精灵动作前缀",
        }):
            result = self.api.character_animation_originals(304, "Fight-effect")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["items"], [])
        self.assertTrue(result["data"]["reason"])
        self.assert_selection_preserved()

    def test_original_preview_verifies_role_raw_action_and_bundle_and_keeps_current_selection(self):
        result = self.api.character_animation_original_preview(304, "Fight-attack-effect", "skin304.bundle")
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["runtime_action"], "Fight-effect")
        self.api._animation_preview.assert_called_once_with(self.bundles["skin304.bundle"], "Fight-attack-effect", "sprite_clip")
        self.assert_selection_preserved()

    def test_foreign_role_wrong_raw_action_and_path_escape_are_rejected_before_preview(self):
        for role, action, bundle in [(304, "Fight-attack-effect", "hero101.bundle"),
                                     (101, "Fight-attack-effect", "default304.bundle"),
                                     (304, "Fight-effect", "default304.bundle"),
                                     (304, "Fight-attack-effect", "../default304.bundle")]:
            with self.subTest(role=role, action=action, bundle=bundle):
                result = self.api.character_animation_original_preview(role, action, bundle)
                self.assertFalse(result["ok"])
                self.assertIn("不属于", result["error"])
        self.api._animation_preview.assert_not_called()
        self.api.controller.original_bundle_path.assert_not_called()
        self.assert_selection_preserved()

    def test_missing_original_source_is_reported_without_changing_selection(self):
        self.api.controller.original_bundle_path.side_effect = lambda _bundle: None
        result = self.api.character_animation_original_preview(304, "Fight-attack-effect", "default304.bundle")
        self.assertFalse(result["ok"])
        self.assertIn("缺失", result["error"])
        self.api._animation_preview.assert_not_called()
        self.assert_selection_preserved()

    def test_controller_preserves_runtime_alias_and_raw_clip_metadata(self):
        controller = object.__new__(mod_controller.ModController)
        controller.original_bundle_path = Mock(return_value=self.bundles["default304.bundle"])
        controller.bundle_path = Mock(return_value=self.bundles["default304.bundle"])
        info = {"playable": True, "runtime_action": "Fight-effect", "runtime_binding_reason": "",
                "duration": 3.4, "total": 75, "width": 128, "height": 160, "sample_rate": 30, "loop": True}
        with patch.object(clip_preview, "inspect_clip_preview", return_value=info), \
                patch.object(clip_atlas, "inspect_clip_atlas", return_value={"atlas_name": ""}), \
                patch.object(mod_controller, "find_anim_preview_texture", return_value=None):
            selection = controller.set_selection("default304.bundle", "Fight-attack-effect", asset_type="anim")
        self.assertTrue(selection["playable"])
        self.assertEqual(selection["name"], "Fight-attack-effect")
        self.assertEqual(selection["clip_runtime_action"], "Fight-effect")
        self.assertEqual(selection["clip_runtime_binding_reason"], "")
        self.assertEqual(selection["clip_sample_rate"], 30)
        self.assertTrue(selection["clip_loop"])


if __name__ == "__main__":
    unittest.main()
