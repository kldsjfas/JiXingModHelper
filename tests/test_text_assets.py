"""Safe text edits use generated fixtures and never change an installed game."""
from __future__ import annotations

import codecs
import json
import struct
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from astral_party_auto.modkit import fairygui_text
from astral_party_auto.modkit import maker
from astral_party_auto.modkit.text_assets import inspect_text, replacement_bytes, script_bytes


def u16(value: int) -> bytes:
    return struct.pack(">H", value)


def u32(value: int) -> bytes:
    return struct.pack(">I", value)


def indexed(blocks: list[bytes]) -> bytes:
    offset = 2 + len(blocks) * 4
    offsets = []
    for block in blocks:
        offsets.append(offset if block else 0)
        offset += len(block)
    return bytes((len(blocks), 0)) + b"".join(u32(value) for value in offsets) + b"".join(blocks)


def string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return u16(len(raw)) + raw


def make_package() -> bytes:
    # The text and the package-item name deliberately share string index 0.
    child = indexed([b"\x06", u16(65534), u16(0), b"", u16(65534), b"", u16(0)])
    component = indexed([b"", b"", u16(1) + u16(len(child)) + child])
    item = b"\x03" + u16(0) * 4 + b"\x00" + u32(100) * 2 + b"\x00" + u32(len(component)) + component
    strings = u32(2) + string("原始文字") + string("texture.png")
    return b"FGUI" + u32(6) + b"\x00" + string("testid") + string("Test") + bytes(20) + indexed([
        u16(0) + u16(0), u16(1) + u32(len(item)) + item, b"", b"", strings, b"",
    ])


class PlainTextTests(unittest.TestCase):
    def test_preserves_bom_encoding_and_crlf(self):
        for encoding, bom in (("utf-8", codecs.BOM_UTF8), ("utf-16-le", codecs.BOM_UTF16_LE), ("utf-32-be", codecs.BOM_UTF32_BE), ("gb18030", b"")):
            original = bom + "名称=星趴\r\n按钮=确定\r\n".encode(encoding)
            output = replacement_bytes(original.decode("utf-8", "surrogateescape"), "名称=测试\n按钮=继续\n")
            self.assertEqual(output, bom + "名称=测试\r\n按钮=继续\r\n".encode(encoding))

    def test_preserves_mixed_newlines(self):
        self.assertEqual(replacement_bytes(b"a\r\nb\nc\r", "A\nB\nC\n"), b"A\r\nB\nC\r")

    def test_short_empty_and_unicode_text(self):
        for raw in (b"", b"OK", "喵".encode()):
            self.assertTrue(inspect_text(raw).editable)
            self.assertEqual(replacement_bytes(raw, inspect_text(raw).text), raw)

    def test_binary_extraction_never_becomes_editable(self):
        for raw in (b"\x00" + "这是中文二进制结构里面的文本".encode(), b"abc\x01def", b"\xff\xfe\x00", b"header" * 200 + b"\x01"):
            self.assertFalse(inspect_text(raw).editable)
            with self.assertRaises(ValueError):
                replacement_bytes(raw, "修改后的文本")

    def test_surrogateescape_is_lossless(self):
        raw = bytes(range(256))
        self.assertEqual(script_bytes(raw.decode("utf-8", "surrogateescape")), raw)

    def test_validates_json_and_xml(self):
        for raw, bad in ((b'{"name":"test"}', '{"name":'), (b"<root>old</root>", "<root>")):
            with self.assertRaises(ValueError):
                replacement_bytes(raw, bad)


class FairyGUITextTests(unittest.TestCase):
    def test_only_display_field_changes_shared_names_are_preserved(self):
        original = make_package()
        package = fairygui_text.parse_package(original)
        before = package.values()
        key = next(iter(before))
        after = {key: "界面文字替换测试"}
        updated = replacement_bytes(original, json.dumps(after))
        self.assertEqual(fairygui_text.parse_package(updated).values(), after)
        self.assertEqual(fairygui_text.parse_package(updated).strings[:2], package.strings)
        allowed = set(range(int(key), int(key) + 2)) | set(range(package.table_start, package.table_start + 4))
        self.assertTrue(all(a == b or index in allowed for index, (a, b) in enumerate(zip(original, updated))))

    def test_original_json_can_restore_modified_package(self):
        original = make_package()
        before = fairygui_text.read_text(original)
        edited = {key: "更长的文字内容" for key in json.loads(before)}
        updated = replacement_bytes(original, json.dumps(edited))
        restored = replacement_bytes(updated, before)
        self.assertEqual(fairygui_text.read_text(restored), before)
        self.assertEqual(replacement_bytes(original, before), original)

    def test_rejects_changed_missing_or_duplicate_keys(self):
        raw = make_package()
        key = next(iter(fairygui_text.parse_package(raw).values()))
        for bad in ('{}', '{"new": "value"}', '{"' + key + '": "a", "' + key + '": "b"}', '{"' + key + '": 3}'):
            with self.assertRaises(ValueError):
                replacement_bytes(raw, bad)

    def test_unsupported_or_malformed_packages_are_readonly(self):
        raw = make_package()
        for invalid in (raw[:7], raw[:7] + b"\x07" + raw[8:], raw[:8] + b"\x01" + raw[9:], raw[:-1]):
            self.assertFalse(inspect_text(invalid).editable)


