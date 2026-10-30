"""目录元数据与资源包分居两处时，独立表情仍可安装并完整还原。"""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from astral_party_auto.modkit.catalog_mods import active_catalog_path, write_address_manifest
from astral_party_auto.modkit.manager import ModManager
from tests.test_emote_addressables import fixture


class CatalogMetadataInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-metadata-install-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.executable = self.root / "client" / "AstralParty_CN.exe"
        self.client_metadata = self.executable.parent / "AstralParty_CN_Data" / "com.unity.addressables"
        self.bundles = self.client_metadata / "AssetBundles"
        self.bundles.mkdir(parents=True)
        self.executable.write_bytes(b"fixture")
        self.user_metadata = self.root / "redirected-Low" / "feimo" / "AstralParty_CN" / "com.unity.addressables"
        self.user_metadata.mkdir(parents=True)
        self.catalog = self.user_metadata / "catalog_3.3.0.json"
        self.baseline = json.dumps(fixture()).encode()
        self.catalog.write_bytes(self.baseline)
        self.hash_file = self.catalog.with_suffix(".hash")
        self.hash_file.write_bytes(b"CURRENT_OFFICIAL_VERSION\n")
        for name in ("config.bundle", "video.bundle", "scripts.bundle"):
            (self.bundles / name).write_bytes(("original " + name).encode())
        self.enterContext(patch("astral_party_auto.modkit.bundles.unity_catalog_roots_for_exe",
                                return_value=(self.client_metadata, self.user_metadata)))
        self.manager = ModManager((self.bundles,), self.root / "data", executable=self.executable)
        self.source = self.root / "mod"
        self.source.mkdir()
        self.bundle_name = "jixing_emote_120101008.bundle"
        (self.source / self.bundle_name).write_bytes(b"independent fixture")
        write_address_manifest(self.source, [{"emote_id": 120101008, "video_name": "VEmoji_101_08_999",
            "bundle_name": self.bundle_name, "template_video_key": "VEmoji_101_09", "bundle_size": 19}])

    def test_install_toggle_uninstall_without_creating_metadata_bundle_directory(self):
        self.assertEqual(active_catalog_path((self.bundles,), self.executable), self.catalog)
        self.assertFalse((self.user_metadata / "AssetBundles").exists())
        self.manager.install_mod(self.source, "independent")
        self.assertTrue((self.bundles / self.bundle_name).is_file())
        self.assertNotEqual(self.catalog.read_bytes(), self.baseline)
        self.manager.disable_mod("independent")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.manager.enable_mod("independent")
        self.manager.uninstall_mod("independent")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.assertEqual(self.hash_file.read_bytes(), b"CURRENT_OFFICIAL_VERSION\n")
        self.assertFalse((self.bundles / self.bundle_name).exists())
        self.assertFalse((self.user_metadata / "AssetBundles").exists())

    def test_missing_official_hash_rejects_before_installing_any_bundle(self):
        self.hash_file.unlink()
        with self.assertRaisesRegex(ValueError, "官方版本 hash"):
            self.manager.install_mod(self.source, "independent")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.assertFalse((self.bundles / self.bundle_name).exists())

    def test_version_drift_preserves_new_catalog_and_owned_files(self):
        self.manager.install_mod(self.source, "independent")
        before = self.catalog.read_bytes()
        self.hash_file.write_bytes(b"NEW_OFFICIAL_VERSION\n")
        with self.assertRaisesRegex(ValueError, "catalog 已更新"):
            self.manager.uninstall_mod("independent")
        self.assertEqual(self.catalog.read_bytes(), before)
        self.assertTrue((self.bundles / self.bundle_name).is_file())

    def test_external_official_hash_change_invalidates_source_fingerprint(self):
        from astral_party_auto.modkit.visual_catalog import find_catalog_source
        source = find_catalog_source((self.bundles,), self.executable)
        before = source.fingerprint()
        self.hash_file.write_bytes(b"NEW_OFFICIAL_VERSION\n")
        self.assertNotEqual(before, source.fingerprint())
        self.hash_file.unlink()
        self.assertNotEqual(before, source.fingerprint())


if __name__ == "__main__":
    unittest.main()
