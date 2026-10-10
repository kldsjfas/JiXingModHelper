"""图集导入回归；真实游戏仅只读，写入均使用隔离副本。"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageChops, ImageStat

from astral_party_auto.modkit.animation import _load_bundle
from astral_party_auto.modkit.clip_atlas import (
    _atlas_details, _read_atlas_image, inspect_clip_atlas, replace_clip_atlas_checked,
)
from astral_party_auto.modkit.clip_edit import _clip, replace_clip_checked
from astral_party_auto.modkit.clip_preview import read_clip_preview
from astral_party_auto.mod_controller import ModController
from astral_party_auto.web_app import DesktopApi


class ClipAtlasChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="jixing-atlas-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.details = {"atlas_name": "Idle-001", "atlas_width": 64, "atlas_height": 64,
                        "atlas_editable": True, "atlas_reason": "complete atlas"}
        self.image = self.root / "atlas.png"
        Image.new("RGBA", (64, 64), (50, 100, 150, 255)).save(self.image)
        self.draft = self.root / "draft.bundle"
        self.draft.write_bytes(b"unchanged existing draft")

    def test_exact_size_static_png_is_accepted_without_resizing(self):
        with _read_atlas_image(self.image, self.details) as image:
            self.assertEqual(image.size, (64, 64))
            self.assertEqual(image.getpixel((0, 0)), (50, 100, 150, 255))

    def test_single_frame_does_not_write_or_resize_existing_draft(self):
        frame = self.root / "frame_30_000000.png"
        Image.new("RGBA", (20, 30)).save(frame)
        baseline = self.draft.read_bytes()
        with patch("astral_party_auto.modkit.clip_atlas._load_bundle"), \
                patch("astral_party_auto.modkit.clip_atlas._atlas_details", return_value=(Mock(), self.details)), \
                patch("astral_party_auto.modkit.clip_atlas._set_image") as write_image, \
                patch("astral_party_auto.modkit.clip_atlas._atomic_save") as save:
            with self.assertRaisesRegex(ValueError, "整张图集.*64 × 64"):
                replace_clip_atlas_checked(self.draft, "Idle", frame, self.draft)
            write_image.assert_not_called()
            save.assert_not_called()
        self.assertEqual(self.draft.read_bytes(), baseline)

    def test_matching_size_multiframe_files_are_rejected(self):
        frames = [Image.new("RGBA", (64, 64), color) for color in ("red", "blue")]
        for suffix, fmt in (("gif", "GIF"), ("png", "PNG"), ("webp", "WEBP")):
            with self.subTest(fmt=fmt):
                path = self.root / f"animated.{suffix}"
                frames[0].save(path, format=fmt, save_all=True, append_images=frames[1:], duration=100, loop=0)
                with self.assertRaisesRegex(ValueError, "多帧动画"):
                    _read_atlas_image(path, self.details)

    def test_frame_directory_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "帧目录"):
            _read_atlas_image(self.root, self.details)

    def test_multiple_atlases_are_not_reduced_to_the_first_texture(self):
        asset_file = object()
        readers = [SimpleNamespace(assets_file=asset_file, path_id=index,
                                   type=SimpleNamespace(name="Texture2D"), read=Mock()) for index in (1, 2)]
        frames = [SimpleNamespace(sprite=object()), SimpleNamespace(sprite=object())]
        env = SimpleNamespace(objects=readers)
        data = [SimpleNamespace(texture=SimpleNamespace(deref=Mock(return_value=reader))) for reader in readers]
        with patch("astral_party_auto.modkit.clip_atlas._read_clip", return_value=(frames, [], {}, None)), \
                patch("astral_party_auto.modkit.clip_atlas._render_data", side_effect=data):
            with self.assertRaisesRegex(ValueError, "2 张图集"):
                _atlas_details(env, "Idle")

    def test_separate_alpha_atlas_is_not_silently_left_unchanged(self):
        frame = SimpleNamespace(sprite=object())
        render_data = SimpleNamespace(alphaTexture=SimpleNamespace(m_PathID=2))
        with patch("astral_party_auto.modkit.clip_atlas._read_clip", return_value=([frame], [], {}, None)), \
                patch("astral_party_auto.modkit.clip_atlas._render_data", return_value=render_data):
            with self.assertRaisesRegex(ValueError, "独立透明度图集"):
                _atlas_details(SimpleNamespace(objects=[]), "Idle")

    def api(self):
        api = object.__new__(DesktopApi)
        api._controller_lock = threading.RLock()
        api._append_log = Mock()
        api._clear_draft_crop()
        api.controller = Mock()
        api.controller.selection = {"asset_type": "anim", "name": "Idle", "bundle": "draft.bundle",
                                    "original_path": str(self.draft), "playable": True, "editable": True}
        api._replacement_path = self.image
        api._pick_path = Mock(return_value=self.image)
        api._image_data = Mock(return_value="image")
        return api

    def test_import_failure_and_cancel_clear_previous_candidate(self):
        api = self.api()
        api._clip_candidate_preview = Mock(side_effect=ValueError("incorrect atlas size"))
        self.assertFalse(api.choose_replacement()["ok"])
        self.assertIsNone(api._replacement_path)
        api._replacement_path = self.image
        api._pick_path.return_value = None
        self.assertTrue(api.choose_replacement()["ok"])
        self.assertIsNone(api._replacement_path)
        self.assertFalse(api.commit_replacement()["ok"])
        api.controller.add_anim_to_draft.assert_not_called()

    def test_preview_encoding_failure_does_not_leave_a_candidate(self):
        api = self.api()
        api._clip_candidate_preview = Mock(return_value={"frames": ["frame"]})
        api._image_data.side_effect = ValueError("unreadable image")
        self.assertFalse(api.choose_replacement()["ok"])
        self.assertIsNone(api._replacement_path)

    def test_ordinary_texture_cancel_or_failure_keeps_previous_candidate(self):
        api = self.api()
        api.controller.selection["asset_type"] = "texture"
        api._pick_path.return_value = None
        self.assertTrue(api.choose_replacement()["ok"])
        self.assertEqual(api._replacement_path, self.image)
        api._pick_path.side_effect = ValueError("dialog failed")
        self.assertFalse(api.choose_replacement()["ok"])
        self.assertEqual(api._replacement_path, self.image)

    def test_save_failure_clears_candidate_and_does_not_change_bundle(self):
        api = self.api()
        baseline = self.draft.read_bytes()
        api.controller.add_anim_to_draft.side_effect = ValueError("incorrect atlas size")
        self.assertFalse(api.commit_replacement()["ok"])
        self.assertIsNone(api._replacement_path)
        self.assertEqual(self.draft.read_bytes(), baseline)

    def test_animation_atlas_cannot_be_cropped(self):
        api = self.api()
        reply = api.crop_replacement([0, 0, 20, 30])
        self.assertFalse(reply["ok"])
        self.assertIn("不能裁剪", reply["error"])

    def test_legacy_animation_draft_crop_is_rejected_and_clears_pending_crop(self):
        api = self.api()
        api.controller.draft_items = [{"kind": "texture", "name": "Idle-001", "from_anim": "Idle"}]
        api._draft_crop_path = self.image
        api._draft_crop_index = 0
        self.assertFalse(api.commit_draft_crop([0, 0, 20, 20])["ok"])
        self.assertIsNone(api._draft_crop_path)
        self.assertIsNone(api._draft_crop_index)
        api.controller.update_draft_texture.assert_not_called()
        self.assertFalse(api.pick_draft_crop_source(0)["ok"])
        api._pick_path.assert_not_called()

    def test_ordinary_texture_draft_still_uses_automatic_size_matching(self):
        controller = object.__new__(ModController)
        controller.draft_items = [{"kind": "texture", "bundle": "draft.bundle", "name": "card"}]
        controller.bundle_path = lambda _name: self.draft
        controller._draft_dir = lambda: self.root
        controller._save_draft = Mock()
        controller.log = Mock()
        with patch("astral_party_auto.mod_controller.replace_bundle_texture", return_value="card") as replace:
            controller.update_draft_texture(0, self.image, crop_box=(0, 0, 20, 20))
        replace.assert_called_once_with(self.draft, self.image, self.draft, target_name="card", crop_box=(0, 0, 20, 20))


def check_real_bundle(source: Path, evidence: Path) -> dict:
    payload = source.read_bytes()
    source_hash = hashlib.sha256(payload).hexdigest()
    work = evidence / "_work"
    work.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="isolated-", dir=work) as folder:
            root = Path(folder)
            original = root / source.name
            original.write_bytes(payload)
            draft = root / "candidate.bundle"
            draft.write_bytes(payload)
            details = inspect_clip_atlas(original, "Idle")
            assert details["atlas_editable"], details
            atlas_name = details["atlas_name"]
            env = _load_bundle(original)
            atlas_reader = next(obj for obj in env.objects if obj.type.name == "Texture2D" and obj.peek_name() == atlas_name)
            atlas = root / "original-atlas.png"
            atlas_reader.read().image.save(atlas)
            baseline_objects = {obj.path_id: obj.get_raw_data() for obj in env.objects if obj is not atlas_reader}
            small = root / "frame_30_000000.png"
            Image.new("RGBA", (425, 470), "red").save(small)
            animation = root / "animated-atlas.png"
            frames = [Image.new("RGBA", (details["atlas_width"], details["atlas_height"]), color)
                      for color in ("red", "blue")]
            frames[0].save(animation, save_all=True, append_images=frames[1:], duration=100, loop=0)
            api = object.__new__(DesktopApi)
            api.controller = SimpleNamespace(_draft_dir=lambda: root / "empty-draft")
            selection = {"name": "Idle", "bundle": source.name, "original_path": str(original)}
            invalid_count = 0
            for invalid in (small, animation):
                for action in (lambda: replace_clip_atlas_checked(original, "Idle", invalid, draft),
                               lambda: api._clip_candidate_preview(selection, invalid)):
                    try:
                        action()
                    except ValueError:
                        invalid_count += 1
                    else:
                        raise AssertionError("Invalid atlas import was accepted")
                    assert draft.read_bytes() == payload, "Rejected import changed the draft"
            original_preview = read_clip_preview(original, "Idle")
            replace_clip_atlas_checked(original, "Idle", atlas, draft)
            saved = _load_bundle(draft)
            assert all(obj.get_raw_data() == baseline_objects[obj.path_id]
                       for obj in saved.objects if not (obj.type.name == "Texture2D" and obj.peek_name() == atlas_name))
            modified_preview = read_clip_preview(draft, "Idle")
            # The game's BC7 atlas is lossy: exporting and recompressing changes
            # edge pixels slightly. Compare displayed pixels instead of hashes.
            frame_errors = []
            for before, after in zip(original_preview["frames"], modified_preview["frames"]):
                images = [Image.open(io.BytesIO(base64.b64decode(data.split(",", 1)[1]))).convert("RGBA")
                          for data in (before, after)]
                displayed = []
                for image in images:
                    background = Image.new("RGBA", image.size, (60, 65, 76, 255))
                    background.alpha_composite(image)
                    displayed.append(background.convert("RGB"))
                frame_errors.append(max(ImageStat.Stat(ImageChops.difference(*displayed)).mean))
            assert max(frame_errors) < 3, "Unmodified atlas no longer reconstructs the same character"
            assert modified_preview["durations"] == original_preview["durations"]
            assert api._clip_candidate_preview(selection, atlas)["frames"] == modified_preview["frames"]

            # The public save route must apply the same checks without requiring a real game.
            controller = object.__new__(ModController)
            controller.aa_dir = root
            controller.aa_dirs = (root,)
            controller.draft_items = []
            controller.log = lambda _message: None
            controller._require_game = lambda: None
            controller._draft_dir = lambda: root / "saved-draft"
            controller._save_draft = lambda: None
            controller.bundle_path = lambda _name: original
            controller._check_sequence_overlap = lambda *args: None
            stable = draft.read_bytes()
            controller._draft_dir().mkdir()
            saved_draft = controller._draft_dir() / source.name
            saved_draft.write_bytes(stable)
            for invalid in (small, animation):
                try:
                    controller.add_anim_to_draft(original, "Idle", image_path=invalid, bundle_name=source.name)
                except ValueError:
                    pass
                else:
                    raise AssertionError("Direct save accepted an invalid atlas")
                assert saved_draft.read_bytes() == stable
                assert controller.draft_items == []
            controller.draft_items = [{"kind": "texture", "bundle": source.name, "name": atlas_name, "from_anim": "Idle"}]
            item_before = dict(controller.draft_items[0])
            for invalid in (small, animation):
                try:
                    controller.update_draft_texture(0, invalid)
                except ValueError:
                    pass
                else:
                    raise AssertionError("Legacy animation draft accepted an invalid atlas")
                assert saved_draft.read_bytes() == stable
                assert controller.draft_items[0] == item_before
            try:
                controller.update_draft_texture(0, atlas, crop_box=(0, 0, 100, 100))
            except RuntimeError:
                pass
            else:
                raise AssertionError("Legacy animation draft accepted cropping")
            assert saved_draft.read_bytes() == stable
            raw = _clip(env, "Idle").get_raw_data()
            replace_clip_checked(draft, "Idle", raw, draft, reference_bundle=original)
            assert _clip(_load_bundle(draft), "Idle").get_raw_data() == raw
            result = {"atlas": details, "invalid_preview_and_save_rejections": invalid_count,
                      "direct_save_rejections_preserve_draft": 2, "full_atlas_render_roundtrip": True,
                      "legacy_draft_rejections_preserve_bundle_and_metadata": 3,
                      "max_displayed_frame_mean_pixel_error": max(frame_errors),
                      "unrelated_objects_preserved": True, "same_source_animbin_roundtrip": True,
                      "frame_count": original_preview["total"], "game_bundle_unchanged": True}
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    finally:
        if work.exists() and not any(work.iterdir()):
            work.rmdir()
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "clip-atlas-check.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-bundle", type=Path)
    parser.add_argument("--evidence", type=Path, default=Path("qa-evidence/clip-atlas-20261010"))
    args = parser.parse_args()
    checks = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ClipAtlasChecks))
    if not checks.wasSuccessful():
        raise SystemExit(1)
    if args.game_bundle:
        print(json.dumps(check_real_bundle(args.game_bundle, args.evidence), ensure_ascii=False))
