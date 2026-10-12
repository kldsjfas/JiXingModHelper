"""紧凑 Addressables 表情目录的边界与依赖回归，不需要本机游戏。"""
from __future__ import annotations

import base64
import copy
import json
import struct
import tempfile
import unittest
from pathlib import Path

from astral_party_auto.modkit import emote_catalog as catalog


def fixture(*, unicode_names=False):
    names = ["GameData_CN", "VEmoji_101_09", "config.bundle", "video.bundle", "scripts.bundle", "config-dependency", "video-dependency"]
    keys = bytearray(struct.pack("<i", len(names)))
    offsets = []
    for name in names:
        offsets.append(len(keys))
        raw = name.encode("utf-16le" if unicode_names else "ascii")
        keys.extend(bytes([1 if unicode_names else 0]) + struct.pack("<i", len(raw)) + raw)
    membership = [[0], [1], [2], [3], [4], [2], [3, 4]]
    buckets = bytearray(struct.pack("<i", len(names)))
    for offset, entries in zip(offsets, membership):
        buckets.extend(struct.pack("<ii", offset, len(entries)))
        buckets.extend(struct.pack(f"<{len(entries)}i", *entries))
    entries = [(0, 1, 5, 0, -1, 0, 0), (1, 1, 6, 0, -1, 1, 1),
               (2, 0, -1, 0, -1, 2, 2), (3, 0, -1, 0, -1, 3, 2), (4, 0, -1, 0, -1, 4, 2)]
    encoded = struct.pack("<i", len(entries)) + b"".join(struct.pack("<7i", *row) for row in entries)
    return {"m_KeyDataString": base64.b64encode(keys).decode(), "m_BucketDataString": base64.b64encode(buckets).decode(),
            "m_EntryDataString": base64.b64encode(encoded).decode(), "m_InternalIds": ["0", "1", "/remote/config.bundle", "/remote/video.bundle", "/remote/scripts.bundle"],
            "m_resourceTypes": [{"m_ClassName": "UnityEngine.TextAsset"}, {"m_ClassName": "CriWare.Assets.CriManaUsmAsset"}, {"m_ClassName": "UnityEngine.ResourceManagement.ResourceProviders.IAssetBundleResource"}]}


def patch_bytes(data, key, offset, value):
    raw = bytearray(base64.b64decode(data[key]))
    raw[offset:offset + 4] = struct.pack("<i", value)
    data[key] = base64.b64encode(raw).decode()


