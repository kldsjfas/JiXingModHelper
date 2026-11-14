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


if __name__ == "__main__":
    unittest.main()
