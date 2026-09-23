"""AnimationClip 导入自检；真实包仅只读，写入始终放临时目录。"""
from __future__ import annotations

import argparse
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import UnityPy

from astral_party_auto.modkit.clip_edit import _clip, _pointers, replace_clip_checked


class ClipChecks(unittest.TestCase):
    def test_pointer_collection_keeps_file_identity(self):
        tree = {"nested": [{"m_FileID": 0, "m_PathID": 12}, {"m_FileID": 1, "m_PathID": 12},
                           {"m_FileID": 0, "m_PathID": 0}], "ignored": {"m_PathID": 99}}
        self.assertEqual(_pointers(tree), {(0, 12), (1, 12)})

    def test_empty_import_rejected_before_reading_bundle(self):
        with patch("astral_party_auto.modkit.clip_edit.UnityPy.load") as load:
            with self.assertRaises(ValueError):
                replace_clip_checked("unused.bundle", "Idle", b"", "unused-output.bundle")
            load.assert_not_called()

    def test_oversized_import_rejected_before_reading_bundle(self):
        with patch("astral_party_auto.modkit.clip_edit.UnityPy.load") as load:
            with self.assertRaises(ValueError):
                replace_clip_checked("unused.bundle", "Idle", b"x" * (32 * 1024 * 1024 + 1), "unused-output.bundle")
            load.assert_not_called()


def _change_pointers(value, *, add_foreign=False):
    if isinstance(value, dict):
        if "m_FileID" in value and value.get("m_PathID"):
            value["m_PathID"] = 9_223_372_036_854_770_001 if add_foreign else 0
        for child in value.values():
            _change_pointers(child, add_foreign=add_foreign)
    elif isinstance(value, list):
        for child in value:
            _change_pointers(child, add_foreign=add_foreign)


def check_real_bundle_copies(root: Path) -> dict:
    source = root / "014384d605bf10b9bad39dc24ab08425.bundle"
    if not source.is_file():
        raise RuntimeError("当前安装版本没有原测试包，请重新选择包含 AnimationClip 的资源验证。")
    original_payload = source.read_bytes()
    original_env = UnityPy.load(original_payload, path=str(root))
    target = next((obj for obj in original_env.objects if obj.type.name == "AnimationClip"
                   and _pointers(obj.read_typetree())), None)
    if target is None:
        raise RuntimeError("样本包没有含资源引用的动画片段。")
    baseline = target.read_typetree()
    name, target_id = baseline["m_Name"], target.path_id
    original_raw = target.get_raw_data()
    other_objects = {obj.path_id: obj.get_raw_data() for obj in original_env.objects if obj.path_id != target_id}

    def encode(tree):
        # UnityPy get_raw_data reads the original buffer; .data is the newly
        # serialized value produced by save_typetree.
        env = UnityPy.load(original_payload)
        obj = _clip(env, name)
        obj.save_typetree(tree)
        return obj.data

    def load(path):
        return UnityPy.load(path.read_bytes(), path=str(root))

    with tempfile.TemporaryDirectory(prefix="jixing-clip-check-") as folder:
        draft = Path(folder) / "sample.bundle"
        replace_clip_checked(source, name, original_raw, draft, reference_bundle=source)
        assert _clip(load(draft), name).get_raw_data() == original_raw
        stable = draft.read_bytes()

        invalid = [b"invalid clip", original_raw[: max(1, len(original_raw) // 2)]]
        foreign = copy.deepcopy(baseline)
        _change_pointers(foreign, add_foreign=True)
        invalid.append(encode(foreign))
        bad_rate = copy.deepcopy(baseline)
        bad_rate["m_SampleRate"] = float("nan")
        invalid.append(encode(bad_rate))
        for raw in invalid:
            try:
                replace_clip_checked(draft, name, raw, draft, reference_bundle=source)
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid clip was accepted")
            assert draft.read_bytes() == stable, "Failed import changed the existing draft"

        fewer_references = copy.deepcopy(baseline)
        _change_pointers(fewer_references)
        replace_clip_checked(draft, name, encode(fewer_references), draft, reference_bundle=source)
        assert not _pointers(_clip(load(draft), name).read_typetree())
        replace_clip_checked(draft, name, original_raw, draft, reference_bundle=source)
        restored = load(draft)
        assert _clip(restored, name).get_raw_data() == original_raw
        assert _pointers(_clip(restored, name).read_typetree()) == _pointers(baseline)
        assert all(obj.get_raw_data() == other_objects[obj.path_id]
                   for obj in restored.objects if obj.path_id != target_id)
        assert list(Path(folder).iterdir()) == [draft], "Temporary write file leaked"
    assert source.read_bytes() == original_payload, "Original game bundle changed"
    return {"clip": name, "original_reference_count": len(_pointers(baseline)),
            "original_raw_roundtrip": True, "invalid_inputs_keep_draft": len(invalid),
            "removed_references_restored": True, "unrelated_objects_preserved": True,
            "game_bundle_unchanged": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-bundles", type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ClipChecks))
    if not result.wasSuccessful():
        raise SystemExit(1)
    if args.game_bundles:
        print(check_real_bundle_copies(args.game_bundles))
