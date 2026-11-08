"""全帧 PNG 包、真实时间线及 catalog 角色身份回归。"""
from __future__ import annotations

import io
import json
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from astral_party_auto.modkit import clip_frame_export as module
from astral_party_auto.modkit.character_animation import CharacterAnimationWorkspace
from astral_party_auto.modkit import character_animation as character_module


class ClipFrameExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-clip-frame-export-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.bundle"
        self.source.write_bytes(b"isolated source bundle")
        self.output = self.root / "Walk-frames.zip"

    def fake_clip(self, count=3):
        durations = [50.0] * count
        durations[-1] = 200.0
        info = {"width": 3, "height": 2, "total": count, "fps": count / (sum(durations) / 1000),
                "sample_rate": 30, "loop": True, "frame_names": [f"Walk_{index:05d}" for index in range(count)]}
        return [None] * count, durations, info, (100.0, -1, 1)

    def patches(self, count=3, renderer=None):
        stack = ExitStack()
        stack.enter_context(patch.object(module, "_load_bundle", return_value=object()))
        stack.enter_context(patch.object(module, "_read_clip", return_value=self.fake_clip(count)))
        if renderer is None:
            def renderer(*_args):
                for index in range(count):
                    yield Image.new("RGBA", (3, 2), (index % 256, 10, 20, 90))
        stack.enter_context(patch.object(module, "iter_clip_canvases", side_effect=renderer))
        return stack

    def test_export_uses_every_frame_instead_of_preview_sampling(self):
        with self.patches(140):
            result = module.export_clip_frames(self.source, "Walk", self.output,
                                               bundle_name="real.bundle", role_id=304,
                                               identity={"role_id": 304, "catalog_keys": ["Hero304_Walk_PC"]})
        self.assertEqual(result["frame_count"], 140)
        with zipfile.ZipFile(self.output) as archive:
            metadata = json.loads(archive.read("clip-frames.json"))
            self.assertEqual(metadata["schema"], module.FRAME_PACKAGE_SCHEMA)
            self.assertEqual(len(metadata["frames"]), 140)
            self.assertEqual(len([name for name in archive.namelist() if name.endswith(".png")]), 140)
            self.assertEqual(metadata["duration_ms"], 139 * 50 + 200)
            self.assertEqual(metadata["frames"][-1]["duration_ms"], 200)
            self.assertEqual(metadata["source_bundle"], "real.bundle")
            self.assertEqual(metadata["source_animation"], "Walk")
            self.assertEqual(metadata["role_id"], 304)
            self.assertEqual(metadata["sample_rate"], 30)
            self.assertTrue(metadata["loop"])
            self.assertEqual(metadata["canvas"]["origin_pixels"], [1, 1])
            self.assertAlmostEqual(metadata["canvas"]["pivot"][0], 1 / 3)
            payload = archive.read(metadata["frames"][-1]["path"])
            with Image.open(io.BytesIO(payload)) as image:
                self.assertEqual(image.size, (3, 2))
                self.assertEqual(image.getpixel((0, 0)), (139, 10, 20, 90))
            self.assertEqual(archive.testzip(), None)
        self.assertEqual(result["source_bundle_sha256"], module._digest(self.source))

    def test_render_failure_preserves_previous_output_and_removes_temporary_zip(self):
        self.output.write_bytes(b"existing export package")

        def failed_renderer(*_args):
            yield Image.new("RGBA", (3, 2), "red")
            raise ValueError("simulated rendering failure")

        with self.patches(renderer=failed_renderer):
            with self.assertRaisesRegex(ValueError, "rendering failure"):
                module.export_clip_frames(self.source, "Walk", self.output)
        self.assertEqual(self.output.read_bytes(), b"existing export package")
        self.assertFalse(list(self.root.glob(".clip-frames-*")))

    def test_changed_source_during_render_rejects_new_output(self):
        self.output.write_bytes(b"existing export package")

        def changed_renderer(*_args):
            for index in range(3):
                if index == 1:
                    self.source.write_bytes(b"updated game bundle")
                yield Image.new("RGBA", (3, 2), "red")

        with self.patches(renderer=changed_renderer):
            with self.assertRaisesRegex(ValueError, "发生变化"):
                module.export_clip_frames(self.source, "Walk", self.output)
        self.assertEqual(self.output.read_bytes(), b"existing export package")

    def test_incomplete_render_or_oversized_full_clip_never_becomes_sampled_export(self):
        with self.patches(renderer=lambda *_args: iter([Image.new("RGBA", (3, 2), "red")])):
            with self.assertRaisesRegex(ValueError, "不完整"):
                module.export_clip_frames(self.source, "Walk", self.output)
        self.assertFalse(self.output.exists())
        with self.patches(513):
            with self.assertRaisesRegex(ValueError, "512"):
                module.export_clip_frames(self.source, "Walk", self.output)
        self.assertFalse(self.output.exists())

    def test_export_refuses_to_overwrite_original_bundle(self):
        original = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "覆盖原始"):
            module.export_clip_frames(self.source, "Walk", self.source)
        self.assertEqual(self.source.read_bytes(), original)

    def test_effect_clip_exports_verified_runtime_action_and_keeps_raw_source_name(self):
        frames, durations, info, origin = self.fake_clip()
        info["runtime_action"] = "Fight-effect"
        info["runtime_binding_reason"] = ""
        info["frame_names"] = [f"Fight-effect_{index:05d}" for index in range(3)]
        with self.patches(), patch.object(module, "_read_clip", return_value=(frames, durations, info, origin)):
            exported = module.export_clip_frames(self.source, "Fight-attack-effect", self.output, role_id=112)
        self.assertEqual(exported["action"], "Fight-effect")
        self.assertEqual(exported["source_animation"], "Fight-attack-effect")
        self.assertEqual(exported["runtime_action"], "Fight-effect")
        extracted = self.root / "effect-frames"
        with zipfile.ZipFile(self.output) as archive:
            metadata = json.loads(archive.read("clip-frames.json"))
            self.assertEqual(metadata["action"], "Fight-effect")
            self.assertEqual(metadata["source_animation"], "Fight-attack-effect")
            archive.extractall(extracted)
        workspace = CharacterAnimationWorkspace(self.root / "character-workspace")
        workspace.import_action(metadata["role_id"], metadata["action"], extracted)
        package = workspace.export(112)
        with zipfile.ZipFile(package["path"]) as archive:
            manifest = json.loads(archive.read("character-skin.json"))
            self.assertEqual(list(manifest["Animations"]), ["Fight-effect"])

    def test_missing_runtime_action_is_explicit_without_guessing_from_clip_name(self):
        frames, durations, info, origin = self.fake_clip()
        info.update(runtime_action=None, runtime_binding_reason="片段包含多个动作前缀。")
        with self.patches(), patch.object(module, "_read_clip", return_value=(frames, durations, info, origin)):
            exported = module.export_clip_frames(self.source, "Mixed", self.output, role_id=112)
        self.assertIsNone(exported["runtime_action"])
        self.assertEqual(exported["source_animation"], "Mixed")
        self.assertIn("多个", exported["runtime_binding_reason"])

    def extracted_clip(self):
        with self.patches():
            module.export_clip_frames(self.source, "Walk", self.output, role_id=304)
        folder = self.root / "edited-frames"
        with zipfile.ZipFile(self.output) as archive:
            archive.extractall(folder)
        return folder

    def test_exported_directory_can_be_edited_and_imported_with_original_timing(self):
        folder = self.extracted_clip()
        Image.new("RGBA", (3, 2), (200, 100, 50, 75)).save(folder / "0001.png")
        frames, durations, metadata = module.read_clip_frame_directory(folder)
        try:
            self.assertEqual(durations, [50, 50, 200])
            self.assertEqual(metadata["modified_frame_count"], 1)
            self.assertEqual(frames[1].getpixel((0, 0)), (200, 100, 50, 75))
        finally:
            for frame in frames:
                frame.close()
        workspace = CharacterAnimationWorkspace(self.root / "character-workspace")
        imported = workspace.import_action(304, "Walk", folder, fps=15)
        self.assertEqual(imported["preview"]["duration_ms"], 300)
        self.assertEqual(workspace.preview(304, "Walk")["durations"], [50, 50, 200])
        self.assertEqual(workspace.export(304)["frame_count"], 3)

    def test_directory_import_rejects_path_escape_wrong_size_and_inconsistent_timing(self):
        folder = self.extracted_clip()
        metadata_path = folder / "clip-frames.json"
        original = json.loads(metadata_path.read_text(encoding="utf-8"))
        invalid = json.loads(json.dumps(original))
        invalid["frames"][0]["path"] = "../outside.png"
        metadata_path.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "路径"):
            module.read_clip_frame_directory(folder)
        invalid = json.loads(json.dumps(original))
        invalid["frames"][0]["duration_ms"] = 999
        metadata_path.write_text(json.dumps(invalid), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "总时长"):
            module.read_clip_frame_directory(folder)
        metadata_path.write_text(json.dumps(original), encoding="utf-8")
        Image.new("RGBA", (4, 2), "red").save(folder / "0001.png")
        with self.assertRaisesRegex(ValueError, "画布尺寸"):
            module.read_clip_frame_directory(folder)

    def test_native_package_uses_its_own_pixel_budget_without_raising_ordinary_source_limit(self):
        native = self.extracted_clip()
        ordinary = self.root / "ordinary"
        ordinary.mkdir()
        Image.new("RGBA", (3, 2), "red").save(ordinary / "frame.png")
        workspace = CharacterAnimationWorkspace(self.root / "character-workspace")
        with patch.object(character_module, "MAX_TOTAL_PIXELS", 1):
            imported = workspace.import_action(304, "Walk", native)
            self.assertEqual(imported["preview"]["frame_count"], 3)
            with self.assertRaisesRegex(ValueError, "总像素"):
                workspace.import_action(304, "Idle", ordinary)
        self.assertEqual([row["action"] for row in workspace.status(304)["actions"]], ["Walk"])

    def test_full_frame_package_still_enforces_native_pixel_budget(self):
        with self.patches(), patch.object(module, "MAX_CLIP_PACKAGE_PIXELS", 17):
            with self.assertRaisesRegex(ValueError, "总像素"):
                module.export_clip_frames(self.source, "Walk", self.output)
        self.assertFalse(self.output.exists())

    def resolve(self, addresses, *, target_bundle="source.bundle", action="Walk"):
        catalog_path = self.root / "catalog.json"
        catalog = {"m_InternalIds": ["{RuntimePath}/StandaloneWindows64/source.bundle"],
                   "m_resourceTypes": [{"m_ClassName": "IAssetBundleResource"},
                                       {"m_ClassName": "Core.CharacterAsset"},
                                       {"m_ClassName": "UnityEngine.AnimationClip"}]}
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        buckets = [(0, "source.bundle", [0])]
        entries = [(0, 0, -1, 0, 0, 0, 0)]
        for key, kind in addresses:
            entries.append((0, 0, 0, 0, 0, 0, kind))
            buckets.append((0, key, [len(entries) - 1]))
        with patch.object(module, "active_catalog_path", return_value=catalog_path), \
                patch.object(module, "_catalog_parts", return_value=(None, None, buckets, entries)):
            return module.resolve_clip_identity((self.root,), target_bundle, action)

    def test_identity_uses_exact_hero_addresses_and_skin_variants(self):
        result = self.resolve([("Hero124_01_PC", 1), ("Hero124_01_Walk_PC", 2)])
        self.assertEqual(result["role_id"], 124)
        self.assertEqual(result["target_kind"], "hero")
        self.assertEqual(result["action"], "Walk")
        self.assertEqual(result["catalog_keys"], ["Hero124_01_PC", "Hero124_01_Walk_PC"])
        self.assertEqual(self.resolve([("Hero304_Walk_PC", 2)])["role_id"], 304)

    def test_monster_and_ambiguous_or_plain_numeric_addresses_do_not_guess_hero_id(self):
        monster = self.resolve([("Monster1020_PC", 1)])
        self.assertIsNone(monster["role_id"])
        self.assertEqual(monster["target_kind"], "monster")
        ambiguous = self.resolve([("Hero112_02_PC", 1), ("Hero124_01_PC", 1)])
        self.assertIsNone(ambiguous["role_id"])
        self.assertEqual(ambiguous["target_kind"], "ambiguous")
        self.assertIsNone(self.resolve([("304", 1)])["role_id"])
        self.assertIsNone(self.resolve([("Hero304_PC", 1)], target_bundle="different.bundle")["role_id"])

    def test_character_clip_targets_default_prefers_exact_animation_address(self):
        locations = [("Hero304_PC", "Core.CharacterAsset", {"walk.bundle"}),
                     ("Hero304_Walk_PC", "UnityEngine.AnimationClip", {"walk.bundle"})]
        with patch.object(module, "_character_locations", return_value=locations):
            targets = module.find_character_clip_targets((self.root,), 304, "Walk", typed_index={"anim": {"walk.bundle": ["Walk"]}})
        self.assertEqual(targets, [{"bundle": "walk.bundle", "name": "Walk", "role_id": 304,
                                    "action": "Walk", "address": "Hero304_Walk_PC", "label": "默认外观"}])

    def test_character_clip_targets_include_known_variants_and_preferred_source(self):
        locations = [
            ("Hero124_02_PC", "Core.CharacterAsset", {"second.bundle"}),
            ("Hero124_Max_PC", "Core.CharacterAsset", {"max.bundle"}),
            ("Hero124_PC", "Core.CharacterAsset", {"default.bundle"}),
            ("Hero124_01_Walk_PC", "UnityEngine.AnimationClip", {"first.bundle"}),
        ]
        index = {"anim": {bundle: ["Walk"] for _address, _kind, bundles in locations for bundle in bundles}}
        with patch.object(module, "_character_locations", return_value=locations):
            targets = module.find_character_clip_targets((self.root,), 124, "Walk", typed_index=index)
            preferred = module.find_character_clip_targets((self.root,), 124, "Walk", typed_index=index, preferred_bundle="second.bundle")
        self.assertEqual([row["label"] for row in targets], ["默认外观", "外观 01", "外观 02", "Max 变体"])
        self.assertEqual(preferred[0]["bundle"], "second.bundle")
        self.assertTrue(all(row["role_id"] == 124 and row["name"] == "Walk" for row in targets))

    def test_character_clip_targets_reject_wrong_action_monsters_and_other_roles(self):
        locations = [
            ("Hero124_Walk_PC", "UnityEngine.AnimationClip", {"walk.bundle"}),
            ("Hero124_PC", "Core.CharacterAsset", {"walk.bundle"}),
            ("Monster124_PC", "Core.CharacterAsset", {"monster.bundle"}),
            ("Hero1240_PC", "Core.CharacterAsset", {"different-role.bundle"}),
        ]
        index = {"anim": {"walk.bundle": ["Walk"], "monster.bundle": ["Die"], "different-role.bundle": ["Die"]}}
        with patch.object(module, "_character_locations", return_value=locations):
            self.assertEqual(module.find_character_clip_targets((self.root,), 124, "Die", typed_index=index), [])
            self.assertEqual(module.find_character_clip_targets((self.root,), 124, "Fight", typed_index=index), [])

    def test_character_clip_targets_drop_missing_index_entries_without_guessing(self):
        locations = [("Hero304_Walk_PC", "UnityEngine.AnimationClip", {"missing.bundle"})]
        with patch.object(module, "_character_locations", return_value=locations), \
                patch.object(module, "bundle_file_map") as lookup:
            self.assertEqual(module.find_character_clip_targets((self.root,), 304, "Walk", typed_index={"anim": {}}), [])
            self.assertEqual(module.find_character_clip_targets((self.root,), 304, "Walk", typed_index={}), [])
            lookup.assert_not_called()

    def test_character_clip_targets_without_index_verify_actual_bundle_once(self):
        locations = [("Hero304_PC", "Core.CharacterAsset", {"walk.bundle"}),
                     ("Hero304_Walk_PC", "UnityEngine.AnimationClip", {"walk.bundle"})]
        with patch.object(module, "_character_locations", return_value=locations), \
                patch.object(module, "bundle_file_map", return_value={"walk.bundle": self.source}), \
                patch.object(module, "read_bundle_asset_names", return_value={"anim": ["Walk"]}) as inspect:
            targets = module.find_character_clip_targets((self.root,), 304, "Walk")
        self.assertEqual(len(targets), 1)
        inspect.assert_called_once_with(self.source)

    def test_empty_action_lists_actual_character_clips_without_generic_aliases(self):
        locations = [
            ("Hero112_PC", "Core.CharacterAsset", {"default.bundle", "walk.bundle"}),
            ("Hero112_Walk_PC", "UnityEngine.AnimationClip", {"walk.bundle"}),
            ("Hero112_02_PC", "Core.CharacterAsset", {"second.bundle"}),
            ("Monster112_PC", "Core.CharacterAsset", {"monster.bundle"}),
        ]
        index = {"anim": {"default.bundle": ["Idle", "Fight-attack", "Die"], "walk.bundle": ["Walk"],
                          "second.bundle": ["Idle", "Fight-attack", "Cry"], "monster.bundle": ["MonsterOnly"]}}
        with patch.object(module, "_character_locations", return_value=locations):
            targets = module.find_character_clip_targets((self.root,), 112, "", typed_index=index)
        names = {row["name"] for row in targets}
        self.assertEqual(names, {"Idle", "Fight-attack", "Die", "Walk", "Cry"})
        self.assertNotIn("Fight", names)
        self.assertNotIn("MonsterOnly", names)
        self.assertTrue(all(row["action"] == row["name"] and row["action_label"] for row in targets))
        walk = [row for row in targets if row["name"] == "Walk"]
        self.assertEqual(len(walk), 1)
        self.assertEqual(walk[0]["address"], "Hero112_Walk_PC")

    def test_empty_action_direct_addresses_do_not_invent_other_clips_from_dependencies(self):
        locations = [("Hero124_01_Walk_PC", "UnityEngine.AnimationClip", {"shared.bundle"})]
        with patch.object(module, "_character_locations", return_value=locations):
            targets = module.find_character_clip_targets((self.root,), 124, "", typed_index={"anim": {"shared.bundle": ["Walk", "Unknown"]}})
        self.assertEqual([row["name"] for row in targets], ["Walk"])
        self.assertEqual(targets[0]["label"], "外观 01")


if __name__ == "__main__":
    unittest.main()
