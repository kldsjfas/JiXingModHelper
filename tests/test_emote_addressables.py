"""独立资源地址的保留、冲突和本地路径回归；不依赖真实游戏。"""
from __future__ import annotations

import base64
import copy
import json
import struct
import tempfile
import unittest
from pathlib import Path

from astral_party_auto.modkit import emote_addressables as addresses
from astral_party_auto.modkit.emote_catalog import read_emote_locations
from tests import test_emote_catalog as catalog_fixtures


def fixture():
    catalog = catalog_fixtures.fixture()
    catalog["m_ProviderIds"] = [addresses._BUNDLE_PROVIDER, addresses._ASSET_PROVIDER]
    catalog["m_LocatorId"] = "AddressablesMainContentCatalog"
    catalog["m_BuildResultHash"] = "official-version-keep"
    assembly = b"Unity.ResourceManager, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null"
    class_name = addresses._OPTIONS_TYPE.encode()
    options = {"m_Hash": "original-hash", "m_Crc": 987, "m_BundleName": "original-video",
               "m_BundleSize": 1234, "m_Timeout": 60, "m_RetryCount": 10,
               "m_UseUWRForLocalBundles": True, "m_UseCrcForCachedBundles": True,
               "m_ClearOtherCachedVersionsWhenLoaded": True}
    body = json.dumps(options, separators=(",", ":")).encode("utf-16le")
    raw = bytes([7, len(assembly)]) + assembly + bytes([len(class_name)]) + class_name + struct.pack("<i", len(body)) + body
    catalog["m_ExtraDataString"] = base64.b64encode(raw).decode()
    catalog_fixtures.patch_bytes(catalog, "m_EntryDataString", 4 + 3 * 28 + 4 * 4, 0)
    return catalog


def apply(catalog, emote_id=120101008, path=None):
    identity = addresses.independent_emote_identity(emote_id)
    return addresses.apply_catalog_address_patch(catalog, "VEmoji_101_09", identity["video_name"],
        identity["bundle_name"], path or f'C:\\Game Cache\\{identity["bundle_name"]}', bundle_size=4321)


class EmoteAddressablesTests(unittest.TestCase):
    def test_identity_matches_selected_static_emote(self):
        self.assertEqual(addresses.independent_emote_identity(120101008), {
            "video_name": "VEmoji_101_08_999", "bundle_name": "jixing_emote_120101008.bundle",
            "internal_id": "jixing/emote/120101008"})
        for emote_id in (True, "120101008", 120101000, 120000001, 119101008, 130000001):
            with self.subTest(emote_id=emote_id), self.assertRaises(ValueError):
                addresses.independent_emote_identity(emote_id)

    def test_new_address_preserves_original_video_and_shared_scripts(self):
        original = fixture()
        baseline = copy.deepcopy(original)
        patched = apply(original)
        self.assertEqual(original, baseline)
        before, after = read_emote_locations(original), read_emote_locations(patched)
        self.assertEqual(after["videos"]["VEmoji_101_09"], before["videos"]["VEmoji_101_09"])
        self.assertEqual(after["videos"]["VEmoji_101_08_999"], {"jixing_emote_120101008.bundle", "scripts.bundle"})
        self.assertEqual(after["config_bundles"], before["config_bundles"])
        self.assertEqual(patched["m_BuildResultHash"], original["m_BuildResultHash"])
        self.assertEqual(patched["m_InternalIds"][:-2], original["m_InternalIds"])
        self.assertEqual(base64.b64decode(patched["m_EntryDataString"])[4:4 + 5 * 28],
                         base64.b64decode(original["m_EntryDataString"])[4:])
        self.assertEqual(base64.b64decode(patched["m_ExtraDataString"])[:len(base64.b64decode(original["m_ExtraDataString"]))],
                         base64.b64decode(original["m_ExtraDataString"]))

    def test_two_static_emotes_get_distinct_bundle_dependencies(self):
        first = apply(fixture())
        second = apply(first, 120102008)
        locations = read_emote_locations(second)["videos"]
        self.assertEqual(locations["VEmoji_101_08_999"], {"jixing_emote_120101008.bundle", "scripts.bundle"})
        self.assertEqual(locations["VEmoji_102_08_999"], {"jixing_emote_120102008.bundle", "scripts.bundle"})

    def test_new_bundle_uses_local_load_and_keeps_official_parameters(self):
        patched = apply(fixture())
        rows = base64.b64decode(patched["m_EntryDataString"])
        new_bundle = struct.unpack_from("<7i", rows, 4 + 5 * 28)
        _, _, options = addresses._read_options(base64.b64decode(patched["m_ExtraDataString"]), new_bundle[4])
        self.assertEqual((options["m_Hash"], options["m_Crc"], options["m_BundleSize"]), ("", 0, 4321))
        self.assertFalse(options["m_UseUWRForLocalBundles"])
        self.assertFalse(options["m_ClearOtherCachedVersionsWhenLoaded"])
        self.assertEqual((options["m_Timeout"], options["m_RetryCount"]), (60, 10))
        self.assertEqual(options["m_BundleName"], "jixing_emote_120101008")

    def test_duplicate_address_is_rejected_without_changing_catalog(self):
        patched = apply(fixture())
        baseline = copy.deepcopy(patched)
        with self.assertRaisesRegex(ValueError, "已存在"):
            apply(patched)
        self.assertEqual(patched, baseline)

    def test_remote_relative_mismatched_and_template_paths_are_rejected(self):
        for path in ("https://example.com/jixing_emote_120101008.bundle", "relative/jixing_emote_120101008.bundle",
                     "C:\\Game\\other.bundle", "{UnityEngine.Application.persistentDataPath}/jixing_emote_120101008.bundle",
                     "C:\\Game\\jixing_emote_120101008.bundle\n"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                apply(fixture(), path=path)
        identity = addresses.independent_emote_identity(120101008)
        with self.assertRaises(ValueError):
            addresses.apply_catalog_address_patch(fixture(), "VEmoji_101_09", "VEmoji_102_08_999",
                identity["bundle_name"], "C:\\Game\\" + identity["bundle_name"], bundle_size=4321)

    def test_missing_scripts_bad_loader_and_bad_options_are_rejected(self):
        no_scripts = fixture()
        buckets = bytearray(base64.b64decode(no_scripts["m_BucketDataString"]))
        # Final dependency bucket contains video and script; keep only video.
        struct.pack_into("<i", buckets, len(buckets) - 12, 1)
        no_scripts["m_BucketDataString"] = base64.b64encode(buckets[:-4]).decode()
        bad_loader = fixture()
        bad_loader["m_ProviderIds"][1] = "UnknownProvider"
        bad_options = fixture()
        bad_options["m_ExtraDataString"] = base64.b64encode(b"truncated").decode()
        for catalog in (no_scripts, bad_loader, bad_options):
            with self.subTest(catalog=catalog["m_ExtraDataString"][:12]), self.assertRaises(ValueError):
                apply(catalog)

    def test_existing_bundle_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "jixing_emote_120101008.bundle"
            destination.write_bytes(b"keep existing")
            with self.assertRaises(ValueError):
                addresses.create_independent_emote_bundle("missing source", "VEmoji_101_09", "missing usm", destination,
                                                         emote_id=120101008)
            self.assertEqual(destination.read_bytes(), b"keep existing")


if __name__ == "__main__":
    unittest.main()
