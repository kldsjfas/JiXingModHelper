"""技能影片索引、裁切预览与原始导出；写入链路仍保持严格限制。"""
from __future__ import annotations

import base64
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from astral_party_auto.modkit import bundles, cri_emote, cri_video, emote_media
from astral_party_auto.modkit.dynamic import is_cri_usm_object
from test_cri_emote import FakeReader, chunk, make_usm, object_tree, utf_table


def movie_reader(raw, name="VSkill_Hero124"):
    reader = FakeReader(object_tree(raw, name))
    fields = {"m_Script": "PPtr<MonoScript>", "implementation": "managedReference",
              "movieInfo": "ManaMovieInfo", "assetInfo": "MovieAssetInfo",
              "references": "ManagedReferencesRegistry"}
    nodes = [SimpleNamespace(m_Name=name, m_Type=kind, m_Level=1) for name, kind in fields.items()]
    reader.serialized_type = SimpleNamespace(node=SimpleNamespace(traverse=lambda: iter(nodes)))
    return reader


def crop_headers(raw, changes):
    output = bytearray(raw)
    for part in cri_emote._chunks(raw):
        if part.tag not in (b"@SFV", b"@ALP") or part.kind != 1:
            continue
        name, rows = cri_emote._utf_table(part.payload)
        rows[0].update(changes(part.tag))
        replacement = chunk(part.tag, part.kind, utf_table(name, rows), time=part.time, rate=part.rate,
                            channel=part.channel)
        original_size = int.from_bytes(raw[part.offset + 4:part.offset + 8], "big") + 8
        if len(replacement) != original_size:
            raise AssertionError("fixture changes cannot move seek offsets")
        output[part.offset:part.offset + original_size] = replacement
    return bytes(output)


