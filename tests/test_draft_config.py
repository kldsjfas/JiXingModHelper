from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astral_party_auto.modkit import draft_config


def obj(path_id, kind, name, data):
    return SimpleNamespace(assets_file=SimpleNamespace(name="CAB-config"), path_id=path_id,
                           type=SimpleNamespace(name=kind), peek_name=lambda: name, get_raw_data=lambda: data)


def bundle(card=b"card", language=b"language", character=b"character"):
    return SimpleNamespace(objects=[obj(1, "AssetBundle", "", b"bundle"), obj(2, "TextAsset", "Card", card),
                                    obj(3, "TextAsset", "Character", character), obj(4, "TextAsset", "Language", language)])


class ConfigVersionTests(unittest.TestCase):
    def matches(self, draft, original):
        environments = {Path("draft"): draft, Path("original"): original}
        with patch.object(draft_config, "_load_bundle", side_effect=lambda path: environments[Path(path)]):
            return draft_config.config_matches_game(Path("draft"), Path("original"))

    def test_tool_edited_tables_do_not_make_a_draft_outdated(self):
        self.assertTrue(self.matches(bundle(card=b"card with animation", character=b"emote"), bundle()))

    def test_any_other_game_data_change_marks_the_draft_outdated(self):
        self.assertFalse(self.matches(bundle(), bundle(language=b"updated language")))

    def test_added_or_removed_objects_mark_the_draft_outdated(self):
        updated = bundle()
        updated.objects.append(obj(5, "TextAsset", "NewTable", b"new"))
        self.assertFalse(self.matches(bundle(), updated))


class CurrentDraftConfigTests(unittest.TestCase):
    def test_missing_draft_uses_the_original_and_unknown_edits_keep_the_draft(self):
        with patch.object(draft_config, "config_matches_game", return_value=False) as matches:
            original = Path(__file__)
            self.assertEqual(draft_config.current_draft_config([], "config.bundle", Path("missing.bundle"), original, Path(".")),
                             original)
            items = [{"kind": "text", "bundle": "config.bundle", "name": "Language"}]
            self.assertEqual(draft_config.current_draft_config(items, "config.bundle", original, Path("x"), Path(".")), original)
            matches.assert_not_called()



class InstallRefreshTests(unittest.TestCase):
    """安装前的版本核对不能依赖表情目录；只做动态卡面的作品集也必须重建过时配置。"""

    def setUp(self):
        from contextlib import contextmanager
        from tempfile import TemporaryDirectory
        from unittest.mock import Mock

        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.draft_dir = root / "draft"
        self.draft_dir.mkdir()
        self.original = root / "original.bundle"
        self.original.write_bytes(b"current game")
        (self.draft_dir / "config.bundle").write_bytes(b"old game")

        @contextmanager
        def prepare_folder(prefix):
            folder = root / prefix
            folder.mkdir()
            yield folder

        workspace = Mock(data_dir=root / "data")
        workspace.context.side_effect = ValueError("表情视频资源尚未下载")
        workspace._prepare_folder.side_effect = prepare_folder
        self.controller = SimpleNamespace(
            draft_items=[{"kind": "visual_animation", "bundle": "config.bundle", "name": "UT_HandCard_1",
                          "tables": ["Card"], "movieclip_url": "ui://xuaw6o8jjmone"},
                         {"kind": "texture", "bundle": "image.bundle", "name": "UT_HandCard_1"}],
            emote_workspace=workspace, log=Mock(), _draft_dir=lambda: self.draft_dir,
            original_bundle_path=lambda name: self.original if name == "config.bundle" else None)

    def tearDown(self):
        self.temp.cleanup()

    def refresh(self, rebuilt):
        from astral_party_auto.mod_controller import ModController

        with patch.object(draft_config, "config_matches_game", return_value=False),                 patch.object(draft_config, "rebuild_draft_config", side_effect=rebuilt):
            ModController._refresh_draft_config(self.controller)

    def test_visual_only_draft_is_rebuilt_without_the_emote_catalog(self):
        def rebuilt(items, name, original, folder):
            self.assertEqual((name, original), ("config.bundle", self.original))
            output = folder / "rebuilt.bundle"
            output.write_bytes(b"current game + animation")
            return output

        self.refresh(rebuilt)
        commit = self.controller.emote_workspace._commit
        commit.assert_called_once()
        staged, items = commit.call_args.args
        self.assertEqual(list(staged), ["config.bundle"])
        self.assertEqual(items, self.controller.draft_items)
        self.controller.emote_workspace.context.assert_not_called()

    def test_failed_rebuild_stops_the_install(self):
        def rebuilt(*_):
            raise ValueError("表情编号已不存在")

        with self.assertRaisesRegex(RuntimeError, "自动重建失败"):
            self.refresh(rebuilt)
        self.controller.emote_workspace._commit.assert_not_called()

if __name__ == "__main__":
    unittest.main()
