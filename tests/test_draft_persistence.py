"""作品集清单写入中断时，保留上一份完整的磁盘记录。"""
from __future__ import annotations

import errno
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from astral_party_auto import mod_controller


class DraftPersistenceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="jixing-draft-persistence-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.metadata = self.root / "draft_pack.json"
        self.original = {"name": "保留的作品", "items": [{"kind": "texture", "name": "card"}]}
        self.metadata.write_text(json.dumps(self.original, ensure_ascii=False), encoding="utf-8")
        self.original_bytes = self.metadata.read_bytes()
        self.controller = object.__new__(mod_controller.ModController)
        self.controller.draft_name = "新的作品名"
        self.controller.draft_items = [{"kind": "texture", "name": "new-card"}]
        self.enterContext(patch.object(mod_controller, "DRAFT_META", self.metadata))

    def assert_original_remains(self):
        self.assertEqual(self.metadata.read_bytes(), self.original_bytes)
        self.assertEqual(json.loads(self.metadata.read_text(encoding="utf-8")), self.original)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"draft_pack.json"})

    def test_partial_write_cannot_truncate_prior_metadata(self):
        original_open = Path.open
        root = self.root

        class InterruptedWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def write(self, text):
                self.stream.write(text[:12])
                self.stream.flush()
                raise OSError(errno.ENOSPC, "simulated disk full")

        def open_with_interrupted_write(path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            return InterruptedWriter(stream) if path.parent == root and "w" in mode else stream

        with patch.object(Path, "open", open_with_interrupted_write):
            with self.assertRaises(OSError):
                self.controller._save_draft()
        self.assert_original_remains()

    def test_failed_final_replace_preserves_prior_metadata(self):
        with patch("os.replace", side_effect=PermissionError("simulated locked destination")):
            with self.assertRaises(PermissionError):
                self.controller._save_draft()
        self.assert_original_remains()

    def test_serialization_failure_keeps_prior_metadata(self):
        self.controller.draft_items = [{"unsupported": object()}]
        with self.assertRaises(TypeError):
            self.controller._save_draft()
        self.assert_original_remains()

    def test_successful_save_reloads_unicode_name_and_items(self):
        self.controller._save_draft()
        reloaded = object.__new__(mod_controller.ModController)
        reloaded.draft_name, reloaded.draft_items = "default", []
        reloaded._load_draft()
        self.assertEqual(reloaded.draft_name, self.controller.draft_name)
        self.assertEqual(reloaded.draft_items, self.controller.draft_items)
        self.assertEqual({path.name for path in self.root.iterdir()}, {"draft_pack.json"})


class TextureDraftTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="jixing-texture-transaction-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / "data"
        self.pack = self.root / "made"
        self.data.mkdir()
        self.pack.mkdir()
        self.game = self.root / "card.bundle"
        self.game.write_bytes(b"original game")
        self.target = self.pack / "card.bundle"
        self.target.write_bytes(b"previous complete draft")
        self.items = [{"kind": "texture", "bundle": "card.bundle", "name": "card", "note": "before.png"}]
        self.controller = object.__new__(mod_controller.ModController)
        self.controller.draft_items = deepcopy(self.items)
        self.controller._require_game = Mock()
        self.controller.bundle_path = Mock(return_value=self.game)
        self.controller._draft_dir = Mock(return_value=self.pack)
        self.controller._check_visual_bundle_overlap = Mock()
        self.controller._check_sequence_overlap = Mock()
        self.controller.log = Mock()
        self.controller._save_draft = Mock(side_effect=OSError("metadata could not be saved"))
        self.enterContext(patch.object(mod_controller, "DATA_DIR", self.data))

        def replace_texture(_source, _image, output, **_kwargs):
            Path(output).write_bytes(b"new candidate image")
            return "card"

        self.enterContext(patch.object(mod_controller, "replace_bundle_texture", side_effect=replace_texture))

    def assert_rolled_back(self):
        self.assertEqual(self.target.read_bytes(), b"previous complete draft")
        self.assertEqual(self.controller.draft_items, self.items)
        self.assertEqual(self.game.read_bytes(), b"original game")
        self.assertEqual(list(self.data.iterdir()), [])

    def test_failed_crop_metadata_rolls_back_bundle_and_items(self):
        with self.assertRaises(OSError):
            self.controller.update_draft_texture(0, self.root / "replacement.png", crop_box=(1, 2, 5, 8))
        self.assert_rolled_back()

    def test_failed_studio_metadata_rolls_back_existing_draft(self):
        with self.assertRaises(OSError):
            self.controller.add_texture_to_draft(self.game, self.root / "replacement.png", "card")
        self.assert_rolled_back()

    def test_failed_first_save_leaves_no_orphan_bundle(self):
        self.target.unlink()
        self.controller.draft_items = []
        with self.assertRaises(OSError):
            self.controller.add_texture_to_draft(self.game, self.root / "replacement.png", "card")
        self.assertFalse(self.target.exists())
        self.assertEqual(self.controller.draft_items, [])
        self.assertEqual(list(self.data.iterdir()), [])

    def test_failed_rollback_keeps_previous_bundle_and_reports_recovery_path(self):
        with patch.object(mod_controller.os, "replace", side_effect=PermissionError("bundle is locked")):
            with self.assertRaisesRegex(RuntimeError, "恢复副本保留在") as raised:
                self.controller.update_draft_texture(0, self.root / "replacement.png")
        recovery = next(self.data.glob("texture-change-*"))
        self.assertIn(str(recovery), str(raised.exception))
        self.assertEqual((recovery / "previous.bundle").read_bytes(), b"previous complete draft")
        self.assertEqual(json.loads((recovery / "recovery.json").read_text(encoding="utf-8"))["target"], str(self.target))
        self.assertEqual(self.controller.draft_items, self.items)


if __name__ == "__main__":
    unittest.main()
