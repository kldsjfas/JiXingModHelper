"""Read and replace FairyGUI component text without changing resource references.

Only known, uncompressed package versions are accepted. Keys are byte offsets of
actual text fields, not arbitrary entries in the shared string table. Changed
fields receive a new string index so a shared image/controller name stays intact.
The layout follows FairyGUI's UIPackage and TranslationHelper readers.
Reference: https://github.com/fairygui/FairyGUI-unity

FairyGUI reference-reader attribution (MIT):
Copyright (c) 2015 fairygui.com

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass


class FairyGUIError(ValueError):
    pass


class Reader:
    def __init__(self, data: bytes, start: int = 0, end: int | None = None):
        self.data = data
        self.start = start
        self.end = len(data) if end is None else end
        self.pos = start

    def take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > self.end:
            raise FairyGUIError("FairyGUI 数据长度不合法，已停止编辑。")
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def skip(self, size: int) -> None:
        self.take(size)

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def string(self) -> str:
        return self.take(self.u16()).decode("utf-8")

    def block(self, table: int, index: int) -> bool:
        saved = self.pos
        self.pos = table
        count = self.u8()
        short = self.u8()
        if count > 32 or short not in (0, 1):
            raise FairyGUIError("无法识别 FairyGUI 分段表。")
        if index >= count:
            self.pos = saved
            return False
        self.skip(index * (2 if short else 4))
        offset = self.u16() if short else self.u32()
        if not offset:
            self.pos = saved
            return False
        self.pos = table + offset
        if self.pos == self.end:
            self.pos = saved
            return False
        if self.pos < self.start or self.pos > self.end:
            raise FairyGUIError("FairyGUI 分段位置越界。")
        return True

    def sized_record(self, wide: bool = False) -> "Reader":
        size = self.u32() if wide else self.u16()
        start = self.pos
        self.skip(size)
        return Reader(self.data, start, start + size)


@dataclass
class TextPackage:
    raw: bytes
    strings: list[str]
    table_start: int
    table_end: int
    fields: dict[int, int]

    def values(self) -> dict[str, str]:
        return {str(offset): self.strings[index] for offset, index in sorted(self.fields.items())}


def parse_package(raw: bytes) -> TextPackage:
    reader = Reader(raw)
    if reader.take(4) != b"FGUI":
        raise FairyGUIError("不是 FairyGUI 资源。")
    version = reader.u32()
    if version not in range(2, 7) or reader.u8() != 0:
        raise FairyGUIError("此 FairyGUI 版本或压缩方式暂不支持安全文字编辑。")
    reader.string()
    reader.string()
    reader.skip(20)
    package_table = reader.pos
    if not reader.block(package_table, 4):
        raise FairyGUIError("FairyGUI 字符串表缺失。")
    table_start = reader.pos
    count = reader.u32()
    if count > 65533:
        raise FairyGUIError("FairyGUI 字符串表超出安全范围。")
    strings = [reader.string() for _ in range(count)]
    table_end = reader.pos
    # Appending to the final string table preserves every earlier byte offset.
    # Long-string extension tables need a different writer and remain read-only.
    if table_end != len(raw) or reader.block(package_table, 5):
        raise FairyGUIError("含扩展字符串表的 FairyGUI 包暂时只读。")
    fields: dict[int, int] = {}

    def text_field(part: Reader) -> None:
        offset = part.pos
        index = part.u16()
        if index in (65533, 65534):
            return
        if index >= len(strings):
            raise FairyGUIError("FairyGUI 文字索引越界。")
        if offset >= table_start:
            raise FairyGUIError("FairyGUI 文字字段覆盖了字符串表。")
        fields[offset] = index

    def properties(part: Reader) -> None:
        for _ in range(part.u16()):
            part.skip(2)  # target component path
            prop = part.u16()
            if prop == 0:  # ObjectPropID.Text
                text_field(part)
            else:
                part.skip(2)

    def component(part: Reader) -> None:
        if not part.block(part.start, 2):
            return
        for _ in range(part.u16()):
            child = part.sized_record()
            if not child.block(child.start, 0):
                raise FairyGUIError("FairyGUI 组件基础数据缺失。")
            base_type = child.u8()
            kind = base_type
            if base_type == 9 and child.block(child.start, 6):
                kind = child.u8()
            if child.block(child.start, 1):
                text_field(child)  # tooltip
            if child.block(child.start, 2):
                for _ in range(child.u16()):
                    gear = child.sized_record()
                    if gear.u8() != 6:
                        continue
                    gear.skip(2)  # controller index
                    for _ in range(gear.u16()):
                        if gear.u16() != 65534:  # page id
                            text_field(gear)
                    if gear.u8():
                        text_field(gear)
            if base_type == 9 and child.block(child.start, 4):
                child.skip(2)
                child.skip(child.u16() * 4)
                properties(child)
            if kind in (6, 7, 8):
                if child.block(child.start, 6):
                    text_field(child)
                # Block 4 begins with the input prompt for these three types.
                if child.block(child.start, 4):
                    text_field(child)
            elif kind in (10, 17) and child.block(child.start, 8):
                child.skip(2)  # default item URL
                for _ in range(child.u16()):
                    item = child.sized_record()
                    item.skip(4 if kind == 17 else 2)
                    text_field(item)
                    text_field(item)
                    item.skip(6)
                    item.skip(item.u16() * 4)
                    properties(item)
            elif kind in (11, 12, 13) and child.block(child.start, 6):
                if child.u8() != kind:
                    continue
                if kind == 11:  # label title and optional input prompt
                    text_field(child)
                    child.skip(2)
                    if child.u8():
                        child.skip(4)
                    child.skip(4)
                    if child.u8():
                        text_field(child)
                elif kind == 12:  # normal and selected button titles
                    text_field(child)
                    text_field(child)
                else:
                    for _ in range(child.u16()):
                        item = child.sized_record()
                        text_field(item)
                    text_field(child)

    if not reader.block(package_table, 1):
        raise FairyGUIError("FairyGUI 资源表缺失。")
    for _ in range(reader.u16()):
        item = reader.sized_record(wide=True)
        kind = item.u8()
        item.skip(17)  # id/name/path/file, exported, width/height
        if kind == 3:
            item.skip(1)  # component extension
            component(item.sized_record(wide=True))
    return TextPackage(raw, strings, table_start, table_end, fields)


def read_text(raw: bytes) -> str:
    package = parse_package(raw)
    if not package.fields:
        raise FairyGUIError("此 FairyGUI 包里没有可安全识别的界面文字。")
    return json.dumps(package.values(), ensure_ascii=False, indent=2)


def replace_text(raw: bytes, new_text: str) -> bytes:
    package = parse_package(raw)
    try:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise FairyGUIError(f"文字编号重复：{key}")
                result[key] = value
            return result
        values = json.loads(new_text, object_pairs_hook=unique_pairs)
    except json.JSONDecodeError as exc:
        raise FairyGUIError(f"文字 JSON 格式错误（第 {exc.lineno} 行）：{exc.msg}") from exc
    expected = package.values()
    if not isinstance(values, dict) or set(values) != set(expected):
        raise FairyGUIError("请保留全部文字编号，只修改冒号右侧的文字。")
    if not all(isinstance(value, str) for value in values.values()):
        raise FairyGUIError("每条文字都必须是字符串，并放在双引号内。")
    changed = {key: value for key, value in values.items() if value != expected[key]}
    if not changed:
        return raw
    result = bytearray(raw)
    new_strings: dict[str, int] = {}
    tail = bytearray()
    for key, value in changed.items():
        if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise FairyGUIError("文字不能包含 NUL 或无效 Unicode 字符。")
        if value not in new_strings:
            encoded = value.encode("utf-8")
            if len(encoded) > 65535:
                raise FairyGUIError("单条文字太长，UTF-8 编码后不能超过 65535 字节。")
            index = len(package.strings) + len(new_strings)
            if index >= 65533:
                raise FairyGUIError("FairyGUI 字符串表已满。")
            new_strings[value] = index
            tail.extend(struct.pack(">H", len(encoded)) + encoded)
        struct.pack_into(">H", result, int(key), new_strings[value])
    struct.pack_into(">I", result, package.table_start, len(package.strings) + len(new_strings))
    result.extend(tail)
    written = bytes(result)
    if parse_package(written).values() != values:
        raise FairyGUIError("文字回读校验失败，未保存。")
    return written