class EmoteCatalogTests(unittest.TestCase):
    def test_ascii_and_utf16_keys_resolve_direct_dependencies(self):
        for unicode_names in (False, True):
            with self.subTest(unicode_names=unicode_names):
                result = catalog.read_emote_locations(fixture(unicode_names=unicode_names))
                self.assertEqual(result["config_bundles"], {"config.bundle"})
                self.assertEqual(result["videos"], {"VEmoji_101_09": {"video.bundle", "scripts.bundle"}})

    def test_texture_number_is_conservative(self):
        self.assertEqual(catalog.emote_id_from_texture("UT_Item_Emoji_101_08"), 120101008)
        for name in ("UT_Item_Emoji_101_08_JP", "UT_Item_Emoji_0_1", "UT_Item_Emoji_10000_1", "UT_Item_Emoji_101_0", "UT_Item_Emoji_101_1000", "UT_Item_Emoji_101_08\n", "../../UT_Item_Emoji_101_08"):
            with self.subTest(name=name):
                self.assertIsNone(catalog.emote_id_from_texture(name))

    def test_bad_base64_or_missing_binary_section_is_rejected(self):
        for key in ("m_KeyDataString", "m_BucketDataString", "m_EntryDataString"):
            for value in (None, "!bad base64!", ""):
                data = fixture()
                data[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    catalog.read_emote_locations(data)

    def test_entry_and_bucket_counts_are_bounded(self):
        for key, count in (("m_EntryDataString", -1), ("m_EntryDataString", catalog.MAX_ENTRIES + 1), ("m_BucketDataString", -1), ("m_BucketDataString", catalog.MAX_ENTRIES + 1)):
            data = fixture()
            patch_bytes(data, key, 0, count)
            with self.subTest(key=key, count=count), self.assertRaises(ValueError):
                catalog.read_emote_locations(data)

    def test_truncation_and_tail_are_rejected(self):
        for key in ("m_EntryDataString", "m_BucketDataString"):
            for tail in (False, True):
                data = fixture()
                raw = base64.b64decode(data[key])
                data[key] = base64.b64encode(raw + b"tail" if tail else raw[:-1]).decode()
                with self.subTest(key=key, tail=tail), self.assertRaises(ValueError):
                    catalog.read_emote_locations(data)

    def test_key_offsets_lengths_and_bucket_membership_are_checked(self):
        changes = [("m_BucketDataString", 4, -1), ("m_BucketDataString", 4, 999999),
                   ("m_BucketDataString", 8, -1), ("m_BucketDataString", 8, 999999),
                   ("m_BucketDataString", 12, -1), ("m_BucketDataString", 12, 999999),
                   ("m_KeyDataString", 5, -1), ("m_KeyDataString", 5, 4097)]
        for key, offset, value in changes:
            data = fixture()
            patch_bytes(data, key, offset, value)
            with self.subTest(key=key, offset=offset, value=value), self.assertRaises(ValueError):
                catalog.read_emote_locations(data)

    def test_entry_references_and_missing_dependencies_are_checked(self):
        # Seven signed indices follow the count; indices 0/2/5/6 are used here.
        for column in (0, 2, 5, 6):
            for value in (-1, 999999):
                data = fixture()
                patch_bytes(data, "m_EntryDataString", 4 + column * 4, value)
                with self.subTest(column=column, value=value), self.assertRaises(ValueError):
                    catalog.read_emote_locations(data)

    def test_resource_type_and_bundle_suffix_are_checked(self):
        data = fixture()
        data["m_resourceTypes"][1]["m_ClassName"] = "UnityEngine.Texture2D"
        with self.assertRaises(ValueError):
            catalog.read_emote_locations(data)
        data = fixture()
        data["m_InternalIds"][3] = "/remote/not-a-bundle.gif"
        with self.assertRaises(ValueError):
            catalog.read_emote_locations(data)

    def test_invalid_ascii_key_is_rejected(self):
        data = fixture()
        raw = bytearray(base64.b64decode(data["m_KeyDataString"]))
        raw[9] = 255
        data["m_KeyDataString"] = base64.b64encode(raw).decode()
        with self.assertRaises(ValueError):
            catalog.read_emote_locations(data)

    def test_discovery_requires_every_video_dependency_on_disk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "catalog.json").write_text(json.dumps(fixture()), encoding="utf-8")
            for name in ("config.bundle", "video.bundle"):
                (root / name).write_bytes(b"placeholder")
            with self.assertRaises(ValueError):
                catalog.discover_emote_locations((root,))
            (root / "scripts.bundle").write_bytes(b"placeholder")
            result = catalog.discover_emote_locations((root,))
            self.assertEqual(result, {"config_bundle": "config.bundle", "videos": {"VEmoji_101_09": ["scripts.bundle", "video.bundle"]}})

    def test_catalog_size_gate_before_reading(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "catalog.json").write_bytes(b"x" * 1024)
            from unittest import mock
            with mock.patch.object(catalog, "MAX_CATALOG_BYTES", 20), self.assertRaises(ValueError):
                catalog.discover_emote_locations((root,))

    def test_unknown_key_type_does_not_fabricate_an_emote(self):
        data = fixture()
        raw = bytearray(base64.b64decode(data["m_KeyDataString"]))
        raw[4] = 6
        data["m_KeyDataString"] = base64.b64encode(raw).decode()
        result = catalog.read_emote_locations(data)
        self.assertEqual(result["config_bundles"], set())
        self.assertEqual(list(result["videos"]), ["VEmoji_101_09"])


if __name__ == "__main__":
    unittest.main()
