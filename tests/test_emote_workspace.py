"""复合表情草稿的引用、恢复和事务失败回归，全部在临时目录内。"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from astral_party_auto.modkit import emote_workspace as workspace
from astral_party_auto.mod_controller import ModController
import astral_party_auto.mod_controller as controller_module


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def replace_config(source, output, emote_id, video_name):
    table = read_json(source)
    record = next(row for row in table["records"] if row["emote_id"] == emote_id)
    record["video_name"] = video_name
    write_json(output, table)


def replace_video(source, name, replacement, output):
    video = read_json(source)
    video["videos"][name] = Path(replacement).read_text(encoding="utf-8")
    write_json(output, video)


def export_video(source, name, output):
    Path(output).write_text(read_json(source)["videos"][name], encoding="utf-8")


def export_pack(pack, zip_path, *, pack_name, items):
    write_json(Path(pack) / "mod_info.json", {"name": pack_name, "items": items})


class _Controller:
    def __init__(self, root):
        self.original = root / "original-game"
        self.pack = root / "draft"
        self.data_dir = root / "data"
        self.meta = self.data_dir / "draft_pack.json"
        self.aa_dirs = (self.original,)
        self.draft_items = []
        self.draft_name = "测试作品集"
        self.logs = []
        self.log = self.logs.append
        self.selection = {"asset_type": "texture", "category": "emoji", "name": "UT_Item_Emoji_101_08", "bundle": "texture.bundle"}
        self.restore_calls = []

    def _require_game(self):
        pass

    def original_bundle_path(self, name):
        path = self.original / name
        return path if path.exists() else None

    def _draft_dir(self):
        self.pack.mkdir(parents=True, exist_ok=True)
        return self.pack

    def _save_draft(self):
        write_json(self.meta, {"name": self.draft_name, "items": self.draft_items})

    def _restore_removed_draft_item(self, item, target, original):
        self.restore_calls.append(item["name"])
        current = read_json(target)
        baseline = read_json(original)
        current["other"]["texture"] = baseline["other"]["texture"]
        write_json(target, current)


class EmoteWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.controller = _Controller(self.root)
        records = [{"character_id": 101, "emote_id": 120101007, "video_name": ""},
                   {"character_id": 101, "emote_id": 120101008, "video_name": ""},
                   {"character_id": 101, "emote_id": 120101009, "video_name": "VEmoji_101_09"},
                   {"character_id": 101, "emote_id": 120101010, "video_name": "VEmoji_101_10"}]
        write_json(self.controller.original / "config.bundle", {"records": records, "other_fields": "immutable configuration"})
        for number, label in ((9, "nine"), (10, "ten")):
            write_json(self.controller.original / f"video{number}.bundle", {"videos": {f"VEmoji_101_{number:02}": f"original-{label}"}, "other": {"texture": "original icon"}})
        write_json(self.controller.original / "scripts.bundle", {"scripts": []})
        write_json(self.controller.original / "texture.bundle", {"texture": "original"})
        self.original_bytes = {path.name: path.read_bytes() for path in self.controller.original.iterdir()}
        locations = {"config_bundle": "config.bundle", "videos": {"VEmoji_101_09": ["scripts.bundle", "video9.bundle"], "VEmoji_101_10": ["scripts.bundle", "video10.bundle"]}}

        def load_video(raw, **kwargs):
            data = json.loads(raw)
            return SimpleNamespace(objects=[SimpleNamespace(type=SimpleNamespace(name="MonoBehaviour"), peek_name=lambda name=name: name) for name in data.get("videos", {})])

        patches = [mock.patch.object(workspace, "bundle_source_key", return_value="active fixture"),
                   mock.patch.object(workspace, "discover_emote_locations", return_value=locations),
                   mock.patch.object(workspace, "inspect_emote_config_bundle", side_effect=lambda path: {"asset_name": "Character", "records": read_json(path)["records"]}),
                   mock.patch.object(workspace, "replace_emote_config_bundle", side_effect=replace_config),
                   mock.patch.object(workspace, "replace_cri_emote_bundle", side_effect=replace_video),
                   mock.patch.object(workspace, "export_cri_emote_bundle", side_effect=export_video),
                   mock.patch.object(workspace, "validate_usm_file", side_effect=lambda path, *args: Path(path).read_text()),
                   mock.patch.object(workspace.UnityPy, "load", side_effect=load_video),
                   mock.patch.object(workspace, "export_mod_pack", side_effect=export_pack),
                   mock.patch.object(controller_module, "export_mod_pack", side_effect=export_pack)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.edit = workspace.EmoteWorkspace(self.controller, self.controller.data_dir, self.controller.meta)
        self.controller.emote_workspace = self.edit
        self.custom = self.root / "custom.usm"
        self.custom.write_text("custom-video-content", encoding="utf-8")

    def tearDown(self):
        self.assertEqual({path.name: path.read_bytes() for path in self.controller.original.iterdir()}, self.original_bytes,
                         "任何测试流程都不应污染真实来源的替身目录")

    def choose(self, number):
        self.controller.selection["name"] = f"UT_Item_Emoji_101_{number:02}"

    def record(self, emote_id):
        return next(row for row in read_json(self.controller.pack / "config.bundle")["records"] if row["emote_id"] == emote_id)

    def state(self):
        files = {path.name: path.read_bytes() for path in self.controller._draft_dir().iterdir() if path.is_file()}
        files["@metadata"] = self.controller.meta.read_bytes() if self.controller.meta.exists() else None
        return files, copy.deepcopy(self.controller.draft_items)

    def test_static_record_and_unsupported_texture_are_distinguished(self):
        selected = self.edit.selection("UT_Item_Emoji_101_08")
        self.assertTrue(selected["editable"])
        self.assertEqual(selected["video_name"], "")
        self.assertEqual(selected["emote_id"], 120101008)
        self.assertFalse(self.edit.selection("UT_Item_Emoji_101_99")["editable"])
        self.assertFalse(self.edit.selection("not-an-emote")["editable"])

    def test_native_to_custom_to_another_slot_restores_previous_video(self):
        item = self.edit.save("VEmoji_101_09")
        self.assertFalse(item["custom_video"])
        self.assertEqual(self.record(120101008)["video_name"], "VEmoji_101_09")
        self.assertFalse((self.controller.pack / "video9.bundle").exists())
        item = self.edit.save("VEmoji_101_09", self.custom)
        self.assertEqual(item["extra_bundles"], ["video9.bundle"])
        self.assertEqual(read_json(self.controller.pack / "video9.bundle")["videos"]["VEmoji_101_09"], "custom-video-content")
        self.edit.save("VEmoji_101_10", self.custom)
        self.assertEqual(self.record(120101008)["video_name"], "VEmoji_101_10")
        self.assertFalse((self.controller.pack / "video9.bundle").exists())
        self.assertEqual(read_json(self.controller.pack / "video10.bundle")["videos"]["VEmoji_101_10"], "custom-video-content")
        self.assertEqual(len(self.controller.draft_items), 1)

    def test_custom_back_to_native_drops_unreferenced_video_draft(self):
        self.edit.save("VEmoji_101_09", self.custom)
        self.edit.save("VEmoji_101_09")
        self.assertFalse((self.controller.pack / "video9.bundle").exists())
        self.assertFalse(self.controller.draft_items[0]["custom_video"])
        self.assertEqual(self.record(120101008)["video_name"], "VEmoji_101_09")

    def test_removing_one_shared_config_item_restores_only_its_route(self):
        self.edit.save("VEmoji_101_09", self.custom)
        self.choose(7)
        self.edit.save("VEmoji_101_10")
        self.edit.remove(1)
        self.assertEqual(self.record(120101007)["video_name"], "")
        self.assertEqual(self.record(120101008)["video_name"], "VEmoji_101_09")
        self.assertTrue((self.controller.pack / "video9.bundle").exists())
        self.edit.remove(0)
        self.assertEqual(self.controller.draft_items, [])
        self.assertFalse((self.controller.pack / "config.bundle").exists())
        self.assertFalse((self.controller.pack / "video9.bundle").exists())

    def test_custom_video_slot_cannot_be_silently_overwritten_by_another_item(self):
        self.edit.save("VEmoji_101_09", self.custom)
        before = self.state()
        self.choose(7)
        with self.assertRaisesRegex(ValueError, "占用"):
            self.edit.save("VEmoji_101_09", self.custom)
        self.assertEqual(self.state(), before)

    def test_video_and_context_require_complete_originals(self):
        with self.assertRaises(ValueError):
            self.edit.video_bundle("VEmoji_999_99")
        (self.controller.original / "scripts.bundle").unlink()
        try:
            with self.assertRaises(ValueError):
                self.edit.video_bundle("VEmoji_101_09")
        finally:
            (self.controller.original / "scripts.bundle").write_bytes(self.original_bytes["scripts.bundle"])

    def test_ordinary_item_removal_keeps_an_emotes_extra_video_bundle(self):
        self.edit.save("VEmoji_101_09", self.custom)
        ordinary = {"kind": "texture", "bundle": "other.bundle", "name": "ordinary icon"}
        self.controller.draft_items.append(ordinary)
        (self.controller.pack / "other.bundle").write_bytes(b"temporary ordinary item")
        ModController.remove_draft_item(self.controller, 1)
        self.assertTrue((self.controller.pack / "video9.bundle").exists())
        self.assertTrue((self.controller.pack / "config.bundle").exists())
        self.assertFalse((self.controller.pack / "other.bundle").exists())

    def test_removing_a_texture_in_the_same_extra_bundle_restores_texture_only(self):
        self.edit.save("VEmoji_101_09", self.custom)
        video_path = self.controller.pack / "video9.bundle"
        edited = read_json(video_path)
        edited["other"]["texture"] = "modified icon"
        write_json(video_path, edited)
        self.controller.draft_items.append({"kind": "texture", "bundle": "video9.bundle", "name": "shared icon"})
        ModController.remove_draft_item(self.controller, 1)
        self.assertTrue(video_path.exists())
        self.assertEqual(read_json(video_path)["other"]["texture"], "original icon")
        self.assertEqual(read_json(video_path)["videos"]["VEmoji_101_09"], "custom-video-content")
        self.assertEqual(self.controller.restore_calls, ["shared icon"])

    def test_failed_second_bundle_copy_rolls_back_all_state(self):
        self.edit.save("VEmoji_101_09", self.custom)
        before = self.state()
        original_copy = workspace._copy_atomic
        calls = 0

        def fail_second_commit(source, target):
            nonlocal calls
            if "emote-prepare-" in str(source.parent):
                calls += 1
                if calls == 2:
                    raise OSError("second commit failed")
            return original_copy(source, target)

        with mock.patch.object(workspace, "_copy_atomic", side_effect=fail_second_commit):
            with self.assertRaisesRegex(OSError, "second commit"):
                self.edit.save("VEmoji_101_10", self.custom)
        self.assertEqual(self.state(), before)
        self.assertFalse(list(self.controller.data_dir.glob("emote-change-*")))

    def test_metadata_write_failure_restores_files_metadata_and_memory(self):
        self.edit.save("VEmoji_101_09", self.custom)
        before = self.state()

        def broken_metadata():
            self.controller.meta.write_bytes(b"half-written metadata")
            raise OSError("metadata full")

        with mock.patch.object(self.controller, "_save_draft", side_effect=broken_metadata):
            with self.assertRaisesRegex(OSError, "metadata full"):
                self.edit.save("VEmoji_101_10", self.custom)
        self.assertEqual(self.state(), before)
        self.assertFalse(list(self.controller.data_dir.glob("emote-change-*")))

    def test_pack_metadata_failure_also_restores_draft_metadata(self):
        self.edit.save("VEmoji_101_09", self.custom)
        before = self.state()

        def broken_pack(pack, *args, **kwargs):
            (Path(pack) / "mod_info.json").write_bytes(b"half-written pack metadata")
            raise OSError("pack metadata failed")

        with mock.patch.object(workspace, "export_mod_pack", side_effect=broken_pack):
            with self.assertRaisesRegex(OSError, "pack metadata"):
                self.edit.save("VEmoji_101_10", self.custom)
        self.assertEqual(self.state(), before)

    def test_failed_initial_save_removes_new_files_and_restores_empty_items(self):
        self.controller._draft_dir()
        before = self.state()
        with mock.patch.object(self.controller, "_save_draft", side_effect=OSError("metadata failed")):
            with self.assertRaises(OSError):
                self.edit.save("VEmoji_101_09", self.custom)
        self.assertEqual(self.state(), before)

    def test_locked_temporary_cleanup_does_not_turn_a_successful_save_into_failure(self):
        with mock.patch.object(workspace.shutil, "rmtree", side_effect=PermissionError("temporary directory locked")):
            item = self.edit.save("VEmoji_101_09", self.custom)
        self.assertTrue(item["custom_video"])
        self.assertEqual(self.record(120101008)["video_name"], "VEmoji_101_09")
        self.assertEqual(read_json(self.controller.pack / "video9.bundle")["videos"]["VEmoji_101_09"], "custom-video-content")
        self.assertEqual(read_json(self.controller.meta)["items"], self.controller.draft_items)
        retained = list(self.controller.data_dir.glob("emote-change-*")) + list(self.controller.data_dir.glob("emote-prepare-*"))
        self.assertEqual(len(retained), 2)
        cleanup_logs = [line for line in self.controller.logs if "未能清理" in line]
        self.assertEqual(len(cleanup_logs), 2)
        for directory in retained:
            self.assertTrue(any(str(directory) in line for line in cleanup_logs))
        self.assertTrue(any("已保存动态表情" in line for line in self.controller.logs))

    def test_locked_cleanup_preserves_the_commit_error_and_successful_rollback(self):
        self.edit.save("VEmoji_101_09", self.custom)
        before = self.state()

        def broken_metadata():
            self.controller.meta.write_bytes(b"half-written metadata")
            raise OSError("original metadata failure")

        with mock.patch.object(self.controller, "_save_draft", side_effect=broken_metadata), mock.patch.object(workspace.shutil, "rmtree", side_effect=PermissionError("temporary directory locked")):
            with self.assertRaisesRegex(OSError, "original metadata failure") as failure:
                self.edit.save("VEmoji_101_10", self.custom)
        self.assertNotIsInstance(failure.exception, PermissionError)
        self.assertEqual(self.state(), before)
        retained = list(self.controller.data_dir.glob("emote-change-*")) + list(self.controller.data_dir.glob("emote-prepare-*"))
        self.assertEqual(len(retained), 2)
        for directory in retained:
            self.assertTrue(any(str(directory) in line and "未能清理" in line for line in self.controller.logs))
        recovery = next(directory for directory in retained if directory.name.startswith("emote-change-"))
        self.assertTrue((recovery / "recovery.json").exists())

    def test_rollback_failure_keeps_recovery_copies_and_manifest(self):
        self.edit.save("VEmoji_101_09", self.custom)
        before_files, before_items = self.state()
        original_copy = workspace._copy_atomic

        def fail_rollback(source, target):
            if "emote-change-" in str(source.parent) and target.name == "config.bundle":
                raise OSError("restore locked")
            return original_copy(source, target)

        with mock.patch.object(self.controller, "_save_draft", side_effect=OSError("metadata failed")), mock.patch.object(workspace, "_copy_atomic", side_effect=fail_rollback):
            with self.assertRaisesRegex(RuntimeError, "恢复副本"):
                self.edit.save("VEmoji_101_10", self.custom)
        recovery = list(self.controller.data_dir.glob("emote-change-*"))
        self.assertEqual(len(recovery), 1)
        manifest = read_json(recovery[0] / "recovery.json")
        config = next(row for row in manifest if Path(row["target"]).name == "config.bundle")
        self.assertEqual((recovery[0] / config["copy"]).read_bytes(), before_files["config.bundle"])
        self.assertEqual(self.controller.draft_items, before_items)
        self.assertEqual(self.controller.meta.read_bytes(), before_files["@metadata"])

    def test_item_bundle_names_tracks_only_legal_primary_and_extra_bundles(self):
        self.assertEqual(workspace.item_bundle_names({"bundle": "config.bundle", "extra_bundles": ["video.bundle", "../evil.bundle", "not-a-bundle"]}), {"config.bundle", "video.bundle"})


if __name__ == "__main__":
    unittest.main()
