"""生成 USM 结构夹具；不把容器测试当成 CRI 解码或游戏播放验证。"""
from __future__ import annotations

import copy
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from astral_party_auto.modkit import cri_emote


def utf_table(name, rows):
    strings = bytearray()
    offsets = {}

    def string(value):
        if value not in offsets:
            offsets[value] = len(strings)
            strings.extend(value.encode() + b"\0")
        return offsets[value]

    table_name = string(name)
    schema, values = bytearray(), bytearray()
    for field, sample in rows[0].items():
        schema.extend(struct.pack(">BI", 0x5A if isinstance(sample, str) else 0x54, string(field)))
    for row in rows:
        for value in row.values():
            values.extend(struct.pack(">I", string(value) if isinstance(value, str) else value))
    rows_at = 32 + len(schema)
    strings_at = rows_at + len(values)
    end = strings_at + len(strings)
    return (b"@UTF" + struct.pack(">IIIIIHHI", end - 8, rows_at - 8, strings_at - 8, end - 8,
                                table_name, len(rows[0]), 4 * len(rows[0]), len(rows))
            + schema + values + strings)


def chunk(tag, kind, payload, *, time=0, rate=30, channel=0):
    padding = (-32 - len(payload)) % 32
    return (tag + struct.pack(">I", 24 + len(payload) + padding) + bytes([0, 24])
            + struct.pack(">H", padding) + bytes([channel, 0, 0, kind])
            + struct.pack(">II", time, rate) + b"\0" * 8 + payload + b"\0" * padding)


def make_usm(*, alpha=True, codec=1, width=160, frames=1, alpha_frames=None, bad_seek=False, body=b"sample"):
    tags = (b"@SFV", b"@ALP") if alpha else (b"@SFV",)
    markers = {kind: next(marker for marker, value in cri_emote._END_MARKERS.items() if value == kind)
               for kind in ("header", "metadata", "contents")}

    def build(length, offsets):
        directory = [{"filename": "sample.usm", "stmid": 0, "chno": 65535, "filesize": length}]
        directory.extend({"filename": "sample.m2v", "stmid": int.from_bytes(tag, "big"),
                          "chno": 0, "filesize": 100} for tag in tags)
        output = chunk(b"CRID", 1, utf_table("CRIUSF_DIR_STREAM", directory))
        for tag in tags:
            header = {"width": width, "height": 160, "mat_width": width, "mat_height": 160,
                      "disp_width": width, "disp_height": 160, "framerate_n": 30000,
                      "framerate_d": 1000, "total_frames": alpha_frames if tag == b"@ALP" and alpha_frames else frames,
                      "mpeg_codec": codec, "alpha_type": int(tag == b"@ALP")}
            output += chunk(tag, 1, utf_table("VIDEO_HDRINFO", [header]))
        for tag in tags:
            output += chunk(tag, 2, markers["header"])
        for tag in tags:
            row = {"ofs_byte": 999 if bad_seek else offsets.get(tag, 0), "ofs_frmid": 0,
                   "num_skip": 0, "resv": 0}
            output += chunk(tag, 3, utf_table("VIDEO_SEEKINFO", [row]))
        for tag in tags:
            output += chunk(tag, 2, markers["metadata"])
        found = {}
        for index in range(frames):
            for tag in tags:
                found.setdefault(tag, len(output))
                sequence = b"\0\0\1\xb3" + ((width << 12) | 160).to_bytes(3, "big") + b"\x15"
                payload = sequence + b"\0\0\1\0" + body
                output += chunk(tag, 0, payload, time=index * 99, rate=3000)
        for tag in tags:
            output += chunk(tag, 2, markers["contents"])
        return output, found

    first, offsets = build(0, {})
    return build(len(first), offsets)[0]


def object_tree(raw, name="VEmoji_sample"):
    return {"m_Name": name, "m_Script": {"m_FileID": 1, "m_PathID": 700},
            "movieInfo": {"width": 0, "totalFrames": 0}, "assetInfo": {"loop": 1},
            "implementation": {"rid": 1000}, "references": {"version": 2, "RefIds": [
                {"rid": 1000, "type": {"class": "CriSerializedBytesAssetImpl", "ns": "CriWare.Assets",
                                      "asm": "CriMw.CriWare.Assets.Runtime"}, "data": {"data": list(raw)}}]}}


