"""Friendly names retain real ids and do not guess missing character mappings."""
import unittest

from astral_party_auto.modkit.emote_labels import emote_label, video_label
import test_emote_workspace as workspace_fixture


class EmoteLabelTests(unittest.TestCase):
    def test_known_names_follow_explicit_ids_including_nonconsecutive_characters(self):
        self.assertEqual(video_label("VEmoji_102_09"), "芬妮（古怪神探） · 动态表情09")
        self.assertEqual(video_label("VEmoji_103_09"), "阿兰娜（社恐修女） · 动态表情09")
        self.assertEqual(video_label("VEmoji_104_09"), "小町（暗影忍者） · 动态表情09")
        self.assertEqual(video_label("VEmoji_303_01"), "吉尔·斯汀雷（吉尔） · 动态表情01")

    def test_unknown_character_retains_number_without_another_characters_name(self):
        self.assertEqual(video_label("VEmoji_999_07"), "角色999 · 动态表情07")
        self.assertEqual(emote_label(120999007), "角色999 · 表情07")

    def test_emote_item_id_is_distinct_from_video_index_and_handles_suffixes(self):
        self.assertEqual(emote_label(120102013), "芬妮（古怪神探） · 表情13")
        self.assertEqual(video_label("VEmoji_102_09_2"), "芬妮（古怪神探） · 动态表情09-02")

    def test_invalid_values_have_no_internal_address_in_visible_fallback(self):
        for value in (None, [], True, "VEmoji_102_00", "../VEmoji_102_09", "VEmoji_0_1"):
            self.assertEqual(video_label(value), "未识别的动态表情")
        for value in (None, True, "120102013", 101, 120102000, -1):
            self.assertEqual(emote_label(value), "未识别的表情")

    def test_shared_slot_labels_follow_real_affected_ids_including_new_draft_route(self):
        fixture = workspace_fixture.EmoteWorkspaceTests("test_static_record_and_unsupported_texture_are_distinguished")
        fixture.setUp()
        try:
            fixture.edit.save("VEmoji_101_09")
            selected = fixture.edit.selection("UT_Item_Emoji_101_08")
            choice = next(row for row in selected["choices"] if row["name"] == "VEmoji_101_09")
            self.assertNotIn("VEmoji", choice["label"])
            self.assertEqual(choice["affected_labels"], [emote_label(identifier) for identifier in choice["affected_ids"]])
            self.assertIn("帕露南（商业之主） · 表情08", choice["affected_labels"])
            self.assertNotIn("VEmoji", fixture.controller.draft_items[0]["note"])
            fixture.tearDown()
        finally:
            fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
