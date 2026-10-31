"""整批 GIF 的映射、内容绑定与单次提交边界。"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from astral_party_auto.modkit import emote_batch as batch
from astral_party_auto.modkit import emote_workspace as emotes


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class BatchController:
    def __init__(self, root):
        self.root = root
        self.original = root / "game"
        self.original.mkdir()
        self.game_executable = self.original / "game.exe"
        self.game_executable.write_bytes(b"fixture executable")
        self.aa_dirs = (self.original,)
        self._index_source_key = "fixture-index"
        self.pack = root / "draft"
        self.pack.mkdir()
        self.data = root / "data"
        self.data.mkdir()
        self.meta = self.data / "draft_pack.json"
        self.draft_items = []
        self.draft_name = "原有作品"
        self.selection = {"preserve": "current selection"}
        self.logs = []
        self.log = self.logs.append
        self.save_count = 0
        self.typed_index = {"texture": {"textures.bundle": [
            "UT_Item_Emoji_102_01", "UT_Item_Emoji_102_02", "UT_Item_Emoji_102_09", "UT_Item_Emoji_117_01"]}}
        self._save_draft()

    def _require_game(self):
        pass

    def _draft_dir(self):
        return self.pack

    def _save_draft(self):
        self.save_count += 1
        write_json(self.meta, {"name": self.draft_name, "items": self.draft_items})

    def original_bundle_path(self, name):
        path = self.original / name
        return path if path.is_file() else None

    def preview_bundle(self, path, name, *, tag, force=False):
        destination = self.data / (name + ".png")
        Image.new("RGBA", (3, 3), "white").save(destination)
        return destination, None


class EmoteBatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jixing-emote-batch-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.controller = BatchController(self.root)
        self.workspace = emotes.EmoteWorkspace(self.controller, self.controller.data, self.controller.meta)
        self.controller.emote_workspace = self.workspace
        self.catalog = self.controller.original / "catalog_3.json"
        self.catalog.write_text("official catalog", encoding="utf-8")
        self.catalog.with_suffix(".hash").write_text("official-version", encoding="ascii")
        config = self.controller.original / "config.bundle"
        records = [{"character_id": 102, "emote_id": 120102001, "video_name": ""},
                   {"character_id": 102, "emote_id": 120102002, "video_name": ""},
                   {"character_id": 102, "emote_id": 120102009, "video_name": "VEmoji_102_09"},
                   {"character_id": 117, "emote_id": 120117001, "video_name": ""}]
        write_json(config, {"records": records, "other_fields": "do not change"})
        (self.controller.original / "material.bundle").write_bytes(b"verified native material")
        (self.controller.original / "textures.bundle").write_bytes(b"original static textures")
        self.context = {"config_bundle": "config.bundle", "config_path": config, "records": records,
                        "videos": {"VEmoji_102_09": ["material.bundle"]},
                        "video_types": {"VEmoji_102_09": "UnityEngine.Material"}, "source_key": "fixture-source"}
        self.workspace.context = lambda: self.context
        self.workspace.require_independent = mock.Mock()
        self.workspace.independent_support = lambda: {"supported": True, "reason": ""}
        self.workspace.video_bundle = lambda name: ("material.bundle", self.controller.original / "material.bundle")
        self.edit = batch.EmoteBatchWorkspace(self.controller)

        def replace_config(source, output, identifier, video_name):
            content = read_json(source)
            next(row for row in content["records"] if row["emote_id"] == identifier)["video_name"] = video_name
            write_json(output, content)

        self.prepare = mock.Mock(side_effect=self.convert)
        for patch in (mock.patch.object(batch, "active_catalog_path", return_value=self.catalog),
                      mock.patch.object(batch, "prepare_material_emote", self.prepare),
                      mock.patch.object(emotes, "replace_emote_config_bundle", side_effect=replace_config),
                      mock.patch.object(emotes.EmoteWorkspace, "_validate_independent_material", return_value=None)):
            patch.start()
            self.addCleanup(patch.stop)
        self.first = self.gif("120102001.gif")
        self.second = self.gif("120102002.gif", colors=("green", "yellow"))

    def gif(self, name, colors=("red", "blue")):
        path = self.root / name
        frames = [Image.new("RGBA", (4, 4), color) for color in colors]
        frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:], duration=100,
                       loop=0, disposal=2, optimize=False)
        return path

    def convert(self, source, reference, template, output, *, emote_id, work_dir):
        write_json(output, {"emote_id": emote_id, "gif": hashlib.sha256(Path(source).read_bytes()).hexdigest()})
        return {"path": Path(output)}

    def inspect(self, paths=None):
        return self.edit.inspect(paths or [self.first, self.second])

    def mappings(self, inspection):
        return [{"source_id": row["source_id"], "emote_id": row["suggested_emote_id"]}
                for row in inspection["sources"]]

    def draft_state(self):
        return {"items": copy.deepcopy(self.controller.draft_items), "meta": self.controller.meta.read_bytes(),
                "files": {path.name: path.read_bytes() for path in self.controller.pack.iterdir()}}

    def game_state(self):
        return {path.name: path.read_bytes() for path in self.controller.original.iterdir()}

    def test_inspection_reports_real_gif_and_only_static_targets_without_role_hardcoding(self):
        result = self.inspect()
        self.assertEqual([source["suggested_emote_id"] for source in result["sources"]], [120102001, 120102002])
        self.assertEqual(result["sources"][0]["frames"], 2)
        self.assertEqual(result["sources"][0]["duration_ms"], 200)
        targets = {target["emote_id"] for target in result["targets"]}
        self.assertEqual(targets, {120102001, 120102002, 120117001})
        self.assertNotIn(120102009, targets)
        self.assertEqual(self.controller.draft_items, [])

    def test_arbitrary_filename_never_guesses_expression_or_character(self):
        path = self.gif("芬妮_伤心.gif")
        result = self.inspect([path])
        self.assertIsNone(result["sources"][0]["suggested_emote_id"])
        mapping = [{"source_id": result["sources"][0]["source_id"], "emote_id": 120102001}]
        self.assertEqual(self.edit.commit(result["token"], mapping)["count"], 1)

    def test_texture_name_can_identify_exact_target(self):
        result = self.inspect([self.gif("UT_Item_Emoji_102_01.gif")])
        self.assertEqual(result["sources"][0]["suggested_emote_id"], 120102001)

    def add_installed_material(self, name="VEmoji_102_05_999"):
        (self.controller.original / "installed-custom.bundle").write_bytes(b"other installed custom material")
        self.context["videos"][name] = ["installed-custom.bundle"]
        self.context["video_types"][name] = "UnityEngine.Material"

    def test_same_character_custom_address_sorted_first_is_never_a_template(self):
        self.add_installed_material()
        result = self.inspect()
        self.assertTrue(all(target["template_video"] == "VEmoji_102_09" for target in result["targets"]))
        self.edit.commit(result["token"], self.mappings(result))
        self.assertTrue(all(call.args[2] == "VEmoji_102_09" for call in self.prepare.call_args_list))

    def test_only_custom_routes_in_original_records_cannot_be_used_as_native_templates(self):
        custom = "VEmoji_102_09_999"
        self.add_installed_material(custom)
        self.context["records"][2]["video_name"] = custom
        before = self.draft_state()
        with self.assertRaisesRegex(ValueError, "原生.*模板"):
            self.inspect()
        self.assertEqual(self.draft_state(), before)
        self.prepare.assert_not_called()

    def test_missing_official_catalog_dependency_does_not_fall_back_to_installed_custom(self):
        self.add_installed_material()
        self.context["videos"].pop("VEmoji_102_09")
        self.context["video_types"].pop("VEmoji_102_09")
        before = self.draft_state()
        with self.assertRaisesRegex(ValueError, "原生.*模板"):
            self.inspect()
        self.assertEqual(self.draft_state(), before)
        self.prepare.assert_not_called()

    def test_missing_official_bundle_refuses_import_instead_of_using_custom_material(self):
        self.add_installed_material()
        self.context["videos"]["VEmoji_102_09"] = ["missing-official.bundle"]
        before = self.draft_state()
        with self.assertRaisesRegex(ValueError, "资源文件.*丢失"):
            self.inspect()
        self.assertEqual(self.draft_state(), before)
        self.prepare.assert_not_called()

    def test_canonical_filename_must_agree_with_confirmed_target(self):
        result = self.inspect([self.first])
        with self.assertRaisesRegex(ValueError, "文件名"):
            self.edit.commit(result["token"], [{"source_id": result["sources"][0]["source_id"], "emote_id": 120102002}])
        self.prepare.assert_not_called()

    def test_non_gif_and_disguised_png_are_rejected(self):
        png = self.root / "source.png"
        Image.new("RGBA", (4, 4), "red").save(png)
        fake = self.root / "source.gif"
        fake.write_bytes(png.read_bytes())
        for path in (png, fake):
            with self.subTest(path=path.name), self.assertRaisesRegex(ValueError, "GIF"):
                self.inspect([path])

    def test_single_frame_and_invisible_gif_are_rejected(self):
        static = self.gif("static.gif", colors=("red",))
        invisible = self.gif("invisible.gif", colors=((255, 0, 0, 0), (0, 0, 255, 0)))
        for path in (static, invisible):
            with self.subTest(path=path.name), self.assertRaisesRegex(ValueError, "GIF"):
                self.inspect([path])

    def test_batch_file_and_size_budgets_fail_before_conversion(self):
        with self.assertRaisesRegex(ValueError, "1 到 64"):
            self.edit.inspect([])
        with self.assertRaisesRegex(ValueError, "1 到 64"):
            self.edit.inspect([self.first] * 65)
        with mock.patch.object(batch, "MAX_BATCH_SOURCE_BYTES", 1), self.assertRaisesRegex(ValueError, "总大小"):
            self.inspect()
        self.prepare.assert_not_called()

    def test_same_source_path_cannot_be_selected_twice(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            self.inspect([self.first, self.first])

    def test_duplicate_or_incomplete_mappings_never_start_conversion(self):
        result = self.inspect()
        mappings = self.mappings(result)
        invalid = [mappings[:1], [mappings[0], mappings[0]],
                   [mappings[0], {**mappings[1], "emote_id": 120102001}],
                   [{**mappings[0], "emote_id": True}, mappings[1]],
                   [{**mappings[0], "source_id": "not-selected"}, mappings[1]],
                   [{**mappings[0], "emote_id": 120102009}, mappings[1]]]
        before = self.draft_state()
        for mapping in invalid:
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                self.edit.commit(result["token"], mapping)
            self.assertEqual(self.draft_state(), before)
        self.prepare.assert_not_called()

    def test_whole_batch_preserves_other_config_fields_and_commits_live_draft_once(self):
        result = self.inspect()
        original = self.game_state()
        selection = copy.deepcopy(self.controller.selection)
        saves = self.controller.save_count
        progress = []
        committed = self.edit.commit(result["token"], self.mappings(result), progress=progress.append)
        self.assertEqual(committed["count"], 2)
        self.assertEqual(self.controller.save_count, saves + 1)
        self.assertEqual(self.controller.selection, selection)
        config = read_json(self.controller.pack / "config.bundle")
        self.assertEqual(config["other_fields"], "do not change")
        records = {row["emote_id"]: row["video_name"] for row in config["records"]}
        self.assertEqual(records[120102001], "VEmoji_102_01_999")
        self.assertEqual(records[120102002], "VEmoji_102_02_999")
        self.assertEqual(records[120102009], "VEmoji_102_09")
        self.assertEqual(records[120117001], "")
        self.assertEqual(len(read_json(self.controller.pack / "emote_addresses.json")["patches"]), 2)
        self.assertEqual({item["note"] for item in committed["items"]}, {self.first.name, self.second.name})
        self.assertEqual(self.game_state(), original)
        self.assertEqual(progress[-1]["stage"], "verify")
        self.assertFalse(list(self.controller.data.glob("emote-batch-*")))
        with self.assertRaisesRegex(ValueError, "预检"):
            self.edit.commit(result["token"], self.mappings(result))

    def test_failure_on_second_gif_leaves_original_draft_and_metadata_identical(self):
        result = self.inspect()
        before = self.draft_state()

        def fail_second(*args, **kwargs):
            if kwargs["emote_id"] == 120102002:
                raise ValueError("second conversion failed")
            return self.convert(*args, **kwargs)

        self.prepare.side_effect = fail_second
        with self.assertRaisesRegex(ValueError, "second conversion"):
            self.edit.commit(result["token"], self.mappings(result))
        self.assertEqual(self.draft_state(), before)
        self.assertFalse(list(self.controller.data.glob("emote-batch-*")))
        with self.assertRaisesRegex(ValueError, "预检"):
            self.edit.commit(result["token"], self.mappings(result))

    def test_unrelated_existing_work_is_preserved_without_rewriting_its_bundle(self):
        original = self.controller.pack / "unrelated.bundle"
        original.write_bytes(b"existing user's texture replacement")
        self.controller.draft_items = [{"kind": "texture", "bundle": original.name, "name": "unchanged image"}]
        self.controller._save_draft()
        before = (original.read_bytes(), original.stat().st_mtime_ns)
        result = self.inspect()
        self.edit.commit(result["token"], self.mappings(result))
        self.assertEqual((original.read_bytes(), original.stat().st_mtime_ns), before)
        self.assertEqual(self.controller.draft_items[0]["name"], "unchanged image")
        self.assertEqual(len(self.controller.draft_items), 3)

    def test_total_independent_address_budget_is_checked_before_generation(self):
        self.controller.draft_items = [{"kind": "emote", "independent_video": True, "emote_id": 120101000 + number}
                                       for number in range(1, 129)]
        self.controller._save_draft()
        result = self.inspect()
        with self.assertRaisesRegex(ValueError, "128"):
            self.edit.commit(result["token"], self.mappings(result))
        self.prepare.assert_not_called()

    def test_log_window_failure_cannot_turn_a_successful_commit_into_an_error(self):
        result = self.inspect()
        with mock.patch.object(self.controller, "log", side_effect=RuntimeError("window closed")):
            committed = self.edit.commit(result["token"], self.mappings(result))
        self.assertEqual(committed["count"], 2)
        self.assertEqual(len(self.controller.draft_items), 2)

    def test_temporary_cleanup_failure_does_not_misreport_saved_work(self):
        result = self.inspect()
        real_directory = batch.tempfile.TemporaryDirectory
        created = []

        class BusyDirectory:
            def __init__(self, *args, **kwargs):
                self.inner = real_directory(*args, **kwargs)
                self.name = self.inner.name
                created.append(self.inner)

            def cleanup(self):
                raise PermissionError("busy file")

        try:
            with mock.patch.object(batch.tempfile, "TemporaryDirectory", BusyDirectory):
                committed = self.edit.commit(result["token"], self.mappings(result))
        finally:
            for directory in created:
                directory.cleanup()
        self.assertEqual(committed["count"], 2)
        self.assertEqual(len(self.controller.draft_items), 2)
        self.assertTrue(any("暂存未能清理" in message for message in self.controller.logs))

    def test_final_metadata_failure_rolls_back_all_new_bundles_together(self):
        result = self.inspect()
        before = self.draft_state()
        with mock.patch.object(self.controller, "_save_draft", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.edit.commit(result["token"], self.mappings(result))
        self.assertEqual(self.draft_state(), before)

    def test_source_content_change_invalidates_preflight(self):
        result = self.inspect()
        before = self.draft_state()
        self.gif(self.first.name, colors=("black", "white"))
        with self.assertRaisesRegex(ValueError, "GIF 内容"):
            self.edit.commit(result["token"], self.mappings(result))
        self.assertEqual(self.draft_state(), before)
        self.prepare.assert_not_called()

    def test_catalog_content_or_index_change_invalidates_preflight(self):
        result = self.inspect()
        self.catalog.write_text("new official catalog", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "资源或作品集"):
            self.edit.commit(result["token"], self.mappings(result))
        result = self.inspect()
        self.controller._index_source_key = "other game index"
        with self.assertRaisesRegex(ValueError, "资源或作品集"):
            self.edit.commit(result["token"], self.mappings(result))
        self.prepare.assert_not_called()

    def test_official_hash_change_invalidates_preflight(self):
        result = self.inspect()
        self.catalog.with_suffix(".hash").write_text("new-resource-version", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "资源或作品集"):
            self.edit.commit(result["token"], self.mappings(result))
        self.prepare.assert_not_called()

    def test_stale_and_replaced_tokens_are_rejected(self):
        first = self.inspect()
        second = self.inspect()
        with self.assertRaisesRegex(ValueError, "预检"):
            self.edit.commit(first["token"], self.mappings(first))
        created = self.edit._pending["created"]
        with mock.patch.object(batch.time, "monotonic", return_value=created + batch.MAX_PENDING_SECONDS + 1):
            with self.assertRaisesRegex(ValueError, "预检"):
                self.edit.commit(second["token"], self.mappings(second))

    def test_reference_change_during_generation_refuses_commit(self):
        result = self.inspect()
        before = self.draft_state()

        def change_reference(*args, **kwargs):
            converted = self.convert(*args, **kwargs)
            (self.controller.original / "material.bundle").write_bytes(b"updated material")
            return converted

        self.prepare.side_effect = change_reference
        with self.assertRaisesRegex(ValueError, "模板"):
            self.edit.commit(result["token"], self.mappings(result))
        self.assertEqual(self.draft_state(), before)

    def test_reference_content_is_bound_to_preflight_even_when_catalog_does_not_change(self):
        result = self.inspect()
        before = self.draft_state()
        (self.controller.original / "material.bundle").write_bytes(b"different native template")
        with self.assertRaisesRegex(ValueError, "模板内容"):
            self.edit.commit(result["token"], self.mappings(result))
        self.prepare.assert_not_called()
        self.assertEqual(self.draft_state(), before)

    def test_concurrent_external_draft_edit_is_preserved_when_batch_stops(self):
        result = self.inspect()

        def edit_draft(*args, **kwargs):
            converted = self.convert(*args, **kwargs)
            if kwargs["emote_id"] == 120102001:
                self.controller.draft_items.append({"kind": "texture", "bundle": "outside.bundle", "name": "new user work"})
                (self.controller.pack / "outside.bundle").write_bytes(b"user's concurrent edit")
                self.controller._save_draft()
            return converted

        self.prepare.side_effect = edit_draft
        with self.assertRaisesRegex(ValueError, "作品集发生变化"):
            self.edit.commit(result["token"], self.mappings(result))
        self.assertEqual(self.controller.draft_items[-1]["name"], "new user work")
        self.assertEqual((self.controller.pack / "outside.bundle").read_bytes(), b"user's concurrent edit")
        self.assertFalse(list(self.controller.pack.glob("jixing_emote_*.bundle")))

    def test_preview_exposes_only_authorized_source_and_verified_static_target(self):
        result = self.inspect()
        source = result["sources"][0]
        preview = self.edit.preview(result["token"], source["source_id"], 120102001)
        self.assertEqual(Path(preview["source_path"]), self.first)
        self.assertTrue(Path(preview["original_path"]).is_file())
        for source_id, identifier in (("not-authorized", 120102001), (source["source_id"], 120102009),
                                      (source["source_id"], True)):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.edit.preview(result["token"], source_id, identifier)

    def test_changed_static_image_after_preview_requires_fresh_mapping_review(self):
        result = self.inspect()
        self.edit.preview(result["token"], result["sources"][0]["source_id"], 120102001)
        (self.controller.original / "textures.bundle").write_bytes(b"different expression image")
        with self.assertRaisesRegex(ValueError, "原静态表情"):
            self.edit.commit(result["token"], self.mappings(result))
        self.prepare.assert_not_called()

    def test_original_preview_is_refreshed_instead_of_reusing_a_stat_based_cover(self):
        result = self.inspect()
        cached = self.controller.data / "cached-cover.png"
        Image.new("RGBA", (3, 3), "white").save(cached)

        def render(_path, _name, *, tag, force=False):
            if force:
                Image.new("RGBA", (3, 3), "blue").save(cached)
            return cached, None

        self.controller.preview_bundle = render
        preview = self.edit.preview(result["token"], result["sources"][0]["source_id"], 120102001)
        with Image.open(preview["original_path"]) as image:
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 255, 255))

    def test_changed_source_during_generation_leaves_live_draft_unchanged(self):
        result = self.inspect()
        before = self.draft_state()

        def change_gif(*args, **kwargs):
            converted = self.convert(*args, **kwargs)
            if kwargs["emote_id"] == 120102002:
                self.gif(self.first.name, colors=("purple", "orange"))
            return converted

        self.prepare.side_effect = change_gif
        with self.assertRaisesRegex(ValueError, "GIF 内容变化"):
            self.edit.commit(result["token"], self.mappings(result))
        self.assertEqual(self.draft_state(), before)


if __name__ == "__main__":
    unittest.main()
