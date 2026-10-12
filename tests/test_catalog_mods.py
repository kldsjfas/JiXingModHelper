"""Independent addresses, owned file removal and catalog drift stay transactional."""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astral_party_auto.modkit import catalog_mods
from astral_party_auto.modkit.emote_catalog import read_emote_locations
from astral_party_auto.modkit.manager import ModManager
from tests.test_emote_addressables import fixture


class CatalogModTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / "qa-evidence" / "emote-media-oct12" / "catalog-fixtures"
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="case-", dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.hot = self.root / "cache" / "AssetBundles"
        self.base = self.root / "base"
        self.hot.mkdir(parents=True)
        self.base.mkdir()
        self.catalog = self.hot.parent / "catalog_1.json"
        self.hash_file = self.catalog.with_suffix(".hash")
        self.baseline = json.dumps(fixture(), indent=2).encode("utf-8")
        self.catalog.write_bytes(self.baseline)
        self.hash_file.write_bytes(b"OFFICIAL_REMOTE_VERSION\n")
        for name in ("config.bundle", "video.bundle", "scripts.bundle"):
            (self.base / name).write_bytes(("BASE:" + name).encode())
        (self.hot / "config.bundle").write_bytes(b"HOT_ORIGINAL_CONFIG")
        self.manager = ModManager((self.hot, self.base), self.root / "data")

    def make_mod(self, name="one", identifier=120101008, content=b"NEW_VIDEO", config=True):
        folder = self.root / name
        folder.mkdir()
        character, number = divmod(identifier - 120_000_000, 1000)
        item = {"emote_id": identifier, "video_name": f"VEmoji_{character}_{number:02d}_999",
                "bundle_name": f"jixing_emote_{identifier}.bundle", "template_video_key": "VEmoji_101_09",
                "bundle_size": len(content)}
        (folder / item["bundle_name"]).write_bytes(content)
        if config:
            (folder / "config.bundle").write_bytes(("MOD_CONFIG:" + name).encode())
        catalog_mods.write_address_manifest(folder, [item])
        return folder, item

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*")
                if path.is_file() and (path.is_relative_to(self.hot.parent) or path.is_relative_to(self.base)
                                      or path == self.manager.state_path)}

    def videos(self):
        return read_emote_locations(json.loads(self.catalog.read_text(encoding="utf-8")))["videos"]

    def test_install_and_uninstall_preserve_official_hash_and_base_files(self):
        source, item = self.make_mod()
        before = self.snapshot()
        analysis = self.manager.install_mod(source, "One")
        self.assertEqual(analysis.new_bundles, [item["bundle_name"]])
        self.assertEqual(analysis.unmatched, [])
        self.assertEqual(self.videos()[item["video_name"]], {item["bundle_name"], "scripts.bundle"})
        self.assertEqual(self.videos()["VEmoji_101_09"], {"video.bundle", "scripts.bundle"})
        self.assertEqual((self.hot / item["bundle_name"]).read_bytes(), b"NEW_VIDEO")
        self.assertEqual(self.manager.bundle_path(item["bundle_name"]), self.hot / item["bundle_name"])
        self.assertEqual(self.hash_file.read_bytes(), b"OFFICIAL_REMOTE_VERSION\n")
        self.assertEqual((self.base / "config.bundle").read_bytes(), b"BASE:config.bundle")
        state = self.manager._load_state()
        self.assertEqual(state["catalog_overlay"]["bundles"][item["bundle_name"]],
                         catalog_mods.file_sha256(self.hot / item["bundle_name"]))
        self.assertIs(state["catalog_overlay"]["original_exists"][item["bundle_name"]], False)
        self.manager.uninstall_mod("One")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.assertFalse((self.hot / item["bundle_name"]).exists())
        self.assertEqual((self.hot / "config.bundle").read_bytes(), b"HOT_ORIGINAL_CONFIG")
        self.assertNotIn("catalog_overlay", self.manager._load_state())
        self.assertEqual({key: value for key, value in self.snapshot().items() if not key.endswith("mods_state.json")}, before)

    def test_disable_enable_and_restore_all_roundtrip(self):
        source, item = self.make_mod()
        self.manager.install_mod(source, "One")
        applied_catalog = self.catalog.read_bytes()
        self.manager.disable_mod("One")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.assertFalse((self.hot / item["bundle_name"]).exists())
        self.assertIsNone(self.manager.bundle_path(item["bundle_name"]))
        self.manager.enable_mod("One")
        self.assertEqual(self.catalog.read_bytes(), applied_catalog)
        self.assertTrue((self.hot / item["bundle_name"]).exists())
        self.manager.restore_all()
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.assertFalse((self.hot / item["bundle_name"]).exists())
        self.assertEqual(self.manager._load_state(), {"mods": {}})

    def test_two_mods_merge_addresses_then_uninstall_one(self):
        first, first_item = self.make_mod("first", 120101008)
        second, second_item = self.make_mod("second", 120102008)
        self.manager.install_mod(first, "First")
        self.manager.install_mod(second, "Second")
        self.assertIn(first_item["video_name"], self.videos())
        self.assertIn(second_item["video_name"], self.videos())
        self.manager.uninstall_mod("First")
        self.assertNotIn(first_item["video_name"], self.videos())
        self.assertIn(second_item["video_name"], self.videos())
        self.assertFalse((self.hot / first_item["bundle_name"]).exists())
        self.assertTrue((self.hot / second_item["bundle_name"]).exists())
        self.manager.restore_all()
        self.assertEqual(self.catalog.read_bytes(), self.baseline)

    def test_overlapping_addresses_refuse_without_mutation(self):
        first, item = self.make_mod("first")
        second, _ = self.make_mod("second", content=b"OTHER_VIDEO")
        self.manager.install_mod(first, "First")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "两个启用"):
            self.manager.install_mod(second, "Second")
        self.assertEqual(self.snapshot(), before)
        self.manager.disable_mod("First")
        self.manager.install_mod(second, "Second")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.manager.enable_mod("First")
        self.assertEqual(self.snapshot(), before)

    def test_reinstall_same_mod_uses_new_owned_bytes_and_cached_manifest(self):
        first, item = self.make_mod("first")
        second, _ = self.make_mod("second", content=b"UPDATED_VIDEO")
        self.manager.install_mod(first, "One")
        self.manager.install_mod(second, "One")
        self.assertEqual((self.hot / item["bundle_name"]).read_bytes(), b"UPDATED_VIDEO")
        store = Path(self.manager.installed_mods()[0]["store"])
        self.manager.install_mod(store, "One")
        self.manager.uninstall_mod("One")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)

    def test_reloaded_manager_does_not_back_up_created_bundle_as_original(self):
        source, item = self.make_mod()
        self.manager.install_mod(source, "One")
        self.manager = ModManager((self.hot, self.base), self.root / "data")
        self.assertIsNotNone(self.manager.bundle_path(item["bundle_name"]))
        self.manager.disable_mod("One")
        self.assertFalse((self.manager.backup_dir / item["bundle_name"]).exists())
        self.assertFalse((self.hot / item["bundle_name"]).exists())
        self.manager.enable_mod("One")
        self.manager.uninstall_mod("One")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)

    def test_catalog_content_drift_refuses_all_destructive_operations(self):
        source, item = self.make_mod()
        self.manager.install_mod(source, "One")
        self.catalog.write_bytes(self.catalog.read_bytes() + b"\n ")
        before = self.snapshot()
        for operation in (lambda: self.manager.disable_mod("One"), lambda: self.manager.uninstall_mod("One"),
                          self.manager.restore_all, lambda: self.manager.install_mod(source, "One")):
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "catalog"):
                operation()
            self.assertEqual(self.snapshot(), before)

    def test_official_version_or_active_catalog_change_refuses_restore(self):
        source, _ = self.make_mod()
        self.manager.install_mod(source, "One")
        self.hash_file.write_text("NEW_OFFICIAL_VERSION", encoding="ascii")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.manager.restore_all()
        self.assertEqual(self.snapshot(), before)
        self.hash_file.write_bytes(b"OFFICIAL_REMOTE_VERSION\n")
        new_catalog = self.hot.parent / "catalog_2.json"
        new_catalog.write_bytes(self.baseline)
        new_catalog.with_suffix(".hash").write_text("OFFICIAL_REMOTE_VERSION", encoding="ascii")
        newer = self.catalog.stat().st_mtime_ns + 1_000_000_000
        os.utime(new_catalog, ns=(newer, newer))
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.manager.restore_all()
        self.assertEqual(self.snapshot(), before)

    def test_modified_or_missing_owned_bundle_refuses_delete(self):
        source, item = self.make_mod()
        self.manager.install_mod(source, "One")
        target = self.hot / item["bundle_name"]
        for content in (b"USER_CHANGED_VIDEO", None):
            if content is None:
                target.unlink()
            else:
                target.write_bytes(content)
            before = self.snapshot()
            with self.assertRaises(ValueError):
                self.manager.uninstall_mod("One")
            self.assertEqual(self.snapshot(), before)

    def test_existing_unknown_file_is_not_overwritten(self):
        source, item = self.make_mod()
        target = self.hot / item["bundle_name"]
        target.write_bytes(b"KEEP_UNKNOWN")
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "未知"):
            self.manager.install_mod(source, "One")
        self.assertEqual(self.snapshot(), before)

    def test_tampered_source_cache_cannot_be_enabled(self):
        source, item = self.make_mod()
        self.manager.install_mod(source, "One")
        self.manager.disable_mod("One")
        store = Path(self.manager.installed_mods()[0]["store"])
        (store / item["bundle_name"]).write_bytes(b"X" * item["bundle_size"])
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.manager.enable_mod("One")
        self.assertEqual(self.snapshot(), before)

    def test_state_save_failure_rolls_back_catalog_config_and_created_file(self):
        source, item = self.make_mod()
        before = self.snapshot()
        with patch.object(self.manager, "_save_state", side_effect=OSError("state failure")):
            with self.assertRaises(OSError):
                self.manager.install_mod(source, "One")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.hot / item["bundle_name"]).exists())
        self.assertEqual(list(self.manager.data_dir.glob("mod-change-*")), [])

    def test_catalog_commit_failure_rolls_back_created_file(self):
        source, _ = self.make_mod()
        before = self.snapshot()
        atomic = catalog_mods.atomic_bytes

        def fail_catalog(target, payload, **kwargs):
            if target == self.catalog:
                raise OSError("catalog failure")
            return atomic(target, payload, **kwargs)

        with patch.object(catalog_mods, "atomic_bytes", side_effect=fail_catalog):
            with self.assertRaises(OSError):
                self.manager.install_mod(source, "One")
        self.assertEqual(self.snapshot(), before)

    def test_new_bundle_copy_failure_keeps_baseline(self):
        source, item = self.make_mod()
        before = self.snapshot()
        copy_file = shutil.copy2

        def fail_staged_bundle(original, destination, *args, **kwargs):
            if Path(destination).name.startswith(".emote-bundle-"):
                Path(destination).write_bytes(b"PARTIAL")
                raise OSError("copy failure")
            return copy_file(original, destination, *args, **kwargs)

        with patch.object(catalog_mods.shutil, "copy2", side_effect=fail_staged_bundle):
            with self.assertRaises(OSError):
                self.manager.install_mod(source, "One")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list(self.hot.glob(".emote-bundle-*")), [])

    def test_uninstall_delete_failure_rolls_back_catalog_and_state(self):
        source, item = self.make_mod()
        self.manager.install_mod(source, "One")
        before = self.snapshot()
        unlink = Path.unlink
        target = self.hot / item["bundle_name"]

        def fail_owned_delete(path, *args, **kwargs):
            if path == target:
                raise PermissionError("owned file locked")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_owned_delete):
            with self.assertRaises(PermissionError):
                self.manager.uninstall_mod("One")
        self.assertEqual(self.snapshot(), before)

    def test_bad_baseline_backup_refuses_restore(self):
        source, _ = self.make_mod()
        self.manager.install_mod(source, "One")
        backup = next((self.manager.data_dir / "catalog_backups").glob("*.json"))
        backup.write_bytes(b"BAD_BACKUP")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.manager.restore_all()
        self.assertEqual(self.snapshot(), before)

    def test_missing_catalog_hash_refuses_initial_install(self):
        source, _ = self.make_mod()
        self.hash_file.unlink()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.manager.install_mod(source, "One")
        self.assertEqual(self.snapshot(), before)

    def test_manifest_rejects_paths_unknown_fields_sizes_and_duplicate_addresses(self):
        source, item = self.make_mod()
        manifest = source / catalog_mods.MANIFEST_NAME
        baseline = json.loads(manifest.read_text(encoding="utf-8"))
        mutations = [lambda value: value["patches"][0].update(bundle_name="../outside.bundle"),
                     lambda value: value["patches"][0].update(emote_id=True),
                     lambda value: value["patches"][0].update(bundle_size=True),
                     lambda value: value["patches"][0].update(bundle_size=999),
                     lambda value: value["patches"][0].update(target="C:\\outside.bundle"),
                     lambda value: value["patches"].append(dict(value["patches"][0])),
                     lambda value: value.update(version=True), lambda value: value.update(extra="unknown")]
        for mutation in mutations:
            value = copy.deepcopy(baseline)
            mutation(value)
            manifest.write_text(json.dumps(value), encoding="utf-8")
            before = self.snapshot()
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager.install_mod(source, "One")
            self.assertEqual(self.snapshot(), before)
        manifest.write_text('{"format":"jixing-emote-addresses","format":"jixing-emote-addresses","version":1,"patches":[]}', encoding="utf-8")
        with self.assertRaises(ValueError):
            self.manager.analyze_mod(source)

    def test_unmanifested_or_unmatched_files_refuse_independent_install(self):
        source, item = self.make_mod()
        (source / "not-this-version.bundle").write_bytes(b"OTHER_VERSION")
        with self.assertRaises(ValueError):
            self.manager.analyze_mod(source)
        (source / "not-this-version.bundle").unlink()
        (source / catalog_mods.MANIFEST_NAME).unlink()
        with self.assertRaises(ValueError):
            self.manager.analyze_mod(source)

    def test_interrupted_recovery_blocks_new_changes(self):
        source, _ = self.make_mod()
        recovery = self.manager.data_dir / "mod-change-interrupted"
        recovery.mkdir(parents=True)
        (recovery / "recovery.json").write_text("[]", encoding="utf-8")
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, "未完成"):
            self.manager.install_mod(source, "One")
        self.assertEqual(self.snapshot(), before)

    def test_reload_requirement_covers_generic_changes_and_disabled_emotes(self):
        self.assertFalse(self.manager.requires_catalog_reload())
        self.assertFalse(self.manager.requires_catalog_reload("Unknown"))
        source, _ = self.make_mod()
        self.manager.install_mod(source, "One")
        self.assertTrue(self.manager.requires_catalog_reload("One"))
        self.assertTrue(self.manager.requires_catalog_reload("Some ordinary Mod"))
        self.manager.disable_mod("One")
        self.assertTrue(self.manager.requires_catalog_reload())
        self.manager.uninstall_mod("One")
        self.assertFalse(self.manager.requires_catalog_reload())

    def test_changed_created_file_is_preserved_when_rollback_cannot_delete_it(self):
        source, item = self.make_mod()
        target = self.hot / item["bundle_name"]

        def fail_after_unrelated_write(state):
            target.write_bytes(b"")
            raise OSError("state failure after another writer changed the new file")

        with patch.object(self.manager, "_save_state", side_effect=fail_after_unrelated_write):
            with self.assertRaisesRegex(RuntimeError, "副本已保留"):
                self.manager.install_mod(source, "One")
        self.assertEqual(target.read_bytes(), b"")
        recovery = next(self.manager.data_dir.glob("mod-change-*"))
        journal = json.loads((recovery / "recovery.json").read_text(encoding="utf-8"))
        record = next(record for record in journal if record["target"] == str(target))
        self.assertFalse(record["existed"])
        self.assertIsNone(record["copy"])
        self.assertEqual(len(record["installed_sha256"]), 64)
        with self.assertRaisesRegex(RuntimeError, "未完成"):
            self.manager.restore_all()

    def test_false_overlay_version_does_not_pass_integer_version_check(self):
        source, _ = self.make_mod()
        self.manager.install_mod(source, "One")
        state = self.manager._load_state()
        state["catalog_overlay"]["version"] = True
        self.manager._save_state(state)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.manager.restore_all()
        self.assertEqual(self.snapshot(), before)

    def test_external_existing_bundle_and_catalog_survive_failed_reinstall(self):
        source, item = self.make_mod("first")
        replacement, _ = self.make_mod("second", content=b"REINSTALL_VIDEO")
        self.manager.install_mod(source, "One")
        target = self.hot / item["bundle_name"]
        state_before = self.manager.state_path.read_bytes()
        unknown_bundle, unknown_catalog = b"ANOTHER_WRITER_VIDEO", b"ANOTHER_WRITER_CATALOG"

        def fail_after_external_changes(state):
            target.write_bytes(unknown_bundle)
            self.catalog.write_bytes(unknown_catalog)
            raise OSError("state failure after external writes")

        with patch.object(self.manager, "_save_state", side_effect=fail_after_external_changes):
            with self.assertRaisesRegex(RuntimeError, "副本已保留"):
                self.manager.install_mod(replacement, "One")
        self.assertEqual(target.read_bytes(), unknown_bundle)
        self.assertEqual(self.catalog.read_bytes(), unknown_catalog)
        self.assertEqual(self.manager.state_path.read_bytes(), state_before)
        recovery = next(self.manager.data_dir.glob("mod-change-*"))
        journal = json.loads((recovery / "recovery.json").read_text(encoding="utf-8"))
        for path in (target, self.catalog):
            record = next(row for row in journal if row["target"] == str(path))
            self.assertTrue(record["atomic_target"])
            self.assertTrue((recovery / record["copy"]).is_file())

    def test_unknown_new_target_during_staging_is_never_overwritten(self):
        source, item = self.make_mod()
        target = self.hot / item["bundle_name"]
        copy_file = shutil.copy2
        unknown = b"UNKNOWN_FILE_CREATED_DURING_STAGE"

        def create_unknown_target(original, destination, *args, **kwargs):
            result = copy_file(original, destination, *args, **kwargs)
            if Path(destination).name.startswith(".emote-bundle-"):
                target.write_bytes(unknown)
            return result

        with patch.object(catalog_mods.shutil, "copy2", side_effect=create_unknown_target):
            with self.assertRaisesRegex(RuntimeError, "副本已保留"):
                self.manager.install_mod(source, "One")
        self.assertEqual(target.read_bytes(), unknown)
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.assertEqual((self.hot / "config.bundle").read_bytes(), b"HOT_ORIGINAL_CONFIG")
        self.assertFalse(self.manager.state_path.exists())
        self.assertEqual(list(self.hot.glob(".emote-bundle-*")), [])

    def test_changed_owned_bundle_during_staging_is_preserved(self):
        source, item = self.make_mod("first")
        replacement, _ = self.make_mod("second", content=b"REINSTALL_VIDEO")
        self.manager.install_mod(source, "One")
        target = self.hot / item["bundle_name"]
        copy_file = shutil.copy2
        catalog_before, state_before = self.catalog.read_bytes(), self.manager.state_path.read_bytes()

        def change_owned_target(original, destination, *args, **kwargs):
            result = copy_file(original, destination, *args, **kwargs)
            if Path(destination).name.startswith(".emote-bundle-"):
                target.write_bytes(b"UNKNOWN_OWNED_FILE_CHANGE")
            return result

        with patch.object(catalog_mods.shutil, "copy2", side_effect=change_owned_target):
            with self.assertRaisesRegex(RuntimeError, "副本已保留"):
                self.manager.install_mod(replacement, "One")
        self.assertEqual(target.read_bytes(), b"UNKNOWN_OWNED_FILE_CHANGE")
        self.assertEqual(self.catalog.read_bytes(), catalog_before)
        self.assertEqual(self.manager.state_path.read_bytes(), state_before)

    def test_official_hash_change_after_prepare_prevents_install(self):
        source, item = self.make_mod()
        apply_files = self.manager._apply_effective_files

        def change_version(state, names):
            result = apply_files(state, names)
            self.hash_file.write_bytes(b"NEXT_OFFICIAL_VERSION")
            return result

        with patch.object(self.manager, "_apply_effective_files", side_effect=change_version):
            with self.assertRaisesRegex(ValueError, "官方"):
                self.manager.install_mod(source, "One")
        self.assertEqual(self.hash_file.read_bytes(), b"NEXT_OFFICIAL_VERSION")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.assertFalse((self.hot / item["bundle_name"]).exists())
        self.assertFalse(self.manager.state_path.exists())
        self.assertEqual((self.hot / "config.bundle").read_bytes(), b"HOT_ORIGINAL_CONFIG")

    def test_official_hash_is_rechecked_after_catalog_staging(self):
        source, item = self.make_mod()
        atomic = catalog_mods.atomic_bytes

        def change_version_at_commit(target, payload, **kwargs):
            if target == self.catalog:
                check = kwargs["before_commit"]

                def change_then_check():
                    self.hash_file.write_bytes(b"NEXT_OFFICIAL_VERSION")
                    check()

                kwargs["before_commit"] = change_then_check
            return atomic(target, payload, **kwargs)

        with patch.object(catalog_mods, "atomic_bytes", side_effect=change_version_at_commit):
            with self.assertRaisesRegex(ValueError, "官方"):
                self.manager.install_mod(source, "One")
        self.assertEqual(self.hash_file.read_bytes(), b"NEXT_OFFICIAL_VERSION")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.assertFalse((self.hot / item["bundle_name"]).exists())
        self.assertFalse(self.manager.state_path.exists())
        self.assertEqual(list(self.catalog.parent.glob(".emote-catalog-*")), [])

    def test_external_catalog_change_during_staging_survives_rollback(self):
        source, item = self.make_mod()
        atomic = catalog_mods.atomic_bytes
        unknown = b"UNKNOWN_NEW_CATALOG_CONTENT"

        def change_catalog_at_commit(target, payload, **kwargs):
            if target == self.catalog:
                check = kwargs["before_commit"]

                def change_then_check():
                    self.catalog.write_bytes(unknown)
                    check()

                kwargs["before_commit"] = change_then_check
            return atomic(target, payload, **kwargs)

        with patch.object(catalog_mods, "atomic_bytes", side_effect=change_catalog_at_commit):
            with self.assertRaisesRegex(RuntimeError, "副本已保留"):
                self.manager.install_mod(source, "One")
        self.assertEqual(self.catalog.read_bytes(), unknown)
        self.assertFalse((self.hot / item["bundle_name"]).exists())
        self.assertTrue(any(self.manager.data_dir.glob("mod-change-*")))

    def test_final_version_check_rolls_back_saved_state_without_reverting_hash(self):
        source, item = self.make_mod()
        save_state = self.manager._save_state

        def change_version_after_state_save(state):
            save_state(state)
            self.hash_file.write_bytes(b"NEXT_OFFICIAL_VERSION")

        with patch.object(self.manager, "_save_state", side_effect=change_version_after_state_save):
            with self.assertRaisesRegex(ValueError, "官方"):
                self.manager.install_mod(source, "One")
        self.assertEqual(self.hash_file.read_bytes(), b"NEXT_OFFICIAL_VERSION")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.assertFalse((self.hot / item["bundle_name"]).exists())
        self.assertFalse(self.manager.state_path.exists())

    def test_rollback_never_deletes_an_unknown_link_even_when_bytes_match(self):
        source, item = self.make_mod()
        target = self.hot / item["bundle_name"]
        is_symlink = Path.is_symlink

        def unknown_link(path):
            return path == target or is_symlink(path)

        with patch.object(Path, "is_symlink", unknown_link), \
                patch.object(self.manager, "_save_state", side_effect=OSError("state failure")):
            with self.assertRaisesRegex(RuntimeError, "副本已保留"):
                self.manager.install_mod(source, "One")
        self.assertEqual(target.read_bytes(), b"NEW_VIDEO")
        self.assertTrue(any(self.manager.data_dir.glob("mod-change-*")))

    def test_unknown_new_file_matching_mod_bytes_is_not_claimed_or_deleted(self):
        source, item = self.make_mod()
        target = self.hot / item["bundle_name"]
        copy_file = shutil.copy2

        def another_writer_installs_same_bytes(original, destination, *args, **kwargs):
            result = copy_file(original, destination, *args, **kwargs)
            if Path(destination).name.startswith(".emote-bundle-"):
                target.write_bytes(b"NEW_VIDEO")
            return result

        with patch.object(catalog_mods.shutil, "copy2", side_effect=another_writer_installs_same_bytes):
            with self.assertRaisesRegex(RuntimeError, "副本已保留"):
                self.manager.install_mod(source, "One")
        self.assertEqual(target.read_bytes(), b"NEW_VIDEO")
        self.assertEqual(self.catalog.read_bytes(), self.baseline)
        self.assertFalse(self.manager.state_path.exists())
        recovery = next(self.manager.data_dir.glob("mod-change-*"))
        journal = json.loads((recovery / "recovery.json").read_text(encoding="utf-8"))
        record = next(row for row in journal if row["target"] == str(target))
        self.assertFalse(record["committed"])
        self.assertIsNone(record["committed_sha256"])


if __name__ == "__main__":
    unittest.main()
