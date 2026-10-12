"""读取和修改 Character 表中的表情视频地址，保留其它配置的原始字节。"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import UnityPy

from .animation import _load_bundle, _release_readers
from .dynamic import text_asset_bytes

MAX_CONFIG_BYTES = 16 * 1024 * 1024
MAX_FIELDS = 100_000
MAX_CHARACTERS = 1_000
MAX_EMOTES = 10_000
_VIDEO_NAME = re.compile(r"VEmoji_[0-9]+(?:_[0-9]+)*\Z")


@dataclass(frozen=True)
class _Field:
    number: int
    wire: int
    value: bytes
    raw: bytes
    tag: bytes


def _varint(raw: bytes, position: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if position >= len(raw):
            raise ValueError("表情配置的整数被截断。")
        byte = raw[position]
        position += 1
        if index == 9 and byte > 1:
            raise ValueError("表情配置的整数超过 64 位。")
        value |= (byte & 127) << (index * 7)
        if byte < 128:
            return value, position
    raise ValueError("表情配置的整数编码无效。")


def _encode_varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _fields(raw: bytes) -> list[_Field]:
    result, position = [], 0
    while position < len(raw):
        if len(result) >= MAX_FIELDS:
            raise ValueError("表情配置字段过多。")
        start = position
        tag, position = _varint(raw, position)
        tag_bytes = raw[start:position]
        number, wire = tag >> 3, tag & 7
        if not 1 <= number <= 0x1FFFFFFF or wire not in (0, 1, 2, 5):
            raise ValueError("表情配置的字段编号或类型无效。")
        if wire == 0:
            begin = position
            _, position = _varint(raw, position)
        elif wire == 2:
            size, position = _varint(raw, position)
            begin = position
            position += size
        else:
            begin = position
            position += 8 if wire == 1 else 4
        if position > len(raw):
            raise ValueError("表情配置字段越界或被截断。")
        result.append(_Field(number, wire, raw[begin:position], raw[start:position], tag_bytes))
    return result


def _known(rows: list[_Field], number: int, wire: int, *, required: bool = False) -> _Field | None:
    matches = [row for row in rows if row.number == number]
    if len(matches) > 1 or (required and not matches):
        raise ValueError("表情配置缺少必要字段或含重复字段。")
    if matches and matches[0].wire != wire:
        raise ValueError("表情配置的已知字段类型不匹配。")
    return matches[0] if matches else None


def _identifier(rows: list[_Field]) -> int:
    row = _known(rows, 1, 5, required=True)
    value = int.from_bytes(row.value, "little")
    if value == 0:
        raise ValueError("角色或表情编号不能为零。")
    return value


def _video_name(value: str) -> str:
    if not isinstance(value, str) or len(value) > 128 or (value and not _VIDEO_NAME.fullmatch(value)):
        raise ValueError("视频地址需为 VEmoji_ 开头的数字名称，且不超过 128 字符。")
    return value


def _emotes(character: bytes) -> tuple[int, dict[int, tuple[bytes, str]]]:
    rows = _fields(character)
    character_id = _identifier(rows)
    result = {}
    for row in rows:
        if row.number != 2:
            continue
        if row.wire != 2:
            raise ValueError("角色表情列表类型不匹配。")
        fields = _fields(row.value)
        emote_id = _identifier(fields)
        if emote_id in result:
            raise ValueError("角色表情列表含重复表情编号。")
        address = _known(fields, 2, 2)
        _known(fields, 3, 0)  # Optional flags are checked, then preserved as bytes.
        try:
            video_name = address.value.decode("utf-8") if address else ""
        except UnicodeDecodeError as exc:
            raise ValueError("表情视频地址不是有效 UTF-8。") from exc
        result[emote_id] = (row.value, _video_name(video_name))
        if len(result) > MAX_EMOTES:
            raise ValueError("角色表情数量超出处理范围。")
    return character_id, result


def _inspect(raw: bytes):
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("Character 配置为空或超过 16 MB。")
    rows = _fields(raw)
    characters, mirrors = {}, {}
    all_ids = set()
    for row in rows:
        if row.number not in (3, 4):
            continue
        if row.wire != 2:
            raise ValueError("Character 表情集合类型不匹配。")
        if row.number == 3:
            character_id, emotes = _emotes(row.value)
            if character_id in characters:
                raise ValueError("Character 列表含重复角色编号。")
            characters[character_id] = emotes
            if all_ids.intersection(emotes):
                raise ValueError("Character 中含重复表情编号。")
            all_ids.update(emotes)
        else:
            entry = _fields(row.value)
            key = _identifier(entry)
            value = _known(entry, 2, 2, required=True)
            character_id, emotes = _emotes(value.value)
            if key != character_id or key in mirrors:
                raise ValueError("Character 字典的角色编号不一致或重复。")
            mirrors[key] = emotes
        if len(characters) > MAX_CHARACTERS or len(mirrors) > MAX_CHARACTERS or len(all_ids) > MAX_EMOTES:
            raise ValueError("Character 表情数量超出处理范围。")
    if not characters or characters != mirrors:
        raise ValueError("Character 表情列表与字典镜像不一致，未修改。")
    return rows, characters


def inspect_character_emotes(raw: bytes) -> list[dict]:
    """返回已校验的角色、表情编号和原生视频地址；静态表情地址为空。"""
    _, characters = _inspect(raw)
    return [{"character_id": character_id, "emote_id": emote_id, "video_name": video_name}
            for character_id, emotes in characters.items()
            for emote_id, (_, video_name) in emotes.items()]


def _replace_message(row: _Field, value: bytes) -> bytes:
    return row.raw if row.value == value else row.tag + _encode_varint(len(value)) + value


def _replace_character(raw: bytes, emote_id: int, video_name: str) -> bytes:
    result = []
    for row in _fields(raw):
        if row.number != 2 or _identifier(_fields(row.value)) != emote_id:
            result.append(row.raw)
            continue
        fields = _fields(row.value)
        address = _known(fields, 2, 2)
        if address and video_name and address.value == video_name.encode("utf-8"):
            result.append(row.raw)
            continue
        replacement = b"" if not video_name else b"\x12" + _encode_varint(len(video_name)) + video_name.encode("utf-8")
        body = b"".join((replacement if item.number == 2 else item.raw) for item in fields)
        if not address:
            body += replacement
        result.append(_replace_message(row, body))
    return b"".join(result)


def replace_character_emote(raw: bytes, emote_id: int, video_name: str) -> bytes:
    """只改指定表情的两份视频地址；空名称移除该地址。"""
    video_name = _video_name(video_name)
    if isinstance(emote_id, bool) or not isinstance(emote_id, int) or not 0 < emote_id <= 0xFFFFFFFF:
        raise ValueError("表情编号无效。")
    rows, characters = _inspect(raw)
    if not any(emote_id in emotes for emotes in characters.values()):
        raise ValueError("Character 配置中找不到此表情。")
    result = []
    for row in rows:
        if row.number == 3:
            result.append(_replace_message(row, _replace_character(row.value, emote_id, video_name)))
        elif row.number == 4:
            entry = []
            for field in _fields(row.value):
                entry.append(_replace_message(field, _replace_character(field.value, emote_id, video_name))
                             if field.number == 2 else field.raw)
            result.append(_replace_message(row, b"".join(entry)))
        else:
            result.append(row.raw)
    changed = b"".join(result)
    records = inspect_character_emotes(changed)
    if next(record["video_name"] for record in records if record["emote_id"] == emote_id) != video_name:
        raise ValueError("表情视频地址回读失败。")
    return changed


def _character_asset(env):
    matches = [obj for obj in env.objects if obj.type.name == "TextAsset" and obj.peek_name() == "Character"]
    if len(matches) != 1:
        raise ValueError("资源包中 Character 资源不存在或名称不唯一。")
    return matches[0]


@_release_readers
def inspect_emote_config_bundle(path: str | Path) -> dict:
    env = _load_bundle(path)
    target = _character_asset(env)
    return {"asset_name": "Character", "asset_path_id": target.path_id,
            "records": inspect_character_emotes(text_asset_bytes(target.read()))}


def _objects(env) -> dict:
    result = {}
    for obj in env.objects:
        key = (obj.assets_file.name, obj.path_id)
        if key in result:
            raise ValueError("资源包包含重复对象标识。")
        result[key] = (obj.type.name, obj.get_raw_data())
    return result


@_release_readers
def replace_emote_config_bundle(source: str | Path, pathout: str | Path,
                                emote_id: int, video_name: str) -> dict:
    """原子写入草稿，写入前验证目标及其它所有 Unity 对象。"""
    source = Path(source)
    env = _load_bundle(source)
    target = _character_asset(env)
    original_objects = _objects(env)
    target_key = (target.assets_file.name, target.path_id)
    data = target.read()
    changed = replace_character_emote(text_asset_bytes(data), emote_id, video_name)
    original_script = data.m_Script
    data.m_Script = changed.decode("utf-8", errors="surrogateescape") if isinstance(original_script, str) else changed
    data.save()
    payload = env.file.save()
    restored = UnityPy.load(payload, path=str(source.parent))
    saved = _character_asset(restored)
    saved_objects = _objects(restored)
    if saved.path_id != target.path_id or original_objects.keys() != saved_objects.keys():
        raise ValueError("保存改变了资源对象标识，未写入草稿。")
    if any(original_objects[key] != saved_objects[key] for key in original_objects if key != target_key):
        raise ValueError("保存改变了其它资源对象，未写入草稿。")
    saved_data = saved.read()
    if text_asset_bytes(saved_data) != changed or saved_data.m_Name != data.m_Name:
        raise ValueError("Character 资源保存后不一致，未写入草稿。")
    records = inspect_character_emotes(changed)
    out = Path(pathout)
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".emote-config-", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, out)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"asset_name": "Character", "asset_path_id": saved.path_id, "records": records}
