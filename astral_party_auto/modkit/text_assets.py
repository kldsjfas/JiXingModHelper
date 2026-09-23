"""Lossless TextAsset decoding and replacement; binary data stays read-only."""
from __future__ import annotations

import codecs
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass

from . import fairygui_text


def script_bytes(raw: bytes | str | bytearray | None) -> bytes:
    if raw is None:
        return b""
    if isinstance(raw, str):
        # UnityPy decodes m_Script with surrogateescape, not surrogatepass.
        return raw.encode("utf-8", errors="surrogateescape")
    return bytes(raw)


@dataclass
class TextDocument:
    text: str
    editable: bool
    encoding: str
    format: str
    reason: str = ""
    bom: bytes = b""

    def info(self) -> dict:
        endings = re.findall(r"\r\n|\r|\n", self.text)
        newline = Counter(endings).most_common(1)[0][0] if endings else ""
        return {
            "text": self.text, "editable": self.editable,
            "encoding": self.encoding + (" BOM" if self.bom else ""),
            "format": self.format, "reason": self.reason,
            "newline": {"\r\n": "CRLF", "\n": "LF", "\r": "CR"}.get(newline, ""),
        }


def _valid_text(text: str) -> bool:
    return not any(
        (ord(char) < 32 and char not in "\r\n\t")
        or 0x7F <= ord(char) < 0xA0 or 0xD800 <= ord(char) <= 0xDFFF
        for char in text
    )


def inspect_text(raw) -> TextDocument:
    try:
        payload = script_bytes(raw)
    except (TypeError, ValueError, UnicodeError):
        return TextDocument("", False, "", "binary", "含无效字符或二进制数据，不能作为普通文字写回。")
    if payload.startswith(b"FGUI"):
        try:
            text = fairygui_text.read_text(payload)
            return TextDocument(text, True, "UTF-8", "fairygui", "界面文字：在右侧逐条修改；字段编号自动保留，资源引用不受影响。")
        except (ValueError, UnicodeError, IndexError, OverflowError) as exc:
            return TextDocument("", False, "", "fairygui", str(exc))
    bom = b""
    candidates = ["utf-8", "gb18030"]
    for marker, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if payload.startswith(marker):
            bom, candidates = marker, [encoding]
            break
    body = payload[len(bom):]
    text = None
    encoding = ""
    for candidate in candidates:
        try:
            decoded = body.decode(candidate)
            if decoded.encode(candidate) == body and _valid_text(decoded):
                text, encoding = decoded, candidate
                break
        except (UnicodeError, ValueError):
            continue
    if text is None:
        return TextDocument("", False, "", "binary", "这是结构化二进制资源；提取出的片段不能直接替换原文件。")
    kind = "text"
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(text)
            kind = "json"
        except (ValueError, RecursionError):
            pass
    elif stripped.startswith("<"):
        try:
            ET.fromstring(text)
            kind = "xml"
        except ET.ParseError:
            pass
    return TextDocument(text, True, encoding, kind, bom=bom)


def replacement_bytes(raw, new_text: str) -> bytes:
    if not isinstance(new_text, str):
        raise ValueError("替换内容必须是文字。")
    document = inspect_text(raw)
    if not document.editable:
        raise ValueError(document.reason)
    if document.format == "fairygui":
        return fairygui_text.replace_text(script_bytes(raw), new_text)
    if not _valid_text(new_text):
        raise ValueError("文字包含 NUL、控制字符或无效 Unicode，已停止写入。")
    if document.format == "json":
        try:
            json.loads(new_text)
        except (ValueError, RecursionError) as exc:
            raise ValueError(f"JSON 格式不正确，未写入：{exc}") from exc
    elif document.format == "xml":
        try:
            ET.fromstring(new_text)
        except ET.ParseError as exc:
            raise ValueError(f"XML 格式不正确，未写入：{exc}") from exc
    # Textareas normalize line endings to LF. Restore the original convention,
    # including mixed endings when the number of lines has not changed.
    old_endings = re.findall(r"\r\n|\r|\n", document.text)
    lines = re.split(r"\r\n|\r|\n", new_text)
    if old_endings:
        default = Counter(old_endings).most_common(1)[0][0]
        endings = old_endings if len(lines) - 1 == len(old_endings) else [default] * (len(lines) - 1)
        new_text = "".join(line + endings[index] for index, line in enumerate(lines[:-1])) + lines[-1]
    try:
        return document.bom + new_text.encode(document.encoding)
    except UnicodeError as exc:
        raise ValueError(f"新文字包含原编码 {document.encoding} 无法保存的字符；原文件未修改。") from exc
