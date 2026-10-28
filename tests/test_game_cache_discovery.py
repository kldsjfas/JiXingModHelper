"""客户端缓存、用户 catalog 和基础资源可以分处三个目录。"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from astral_party_auto.modkit.bundles import (
    _latest_catalog_path,
    aa_dir_for_exe,
    bundle_dirs_for_exe,
    bundle_file_map,
    bundle_source_key,
    hot_update_dir_for_exe,
    unity_catalog_roots_for_exe,
)
from astral_party_auto.modkit.catalog_mods import active_catalog_path


class GameCacheDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-cache-discovery-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self.root / "game" / "AstralParty_CN.exe"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"")
        self.profile = self.root / "profile"
        self.data = self.executable.parent / "AstralParty_CN_Data"
        self.data.mkdir()
        (self.data / "app.info").write_text("feimo\nAstralParty_CN\n", encoding="utf-8")
        self.client_cache = self.data / "com.unity.addressables" / "AssetBundles"
        self.user_cache = hot_update_dir_for_exe(self.executable, user_profile=self.profile)
        self.base = self.data / "StreamingAssets" / "aa" / "StandaloneWindows64"

    def directories(self):
        return bundle_dirs_for_exe(self.executable, user_profile=self.profile)

    def cache_file(self, directory, name, content):
        path = directory / "cache-key" / Path(name).stem / "__data"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def catalog(self, directory, names, version="3.3.0"):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory.parent / f"catalog_{version}.json"
        path.write_text(json.dumps({"m_InternalIds": [f"remote/{name}" for name in names]}), encoding="utf-8")
        path.with_suffix(".hash").write_text("official-version", encoding="ascii")
        return path

    def test_missing_directories_keep_expected_base_fallback(self):
        self.assertEqual(self.directories(), (self.base,))
        self.assertEqual(bundle_file_map(self.directories()), {})
        self.assertIsNone(_latest_catalog_path(self.client_cache))
        self.base.mkdir(parents=True)
        self.assertEqual(self.directories(), (self.base,))
        self.user_cache.mkdir(parents=True)
        self.assertEqual(self.directories(), (self.user_cache, self.base))

    def test_client_cache_precedes_user_cache_and_base(self):
        for directory in (self.client_cache, self.user_cache, self.base):
            directory.mkdir(parents=True)
        self.assertEqual(self.directories(), (self.client_cache, self.user_cache, self.base))
        self.assertEqual(aa_dir_for_exe(self.executable, user_profile=self.profile), self.client_cache)

    def test_catalog_roots_keep_user_metadata_without_resource_subdirectory(self):
        self.client_cache.mkdir(parents=True)
        self.base.mkdir(parents=True)
        self.user_cache.parent.mkdir(parents=True)
        catalog = self.user_cache.parent / "catalog_3.3.0.json"
        catalog.write_text("{}", encoding="utf-8")
        self.assertFalse(self.user_cache.exists())
        self.assertEqual(self.directories(), (self.client_cache, self.base))
        self.assertEqual(unity_catalog_roots_for_exe(self.executable, user_profile=self.profile),
                         (self.client_cache.parent, self.user_cache.parent))
        self.assertTrue((unity_catalog_roots_for_exe(self.executable, user_profile=self.profile)[1] / catalog.name).is_file())

    def test_client_only_cache_uses_executable_stem_for_each_client(self):
        for client in ("AstralParty", "吉星派对"):
            with self.subTest(client=client):
                executable = self.executable.parent / f"{client}.exe"
                cache = executable.parent / f"{client}_Data" / "com.unity.addressables" / "AssetBundles"
                resource = self.cache_file(cache, "only.bundle", client.encode("utf-8"))
                directories = bundle_dirs_for_exe(executable, user_profile=self.profile)
                self.assertEqual(directories, (cache,))
                self.assertEqual(bundle_file_map(directories), {"only.bundle": resource})

    def test_catalog_can_stay_in_user_directory_while_bundles_move(self):
        current = self.cache_file(self.client_cache, "current.bundle", b"CURRENT")
        self.cache_file(self.user_cache, "current.bundle", b"OLD_USER_COPY")
        user_only = self.cache_file(self.user_cache, "user-only.bundle", b"USER_ONLY")
        self.cache_file(self.client_cache, "obsolete.bundle", b"OBSOLETE")
        self.base.mkdir(parents=True)
        (self.base / "current.bundle").write_bytes(b"BASE_COPY")
        base_only = self.base / "base-only.bundle"
        base_only.write_bytes(b"BASE_ONLY")
        (self.base / "obsolete.bundle").write_bytes(b"OBSOLETE")
        catalog = self.catalog(self.user_cache, ["current.bundle", "user-only.bundle", "base-only.bundle"])
        self.assertIsNone(_latest_catalog_path(self.client_cache))
        self.assertEqual(active_catalog_path(self.directories()), catalog.resolve())
        self.assertEqual(bundle_file_map(self.directories()), {
            "base-only.bundle": base_only, "current.bundle": current, "user-only.bundle": user_only,
        })

    def test_adjacent_client_catalog_takes_priority_without_guessing_client_name(self):
        self.cache_file(self.client_cache, "new.bundle", b"NEW")
        self.cache_file(self.user_cache, "old.bundle", b"OLD")
        client_catalog = self.catalog(self.client_cache, ["new.bundle"])
        self.catalog(self.user_cache, ["old.bundle"])
        self.assertEqual(_latest_catalog_path(self.client_cache), client_catalog)
        self.assertEqual(active_catalog_path(self.directories()), client_catalog.resolve())
        self.assertEqual(set(bundle_file_map(self.directories())), {"new.bundle"})

    def test_latest_adjacent_catalog_ignores_directory_with_catalog_name(self):
        previous = self.catalog(self.client_cache, ["old.bundle"], "3.2.0")
        latest = self.catalog(self.client_cache, ["new.bundle"], "3.3.0")
        os.utime(previous, ns=(10, 10))
        os.utime(latest, ns=(20, 20))
        (self.client_cache.parent / "catalog_fake.json").mkdir()
        self.assertEqual(_latest_catalog_path(self.client_cache), latest)

    def test_duplicate_directory_aliases_keep_first_priority_and_stable_fingerprint(self):
        self.client_cache.mkdir(parents=True)
        with patch("astral_party_auto.modkit.bundles.hot_update_dir_for_exe", return_value=self.client_cache):
            self.assertEqual(self.directories(), (self.client_cache,))
        alias = self.client_cache / ".." / "AssetBundles"
        self.assertEqual(bundle_source_key((self.client_cache, alias)), bundle_source_key((self.client_cache,)))

    def test_new_client_cache_invalidates_old_index_source(self):
        self.cache_file(self.user_cache, "current.bundle", b"OLD")
        self.catalog(self.user_cache, ["current.bundle"])
        before = bundle_source_key(self.directories())
        self.cache_file(self.client_cache, "current.bundle", b"CURRENT")
        self.assertNotEqual(bundle_source_key(self.directories()), before)

    def test_downloaded_nested_bundle_changes_source_without_catalog_update(self):
        self.client_cache.mkdir(parents=True)
        self.catalog(self.user_cache, ["download.bundle"])
        directories = self.directories()
        before = bundle_source_key(directories)
        data = self.cache_file(self.client_cache, "download.bundle", b"FIRST")
        after_download = bundle_source_key(directories)
        self.assertNotEqual(before, after_download)
        data.write_bytes(b"SECOND_CONTENT")
        self.assertNotEqual(after_download, bundle_source_key(directories))


if __name__ == "__main__":
    unittest.main()
