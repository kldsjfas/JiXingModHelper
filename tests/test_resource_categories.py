"""资源用途分类、分页过滤和共享 Sprite 图集冲突回归。"""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from astral_party_auto.mod_controller import ModController
from astral_party_auto.modkit.categories import (
    all_categories,
    asset_category,
    categorize,
    category_group,
    category_label,
    resource_display_name,
)


class ResourceCategoryTests(unittest.TestCase):
    def make_controller(self, index):
        controller = object.__new__(ModController)
        controller.typed_index = index
        controller._category_cache_lock = threading.Lock()
        controller._category_cache_source = None
        controller._category_rows = {}
        controller._category_counts = {}
        controller._bundle_labels = {}
        controller._resource_labels = {}
        controller._valid_dynamic = {}
        controller._dynamic_sequence_bases = None
        return controller

    def test_current_catalog_texture_names_have_specific_categories(self):
        samples = {
            "UT_AltArt_CardFront_00001": "hand_card",
            "Story_Monster_Card_1026_Cry": "monster",
            "UT_Hero_Story_123_02": "hero_bust",
            "UT_Destiny_40001": "destiny",
            "UT_Divination_60001": "destiny",
            "UT_SGBBuilding_10001": "building",
            "UT_Campaign_Level_1001": "game_mode",
            "UT_Exchange_50431": "store",
            "UT_Recharge_100010_Pay": "store",
            "UT_Banner_610170": "promotion",
            "UT_KillVfx_73001": "kill_effect",
            "GuidePic_PVE_1021301": "guide",
            "ActivityLight_atlas4": "ui_atlas",
            "Afacad-Regular_TMP": "font",
        }
        for name, expected in samples.items():
            with self.subTest(name=name):
                self.assertEqual(categorize(name), expected)
                self.assertEqual(asset_category(name, "texture"), expected)

    def test_existing_texture_categories_keep_their_ids_and_priority(self):
        self.assertEqual(categorize("UT_Item_Currency_1"), "currency")
        self.assertEqual(categorize("UT_Item_Emoji_102"), "emoji")
        self.assertEqual(categorize("UT_Platform_Relic_1"), "chip")
        self.assertEqual(categorize("Fight_00001"), "sprite_anim")
        self.assertEqual(categorize("unknown", 404, 400), "hand_card")

    def test_all_texture_categories_have_a_readable_group(self):
        for category_id, _label in all_categories():
            self.assertNotEqual(category_group(category_id), "其它资源")
        self.assertEqual(category_group("hero_card"), "角色")
        self.assertEqual(category_group("ui_atlas"), "界面与活动")

    def test_non_texture_names_are_classified_without_using_arbitrary_digits(self):
        samples = [
            ("Simplified Chinese", "text", "text_language"),
            ("StoryDialogue", "text", "text_story"),
            ("Hero", "text", "text_character"),
            ("Card", "text", "text_card"),
            ("MapLevel.json", "text", "text_map"),
            ("GameData_CN", "text", "text_config"),
            ("001", "text", "text_other"),
            ("Hero101_01_Walk_PC", "anim", "anim_walk"),
            ("Fight_EX", "anim", "anim_fight"),
            ("Hit", "anim", "anim_reaction"),
            ("Idle", "anim", "anim_idle"),
            ("Cry", "anim", "anim_emotion"),
            ("CameraShake", "anim", "anim_ui"),
            ("SpineHandCard_13020001", "mesh", "mesh_card"),
            ("FightBackGround_117006", "mesh", "mesh_map"),
            ("Hero101_Body", "mesh", "mesh_character"),
            ("Dice_01", "mesh", "mesh_dice"),
        ]
        for name, asset_type, expected in samples:
            with self.subTest(name=name, asset_type=asset_type):
                self.assertEqual(asset_category(name, asset_type), expected)
                self.assertNotEqual(category_label(expected), expected)

    def test_text_counts_and_both_browse_paths_use_the_same_filter(self):
        controller = self.make_controller({
            "text": {"b.bundle": ["Card", "Skin", "StoryDialogue"], "a.bundle": ["Item", "MapLevel"]},
        })
        counts = {category_id: count for category_id, _label, count in controller.categories(asset_type="text")}
        self.assertEqual(counts["all"], 5)
        self.assertEqual(counts["text_card"], 2)
        expected = [("a.bundle", "Item"), ("b.bundle", "Card")]
        self.assertEqual(controller.browse("text_card", asset_type="text"), expected)
        labelled = controller.browse_labelled("text_card", asset_type="text")
        self.assertEqual([(bundle, name) for bundle, name, _character in labelled], expected)
        self.assertEqual(controller.count_labelled("text_card", asset_type="text"), 2)
        self.assertEqual(controller.browse("text_card", "card", asset_type="text"), [("b.bundle", "Card")])

    def test_display_names_translate_purpose_and_keep_ids_and_variants(self):
        samples = [
            ("UT_HandCard_10001", "texture", "手牌 · 10001"),
            ("UT_Hero_ThinCard_11502_sfw", "texture", "角色窄卡面 · 11502 · 和谐版"),
            ("UT_Item_Gift_001", "texture", "礼物 · 001"),
            ("Walk-001", "texture", "行走 · 帧 001"),
            ("walk.001.PNG", "texture", "行走 · 帧 001"),
            ("IDLE", "anim", "待机"),
            ("Attack_Loop", "anim", "攻击 · 循环"),
            ("Hit_001", "texture", "受击 · 帧 001"),
            ("Die", "anim", "倒地"),
            ("Hero101_Walk_PC", "anim", "行走 · 角色 · 101 · 电脑端"),
            ("Common_atlas0", "texture", "界面图集 · 公共 · 图集 0"),
            ("Common_fui/Expression", "dynamic", "界面动画 · 公共 · 表情"),
            ("Afacad-Regular_TMP", "texture", "字体图集 · Afacad · 常规"),
            ("VSkill_Hero115", "dynamic", "技能演出 · 角色 · 115"),
            ("Card", "text", "卡牌配置"),
            ("GameData_CN.json", "text", "游戏数据 · 简中"),
        ]
        for name, asset_type, expected in samples:
            with self.subTest(name=name):
                self.assertEqual(resource_display_name(name, asset_type), expected)

    def test_unknown_display_names_keep_short_identity_without_guessing_game_names(self):
        asset_hash = "b855f8a8f2c7bbde0e0dbdd76a9cd2f7"
        displayed = resource_display_name(asset_hash, "texture")
        self.assertEqual(displayed, "其它资源 · 标识 b855f8a8")
        self.assertNotIn(asset_hash, displayed)
        self.assertEqual(resource_display_name("001", "text"), "其它文本 · 001")
        self.assertEqual(resource_display_name("", "texture"), "其它资源")

    def test_chinese_search_matches_display_names_and_categories(self):
        controller = self.make_controller({
            "texture": {"a.bundle": ["UT_HandCard_10001", "UT_Buff_002", "Walk_001"]},
            "text": {"a.bundle": ["Card", "Skin"]},
            "dynamic": {"a.bundle": ["VSkill_Hero115"]},
            "cri_usm": {"a.bundle": ["VSkill_Hero115"]},
        })
        self.assertEqual(controller.browse("all", "手牌"), [("a.bundle", "UT_HandCard_10001")])
        self.assertEqual(controller.browse("buff", "状态"), [("a.bundle", "UT_Buff_002")])
        self.assertEqual(controller.browse("all", "行走"), [("a.bundle", "Walk_001")])
        self.assertEqual(controller.browse("all", "皮肤", asset_type="text"), [("a.bundle", "Skin")])
        self.assertEqual(controller.browse("all", "视频", asset_type="dynamic"), [("a.bundle", "VSkill_Hero115")])
        self.assertEqual(controller.count_labelled("all", "手牌"), 1)
        self.assertEqual(controller.browse_labelled("all", "手牌"), [("a.bundle", "UT_HandCard_10001", "")])

    def test_animation_and_mesh_subcategories_filter_results(self):
        controller = self.make_controller({
            "anim": {"a.bundle": ["Walk", "Cry", "Fight"]},
            "mesh": {"a.bundle": ["Dice_01", "Hero101_Body"]},
        })
        self.assertEqual(controller.browse("anim_walk", asset_type="anim"), [("a.bundle", "Walk")])
        self.assertEqual(controller.browse("mesh_dice", asset_type="mesh"), [("a.bundle", "Dice_01")])
        self.assertEqual(controller.browse("does_not_exist", asset_type="mesh"), [])

    def test_shared_atlas_conflicts_with_other_sprite_groups_and_whole_texture(self):
        controller = self.make_controller({})
        controller.draft_items = [{
            "kind": "dynamic", "bundle": "a.bundle", "name": "Walk",
            "frame_names": ["Walk_001", "Walk_002"], "affected_textures": ["SharedAtlas"],
        }]
        for names, kind, name in [
            (["SharedAtlas"], "texture", "SharedAtlas"),
            (["Idle_001", "SharedAtlas"], "dynamic", "Idle"),
        ]:
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "共用帧图"):
                controller._check_sequence_overlap("a.bundle", names, kind, name)
        controller._check_sequence_overlap("a.bundle", ["SharedAtlas"], "dynamic", "Walk")
        controller._check_sequence_overlap("another.bundle", ["SharedAtlas"], "texture", "SharedAtlas")

    def test_dynamic_import_checks_and_saves_affected_textures(self):
        controller = self.make_controller({})
        controller.draft_items = []
        controller._require_game = Mock()
        controller._save_draft = Mock()
        controller.log = Mock()
        inspection = {"editable": True, "frame_names": ["Walk_001"], "affected_textures": ["SharedAtlas"]}
        with TemporaryDirectory(prefix="jixing-category-overlap-") as folder:
            controller._draft_dir = Mock(return_value=Path(folder))
            with patch("astral_party_auto.modkit.animation.inspect_animation", return_value=inspection), \
                    patch("astral_party_auto.modkit.animation.replace_bundle_sequence", return_value={}) as replace:
                item = controller.add_dynamic_to_draft(
                    "original.bundle", "Walk", "replacement.gif", bundle_name="a.bundle", animation_kind="sequence",
                )
                self.assertEqual(item["affected_textures"], ["SharedAtlas"])
                replace.assert_called_once()
                replace.reset_mock()
                with self.assertRaisesRegex(RuntimeError, "共用帧图"):
                    controller.add_dynamic_to_draft(
                        "original.bundle", "Idle", "replacement.gif", bundle_name="a.bundle", animation_kind="sequence",
                    )
                replace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
