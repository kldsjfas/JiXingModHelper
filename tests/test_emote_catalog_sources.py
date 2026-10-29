"""当前目录身份、材质表情与 CRI 视频能力分别校验。"""
from __future__ import annotations

import base64
import copy
import json
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astral_party_auto.modkit import emote_catalog, emote_workspace
from astral_party_auto.modkit.emote_addressables import apply_catalog_address_patch, independent_emote_identity
from test_emote_catalog import fixture
from test_emote_addressables import fixture as address_fixture


class EmoteCatalogSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="jixing-emote-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.executable = self.root / "game" / "RenamedClient.exe"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"fixture executable")
        self.data = self.executable.parent / "RenamedClient_Data"
        self.client = self.data / "com.unity.addressables" / "AssetBundles"
        self.client.mkdir(parents=True)
        (self.data / "app.info").write_text("feimo\nAstralParty_CN\n", encoding="utf-8")
        self.user_metadata = self.root / "redirected-Low" / "feimo" / "AstralParty_CN" / "com.unity.addressables"
        self.user_metadata.mkdir(parents=True)
        self.base = self.data / "StreamingAssets" / "aa"
        self.base_bundles = self.base / "StandaloneWindows64"
        self.base_bundles.mkdir(parents=True)
        self.directories = (self.client, self.base_bundles)
        self.enterContext(patch("astral_party_auto.modkit.bundles.unity_catalog_roots_for_exe",
                                return_value=(self.client.parent, self.user_metadata)))
        for name in ("config.bundle", "video.bundle", "scripts.bundle"):
            (self.client / name).write_bytes(b"fixture bundle")
        self.catalog_path = self.user_metadata / "catalog_3.3.0.json"
        self.write_catalog(fixture())

    def write_catalog(self, document):
        self.catalog_path.write_text(json.dumps(document), encoding="utf-8")

    def discover(self):
        return emote_catalog.discover_emote_locations(self.directories, self.executable)

    def workspace(self, records=None):
        controller = SimpleNamespace(aa_dirs=self.directories, game_executable=self.executable,
                                     _require_game=lambda: None, draft_items=[],
                                     selection={"asset_type": "texture", "category": "emoji", "name": "UT_Item_Emoji_101_08"},
                                     original_bundle_path=lambda name: self.client / name if (self.client / name).is_file() else None)
        inspector = Mock(return_value={"records": records or [
            {"emote_id": 120101008, "video_name": ""},
            {"emote_id": 120101009, "video_name": "VEmoji_101_09"}]})
        self.enterContext(patch.object(emote_workspace, "inspect_emote_config_bundle", inspector))
        return emote_workspace.EmoteWorkspace(controller, self.root / "draft-data", self.root / "draft.json"), inspector

    def test_metadata_catalog_is_found_without_user_resource_subdirectory(self):
        self.assertFalse((self.user_metadata / "AssetBundles").exists())
        result = self.discover()
        self.assertEqual(result["config_bundle"], "config.bundle")
        self.assertEqual(result["videos"]["VEmoji_101_09"], ["scripts.bundle", "video.bundle"])
        self.assertEqual(result["video_types"]["VEmoji_101_09"], emote_catalog.CRI_VIDEO_TYPE)
        self.assertFalse((self.user_metadata / "AssetBundles").exists())

    def test_material_addresses_are_classified_without_treating_them_as_cri(self):
        document = fixture()
        document["m_resourceTypes"][1]["m_ClassName"] = emote_catalog.MATERIAL_VIDEO_TYPE
        self.write_catalog(document)
        result = self.discover()
        self.assertEqual(result["video_types"], {"VEmoji_101_09": "UnityEngine.Material"})
        editor, _ = self.workspace()
        selected = editor.selection("UT_Item_Emoji_101_08")
        self.assertTrue(selected["editable"])
        self.assertEqual(selected["media_kind"], "material_sequence")
        self.assertEqual(selected["choices"][0]["media_kind"], "material_sequence")

    def test_unknown_or_malformed_resource_types_are_rejected(self):
        for type_record in ({"m_ClassName": "UnityEngine.Texture2D"}, None, "UnityEngine.Material"):
            document = fixture()
            document["m_resourceTypes"][1] = type_record
            self.write_catalog(document)
            with self.subTest(type_record=type_record), self.assertRaisesRegex(ValueError, "类型"):
                self.discover()

    def test_invalid_hot_catalog_does_not_borrow_an_older_builtin_catalog(self):
        self.catalog_path.write_text("not a JSON document", encoding="utf-8")
        (self.base / "catalog.json").write_text(json.dumps(fixture()), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "UTF-8 JSON") as result:
            self.discover()
        self.assertIn(str(self.catalog_path.parent), str(result.exception))

    def test_missing_current_dependency_does_not_use_older_catalog_entries(self):
        document = fixture()
        document["m_InternalIds"][3] = "/remote/current-missing.bundle"
        self.write_catalog(document)
        (self.base / "catalog.json").write_text(json.dumps(fixture()), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "current-missing.bundle"):
            self.discover()

    def test_native_binding_remains_available_when_independent_hash_is_missing(self):
        editor, _ = self.workspace()
        selected = editor.selection("UT_Item_Emoji_101_08")
        self.assertTrue(selected["editable"])
        self.assertFalse(selected["independent_supported"])
        self.assertIn(str(self.catalog_path.with_suffix(".hash")), selected["independent_reason"])
        with self.assertRaisesRegex(ValueError, "官方版本 hash"):
            editor.require_independent()
        with self.assertRaisesRegex(ValueError, "官方版本 hash"):
            editor.save_independent("VEmoji_101_09", self.root / "untrusted-replacement.bundle")
        self.assertFalse((self.root / "draft-data").exists())

    def test_pure_local_catalog_allows_native_binding_without_a_synthetic_hot_catalog(self):
        self.catalog_path.unlink()
        (self.base / "catalog.json").write_text(json.dumps(fixture()), encoding="utf-8")
        settings = {"m_DisableCatalogUpdateOnStart": True, "m_IsLocalCatalogInBundle": False,
                    "m_CatalogLocations": [{"m_Keys": ["AddressablesMainContentCatalog"],
                        "m_InternalId": "{UnityEngine.AddressableAssets.Addressables.RuntimePath}/catalog.json",
                        "m_Provider": "UnityEngine.AddressableAssets.ResourceProviders.ContentCatalogProvider",
                        "m_Dependencies": []}]}
        (self.base / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        editor, _ = self.workspace()
        selected = editor.selection("UT_Item_Emoji_101_08")
        self.assertTrue(selected["editable"])
        self.assertFalse(selected["independent_supported"])
        self.assertIn("内置目录不能代替", selected["independent_reason"])
        self.assertFalse(self.catalog_path.exists())

    def test_missing_unrelated_native_animation_does_not_disable_downloaded_choices(self):
        editor, _ = self.workspace(records=[
            {"emote_id": 120101008, "video_name": "VEmoji_101_99"},
            {"emote_id": 120101009, "video_name": "VEmoji_101_09"}])
        selected = editor.selection("UT_Item_Emoji_101_08")
        self.assertTrue(selected["editable"])
        self.assertEqual([row["name"] for row in selected["choices"]], ["VEmoji_101_09"])
        self.assertEqual(selected["default_video_name"], "VEmoji_101_09")

    def test_metadata_content_change_invalidates_context_with_unchanged_bundle_key(self):
        editor, inspector = self.workspace()
        with patch.object(emote_workspace, "bundle_source_key", return_value="unchanged-resources"):
            first_key = editor.source_key()
            document = fixture()
            document["m_LocatorId"] = "same-resources-new-catalog"
            self.write_catalog(document)
            second_key = editor.source_key()
        self.assertNotEqual(first_key, second_key)
        self.assertEqual(inspector.call_count, 2)

    def test_material_address_clone_preserves_type_with_one_bundle_dependency(self):
        document = address_fixture()
        document["m_resourceTypes"][1]["m_ClassName"] = emote_catalog.MATERIAL_VIDEO_TYPE
        buckets = bytearray(base64.b64decode(document["m_BucketDataString"]))
        struct.pack_into("<i", buckets, len(buckets) - 12, 1)
        document["m_BucketDataString"] = base64.b64encode(buckets[:-4]).decode()
        before = copy.deepcopy(document)
        identity = independent_emote_identity(120101008)
        result = apply_catalog_address_patch(document, "VEmoji_101_09", identity["video_name"],
            identity["bundle_name"], str(self.client / identity["bundle_name"]), bundle_size=100)
        locations = emote_catalog.read_emote_locations(result)
        self.assertEqual(locations["video_types"][identity["video_name"]], emote_catalog.MATERIAL_VIDEO_TYPE)
        self.assertEqual(locations["videos"][identity["video_name"]], {identity["bundle_name"]})
        self.assertEqual(document, before)
        self.assertEqual(result["m_BuildResultHash"], before["m_BuildResultHash"])
        self.assertEqual(result["m_resourceTypes"], before["m_resourceTypes"])

    def test_material_capability_requires_actual_material_object(self):
        document = fixture()
        document["m_resourceTypes"][1]["m_ClassName"] = emote_catalog.MATERIAL_VIDEO_TYPE
        self.write_catalog(document)
        editor, _ = self.workspace()
        environment = SimpleNamespace(objects=[SimpleNamespace(type=SimpleNamespace(name="MonoBehaviour"),
                                                              peek_name=lambda: "VEmoji_101_09")])
        with patch.object(emote_workspace.UnityPy, "load", return_value=environment):
            with self.assertRaisesRegex(ValueError, "无法唯一确定"):
                editor.video_bundle("VEmoji_101_09")

    def test_material_multiple_dependency_order_is_not_assumed(self):
        document = address_fixture()
        document["m_resourceTypes"][1]["m_ClassName"] = emote_catalog.MATERIAL_VIDEO_TYPE
        identity = independent_emote_identity(120101008)
        with self.assertRaisesRegex(ValueError, "多包依赖"):
            apply_catalog_address_patch(document, "VEmoji_101_09", identity["video_name"],
                identity["bundle_name"], str(self.client / identity["bundle_name"]), bundle_size=100)
        self.write_catalog(document)
        self.catalog_path.with_suffix(".hash").write_text("verified-official-version", encoding="ascii")
        editor, _ = self.workspace()
        with self.assertRaisesRegex(ValueError, "多包依赖"):
            editor.require_independent("VEmoji_101_09")


if __name__ == "__main__":
    unittest.main()
