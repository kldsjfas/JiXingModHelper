"""Normal browser APIs for full-frame export and action-workbench metadata."""
from __future__ import annotations

import hashlib
import json
import threading
import unittest
import zipfile
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image

from astral_party_auto.modkit import clip_frame_export as module
from astral_party_auto.web_app import DesktopApi


class ClipFrameExportApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-clip-frame-api-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.bundle"
        self.source.write_bytes(b"isolated animation source")
        self.output = self.root / "Fight-attack-frames.zip"
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._pick_path = Mock(return_value=self.output)
        self.api._image_data = Mock(return_value=None)
        self.api.controller = Mock()
        self.api.controller.aa_dirs = (self.root,)
        self.api.controller.selection_comparison.return_value = {}
        self.api.controller.selection = {
            "asset_type": "anim", "bundle": "logical.bundle", "name": "Fight-attack",
            "original_path": str(self.source), "playable": True, "animation_kind": "sprite_clip",
            "clip_sample_rate": 30, "clip_loop": False,
            "clip_runtime_action": "Fight-attack", "clip_runtime_binding_reason": "",
        }
        self.original_selection = deepcopy(self.api.controller.selection)
        self.identity = {"role_id": 304, "target_kind": "hero", "action": "Fight-attack",
                         "catalog_keys": ["Hero304_Fight-attack_PC"]}

    def rendering(self, identity=None):
        count = 3
        info = {"width": 4, "height": 3, "total": count, "fps": count / 0.35,
                "sample_rate": 30, "loop": False, "runtime_action": "Fight-attack", "runtime_binding_reason": "",
                "frame_names": [f"Fight-attack_{index:05d}" for index in range(count)]}
        stack = ExitStack()
        stack.enter_context(patch.object(module, "resolve_clip_identity", return_value=identity or self.identity))
        stack.enter_context(patch.object(module, "_load_bundle", return_value=object()))
        stack.enter_context(patch.object(module, "_read_clip", return_value=(
            [None] * count, [50.0, 100.0, 200.0], info, (100.0, -2, 2))))
        stack.enter_context(patch.object(module, "iter_clip_canvases", side_effect=lambda *_args: (
            Image.new("RGBA", (4, 3), (index * 50, 20, 30, 150)) for index in range(count))))
        return stack

    def test_normal_api_writes_all_actual_pngs_and_source_identity_without_changing_selection(self):
        with self.rendering():
            result = self.api.export_clip_frames()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["data"]["frame_count"], 3)
        self.assertEqual(result["data"]["role_id"], 304)
        with zipfile.ZipFile(self.output) as archive:
            metadata = json.loads(archive.read("clip-frames.json"))
            self.assertEqual(metadata["source_bundle"], "logical.bundle")
            self.assertEqual(metadata["source_animation"], "Fight-attack")
            self.assertEqual(metadata["source_bundle_sha256"], hashlib.sha256(self.source.read_bytes()).hexdigest())
            self.assertEqual(metadata["role_id"], 304)
            self.assertEqual([frame["duration_ms"] for frame in metadata["frames"]], [50, 100, 200])
            self.assertEqual(len([name for name in archive.namelist() if name.endswith(".png")]), 3)
            self.assertIsNone(archive.testzip())
        self.assertEqual(self.api.controller.selection, self.original_selection)
        self.api.controller.export_selection.assert_not_called()
        self.assertEqual(self.api._pick_path.call_args.kwargs["save_filename"], "Fight-attack-frames.zip")

    def test_cancel_does_not_resolve_role_render_or_change_selection(self):
        self.api._pick_path.return_value = None
        with patch.object(module, "resolve_clip_identity") as resolve, patch.object(module, "_load_bundle") as load:
            result = self.api.export_clip_frames()
        self.assertTrue(result["ok"])
        self.assertIsNone(result["data"])
        resolve.assert_not_called()
        load.assert_not_called()
        self.assertEqual(self.api.controller.selection, self.original_selection)
        self.assertFalse(self.output.exists())

    def test_selection_changed_while_choosing_destination_is_rejected(self):
        def change_selection(*_args, **_kwargs):
            self.api.controller.selection = {**self.original_selection, "name": "Walk"}
            return self.output
        self.api._pick_path.side_effect = change_selection
        with patch.object(module, "export_clip_frames") as export:
            result = self.api.export_clip_frames()
        self.assertFalse(result["ok"])
        self.assertIn("已经改变", result["error"])
        export.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_non_animation_unplayable_or_missing_source_never_open_destination_dialog(self):
        for changes in ({"asset_type": "texture"}, {"playable": False}, {"original_path": ""}):
            with self.subTest(changes=changes):
                self.api.controller.selection = {**self.original_selection, **changes}
                result = self.api.export_clip_frames()
                self.assertFalse(result["ok"])
        self.api._pick_path.assert_not_called()

    def test_unknown_role_still_exports_png_frames_without_guessing_an_id(self):
        unknown = {"role_id": None, "target_kind": "monster", "action": "Fight-attack", "catalog_keys": []}
        with self.rendering(identity=unknown):
            result = self.api.export_clip_frames()
        self.assertTrue(result["ok"], result.get("error"))
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(json.loads(archive.read("clip-frames.json"))["role_id"])
        self.assertEqual(self.api.controller.selection, self.original_selection)

    def test_missing_identity_catalog_does_not_block_verified_png_export(self):
        with self.rendering(), patch.object(module, "resolve_clip_identity", side_effect=FileNotFoundError("catalog missing")):
            result = self.api.export_clip_frames()
        self.assertTrue(result["ok"], result.get("error"))
        with zipfile.ZipFile(self.output) as archive:
            self.assertIsNone(json.loads(archive.read("clip-frames.json"))["role_id"])

    def test_selection_payload_carries_exact_action_sample_rate_loop_and_role(self):
        with patch.object(module, "resolve_clip_identity", return_value=self.identity):
            payload = self.api._selection_payload()
        self.assertEqual(payload["frame_animation"]["role_id"], 304)
        self.assertEqual(payload["frame_animation"]["action"], "Fight-attack")
        self.assertEqual(payload["frame_animation"]["source_action"], "Fight-attack")
        self.assertEqual(payload["frame_animation"]["fps"], 30)
        self.assertFalse(payload["frame_animation"]["loop"])
        self.assertTrue(payload["frame_animation"]["exportable"])
        self.assertEqual(self.api.controller.selection, self.original_selection)

    def test_public_selection_entry_returns_frame_workbench_target_without_changing_saved_draft(self):
        self.api._clear_replacement = Mock()
        self.api._clear_emote_candidate = Mock()
        self.api._clear_visual_candidate = Mock()
        self.api.controller.set_selection.return_value = self.original_selection
        saved_draft = [{"kind": "texture", "bundle": "previous.bundle", "name": "saved image"}]
        self.api.controller.draft_items = saved_draft
        with patch.object(module, "resolve_clip_identity", return_value=self.identity):
            result = self.api.select_asset("anim", "logical.bundle", "Fight-attack")
        self.assertTrue(result["ok"], result.get("error"))
        self.api.controller.set_selection.assert_called_once_with(
            "logical.bundle", "Fight-attack", asset_type="anim", force=False)
        self.assertEqual(result["data"]["frame_animation"]["action"], "Fight-attack")
        self.assertEqual(result["data"]["frame_animation"]["role_id"], 304)
        self.assertIs(self.api.controller.draft_items, saved_draft)

    def test_selection_payload_unknown_role_retains_real_action_and_reason(self):
        with patch.object(module, "resolve_clip_identity", side_effect=ValueError("no unique catalog identity")):
            payload = self.api._selection_payload()
        self.assertTrue(payload["frame_animation"]["exportable"])
        self.assertIsNone(payload["frame_animation"]["role_id"])
        self.assertEqual(payload["frame_animation"]["action"], "Fight-attack")
        self.assertIn("no unique", payload["frame_animation"]["reason"])

    def test_unplayable_selection_payload_does_not_claim_full_frame_export(self):
        self.api.controller.selection["playable"] = False
        with patch.object(module, "resolve_clip_identity", return_value=self.identity):
            payload = self.api._selection_payload()
        self.assertFalse(payload["frame_animation"]["exportable"])
        self.assertEqual(payload["frame_animation"]["action"], "")

    def test_selection_payload_routes_runtime_alias_and_retains_raw_source_clip(self):
        self.api.controller.selection.update(name="Fight-attack-effect", clip_runtime_action="Fight-effect")
        with patch.object(module, "resolve_clip_identity", return_value=self.identity):
            payload = self.api._selection_payload()
        self.assertEqual(payload["frame_animation"]["action"], "Fight-effect")
        self.assertEqual(payload["frame_animation"]["source_action"], "Fight-attack-effect")
        self.assertEqual(payload["frame_animation"]["role_id"], 304)
        self.assertEqual(payload["name"], "Fight-attack-effect")
        self.assertTrue(payload["frame_animation"]["exportable"])

    def test_multiple_sprite_prefixes_keep_export_but_do_not_route_to_a_guessed_action(self):
        self.api.controller.selection.update(clip_runtime_action=None,
                                             clip_runtime_binding_reason="此片段包含多个精灵动作前缀。")
        with patch.object(module, "resolve_clip_identity", return_value=self.identity):
            payload = self.api._selection_payload()
        self.assertTrue(payload["frame_animation"]["exportable"])
        self.assertEqual(payload["frame_animation"]["action"], "")
        self.assertEqual(payload["frame_animation"]["source_action"], "Fight-attack")
        self.assertIn("多个", payload["frame_animation"]["reason"])

    def test_original_primary_and_binary_export_paths_keep_using_controller_exporter(self):
        self.api.controller.default_export_filename.return_value = "Fight-attack.anim.json"
        self.api.controller.export_selection.return_value = self.output
        with patch.object(module, "export_clip_frames") as frames_exporter:
            for variant in ("primary", "secondary"):
                with self.subTest(variant=variant):
                    result = self.api.export_selection(variant)
                    self.assertTrue(result["ok"])
                    self.api.controller.export_selection.assert_called_with(self.output, fmt=None)
        frames_exporter.assert_not_called()
        self.assertEqual(self.api.controller.selection, self.original_selection)


if __name__ == "__main__":
    unittest.main()
