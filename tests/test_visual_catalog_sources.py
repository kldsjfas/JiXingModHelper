"""卡面只读目录来源、元数据布局和内置回退约束。"""
from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from astral_party_auto.modkit.catalog_mods import active_catalog_path
from astral_party_auto.modkit.visual_animation import VisualAnimationWorkspace
from astral_party_auto.modkit.visual_catalog import (
    _read_catalog_bundle,
    find_visual_catalog_source,
)
from test_visual_catalog import fixture


class VisualCatalogSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-catalog-source-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.executable = self.root / "game" / "AstralParty_CN.exe"
        self.executable.parent.mkdir()
        self.executable.write_bytes(b"fixture")
        self.data = self.executable.parent / "AstralParty_CN_Data"
        self.client_metadata = self.data / "com.unity.addressables"
        self.user_metadata = self.root / "redirected-Low" / "feimo" / "AstralParty_CN" / "com.unity.addressables"
        self.base = self.data / "StreamingAssets" / "aa"
        self.client_cache = self.client_metadata / "AssetBundles"
        self.base_bundles = self.base / "StandaloneWindows64"
        self.client_cache.mkdir(parents=True)
        self.user_metadata.mkdir(parents=True)
        self.base_bundles.mkdir(parents=True)
        self.directories = (self.client_cache, self.base_bundles)
        self.enterContext(patch("astral_party_auto.modkit.bundles.unity_catalog_roots_for_exe",
                                return_value=(self.client_metadata, self.user_metadata)))
        self.files = {}
        for name in ("config.bundle", "ui.bundle", "atlas.bundle"):
            path = self.base_bundles / name
            path.write_bytes(b"fixture bundle")
            self.files[name] = path

    def write_catalog(self, path, catalog=None):
        path.write_text(json.dumps(fixture() if catalog is None else catalog), encoding="utf-8")
        return path

    def write_settings(self, *, bundled=False, remote=False, **changes):
        name = "catalog.bundle" if bundled else "catalog.json"
        location = {"m_Keys": ["AddressablesMainContentCatalog"],
                    "m_InternalId": "{UnityEngine.AddressableAssets.Addressables.RuntimePath}/" + name,
                    "m_Provider": "UnityEngine.AddressableAssets.ResourceProviders.ContentCatalogProvider",
                    "m_Dependencies": []}
        settings = {"m_IsLocalCatalogInBundle": bundled, "m_DisableCatalogUpdateOnStart": True,
                    "m_CatalogLocations": [location]}
        if remote:
            location["m_Dependencies"] = ["AddressablesMainContentCatalogRemoteHash"]
            settings["m_CatalogLocations"].append({"m_Keys": ["AddressablesMainContentCatalogRemoteHash"],
                                                   "m_InternalId": "{App.WebServerConfig.Path}/catalog_3.hash"})
        settings.update(changes)
        path = self.base / "settings.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        return path

    def source(self):
        return find_visual_catalog_source(self.directories, self.executable)

    def test_catalog_metadata_survives_without_assetbundles_directory(self):
        path = self.write_catalog(self.user_metadata / "catalog_3.3.0.json")
        self.assertFalse((self.user_metadata / "AssetBundles").exists())
        with self.assertRaises(ValueError):
            active_catalog_path(self.directories)
        snapshot = self.source().read(self.files.get)
        self.assertEqual(snapshot.path, path.resolve())
        self.assertEqual(snapshot.kind, "hot_json")
        self.assertEqual(snapshot.locations["GameData_CN"], "config.bundle")
        self.assertEqual(self.directories[0], self.client_cache)
        self.assertFalse((self.user_metadata / "AssetBundles").exists())

    def test_all_hot_roots_are_checked_before_base_catalog(self):
        self.write_catalog(self.base / "catalog.json")
        self.write_settings()
        hot = self.write_catalog(self.user_metadata / "catalog_3.3.0.json")
        self.assertEqual(self.source().path, hot.resolve())

    def test_invalid_hot_catalog_does_not_fall_back_to_valid_base(self):
        hot = self.user_metadata / "catalog_3.json"
        hot.write_bytes(b"not JSON")
        self.write_catalog(self.base / "catalog.json")
        self.write_settings()
        source = self.source()
        self.assertEqual(source.kind, "hot_json")
        with self.assertRaisesRegex(ValueError, "UTF-8 JSON") as result:
            source.read(self.files.get)
        self.assertIn(str(self.user_metadata), str(result.exception))
        self.assertNotIn("独立表情", str(result.exception))

    def test_oversized_json_is_rejected_before_reading_file_contents(self):
        self.write_catalog(self.user_metadata / "catalog_3.json")
        source = self.source()
        with patch("astral_party_auto.modkit.visual_catalog.MAX_CATALOG_BYTES", 5):
            with patch.object(Path, "read_bytes", side_effect=AssertionError("oversized catalog must not be read")):
                with self.assertRaisesRegex(ValueError, "超过 32 MB"):
                    source.read(self.files.get)

    def test_missing_hot_resource_does_not_fall_back_or_guess_another_bundle(self):
        self.write_catalog(self.user_metadata / "catalog_3.json")
        self.write_catalog(self.base / "catalog.json")
        self.write_settings()
        self.files.pop("config.bundle")
        with self.assertRaisesRegex(ValueError, "config.bundle"):
            self.source().read(self.files.get)

    def test_pure_local_json_catalog_is_read_only_and_requires_exact_bundles(self):
        catalog = self.write_catalog(self.base / "catalog.json")
        settings = self.write_settings()
        before = {path: path.read_bytes() for path in (catalog, settings, *self.files.values())}
        snapshot = self.source().read(self.files.get)
        self.assertEqual(snapshot.kind, "base_json")
        self.assertEqual(snapshot.path, catalog.resolve())
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.files["ui.bundle"].unlink()
        with self.assertRaisesRegex(ValueError, "ui.bundle"):
            self.source().read(self.files.get)

    def test_bundled_catalog_has_same_constraints_and_never_creates_hot_json(self):
        catalog = self.base / "catalog.bundle"
        catalog.write_bytes(b"UnityFS fixture")
        self.write_settings(bundled=True)
        with patch("astral_party_auto.modkit.visual_catalog._read_catalog_bundle", return_value=json.dumps(fixture()).encode()):
            snapshot = self.source().read(self.files.get)
        self.assertEqual(snapshot.kind, "base_bundle")
        self.assertFalse(list(self.user_metadata.glob("*.json")))

    def test_remote_or_cache_source_prevents_unverified_base_fallback(self):
        self.write_catalog(self.base / "catalog.json")
        self.write_settings(remote=True)
        with self.assertRaisesRegex(ValueError, "无法确认内置版本当前生效") as result:
            self.source().read(self.files.get)
        self.assertIn(str(self.base / "catalog.json"), str(result.exception))
        self.assertIn(str(self.user_metadata / "catalog_*.json"), str(result.exception))

    def test_unknown_settings_or_identity_cannot_prove_base_source(self):
        self.write_catalog(self.base / "catalog.json")
        for settings in ({"m_CatalogLocations": [{"m_Keys": None}]},
                         {"m_DisableCatalogUpdateOnStart": False},
                         {"m_IsLocalCatalogInBundle": True}):
            with self.subTest(settings=settings):
                self.write_settings(**settings)
                with self.assertRaises(ValueError):
                    self.source().read(self.files.get)
        self.write_settings()
        with patch("astral_party_auto.modkit.bundles.unity_catalog_roots_for_exe", side_effect=ValueError("app.info 缺少产品名")):
            with self.assertRaisesRegex(ValueError, "无法核实当前游戏身份") as result:
                self.source().read(self.files.get)
        self.assertIn("app.info", str(result.exception))

    def test_identity_failure_cannot_reuse_an_existing_hot_catalog(self):
        self.write_catalog(self.user_metadata / "catalog_3.json")
        self.assertEqual(self.source().kind, "hot_json")
        with patch("astral_party_auto.modkit.bundles.unity_catalog_roots_for_exe", side_effect=ValueError("app.info 不再可读")):
            with self.assertRaisesRegex(ValueError, "无法核实当前游戏身份"):
                self.source()

    def test_selected_executable_does_not_search_stale_other_install_directories(self):
        self.write_catalog(self.user_metadata / "catalog_3.json")
        other_executable = self.root / "international" / "AstralParty.exe"
        other_executable.parent.mkdir()
        other_executable.write_bytes(b"fixture")
        international = self.root / "redirected-Low" / "feimo" / "AstralParty" / "com.unity.addressables"
        with patch("astral_party_auto.modkit.bundles.unity_catalog_roots_for_exe",
                   return_value=(other_executable.parent / "AstralParty_Data/com.unity.addressables", international)):
            with self.assertRaises(ValueError) as result:
                find_visual_catalog_source(self.directories + (self.user_metadata / "AssetBundles",), other_executable)
        self.assertIn(str(international), str(result.exception))
        self.assertNotIn(str(self.user_metadata), str(result.exception))

    def test_cached_remote_hash_without_json_refuses_old_local_catalog(self):
        self.write_catalog(self.base / "catalog.json")
        self.write_settings()
        (self.user_metadata / "catalog_3.hash").write_text("official-hash", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "hash.*catalog JSON 缺失"):
            self.source().read(self.files.get)

    def test_new_hash_marker_cannot_reuse_previously_valid_base_context(self):
        self.write_catalog(self.base / "catalog.json")
        self.write_settings()
        controller = SimpleNamespace(aa_dirs=self.directories, game_executable=self.executable,
                                     _require_game=lambda: None, original_bundle_path=self.files.get)
        workspace = VisualAnimationWorkspace(controller)
        workspace._context = {"cached": True}
        workspace._source_key = ("unchanged-resource-files", self.source().fingerprint())
        with patch("astral_party_auto.modkit.visual_animation.bundle_source_key", return_value="unchanged-resource-files"):
            self.assertEqual(workspace.context(), {"cached": True})
            (self.user_metadata / "catalog_3.hash").write_text("new-official-hash", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "hash.*catalog JSON 缺失"):
                workspace.context()

    def test_catalog_and_settings_changes_alter_source_fingerprint(self):
        catalog = self.write_catalog(self.base / "catalog.json")
        settings = self.write_settings()
        source = self.source()
        before = source.fingerprint()
        stamp = catalog.stat()
        raw = catalog.read_bytes()
        catalog.write_bytes(raw.replace(b"config", b"Config"))
        os.utime(catalog, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        self.assertNotEqual(source.fingerprint(), before)
        second = source.fingerprint()
        settings.write_text(settings.read_text().replace("true", "false"), encoding="utf-8")
        self.assertNotEqual(source.fingerprint(), second)

    def test_missing_catalog_diagnostic_lists_actual_searched_paths(self):
        with self.assertRaises(ValueError) as result:
            self.source()
        message = str(result.exception)
        self.assertIn("卡面制作", message)
        self.assertIn(str(self.user_metadata / "catalog_*.json"), message)
        self.assertIn(str(self.base / "catalog.bundle"), message)
        self.assertNotIn("独立表情", message)


class BundledCatalogReadTests(unittest.TestCase):
    def test_unique_text_asset_and_expansion_limits_are_required(self):
        with TemporaryDirectory(prefix="jixing-bundled-catalog-") as folder:
            path = Path(folder) / "catalog.bundle"
            path.write_bytes(b"UnityFS mock")
            asset = Mock()
            asset.type.name = "TextAsset"
            asset.peek_name.return_value = "catalog"
            asset.read.return_value = SimpleNamespace(m_Script=b'{"catalog":true}')
            with patch("astral_party_auto.modkit.workshop_security.inspect_resource_bundle") as inspect:
                with patch("astral_party_auto.modkit.animation._load_bundle", return_value=SimpleNamespace(objects=[asset])):
                    self.assertEqual(_read_catalog_bundle(path), b'{"catalog":true}')
                self.assertEqual(inspect.call_args.kwargs["max_expanded_bytes"], 48 * 1024 * 1024)
                for objects in ([], [asset, asset]):
                    with patch("astral_party_auto.modkit.animation._load_bundle", return_value=SimpleNamespace(objects=objects)):
                        with self.assertRaisesRegex(ValueError, "只含一份"):
                            _read_catalog_bundle(path)
            with patch("astral_party_auto.modkit.visual_catalog.MAX_CATALOG_BYTES", 5):
                with self.assertRaisesRegex(ValueError, "超过"):
                    _read_catalog_bundle(path)


if __name__ == "__main__":
    unittest.main()
