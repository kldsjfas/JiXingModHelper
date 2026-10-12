"""独立表情不占用原视频，新增资源与目录清单跟随草稿一起回退。"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import test_emote_workspace as fixtures
from astral_party_auto.modkit import emote_addressables


class IndependentEmoteWorkspaceTests(unittest.TestCase):
    setUp = fixtures.EmoteWorkspaceTests.setUp
    tearDown = fixtures.EmoteWorkspaceTests.tearDown
    choose = fixtures.EmoteWorkspaceTests.choose
    record = fixtures.EmoteWorkspaceTests.record
    state = fixtures.EmoteWorkspaceTests.state

    def clone_video(self, source, template, replacement, output, *, emote_id):
        identity = emote_addressables.independent_emote_identity(emote_id)
        video = fixtures.read_json(source)
        video["videos"] = {identity["video_name"]: Path(replacement).read_text(encoding="utf-8")}
        fixtures.write_json(output, video)
        return identity

    def save(self):
        with patch.object(emote_addressables, "create_independent_emote_bundle", side_effect=self.clone_video):
            return self.edit.save_independent("VEmoji_101_09", self.custom)

    def test_independent_video_changes_only_selected_static_route(self):
        item = self.save()
        identity = emote_addressables.independent_emote_identity(120101008)
        self.assertTrue(item["independent_video"])
        self.assertEqual(item["video_name"], identity["video_name"])
        self.assertEqual(self.record(120101008)["video_name"], identity["video_name"])
        self.assertEqual(self.record(120101009)["video_name"], "VEmoji_101_09")
        self.assertFalse((self.controller.pack / "video9.bundle").exists())
        manifest = fixtures.read_json(self.controller.pack / "emote_addresses.json")
        self.assertEqual(manifest["format"], "jixing-emote-addresses")
        self.assertEqual(manifest["patches"][0]["emote_id"], 120101008)
        self.assertEqual(manifest["patches"][0]["bundle_size"], (self.controller.pack / identity["bundle_name"]).stat().st_size)

    def test_multiple_independent_emotes_share_template_without_sharing_video(self):
        first = self.save()
        self.choose(7)
        self.custom.write_text("another-animation", encoding="utf-8")
        second = self.save()
        self.assertNotEqual(first["video_name"], second["video_name"])
        self.assertEqual(len(fixtures.read_json(self.controller.pack / "emote_addresses.json")["patches"]), 2)
        self.edit.remove(0)
        self.assertFalse((self.controller.pack / first["extra_bundles"][0]).exists())
        self.assertTrue((self.controller.pack / second["extra_bundles"][0]).exists())
        self.assertEqual(self.record(120101008)["video_name"], "")
        self.assertEqual(self.record(120101007)["video_name"], second["video_name"])
        self.edit.remove(0)
        self.assertFalse((self.controller.pack / "emote_addresses.json").exists())
        self.assertFalse(list(self.controller.pack.glob("*.bundle")))

    def test_switching_independent_to_native_removes_its_video_and_manifest(self):
        custom = self.save()
        self.edit.save("VEmoji_101_10")
        self.assertFalse((self.controller.pack / custom["extra_bundles"][0]).exists())
        self.assertFalse((self.controller.pack / "emote_addresses.json").exists())
        self.assertEqual(self.record(120101008)["video_name"], "VEmoji_101_10")

    def test_legacy_custom_to_independent_restores_shared_original_video(self):
        self.edit.save("VEmoji_101_09", self.custom)
        self.choose(7)
        self.edit.save("VEmoji_101_09")
        self.choose(8)
        self.save()
        self.assertFalse((self.controller.pack / "video9.bundle").exists())
        self.assertEqual(fixtures.read_json(self.controller.original / "video9.bundle")["videos"]["VEmoji_101_09"], "original-nine")
        self.assertEqual(self.record(120101007)["video_name"], "VEmoji_101_09")

    def test_commit_failure_restores_address_manifest_and_new_bundle_together(self):
        self.save()
        before = self.state()
        self.choose(7)
        with patch.object(self.controller, "_save_draft", side_effect=OSError("metadata failed")):
            with self.assertRaises(OSError):
                self.save()
        self.assertEqual(self.state(), before)

    def test_clone_failure_leaves_previous_collection_unchanged(self):
        self.save()
        before = self.state()
        with patch.object(emote_addressables, "create_independent_emote_bundle", side_effect=ValueError("bad resource")):
            with self.assertRaises(ValueError):
                self.edit.save_independent("VEmoji_101_09", self.custom)
        self.assertEqual(self.state(), before)


if __name__ == "__main__":
    unittest.main()
