"""Sprite AnimationClip 曲线、原点和真实资源副本预览自检。"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import io
import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from astral_party_auto.modkit.animation import _load_bundle
from astral_party_auto.modkit.clip_edit import _clip
from astral_party_auto.modkit.clip_preview import (
    _streamed_keys, _timeline, inspect_clip_preview, read_clip_preview,
)


def _stream(rows):
    data = bytearray()
    for time, value in rows:
        data.extend(struct.pack("<fi", time, 0 if value is None else 1))
        if value is not None:
            data.extend(struct.pack("<i4f", 0, 0, 0, 0, value))
    return {"curveCount": 1, "data": list(struct.unpack(f"<{len(data) // 4}I", data))}


def _tree(rows=None):
    pointers = [{"m_FileID": 0, "m_PathID": 101}, {"m_FileID": 0, "m_PathID": 202}]
    return {"m_SampleRate": 30, "m_ClipBindingConstant": {
        "genericBindings": [{"typeID": 212, "isPPtrCurve": 1}], "pptrCurveMapping": pointers},
        "m_MuscleClip": {"m_StartTime": 0, "m_StopTime": 1, "m_Clip": {"data": {
            "m_StreamedClip": _stream(rows if rows is not None else [(-3.4e38, 0), (0.25, 1), (math.inf, None)]),
            "m_DenseClip": {"m_CurveCount": 0}, "m_ConstantClip": {"data": []}}}}}


class ClipPreviewChecks(unittest.TestCase):
    def test_stream_sentinel_and_real_keyframe_times(self):
        pointers, durations, duration = _timeline(_tree())
        self.assertEqual([row["m_PathID"] for row in pointers], [101, 202])
        self.assertEqual(durations, [250, 750])
        self.assertEqual(duration, 1)

    def test_duplicate_timestamp_uses_last_sprite_and_ignores_stop_key(self):
        pointers, durations, _ = _timeline(_tree([(-3.4e38, 0), (0, 1), (1, 0)]))
        self.assertEqual([row["m_PathID"] for row in pointers], [202])
        self.assertEqual(durations, [1000])

    def test_dense_and_constant_reference_curves(self):
        tree = _tree()
        data = tree["m_MuscleClip"]["m_Clip"]["data"]
        data["m_StreamedClip"] = {"curveCount": 0, "data": []}
        data["m_DenseClip"] = {"m_CurveCount": 1, "m_FrameCount": 3,
                               "m_SampleRate": 2, "m_BeginTime": 0, "m_SampleArray": [0, 1, 0]}
        self.assertEqual(_timeline(tree)[1], [500, 500])
        data["m_DenseClip"] = {"m_CurveCount": 0}
        data["m_ConstantClip"] = {"data": [1]}
        self.assertEqual(_timeline(tree)[0][0]["m_PathID"], 202)
        self.assertEqual(_timeline(tree)[1], [1000])

    def test_editor_curve_uses_actual_pointer_order(self):
        tree = _tree()
        tree["m_PPtrCurves"] = [{"classID": 212, "attribute": "m_Sprite", "curve": [
            {"time": 0, "value": {"m_FileID": 0, "m_PathID": 202}},
            {"time": 0.7, "value": {"m_FileID": 0, "m_PathID": 101}}]}]
        pointers, durations, _ = _timeline(tree)
        self.assertEqual([row["m_PathID"] for row in pointers], [202, 101])
        self.assertAlmostEqual(durations[0], 700)
        self.assertAlmostEqual(durations[1], 300)

    def test_multi_track_and_transform_curves_are_explicitly_unsupported(self):
        tree = _tree()
        tree["m_FloatCurves"] = [{"attribute": "m_Color.a"}]
        with self.assertRaisesRegex(ValueError, "游戏运行时"):
            _timeline(tree)
        tree = _tree()
        tree["m_ClipBindingConstant"]["genericBindings"] *= 2
        with self.assertRaisesRegex(ValueError, "游戏运行时"):
            _timeline(tree)

    def test_broken_stream_and_foreign_mapping_do_not_fake_frames(self):
        with self.assertRaises(ValueError):
            _streamed_keys({"data": [1, 2, 3]})
        for rows in [[(0, 2)], [(0, 0.25)], [(0, float("nan"))], [(0.5, 1)], [(0.4, 1), (0.3, 0)]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                _timeline(_tree(rows))


def check_real_bundle(root: Path, evidence: Path | None = None) -> dict:
    source = root / "025248d9524b4b3d2551b1630254afa8.bundle"
    payload = source.read_bytes()
    source_hash = hashlib.sha256(payload).hexdigest()
    results = {}
    with tempfile.TemporaryDirectory(prefix="jixing-clip-preview-") as folder:
        copied = Path(folder) / source.name
        copied.write_bytes(payload)
        for name in ("Cry", "Hit", "Walk-back"):
            info = inspect_clip_preview(copied, name)
            assert info["playable"], info
            preview = read_clip_preview(copied, name)
            assert len(preview["frames"]) == len(preview["durations"])
            assert math.isclose(sum(preview["durations"]), info["duration"] * 1000, abs_tol=0.001)
            assert len(set(preview["frames"])) > 1, "Preview does not actually move"
            images = [Image.open(io.BytesIO(base64.b64decode(frame.split(",", 1)[1]))).convert("RGBA")
                      for frame in preview["frames"]]
            assert len({image.size for image in images}) == 1, "Frame canvas sizes are inconsistent"
            assert all(image.getbbox() is not None for image in images)
            results[name] = {"frames": len(images), "duration": info["duration"],
                             "size": [info["width"], info["height"]], "distinct_frames": len(set(preview["frames"]))}
            if name == "Cry":
                assert preview["names"] == [f"Cry_{index:05}" for index in range(30)]
                assert info["duration"] == 1 and len(images) == 30
                if evidence:
                    evidence.mkdir(parents=True, exist_ok=True)
                    backgrounds = []
                    for image in images:
                        background = Image.new("RGBA", image.size, (60, 65, 76, 255))
                        background.alpha_composite(image)
                        backgrounds.append(background.convert("RGB"))
                    backgrounds[0].save(evidence / "Cry-preview-first.png")
                    # GIF stores centiseconds; alternate 30/40 ms to preserve 1 s.
                    durations = [round((index + 1) * 100 / 30) * 10 - round(index * 100 / 30) * 10
                                 for index in range(30)]
                    backgrounds[0].save(evidence / "Cry-preview.gif", save_all=True,
                                        append_images=backgrounds[1:], duration=durations, loop=0)
        env = _load_bundle(copied)
        clip = _clip(env, "Cry")
        tree = copy.deepcopy(clip.read_typetree())
        tree["m_MuscleClip"]["m_StopTime"] = 2.0
        stream = tree["m_MuscleClip"]["m_Clip"]["data"]["m_StreamedClip"]
        keys = _streamed_keys(stream)
        stream.update(_stream([(time * 2 if -100 < time < 100 else time, value) for time, value in keys]))
        clip.save_typetree(tree)
        changed = Path(folder) / "double-duration.bundle"
        changed.write_bytes(env.file.save())
        slower = read_clip_preview(changed, "Cry")
        assert math.isclose(sum(slower["durations"]), 2000, abs_tol=0.001)
        assert slower["duration"] == 2.0
        results["modified_copy"] = {"duration": slower["duration"], "frames": len(slower["frames"])}
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    results["game_bundle_unchanged"] = True
    if evidence:
        (evidence / "clip-preview-check.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--game-bundles", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ClipPreviewChecks))
    if not result.wasSuccessful():
        raise SystemExit(1)
    if args.game_bundles:
        print(json.dumps(check_real_bundle(args.game_bundles, args.evidence), ensure_ascii=False))