class CriVideoTests(unittest.TestCase):
    def setUp(self):
        self.cropped = crop_headers(make_usm(frames=2), lambda _tag: {"mat_width": 156, "disp_width": 156})

    def test_index_finds_real_cri_structure_without_reading_video_bytes_or_using_name_prefix(self):
        readers = [movie_reader(self.cropped), movie_reader(self.cropped, "UnexpectedMovieName")]
        for reader in readers:
            reader.read_typetree = lambda: self.fail("indexing must not deserialize embedded video bytes")
        ordinary = FakeReader({"m_Name": "VSkill_not_a_movie"})
        env = SimpleNamespace(objects=readers + [ordinary])
        with tempfile.TemporaryDirectory() as folder, patch.object(bundles.UnityPy, "load", return_value=env):
            source = Path(folder) / "movie.bundle"
            source.write_bytes(b"fake bundle reader")
            index = bundles.read_bundle_asset_names(source)
        self.assertEqual(index["dynamic"], ["VSkill_Hero124", "UnexpectedMovieName"])
        self.assertEqual(index["cri_usm"], ["UnexpectedMovieName", "VSkill_Hero124"])
        self.assertEqual(index["anim"], [])
        self.assertFalse(is_cri_usm_object(ordinary))

    def test_old_cache_is_invalidated_and_new_index_keeps_cri_videos_in_dynamic(self):
        reader = movie_reader(self.cropped)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bundle = root / "movie.bundle"
            bundle.write_bytes(b"fake bundle reader")
            cache = root / "index.json"
            with patch.object(bundles.UnityPy, "load", return_value=SimpleNamespace(objects=[reader])):
                typed = bundles.build_asset_index((root,), cache)
            self.assertEqual(typed["dynamic"], {"movie.bundle": ["VSkill_Hero124"]})
            self.assertEqual(typed["cri_usm"], typed["dynamic"])
            self.assertNotIn("cri_usm", bundles.ASSET_TYPE_KEYS)
            self.assertEqual(bundles.load_asset_index(cache)["dynamic"], typed["dynamic"])
            self.assertEqual(bundles.load_asset_index(cache)["cri_usm"], typed["dynamic"])
            import json
            payload = json.loads(cache.read_text(encoding="utf-8"))
            payload["version"] = 8
            cache.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(any(bundles.load_asset_index(cache).values()))

    def test_readonly_crop_does_not_relax_existing_import_or_replacement(self):
        with self.assertRaisesRegex(ValueError, "裁切"):
            cri_emote.inspect_usm_bytes(self.cropped)
        info = cri_emote.inspect_usm_bytes(self.cropped, allow_display_crop=True)
        self.assertEqual((info["width"], info["display_width"]), (160, 156))
        with tempfile.TemporaryDirectory() as folder, patch.object(cri_emote, "_load_bundle") as load:
            imported = Path(folder) / "movie.usm"
            imported.write_bytes(self.cropped)
            with self.assertRaises(ValueError):
                cri_emote.validate_usm_file(imported, "reference.bundle", "VEmoji_sample")
            with self.assertRaises(ValueError):
                cri_emote.replace_cri_emote_bundle("reference.bundle", "VEmoji_sample", imported, Path(folder) / "out.bundle")
            load.assert_not_called()

    def test_readonly_crop_rejects_large_crop_or_different_alpha_geometry(self):
        cases = [lambda _tag: {"mat_width": 140, "disp_width": 140},
                 lambda tag: {"mat_width": 156 if tag == b"@SFV" else 155,
                              "disp_width": 156 if tag == b"@SFV" else 155},
                 lambda _tag: {"mat_width": 155, "disp_width": 156}]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                cri_emote.inspect_usm_bytes(crop_headers(make_usm(), changes), allow_display_crop=True)

    def test_inspection_and_export_validate_the_actual_object_and_preserve_bytes(self):
        reader = movie_reader(self.cropped)
        env = SimpleNamespace(objects=[reader])
        with tempfile.TemporaryDirectory() as folder, patch.object(cri_emote, "_load_bundle", return_value=env):
            source = Path(folder) / "movie.bundle"
            source.write_bytes(b"original bundle")
            info = cri_video.inspect_cri_video_bundle(source, "VSkill_Hero124")
            self.assertEqual((info["animation_kind"], info["width"], info["encoded_width"]), ("cri_usm", 156, 160))
            self.assertFalse(info["editable"])
            output = cri_video.export_cri_video_bundle(source, "VSkill_Hero124", Path(folder) / "export.bin")
            self.assertEqual(output.suffix, ".usm")
            self.assertEqual(output.read_bytes(), self.cropped)
            self.assertEqual(source.read_bytes(), b"original bundle")
            with self.assertRaises(ValueError):
                cri_video.read_cri_video_preview(source, "wrong_name")
            reader.serialized_type = None
            with self.assertRaises(ValueError):
                cri_video.inspect_cri_video_bundle(source, "VSkill_Hero124")

    def test_external_video_implementation_cannot_be_previewed_or_exported(self):
        reader = movie_reader(self.cropped)
        reader.tree["references"]["RefIds"][0]["type"]["class"] = "CriStreamingAssetsImpl"
        with patch.object(cri_emote, "_load_bundle", return_value=SimpleNamespace(objects=[reader])):
            with self.assertRaisesRegex(ValueError, "外部视频"):
                cri_video.inspect_cri_video_bundle("source.bundle", "VSkill_Hero124")

    def test_cropped_preview_uses_both_streams_and_original_display_ratio(self):
        calls = []

        def decode(_ffmpeg, source, _output, indices, width, height, pixel_format, **options):
            calls.append((pixel_format, options.get("crop_size")))
            self.assertEqual((width, height), (156, 160))
            self.assertTrue(source.read_bytes())
            return (b"\x20\x40\x60" * width * height * len(indices) if pixel_format == "rgb24"
                    else b"\x80" * width * height * len(indices))

        reader = movie_reader(self.cropped)
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(cri_emote, "_load_bundle", return_value=SimpleNamespace(objects=[reader])), \
                patch.object(emote_media, "find_ffmpeg", return_value=Path("ffmpeg")), \
                patch.object(emote_media, "_decode_stream", side_effect=decode):
            result = cri_video.read_cri_video_preview("source.bundle", "VSkill_Hero124", work_dir=folder)
            image = Image.open(io.BytesIO(base64.b64decode(result["frames"][0].split(",", 1)[1])))
            self.assertEqual(image.getpixel((0, 0)), (32, 64, 96, 128))
            self.assertEqual(result["animation_kind"], "cri_usm")
            self.assertEqual((result["width"], result["encoded_width"]), (156, 160))
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertEqual(calls, [("rgb24", (156, 160)), ("gray", (156, 160))])

    def test_decode_command_crops_before_scaling_and_keeps_top_left_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "out.raw"
            commands = []

            def run(command, **_options):
                commands.append(command)
                output.write_bytes(b"\0" * (100 * 72 * 3))
                return SimpleNamespace(returncode=0)

            with patch.object(emote_media.subprocess, "run", side_effect=run):
                emote_media._decode_stream(Path("ffmpeg"), Path("input.m1v"), output, [0], 100, 72, "rgb24",
                                          crop_size=(1500, 1080))
            filters = commands[0][commands[0].index("-vf") + 1]
            self.assertIn("crop=1500:1080:0:0:exact=1,scale=100:72", filters)


if __name__ == "__main__":
    unittest.main()
