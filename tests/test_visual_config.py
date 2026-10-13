"""卡面/立绘地址修改的镜像、保真和 bundle 保存回归。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from astral_party_auto.modkit import visual_config as config

ANIMATION_URL = "ui://xuaw6o8jjmTest123"
NAME = "UT_Test_Image"
UNKNOWN = b"\x89\x03" + bytes(range(8)) + b"\x90\x03\x81\x00"


def integer(number, value):
    return config._encode_varint(number << 3) + config._encode_varint(value)


def fixed(number, value):
    return config._encode_varint((number << 3) | 5) + value.to_bytes(4, "little")


def message(number, raw):
    value = raw.encode("utf-8") if isinstance(raw, str) else raw
    return config._encode_varint((number << 3) | 2) + config._encode_varint(len(value)) + value


def card(record_id, addresses=None, extra=b""):
    addresses = {6: NAME, 7: NAME} if addresses is None else addresses
    return fixed(1, record_id) + b"".join(message(n, v) for n, v in addresses.items()) + extra


def skin(record_id, addresses=None, extra=b""):
    addresses = {7: NAME, 8: NAME, 9: NAME, 10: "UT_Other"} if addresses is None else addresses
    return fixed(3, record_id) + b"".join(message(n, v) for n, v in addresses.items()) + extra


def parent(parent_id, children, extra=b""):
    return fixed(1, parent_id) + b"".join(message(2, row) for row in children) + extra


def table(rows, mirrors=None, extra=b""):
    mirrors = rows if mirrors is None else mirrors
    return (b"".join(message(1, raw) for _, raw in rows)
            + b"".join(message(2, fixed(1, key) + message(2, raw) + UNKNOWN) for key, raw in mirrors)
            + extra)


def fixture(name):
    if name == "Card":
        rows = [(10001, card(10001, extra=fixed(3, 99) + message(4, NAME) + UNKNOWN)),
                (10002, card(10002, {6: NAME + "_sfw"})),
                (10003, card(10003, {6: NAME}, extra=integer(20, 1)))]
    else:
        rows = [(101, parent(101, [skin(100101001, extra=fixed(1, 0) + integer(4, 1)
                                                   + message(14, NAME) + UNKNOWN),
                                  skin(100101002, {7: NAME + "_sfw"})], extra=UNKNOWN)),
                (102, parent(102, [skin(100102001, {7: NAME})]))]
    return table(rows, extra=message(3, NAME) + UNKNOWN)


class VisualAddressTests(unittest.TestCase):
    def test_inspection_groups_exact_names_with_usage_and_record_ids(self):
        for name in ("Card", "Skin"):
            report = config.inspect_visual_config(fixture(name), name)
            target = next(t for t in report["targets"] if t["name"] == NAME)
            self.assertEqual(target["record_ids"], [10001, 10003] if name == "Card" else [100101001, 100102001])
            self.assertEqual(target["field_numbers"], [6, 7] if name == "Card" else [7, 8, 9])
            self.assertEqual(report["record_count"], 3)
            self.assertIn("SFW", target["usage"])

    def test_both_mirrors_and_all_exact_leaves_change_and_restore(self):
        for name in ("Card", "Skin"):
            with self.subTest(table=name):
                source = fixture(name)
                changed = config.replace_visual_address(source, name, NAME, ANIMATION_URL)
                self.assertEqual(changed.count(ANIMATION_URL.encode()), 6 if name == "Card" else 8)
                self.assertEqual(config.replace_visual_address(changed, name, ANIMATION_URL, NAME), source)
                self.assertIn((NAME + "_sfw").encode(), changed)

    def test_non_target_fields_and_records_remain_byte_identical(self):
        for name in ("Card", "Skin"):
            source = fixture(name)
            changed = config.replace_visual_address(source, name, NAME, ANIMATION_URL)
            before_rows, before, _ = config._inspect(source, name)
            after_rows, after, _ = config._inspect(changed, name)
            self.assertEqual([r.raw for r in before_rows if r.number not in (1, 2)],
                             [r.raw for r in after_rows if r.number not in (1, 2)])
            allowed = config._USAGES[name]
            for record_id, old in before.items():
                new = after[record_id]
                self.assertEqual([r.raw for r in config._fields(old.raw) if r.number not in allowed],
                                 [r.raw for r in config._fields(new.raw) if r.number not in allowed])
                if NAME not in old.addresses.values():
                    self.assertEqual(old.raw, new.raw)
            self.assertEqual(source.count(UNKNOWN), changed.count(UNKNOWN))

    def test_omitted_and_empty_addresses_are_not_added(self):
        for name, row in (("Card", card(1, {6: ""})), ("Skin", parent(1, [skin(2, {7: ""})]))):
            source = table([(1, row)])
            self.assertEqual(config.inspect_visual_config(source, name)["targets"], [])
            with self.assertRaises(ValueError):
                config.replace_visual_address(source, name, "", ANIMATION_URL)

    def test_same_address_is_byte_identical(self):
        for name in ("Card", "Skin"):
            source = fixture(name)
            self.assertEqual(config.replace_visual_address(source, name, NAME, NAME), source)

    def test_profile_photo_and_other_tables_are_not_editable(self):
        row = parent(1, [skin(2, {7: "UT_Other"}, extra=message(14, NAME))])
        with self.assertRaisesRegex(ValueError, "找不到"):
            config.replace_visual_address(table([(1, row)]), "Skin", NAME, ANIMATION_URL)
        for name in (None, "Character", "card", "ProfilePhoto", ["Card"]):
            with self.subTest(table=name), self.assertRaises(ValueError):
                config.inspect_visual_config(fixture("Card"), name)

    def test_names_reject_paths_external_and_other_package_urls(self):
        for name in (None, "", "../UT_Test", "UT/Test", "UT_Test.png", "https://example.com", "file://x",
                     "ui://xuaw6o8jloading", "ui://otherjmTest", "ui://xuaw6o8jjm", "UT_Test\n",
                     "UT_Test\x00", "UT_中文", "U" * 257, "ui://xuaw6o8jjm" + "x" * 49):
            for side in ("old", "new"):
                old, new = (name, ANIMATION_URL) if side == "old" else (NAME, name)
                with self.subTest(name=name, side=side), self.assertRaises(ValueError):
                    config.replace_visual_address(fixture("Card"), "Card", old, new)

    def test_mirror_must_match_every_parent_byte_not_just_addresses(self):
        original = card(1)
        for mirror in ([], [(2, original)], [(1, card(1, extra=integer(20, 1)))], [(1, original), (1, original)]):
            with self.subTest(mirror=mirror), self.assertRaises(ValueError):
                config.inspect_visual_config(table([(1, original)], mirrors=mirror), "Card")

    def test_duplicate_ids_leaves_and_wrong_known_wires_are_rejected(self):
        malformed_card = [fixed(1, 0), integer(1, 1), fixed(1, 1) * 2,
                          fixed(1, 1) + integer(6, 1), card(1) + message(6, NAME),
                          card(1) + fixed(7, 9), b""]
        for row in malformed_card:
            with self.subTest(row=row), self.assertRaises(ValueError):
                config.inspect_visual_config(table([(1, row)]), "Card")
        malformed_skin = [integer(3, 1), fixed(3, 1) * 2,
                          skin(1) + message(7, NAME), skin(1) + integer(1, 0),
                          skin(1) + fixed(10, 1), b""]
        for row in malformed_skin:
            with self.subTest(row=row), self.assertRaises(ValueError):
                config.inspect_visual_config(table([(1, parent(1, [row]))]), "Skin")
        for name, rows in (("Card", [(1, card(1)), (1, card(1))]),
                           ("Skin", [(1, parent(1, [skin(2), skin(2)]))]),
                           ("Skin", [(1, parent(1, [skin(2)])), (3, parent(3, [skin(2)]))])):
            with self.subTest(table=name), self.assertRaises(ValueError):
                config.inspect_visual_config(table(rows), name)

    def test_unknown_or_missing_known_layout_is_rejected(self):
        for name, raw in (("Card", message(3, b"irrelevant")), ("Card", integer(1, 1)),
                          ("Card", message(1, card(1)) + message(2, fixed(1, 1))),
                          ("Card", message(1, card(1)) + message(2, fixed(1, 1) + integer(2, 1))),
                          ("Skin", table([(1, fixed(1, 1))])),
                          ("Skin", table([(1, fixed(1, 1) + integer(2, 1))]))):
            with self.subTest(table=name, raw=raw), self.assertRaises(ValueError):
                config.inspect_visual_config(raw, name)

    def test_default_skin_item_id_is_unique_within_each_parent(self):
        default = message(7, NAME) + message(14, "UT_ProfilePhoto")
        source = table([(1001, parent(1001, [default])), (1002, parent(1002, [fixed(3, 0) + default]))])
        target = config.inspect_visual_config(source, "Skin")["targets"][0]
        self.assertEqual(target["record_ids"], ["1001:0", "1002:0"])
        self.assertEqual(target["parent_ids"], [1001, 1002])
        changed = config.replace_visual_address(source, "Skin", NAME, ANIMATION_URL)
        self.assertEqual(config.replace_visual_address(changed, "Skin", ANIMATION_URL, NAME), source)
        duplicate = table([(1001, parent(1001, [default, fixed(3, 0) + default]))])
        with self.assertRaisesRegex(ValueError, "重复"):
            config.inspect_visual_config(duplicate, "Skin")

    def test_invalid_utf8_and_truncated_wire_data_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "UTF-8"):
            config.inspect_visual_config(table([(1, card(1, {6: b"UT_\xff"}))]), "Card")
        for raw in (b"", fixture("Card")[:-1], b"\x0a\x80", b"\x0a\xff\xff\xff\xff\x7f",
                    b"\x0d\x00", b"\x09\x00", b"\x00", b"\x0b", b"\x80" * 10 + b"\x00",
                    b"\x0a" + b"\xff" * 10):
            with self.subTest(raw=raw[:15]), self.assertRaises(ValueError):
                config.inspect_visual_config(raw, "Card")

    def test_size_field_and_record_limits(self):
        with self.assertRaises(ValueError):
            config.inspect_visual_config(b"0" * (config.MAX_CONFIG_BYTES + 1), "Card")
        with mock.patch.object(config, "MAX_FIELDS", 1), self.assertRaises(ValueError):
            config.inspect_visual_config(fixture("Card"), "Card")
        with mock.patch.object(config, "MAX_RECORDS", 1), self.assertRaises(ValueError):
            config.inspect_visual_config(fixture("Skin"), "Skin")


class _Asset:
    def __init__(self, name, path_id, raw, *, as_bytes=False):
        self.path_id = path_id
        self.assets_file = SimpleNamespace(name="test.assets")
        self.type = SimpleNamespace(name="TextAsset")
        self.data = SimpleNamespace(m_Name=name, m_Script=raw if as_bytes else raw.decode("utf-8", errors="surrogateescape"))
        self.data.save = self.save
        self.raw = raw

    def peek_name(self):
        return self.data.m_Name

    def read(self):
        return self.data

    def save(self):
        self.raw = self.data.m_Script if isinstance(self.data.m_Script, bytes) else self.data.m_Script.encode("utf-8", errors="surrogateescape")

    def get_raw_data(self):
        return self.raw


class _Bundle:
    def __init__(self, name="Card", objects=None, *, as_bytes=False):
        self.objects = objects if objects is not None else [_Asset(name, 1, fixture(name), as_bytes=as_bytes),
                                                          _Asset("Other", 2, b"unchanged")]
        self.file = self
        self.corrupt_other = False

    def save(self):
        return json.dumps([{"name": obj.peek_name(), "path_id": obj.path_id,
                            "raw": (b"corrupted" if self.corrupt_other and obj.path_id == 2 else obj.raw).hex()}
                           for obj in self.objects]).encode("utf-8")

    @classmethod
    def load(cls, raw, **kwargs):
        return cls(objects=[_Asset(row["name"], row["path_id"], bytes.fromhex(row["raw"])) for row in json.loads(raw)])


class VisualBundleTests(unittest.TestCase):
    def test_saves_both_supported_tables_and_preserves_input_and_other_objects(self):
        for name in ("Card", "Skin"):
            for as_bytes in (False, True):
                with self.subTest(table=name, as_bytes=as_bytes), tempfile.TemporaryDirectory() as directory:
                    source, destination = Path(directory) / "source.bundle", Path(directory) / "draft.bundle"
                    source.write_bytes(b"source remains unchanged")
                    with mock.patch.object(config, "_load_bundle", return_value=_Bundle(name, as_bytes=as_bytes)), \
                            mock.patch.object(config.UnityPy, "load", side_effect=_Bundle.load):
                        result = config.replace_visual_config_bundle(source, destination, name, NAME, ANIMATION_URL)
                    self.assertEqual(result["asset_name"], name)
                    self.assertEqual(source.read_bytes(), b"source remains unchanged")
                    saved = _Bundle.load(destination.read_bytes())
                    self.assertEqual(saved.objects[1].raw, b"unchanged")
                    self.assertIn(ANIMATION_URL.encode(), saved.objects[0].raw)
                    self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["draft.bundle", "source.bundle"])

    def test_changed_other_object_or_target_does_not_replace_existing_output(self):
        for corrupt in ("other", "target", "identity"):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "draft.bundle"
                destination.write_bytes(b"existing draft")
                bundle = _Bundle()
                bundle.corrupt_other = corrupt == "other"

                def reload(payload, **kwargs):
                    saved = _Bundle.load(payload)
                    if corrupt == "target":
                        saved.objects[0].data.m_Script = "wrong script"
                    if corrupt == "identity":
                        saved.objects[0].path_id = 3
                    return saved

                with mock.patch.object(config, "_load_bundle", return_value=bundle), \
                        mock.patch.object(config.UnityPy, "load", side_effect=reload), self.assertRaises(ValueError):
                    config.replace_visual_config_bundle(Path(directory) / "source.bundle", destination, "Card", NAME, ANIMATION_URL)
                self.assertEqual(destination.read_bytes(), b"existing draft")
                self.assertEqual([p.name for p in Path(directory).iterdir()], ["draft.bundle"])

    def test_atomic_replace_failure_keeps_output_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "draft.bundle"
            destination.write_bytes(b"existing draft")
            with mock.patch.object(config, "_load_bundle", return_value=_Bundle()), \
                    mock.patch.object(config.UnityPy, "load", side_effect=_Bundle.load), \
                    mock.patch.object(config.os, "replace", side_effect=PermissionError("locked")), self.assertRaises(PermissionError):
                config.replace_visual_config_bundle(Path(directory) / "source.bundle", destination, "Card", NAME, ANIMATION_URL)
            self.assertEqual(destination.read_bytes(), b"existing draft")
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["draft.bundle"])

    def test_output_cannot_be_input_or_hardlink_to_input(self):
        with tempfile.TemporaryDirectory() as directory:
            source, alias = Path(directory) / "source.bundle", Path(directory) / "alias.bundle"
            source.write_bytes(b"original")
            alias.hardlink_to(source)
            for destination in (source, alias):
                with self.subTest(destination=destination), mock.patch.object(config, "_load_bundle") as load, self.assertRaises(ValueError):
                    config.replace_visual_config_bundle(source, destination, "Card", NAME, ANIMATION_URL)
                load.assert_not_called()
            self.assertEqual(source.read_bytes(), b"original")

    def test_missing_duplicate_assets_and_duplicate_object_ids_are_rejected(self):
        bundles = [_Bundle(objects=[_Asset("Other", 1, b"irrelevant")]),
                   _Bundle(objects=[_Asset("Card", 1, fixture("Card")), _Asset("Card", 2, fixture("Card"))]),
                   _Bundle(objects=[_Asset("Card", 1, fixture("Card")), _Asset("Other", 1, b"irrelevant")])]
        for bundle in bundles:
            with self.subTest(bundle=bundle), mock.patch.object(config, "_load_bundle", return_value=bundle), self.assertRaises(ValueError):
                config.replace_visual_config_bundle("input.bundle", "output.bundle", "Card", NAME, ANIMATION_URL)


if __name__ == "__main__":
    unittest.main()