class TextDraftRoundtripTests(unittest.TestCase):
    """Exercise file writes with a small stand-in for Unity's bundle container."""

    class Bundle:
        def __init__(self, payload: bytes):
            self.contents = {name: bytes.fromhex(value) for name, value in json.loads(payload).items()}
            self.file = SimpleNamespace(save=self.serialize)
            self.objects = []
            for name, raw in self.contents.items():
                data = SimpleNamespace(m_Name=name, m_Script=raw.decode("utf-8", "surrogateescape"))
                data.save = lambda data=data: self.contents.update({data.m_Name: script_bytes(data.m_Script)})
                self.objects.append(SimpleNamespace(type=SimpleNamespace(name="TextAsset"), read=lambda data=data: data))

        def serialize(self):
            return json.dumps({name: raw.hex() for name, raw in self.contents.items()}).encode()

    def setUp(self):
        temporary = TemporaryDirectory(prefix="jixing-text-roundtrip-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = self.root / "original.bundle"
        self.draft = self.root / "draft.bundle"
        reader = patch.object(maker, "_load_bundle", side_effect=lambda path: self.Bundle(Path(path).read_bytes()))
        validator = patch.object(maker.UnityPy, "load", side_effect=self.Bundle)
        reader.start()
        validator.start()
        self.addCleanup(reader.stop)
        self.addCleanup(validator.stop)

    def prepare(self, text: bytes):
        original = {"Caption": text.hex(), "Other": b"original other".hex()}
        draft = {**original, "Other": b"keep this edit".hex()}
        self.original.write_bytes(json.dumps(original).encode())
        self.draft.write_bytes(json.dumps(draft).encode())

    def saved_bytes(self, name="Caption"):
        return self.Bundle(self.draft.read_bytes()).contents[name]

    def edit(self, text: str):
        maker.replace_bundle_text(self.draft, "Caption", text, self.draft, reference_bundle=self.original)

    def test_plain_text_can_change_back_after_looking_like_json_or_xml(self):
        for intermediate in ('{"message": "changed"}', "<root>changed</root>"):
            with self.subTest(intermediate=intermediate):
                self.prepare(b"hello")
                self.edit(intermediate)
                self.edit("hello again")
                self.assertEqual(self.saved_bytes(), b"hello again")
                self.assertEqual(self.saved_bytes("Other"), b"keep this edit")

    def test_ascii_edit_does_not_lose_original_gb18030_encoding(self):
        self.prepare("原始文字".encode("gb18030"))
        self.edit("ASCII")
        self.edit("修改文字")
        self.assertEqual(self.saved_bytes(), "修改文字".encode("gb18030"))

    def test_original_json_validation_remains_active(self):
        self.prepare(b'{"message":"original"}')
        before = self.draft.read_bytes()
        with self.assertRaises(ValueError):
            self.edit("not json")
        self.assertEqual(self.draft.read_bytes(), before)

    def test_repeated_fairygui_edits_do_not_accumulate_unused_strings(self):
        original = make_package()
        self.prepare(original)
        values = json.loads(fairygui_text.read_text(original))
        key = next(iter(values))
        for text in ("first edit", "second edit"):
            values[key] = text
            self.edit(json.dumps(values))
            saved = fairygui_text.parse_package(self.saved_bytes())
            self.assertEqual(saved.values(), values)
            self.assertEqual(len(saved.strings), len(fairygui_text.parse_package(original).strings) + 1)

    def test_removal_restores_exact_bytes_and_preserves_other_edits(self):
        for original in (b"hello", b"a\r\nb\nc\r", make_package()):
            with self.subTest(original=original[:8]):
                self.prepare(original)
                if original.startswith(b"FGUI"):
                    values = json.loads(fairygui_text.read_text(original))
                    self.edit(json.dumps({key: "changed" for key in values}))
                else:
                    self.edit('{"message":"changed"}')
                maker.restore_bundle_text(self.draft, self.original, "Caption")
                self.assertEqual(self.saved_bytes(), original)
                self.assertEqual(self.saved_bytes("Other"), b"keep this edit")


if __name__ == "__main__":
    unittest.main()