class FakeReader:
    def __init__(self, tree, path_id=100, kind="MonoBehaviour"):
        self.tree, self.path_id = copy.deepcopy(tree), path_id
        self.type, self.assets_file = SimpleNamespace(name=kind), SimpleNamespace(name="file")

    def peek_name(self):
        return self.tree["m_Name"]

    def read_typetree(self):
        return copy.deepcopy(self.tree)

    def save_typetree(self, tree):
        self.tree = copy.deepcopy(tree)

    def get_raw_data(self):
        return repr(self.tree).encode()


class CriEmoteTests(unittest.TestCase):
    def test_generated_dual_stream_summary_is_bounded_and_honest(self):
        info = cri_emote.inspect_usm_bytes(make_usm(frames=2))
        self.assertEqual((info["width"], info["height"], info["frames"]), (160, 160, 2))
        self.assertTrue(info["has_alpha"])
        self.assertEqual(info["codec"], "MPEG-1")
        self.assertFalse(info["preview_available"])
        self.assertEqual([row["alpha_type"] for row in info["streams"]], [0, 1])

    def test_codec_comes_from_elementary_stream_instead_of_header_id(self):
        info = cri_emote.inspect_usm_bytes(make_usm(body=b"\0\0\1\xb5\x10extension"))
        self.assertEqual(info["codec"], "MPEG-2")
        with self.assertRaises(ValueError):
            cri_emote._compatible(info, cri_emote.inspect_usm_bytes(make_usm()))

    def test_rgb_only_unknown_codec_and_mismatched_alpha_are_rejected(self):
        for raw in (make_usm(alpha=False), make_usm(codec=9), make_usm(alpha_frames=2)):
            with self.subTest(length=len(raw)), self.assertRaises(ValueError):
                cri_emote.inspect_usm_bytes(raw)

    def test_bad_or_truncated_container_does_not_pass_as_video(self):
        raw = make_usm()
        broken = bytearray(raw)
        struct.pack_into(">I", broken, 4, len(raw) + 100)
        for candidate in (b"GIF89a" + raw, raw[:-1], raw + b"tail", bytes(broken), make_usm(bad_seek=True)):
            with self.subTest(length=len(candidate)), self.assertRaises(ValueError):
                cri_emote.inspect_usm_bytes(candidate)

    def test_missing_contents_end_and_duplicate_header_are_rejected(self):
        raw = make_usm()
        chunks = list(cri_emote._chunks(raw))
        header = next(row for row in chunks if row.tag == b"@SFV" and row.kind == 1)
        duplicate = chunk(header.tag, header.kind, bytes(header.payload))
        for candidate in (raw[:-64], raw[:header.offset] + duplicate + raw[header.offset:]):
            with self.assertRaises(ValueError):
                cri_emote.inspect_usm_bytes(candidate)

    def test_invalid_utf_offset_and_duplicate_column_are_rejected(self):
        table = bytearray(utf_table("table", [{"a": 1, "b": 2}]))
        struct.pack_into(">I", table, 12, 0xFFFFFFFF)
        with self.assertRaises(ValueError):
            cri_emote._utf_table(table)
        table = bytearray(utf_table("table", [{"a": 1, "b": 2}]))
        table[38:42] = table[33:37]
        with self.assertRaises(ValueError):
            cri_emote._utf_table(table)

    def test_legacy_filename_encoding_is_descriptive_but_schema_stays_strict(self):
        table = utf_table("table", [{"filename": "source"}])
        changed = table.replace(b"source\0", b"\xffource\0")
        name, rows = cri_emote._utf_table(changed)
        self.assertEqual(name, "table")
        self.assertIn("\ufffd", rows[0]["filename"])
        with self.assertRaises(ValueError):
            cri_emote._utf_table(table.replace(b"table\0", b"\xffable\0"))

    def test_exact_geometry_and_alpha_layout_required_for_import(self):
        reference = cri_emote.inspect_usm_bytes(make_usm())
        candidate = cri_emote.inspect_usm_bytes(make_usm(width=320))
        with self.assertRaises(ValueError):
            cri_emote._compatible(candidate, reference)
        cri_emote._compatible(cri_emote.inspect_usm_bytes(make_usm(frames=2)), reference)

    def test_embedded_object_requires_unique_name_and_matching_implementation(self):
        reader = FakeReader(object_tree(make_usm()))
        for objects in ([], [reader, reader]):
            with self.assertRaises(ValueError):
                cri_emote._embedded_object(SimpleNamespace(objects=objects), "VEmoji_sample")
        tree = object_tree(make_usm())
        tree["implementation"]["rid"] = 2000
        with self.assertRaises(ValueError):
            cri_emote._embedded_object(SimpleNamespace(objects=[FakeReader(tree)]), "VEmoji_sample")

    def test_external_impl_and_duplicate_rid_are_rejected(self):
        tree = object_tree(make_usm())
        tree["references"]["RefIds"][0]["type"]["class"] = "CriStreamingAssetsImpl"
        with self.assertRaises(ValueError):
            cri_emote._embedded_object(SimpleNamespace(objects=[FakeReader(tree)]), "VEmoji_sample")
        tree = object_tree(make_usm())
        tree["references"]["RefIds"] *= 2
        with self.assertRaises(ValueError):
            cri_emote._embedded_object(SimpleNamespace(objects=[FakeReader(tree)]), "VEmoji_sample")

    def test_cached_movie_info_only_accepts_original_bytes(self):
        original = make_usm()
        tree = object_tree(original)
        tree["movieInfo"]["totalFrames"] = 1
        cri_emote._check_movie_cache(tree, original, original)
        with self.assertRaises(ValueError):
            cri_emote._check_movie_cache(tree, make_usm(body=b"changed"), original)

    def test_failed_atomic_replace_keeps_existing_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "draft.bundle"
            output.write_bytes(b"old")
            with patch.object(cri_emote.os, "replace", side_effect=OSError("locked")):
                with self.assertRaises(OSError):
                    cri_emote._write_atomic(output, b"new")
            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(Path(folder).iterdir()), [output])

    def test_replacement_roundtrip_checks_all_nonvideo_fields_before_write(self):
        raw, replacement = make_usm(), make_usm(body=b"replacement")
        with tempfile.TemporaryDirectory() as folder:
            source, imported, output = (Path(folder) / name for name in ("source.bundle", "new.usm", "out.bundle"))
            imported.write_bytes(replacement)
            output.write_bytes(b"old")
            reader = FakeReader(object_tree(raw))
            other = FakeReader({"m_Name": "unrelated", "value": 42}, path_id=200, kind="AssetBundle")
            env = SimpleNamespace(objects=[reader, other], file=SimpleNamespace(save=lambda: b"new-bundle"))

            def reload_bundle(*_args, **_kwargs):
                return SimpleNamespace(objects=[FakeReader(reader.tree), FakeReader(other.tree, 200, "AssetBundle")])

            with patch.object(cri_emote, "_load_bundle", return_value=env), \
                    patch.object(cri_emote.UnityPy, "load", side_effect=reload_bundle):
                result = cri_emote.replace_cri_emote_bundle(source, "VEmoji_sample", imported, output)
            self.assertTrue(result["compatible"])
            self.assertEqual(output.read_bytes(), b"new-bundle")

            reader = FakeReader(object_tree(raw))
            env.objects[0] = reader

            def bad_reload(*_args, **_kwargs):
                tree = copy.deepcopy(reader.tree)
                tree["assetInfo"]["loop"] = 0
                return SimpleNamespace(objects=[FakeReader(tree), other])

            with patch.object(cri_emote, "_load_bundle", return_value=env), \
                    patch.object(cri_emote.UnityPy, "load", side_effect=bad_reload):
                with self.assertRaises(ValueError):
                    cri_emote.replace_cri_emote_bundle(source, "VEmoji_sample", imported, output)
            self.assertEqual(output.read_bytes(), b"new-bundle")

    def test_gif_rejection_happens_before_bundle_load_or_output_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            imported, output = Path(folder) / "fake.usm", Path(folder) / "draft.bundle"
            imported.write_bytes(b"GIF89a" + b"0" * 100)
            output.write_bytes(b"old")
            with patch.object(cri_emote, "_load_bundle") as load:
                with self.assertRaisesRegex(ValueError, "GIF"):
                    cri_emote.replace_cri_emote_bundle("source.bundle", "VEmoji_sample", imported, output)
            load.assert_not_called()
            self.assertEqual(output.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
