"""Character 表情视频地址编辑的有界结构和保真回归。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from astral_party_auto.modkit import emote_config as config


def integer(number, value):
    return config._encode_varint(number << 3) + config._encode_varint(value)


def fixed(number, value):
    return config._encode_varint((number << 3) | 5) + value.to_bytes(4, "little")


def message(number, raw):
    return config._encode_varint((number << 3) | 2) + config._encode_varint(len(raw)) + raw


def emote(emote_id, video=None, *, flag=True, unknown=b""):
    raw = fixed(1, emote_id)
    if video is not None:
        raw += message(2, video.encode("utf-8") if isinstance(video, str) else video)
    if flag:
        raw += integer(3, 1)
    return raw + unknown


def character(character_id, emotes, *, unknown=b""):
    return fixed(1, character_id) + b"".join(message(2, row) for row in emotes) + unknown


def table(characters, *, mirrors=None, unknown=b""):
    mirrors = characters if mirrors is None else mirrors
    return (b"".join(message(3, raw) for _, raw in characters)
            + b"".join(message(4, fixed(1, key) + message(2, raw) + integer(80, 9)) for key, raw in mirrors)
            + unknown)


def fixture():
    unknown_emote = message(33, b"future emote extension") + b"\xd0\x02\x81\x00"
    role101 = character(101, [emote(120101008, flag=False, unknown=unknown_emote),
                              emote(120101009, "VEmoji_101_09")], unknown=message(30, b"other role data"))
    role102 = character(102, [emote(120102001, flag=True), emote(120102013, "VEmoji_102_13")])
    return table([(101, role101), (102, role102)], unknown=message(1, b"unrelated character collection")
                 + message(2, b"unrelated dictionary") + integer(91, 129))


class CharacterEmoteTests(unittest.TestCase):
    def test_reads_real_field_shape(self):
        records = config.inspect_character_emotes(fixture())
        self.assertEqual(records[0], {"character_id": 101, "emote_id": 120101008, "video_name": ""})
        self.assertEqual(records[-1]["video_name"], "VEmoji_102_13")

    def test_static_becomes_video_in_both_mirrors(self):
        source = fixture()
        changed = config.replace_character_emote(source, 120101008, "VEmoji_101_09")
        self.assertEqual(changed.count(b"VEmoji_101_09"), 4)
        self.assertEqual(config.inspect_character_emotes(changed)[0]["video_name"], "VEmoji_101_09")
        self.assertEqual(config.replace_character_emote(changed, 120101008, ""), source)

    def test_unknown_bytes_and_other_records_remain_identical(self):
        source = fixture()
        changed = config.replace_character_emote(source, 120101008, "VEmoji_101_09")
        before_rows, before = config._inspect(source)
        after_rows, after = config._inspect(changed)
        self.assertEqual([r.raw for r in before_rows if r.number not in (3, 4)],
                         [r.raw for r in after_rows if r.number not in (3, 4)])
        self.assertEqual(before[102], after[102])
        self.assertEqual(before[101][120101009], after[101][120101009])
        for role_rows in (before[101], after[101]):
            fields = config._fields(role_rows[120101008][0])
            other = [row.raw for row in fields if row.number != 2]
            if role_rows is before[101]:
                baseline = other
            else:
                self.assertEqual(other, baseline)

    def test_same_address_is_byte_identical(self):
        source = fixture()
        self.assertEqual(config.replace_character_emote(source, 120101009, "VEmoji_101_09"), source)
        self.assertEqual(config.replace_character_emote(source, 120101008, ""), source)

    def test_remove_existing_and_explicit_empty_address(self):
        for address in ("VEmoji_101_09", ""):
            role = character(101, [emote(120101008, address)])
            source = table([(101, role)])
            changed = config.replace_character_emote(source, 120101008, "")
            expected = table([(101, character(101, [emote(120101008)]))])
            self.assertEqual(changed, expected)

    def test_requires_existing_id_and_valid_integer(self):
        for value in (True, False, "120101008", 0, -1, 2 ** 32, 120999999):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.replace_character_emote(fixture(), value, "VEmoji_101_09")

    def test_rejects_unsafe_video_names(self):
        for value in (None, "https://example.com", "../VEmoji_1", "VEmoji_1_09.usm", "VEmoji_", "VEmoji__1", "VEmoji_1\n", "VEmoji_中文", "VEmoji_" + "1" * 129):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.replace_character_emote(fixture(), 120101008, value)

    def test_rejects_missing_or_different_mirror(self):
        role = character(101, [emote(120101008)])
        other = character(101, [emote(120101008, "VEmoji_101_09")])
        for raw in (table([(101, role)], mirrors=[]), table([(101, role)], mirrors=[(101, other)]),
                    table([(101, role)], mirrors=[(102, role)])):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                config.inspect_character_emotes(raw)

    def test_rejects_duplicate_characters_or_emotes(self):
        role = character(101, [emote(120101008)])
        duplicate_emote = character(101, [emote(120101008), emote(120101008)])
        duplicate_global = character(102, [emote(120101008)])
        for raw in (table([(101, role), (101, role)]), table([(101, duplicate_emote)]),
                    table([(101, role), (102, duplicate_global)]),
                    table([(101, role)], mirrors=[(101, role), (101, role)])):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                config.inspect_character_emotes(raw)

    def test_rejects_duplicate_or_wrong_known_fields(self):
        malformed = [fixed(1, 120101008) + fixed(1, 120101009),
                     integer(1, 120101008),
                     fixed(1, 120101008) + integer(2, 1),
                     fixed(1, 120101008) + message(2, b"VEmoji_101_09") * 2,
                     fixed(1, 120101008) + message(3, b"wrong flag"),
                     fixed(1, 120101008) + integer(3, 1) * 2,
                     fixed(1, 0), b""]
        for emote_row in malformed:
            role = character(101, [emote_row])
            with self.subTest(row=emote_row), self.assertRaises(ValueError):
                config.inspect_character_emotes(table([(101, role)]))

    def test_rejects_invalid_utf8(self):
        role = character(101, [emote(120101008, b"VEmoji_\xff")])
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            config.inspect_character_emotes(table([(101, role)]))

    def test_rejects_truncated_and_out_of_bounds_wire_data(self):
        for raw in (b"", fixture()[:-1], b"\x1a\x80", b"\x1a\xff\xff\xff\xff\x7f", b"\x1d\x00", b"\x08\xff" * 6,
                    b"\x00", b"\x1b", b"\x1a" + b"\xff" * 10, b"\x80" * 10 + b"\x00"):
            with self.subTest(raw=raw[:15]), self.assertRaises(ValueError):
                config.inspect_character_emotes(raw)

    def test_limits_configuration_size(self):
        with self.assertRaises(ValueError):
            config.inspect_character_emotes(b"0" * (config.MAX_CONFIG_BYTES + 1))

    def test_unknown_fixed64_and_noncanonical_varint_survive(self):
        unknown = b"\x89\x03" + bytes(range(8)) + b"\x90\x03\x81\x00"
        role = character(101, [emote(120101008)], unknown=unknown)
        source = table([(101, role)], unknown=unknown)
        changed = config.replace_character_emote(source, 120101008, "VEmoji_101_09")
        self.assertEqual(changed.count(unknown), source.count(unknown))


class _Asset:
    def __init__(self, name, path_id, raw):
        self.path_id = path_id
        self.assets_file = SimpleNamespace(name="test.assets")
        self.type = SimpleNamespace(name="TextAsset")
        self.data = SimpleNamespace(m_Name=name, m_Script=raw.decode("utf-8", errors="surrogateescape"))
        self.raw = raw
        self.data.save = self.save

    def peek_name(self):
        return self.data.m_Name

    def read(self):
        return self.data

    def save(self):
        self.raw = self.data.m_Script.encode("utf-8", errors="surrogateescape")

    def get_raw_data(self):
        return self.raw


class _Bundle:
    def __init__(self, rows=None):
        self.objects = rows if rows is not None else [_Asset("Character", 1, fixture()), _Asset("Other", 2, b"unchanged")]
        self.file = self
        self.corrupt_other = False

    def save(self):
        rows = [{"name": obj.peek_name(), "path_id": obj.path_id,
                 "raw": (b"corrupted" if self.corrupt_other and obj.path_id == 2 else obj.raw).hex()}
                for obj in self.objects]
        return json.dumps(rows).encode("utf-8")

    @classmethod
    def load(cls, raw, **kwargs):
        return cls([_Asset(row["name"], row["path_id"], bytes.fromhex(row["raw"])) for row in json.loads(raw)])


class ConfigBundleAdapterTests(unittest.TestCase):
    def test_bundle_inspection_and_successful_atomic_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.bundle"
            destination = Path(directory) / "draft.bundle"
            source.write_bytes(b"source remains unchanged")
            bundle = _Bundle()
            with mock.patch.object(config, "_load_bundle", return_value=bundle), mock.patch.object(config.UnityPy, "load", side_effect=_Bundle.load):
                original = config.inspect_emote_config_bundle(source)
                result = config.replace_emote_config_bundle(source, destination, 120101008, "VEmoji_101_09")
            self.assertEqual(original["asset_path_id"], result["asset_path_id"])
            self.assertEqual(result["records"][0]["video_name"], "VEmoji_101_09")
            saved = _Bundle.load(destination.read_bytes())
            self.assertEqual(saved.objects[1].raw, b"unchanged")
            self.assertEqual(source.read_bytes(), b"source remains unchanged")
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["draft.bundle", "source.bundle"])

    def test_corrupted_other_object_never_replaces_existing_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "draft.bundle"
            destination.write_bytes(b"existing draft")
            bundle = _Bundle()
            bundle.corrupt_other = True
            with mock.patch.object(config, "_load_bundle", return_value=bundle), mock.patch.object(config.UnityPy, "load", side_effect=_Bundle.load):
                with self.assertRaisesRegex(ValueError, "其它资源对象"):
                    config.replace_emote_config_bundle(Path(directory) / "source.bundle", destination, 120101008, "VEmoji_101_09")
            self.assertEqual(destination.read_bytes(), b"existing draft")
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["draft.bundle"])

    def test_failed_atomic_replace_keeps_original_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "draft.bundle"
            destination.write_bytes(b"existing draft")
            with mock.patch.object(config, "_load_bundle", return_value=_Bundle()), mock.patch.object(config.UnityPy, "load", side_effect=_Bundle.load), mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    config.replace_emote_config_bundle(Path(directory) / "source.bundle", destination, 120101008, "VEmoji_101_09")
            self.assertEqual(destination.read_bytes(), b"existing draft")
            self.assertEqual([path.name for path in Path(directory).iterdir()], ["draft.bundle"])

    def test_missing_or_duplicate_character_asset_is_rejected(self):
        duplicate = _Bundle([_Asset("Character", 1, fixture()), _Asset("Character", 2, fixture())])
        missing = _Bundle([_Asset("Other", 1, fixture())])
        for bundle in (duplicate, missing):
            with self.subTest(bundle=bundle), mock.patch.object(config, "_load_bundle", return_value=bundle):
                with self.assertRaises(ValueError):
                    config.inspect_emote_config_bundle("ignored.bundle")


if __name__ == "__main__":
    unittest.main()
