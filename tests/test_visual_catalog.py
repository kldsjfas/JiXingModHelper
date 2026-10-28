"""卡面目录定位与表情资源解耦的回归。"""
from __future__ import annotations

import base64
import copy
import struct
import unittest

from astral_party_auto.modkit.visual_catalog import visual_bundle_locations


def fixture(*, config_entries=1, video_type="UnityEngine.Object", extra_dependency=False):
    names = ["GameData_CN", "Common_fui", "Common_atlas0", "VEmoji_101_01",
             "config.bundle", "ui.bundle", "atlas.bundle", "video.bundle"]
    keys, offsets = bytearray(struct.pack("<i", len(names))), []
    for name in names:
        offsets.append(len(keys))
        encoded = name.encode("ascii")
        keys.extend(b"\0" + struct.pack("<i", len(encoded)) + encoded)
    rows = [(0, 1, 4, 0, -1, 0, 0)] * config_entries
    ui, atlas, video = len(rows), len(rows) + 1, len(rows) + 2
    config_bundle, ui_bundle, atlas_bundle, video_bundle = len(rows) + 3, len(rows) + 4, len(rows) + 5, len(rows) + 6
    rows.extend([(1, 1, 5, 0, -1, 1, 0), (2, 1, 6, 0, -1, 2, 1), (3, 1, 7, 0, -1, 3, 2),
                 (4, 0, -1, 0, -1, 4, 3), (5, 0, -1, 0, -1, 5, 3),
                 (6, 0, -1, 0, -1, 6, 3), (7, 0, -1, 0, -1, 7, 3)])
    membership = [list(range(config_entries)), [ui], [atlas], [video],
                  [config_bundle] + ([ui_bundle] if extra_dependency else []), [ui_bundle], [atlas_bundle], [video_bundle]]
    buckets = struct.pack("<i", len(names))
    for offset, ids in zip(offsets, membership):
        buckets += struct.pack("<ii", offset, len(ids)) + struct.pack(f"<{len(ids)}i", *ids)
    entries = struct.pack("<i", len(rows)) + b"".join(struct.pack("<7i", *row) for row in rows)
    return {"m_KeyDataString": base64.b64encode(keys).decode(),
            "m_BucketDataString": base64.b64encode(buckets).decode(),
            "m_EntryDataString": base64.b64encode(entries).decode(),
            "m_InternalIds": ["config", "ui", "atlas", "video", "/config.bundle", "/ui.bundle", "/atlas.bundle", "/video.bundle"],
            "m_resourceTypes": [{"m_ClassName": "UnityEngine.TextAsset"}, {"m_ClassName": "UnityEngine.Texture2D"},
                                {"m_ClassName": video_type}, {"m_ClassName": "UnityEngine.ResourceManagement.ResourceProviders.IAssetBundleResource"}]}


class VisualCatalogTests(unittest.TestCase):
    def test_configuration_label_can_contain_many_text_assets_in_one_bundle(self):
        for count in (1, 159):
            with self.subTest(count=count):
                self.assertEqual(visual_bundle_locations(fixture(config_entries=count)),
                                 {"GameData_CN": "config.bundle", "Common_fui": "ui.bundle", "Common_atlas0": "atlas.bundle"})

    def test_unrelated_video_type_changes_do_not_disable_card_images(self):
        for video_type in ("UnityEngine.Object", "NewGame.VideoAsset", "CriWare.Assets.CriManaUsmAsset"):
            self.assertEqual(visual_bundle_locations(fixture(video_type=video_type))["GameData_CN"], "config.bundle")

    def test_selected_resource_types_and_extra_dependencies_are_rejected(self):
        data = fixture()
        data["m_resourceTypes"][0]["m_ClassName"] = "UnityEngine.Texture2D"
        for invalid in (data, fixture(extra_dependency=True)):
            with self.assertRaises(ValueError):
                visual_bundle_locations(invalid)

    def test_truncated_or_out_of_range_sections_are_rejected(self):
        for section in ("m_KeyDataString", "m_BucketDataString", "m_EntryDataString"):
            for bad in (None, "!", base64.b64encode(b"\x00").decode()):
                data = fixture()
                data[section] = bad
                with self.subTest(section=section, bad=bad), self.assertRaises(ValueError):
                    visual_bundle_locations(data)
        for column in (0, 2, 5, 6):
            data = fixture()
            raw = bytearray(base64.b64decode(data["m_EntryDataString"]))
            struct.pack_into("<i", raw, 4 + column * 4, 999999)
            data["m_EntryDataString"] = base64.b64encode(raw).decode()
            with self.subTest(column=column), self.assertRaises(ValueError):
                visual_bundle_locations(data)

    def test_inputs_are_not_changed_by_discovery(self):
        source = fixture(config_entries=3)
        before = copy.deepcopy(source)
        visual_bundle_locations(source)
        self.assertEqual(source, before)


if __name__ == "__main__":
    unittest.main()
