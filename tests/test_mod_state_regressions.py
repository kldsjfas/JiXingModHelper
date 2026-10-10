"""Mod 文件操作失败时，游戏、覆盖顺序和缓存必须仍能回到操作前。"""
from __future__ import annotations

import shutil
import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astral_party_auto.modkit.manager import ModManager


class ModStateTests(unittest.TestCase):
    def setUp(self):
        fixture_parent = Path(__file__).resolve().parents[1] / "qa-evidence" / "bug-audit-oct10" / "mod-state-fixtures"
        fixture_parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="case-", dir=fixture_parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.game = self.root / "game"
        self.game.mkdir()
        self.originals = {"a.bundle": b"ORIGINAL_A", "b.bundle": b"ORIGINAL_B"}
        self.replacements = {"a.bundle": b"MOD_A", "b.bundle": b"MOD_B"}
        for name, data in self.originals.items():
            (self.game / name).write_bytes(data)
        self.source = self.make_mod("source", self.replacements)
        self.manager = ModManager(self.game, self.root / "data")

    def make_mod(self, directory, files):
        folder = self.root / directory
        folder.mkdir()
        for name, data in files.items():
            (folder / name).write_bytes(data)
        return folder

    def game_contents(self):
        return {name: (self.game / name).read_bytes() for name in self.originals}

    def state_bytes(self):
        return self.manager.state_path.read_bytes() if self.manager.state_path.exists() else None

    def fail_second_game_copy(self, source, destination, *args, **kwargs):
        if Path(destination) == self.game / "b.bundle":
            Path(destination).write_bytes(b"PARTIAL_COPY")
            raise OSError("simulated locked or full target")
        return self.real_copy(source, destination, *args, **kwargs)

    def assert_copy_failure_keeps_state(self, operation):
        game_before = self.game_contents()
        state_before = self.state_bytes()
        self.real_copy = shutil.copy2
        with patch("astral_party_auto.modkit.manager.shutil.copy2", side_effect=self.fail_second_game_copy):
            with self.assertRaises((OSError, RuntimeError)):
                operation()
        self.assertEqual(self.game_contents(), game_before)
        self.assertEqual(self.state_bytes(), state_before)
        self.assertEqual(list(self.manager.data_dir.glob("mod-change-*")), [])

    def test_install_copy_failure_keeps_game_and_state(self):
        self.assert_copy_failure_keeps_state(lambda: self.manager.install_mod(self.source, "A"))

    def test_failed_original_backup_never_becomes_a_restore_source(self):
        copy = shutil.copy2

        def fail_backup(source, destination, *args, **kwargs):
            if Path(source) == self.game / "b.bundle" and Path(destination).parent == self.manager.backup_dir:
                Path(destination).write_bytes(b"PARTIAL_BACKUP")
                raise OSError("simulated full backup disk")
            return copy(source, destination, *args, **kwargs)

        with patch("astral_party_auto.modkit.manager.shutil.copy2", side_effect=fail_backup):
            with self.assertRaises(OSError):
                self.manager.install_mod(self.source, "A")
        self.assertFalse((self.manager.backup_dir / "b.bundle").exists())
        self.assertEqual(self.game_contents(), self.originals)
        self.manager.install_mod(self.source, "A")
        self.manager.restore_all()
        self.assertEqual(self.game_contents(), self.originals)

    def test_disable_copy_failure_keeps_game_and_state(self):
        self.manager.install_mod(self.source, "A")
        self.assert_copy_failure_keeps_state(lambda: self.manager.disable_mod("A"))

    def test_enable_copy_failure_keeps_game_and_state(self):
        self.manager.install_mod(self.source, "A")
        self.manager.disable_mod("A")
        self.assert_copy_failure_keeps_state(lambda: self.manager.enable_mod("A"))

    def test_uninstall_copy_failure_keeps_game_and_cache(self):
        self.manager.install_mod(self.source, "A")
        store = Path(self.manager.installed_mods()[0]["store"])
        self.assert_copy_failure_keeps_state(lambda: self.manager.uninstall_mod("A"))
        self.assertEqual((store / "b.bundle").read_bytes(), self.replacements["b.bundle"])

    def test_restore_copy_failure_keeps_game_and_state(self):
        self.manager.install_mod(self.source, "A")
        self.assert_copy_failure_keeps_state(self.manager.restore_all)

    def test_rollback_stages_in_game_directory_for_cross_volume_support(self):
        self.manager.install_mod(self.source, "A")
        replace = Path.replace

        def reject_cross_directory_move(source, destination):
            destination = Path(destination)
            if destination.parent == self.game and source.parent != self.game:
                raise OSError(errno.EXDEV, "simulated game and data on different volumes")
            return replace(source, destination)

        with patch.object(Path, "replace", reject_cross_directory_move):
            self.assert_copy_failure_keeps_state(lambda: self.manager.disable_mod("A"))
        self.assertEqual(list(self.game.glob(".mod-restore-*")), [])

    def test_rollback_failure_keeps_named_recovery_evidence(self):
        self.manager.install_mod(self.source, "A")
        self.real_copy = shutil.copy2
        replace = Path.replace
        state_before = self.state_bytes()

        def fail_locked_recovery(source, destination):
            if source.name.startswith(".mod-restore-") and Path(destination) == self.game / "b.bundle":
                raise PermissionError("simulated game still using this file")
            return replace(source, destination)

        with patch("astral_party_auto.modkit.manager.shutil.copy2", side_effect=self.fail_second_game_copy), \
                patch.object(Path, "replace", fail_locked_recovery):
            with self.assertRaisesRegex(RuntimeError, "副本已保留"):
                self.manager.disable_mod("A")
        recovery = list(self.manager.data_dir.glob("mod-change-*"))
        self.assertEqual(len(recovery), 1)
        self.assertEqual((recovery[0] / "1").read_bytes(), self.replacements["b.bundle"])
        self.assertIn("b.bundle", (recovery[0] / "recovery.json").read_text(encoding="utf-8"))
        self.assertEqual(self.state_bytes(), state_before)

    def test_invalid_state_refuses_mutations_without_touching_game_or_backups(self):
        self.manager.install_mod(self.source, "A")
        invalid_values = (
            b"{broken", b"\xff\xff", b"[]", b"null", b"{}", b'{"mods": []}',
            b'{"mods": {"A": "invalid"}}', b'{"mods": {"A": {"files": "a.bundle"}}}',
            b'{"mods": {"A": {"files": [{}]}}}',
            b'{"mods": {"A": {"files": ["a.bundle"], "disabled": "yes"}}}',
            b'{"mods": {"A": {"files": ["a.bundle"], "store": []}}}',
        )
        operations = (
            lambda: self.manager.install_mod(self.source, "A"), self.manager.restore_all,
            lambda: self.manager.disable_mod("A"), lambda: self.manager.enable_mod("A"),
            lambda: self.manager.uninstall_mod("A"),
        )
        for invalid in invalid_values:
            self.manager.state_path.write_bytes(invalid)
            data_before = {str(path.relative_to(self.manager.data_dir)): path.read_bytes()
                           for path in self.manager.data_dir.rglob("*") if path.is_file()}
            game_before = self.game_contents()
            for operation in operations:
                with self.subTest(state=invalid, operation=operation):
                    with self.assertRaisesRegex(RuntimeError, "状态"):
                        operation()
                    self.assertEqual(self.game_contents(), game_before)
                    self.assertEqual(
                        {str(path.relative_to(self.manager.data_dir)): path.read_bytes()
                         for path in self.manager.data_dir.rglob("*") if path.is_file()}, data_before,
                    )

    def test_unreadable_state_refuses_mutations_without_touching_data(self):
        self.manager.install_mod(self.source, "A")
        read_text = Path.read_text
        data_before = {str(path.relative_to(self.manager.data_dir)): path.read_bytes()
                       for path in self.manager.data_dir.rglob("*") if path.is_file()}
        game_before = self.game_contents()

        def deny_state_read(path, *args, **kwargs):
            if path == self.manager.state_path:
                raise PermissionError("simulated unreadable state")
            return read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", deny_state_read):
            for operation in (lambda: self.manager.install_mod(self.source, "A"), self.manager.restore_all):
                with self.assertRaisesRegex(RuntimeError, "状态"):
                    operation()
        self.assertEqual(self.game_contents(), game_before)
        self.assertEqual({str(path.relative_to(self.manager.data_dir)): path.read_bytes()
                          for path in self.manager.data_dir.rglob("*") if path.is_file()}, data_before)

    def test_missing_state_is_still_a_clean_initial_install(self):
        self.assertEqual(self.manager._load_state(), {"mods": {}})
        self.manager.install_mod(self.source, "A")
        self.assertEqual(self.game_contents(), self.replacements)

    def test_state_save_failure_keeps_reinstall_and_old_cache(self):
        self.manager.install_mod(self.source, "A")
        store = Path(self.manager.installed_mods()[0]["store"])
        replacement = self.make_mod("new-version", {"a.bundle": b"NEW_A", "b.bundle": b"NEW_B"})
        game_before = self.game_contents()
        state_before = self.state_bytes()
        caches_before = set((self.manager.data_dir / "mod_store").iterdir())
        with patch.object(self.manager, "_save_state", side_effect=OSError("simulated state write failure")):
            with self.assertRaises(OSError):
                self.manager.install_mod(replacement, "A")
        self.assertEqual(self.game_contents(), game_before)
        self.assertEqual(self.state_bytes(), state_before)
        self.assertEqual({name: (store / name).read_bytes() for name in self.originals}, self.replacements)
        self.assertEqual(set((self.manager.data_dir / "mod_store").iterdir()), caches_before)

    def test_state_save_failure_keeps_other_operations_atomic(self):
        self.manager.install_mod(self.source, "A")
        for operation in (lambda: self.manager.disable_mod("A"),
                          lambda: self.manager.uninstall_mod("A"), self.manager.restore_all):
            with self.subTest(operation=operation):
                game_before = self.game_contents()
                state_before = self.state_bytes()
                with patch.object(self.manager, "_save_state", side_effect=OSError("state write failure")):
                    with self.assertRaises(OSError):
                        operation()
                self.assertEqual(self.game_contents(), game_before)
                self.assertEqual(self.state_bytes(), state_before)
        self.manager.disable_mod("A")
        game_before = self.game_contents()
        state_before = self.state_bytes()
        with patch.object(self.manager, "_save_state", side_effect=OSError("state write failure")):
            with self.assertRaises(OSError):
                self.manager.enable_mod("A")
        self.assertEqual(self.game_contents(), game_before)
        self.assertEqual(self.state_bytes(), state_before)

    def test_can_reinstall_from_its_existing_cache(self):
        self.manager.install_mod(self.source, "A")
        store = Path(self.manager.installed_mods()[0]["store"])
        self.manager.install_mod(store, "A")
        self.assertEqual(self.game_contents(), self.replacements)
        self.manager.disable_mod("A")
        self.manager.enable_mod("A")
        self.assertEqual(self.game_contents(), self.replacements)

    def test_nested_duplicate_source_fallback_matches_install_order(self):
        nested_source = self.root / "nested-source"
        first = nested_source / "a-first" / "a.bundle"
        last = nested_source / "z-last" / "a.bundle"
        for bundle, data in ((first, b"FIRST_A"), (last, b"LAST_A")):
            bundle.parent.mkdir(parents=True)
            bundle.write_bytes(data)
        self.manager.install_mod(nested_source, "Nested")
        self.assertEqual((self.game / "a.bundle").read_bytes(), b"FIRST_A")
        self.manager.disable_mod("Nested")
        store = Path(self.manager.installed_mods()[0]["store"])
        (store / "a.bundle").unlink()
        rglob = Path.rglob

        def reversed_source_order(directory, pattern):
            if directory == nested_source and pattern == "a.bundle":
                # 文件系统返回顺序不一定等于安装时按路径排序的顺序。
                return iter((last, first))
            return rglob(directory, pattern)

        with patch.object(Path, "rglob", reversed_source_order):
            self.manager.enable_mod("Nested")
        self.assertEqual((self.game / "a.bundle").read_bytes(), b"FIRST_A")

    def test_root_and_nested_duplicate_fallback_matches_analysis(self):
        nested_source = self.root / "mixed-source"
        preferred = nested_source / "a-first" / "a.bundle"
        preferred.parent.mkdir(parents=True)
        preferred.write_bytes(b"PREFERRED_A")
        (nested_source / "a.bundle").write_bytes(b"ROOT_A")
        self.manager.install_mod(nested_source, "Mixed")
        self.assertEqual((self.game / "a.bundle").read_bytes(), b"PREFERRED_A")
        self.manager.disable_mod("Mixed")
        store = Path(self.manager.installed_mods()[0]["store"])
        (store / "a.bundle").unlink()
        self.manager.enable_mod("Mixed")
        self.assertEqual((self.game / "a.bundle").read_bytes(), b"PREFERRED_A")

    def test_disabling_top_layer_with_missing_underlay_is_atomic(self):
        self.manager.install_mod(self.source, "A")
        top_source = self.make_mod("top", {"a.bundle": b"TOP_A", "b.bundle": b"TOP_B"})
        self.manager.install_mod(top_source, "B")
        underlay = next(mod for mod in self.manager.installed_mods() if mod["name"] == "A")
        (Path(underlay["store"]) / "b.bundle").unlink()
        self.source.rename(self.root / "moved-source")
        game_before = self.game_contents()
        state_before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, "b.bundle"):
            self.manager.disable_mod("B")
        self.assertEqual(self.game_contents(), game_before)
        self.assertEqual(self.state_bytes(), state_before)

    def test_missing_backup_prevents_false_disable_success(self):
        self.manager.install_mod(self.source, "A")
        (self.manager.backup_dir / "b.bundle").unlink()
        game_before = self.game_contents()
        state_before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, "b.bundle"):
            self.manager.disable_mod("A")
        self.assertEqual(self.game_contents(), game_before)
        self.assertEqual(self.state_bytes(), state_before)

    def test_missing_backup_prevents_false_restore_success(self):
        self.manager.install_mod(self.source, "A")
        (self.manager.backup_dir / "b.bundle").unlink()
        game_before = self.game_contents()
        state_before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, "b.bundle"):
            self.manager.restore_all()
        self.assertEqual(self.game_contents(), game_before)
        self.assertEqual(self.state_bytes(), state_before)

    def test_reinstall_does_not_back_up_modded_data_as_original(self):
        self.manager.install_mod(self.source, "A")
        (self.manager.backup_dir / "b.bundle").unlink()
        game_before = self.game_contents()
        state_before = self.state_bytes()
        with self.assertRaisesRegex(RuntimeError, "b.bundle"):
            self.manager.install_mod(self.source, "A")
        self.assertFalse((self.manager.backup_dir / "b.bundle").exists())
        self.assertEqual(self.game_contents(), game_before)
        self.assertEqual(self.state_bytes(), state_before)

    def test_overlapping_mods_reinstall_enable_and_original_backup(self):
        self.manager.install_mod(self.source, "A")
        top_source = self.make_mod("top", {"b.bundle": b"TOP_B"})
        self.manager.install_mod(top_source, "B")
        smaller_source = self.make_mod("smaller", {"a.bundle": b"NEW_A"})
        self.manager.install_mod(smaller_source, "A")
        self.assertEqual(self.game_contents(), {"a.bundle": b"NEW_A", "b.bundle": b"TOP_B"})
        self.manager.disable_mod("B")
        self.assertEqual((self.game / "b.bundle").read_bytes(), b"ORIGINAL_B")
        self.manager.enable_mod("B")
        self.assertEqual((self.game / "b.bundle").read_bytes(), b"TOP_B")
        self.assertEqual(self.manager.restore_all(), 2)
        self.assertEqual(self.game_contents(), self.originals)
        self.assertEqual(self.manager.installed_mods(), [])

    def test_unmatched_install_does_not_touch_an_installed_mod(self):
        self.manager.install_mod(self.source, "A")
        unmatched = self.make_mod("unmatched", {"unknown.bundle": b"UNKNOWN"})
        game_before = self.game_contents()
        state_before = self.state_bytes()
        with self.assertRaises(RuntimeError):
            self.manager.install_mod(unmatched, "A")
        self.assertEqual(self.game_contents(), game_before)
        self.assertEqual(self.state_bytes(), state_before)


if __name__ == "__main__":
    unittest.main()
