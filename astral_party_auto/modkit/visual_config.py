"""只编辑 Card/Skin 的静态图片地址，同步列表与字典镜像。"""
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
MAX_RECORDS = 10_000
_RESOURCE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,255}\Z")
_MOVIECLIP_URL = re.compile(r"ui://xuaw6o8jjm[A-Za-z0-9]{1,48}\Z")
_USAGES = {
    "Card": {6: "卡面", 7: "卡面（SFW）"},
    "Skin": {7: "立绘", 8: "立绘（SFW）", 9: "局内立绘", 10: "局内立绘（SFW）"},
}


@dataclass(frozen=True)
class _Field:
    number: int
    wire: int
    value: bytes
    raw: bytes
    tag: bytes


@dataclass(frozen=True)
class _Record:
    record_id: int
    raw: bytes
    addresses: dict[int, str]


def _varint(raw: bytes, position: int) -> tuple[int, int]:
    value = 0
    for index in range(10):
        if position >= len(raw):
            raise ValueError("图片配置的整数被截断。")
        byte = raw[position]
        position += 1
        if index == 9 and byte > 1:
            raise ValueError("图片配置的整数超过 64 位。")
        value |= (byte & 127) << (index * 7)
        if byte < 128:
            return value, position
    raise ValueError("图片配置的整数编码无效。")


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
            raise ValueError("图片配置字段过多。")
        start = position
        tag, position = _varint(raw, position)
        tag_bytes = raw[start:position]
        number, wire = tag >> 3, tag & 7
        if not 1 <= number <= 0x1FFFFFFF or wire not in (0, 1, 2, 5):
            raise ValueError("图片配置的字段编号或类型无效。")
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
            raise ValueError("图片配置字段越界或被截断。")
        result.append(_Field(number, wire, raw[begin:position], raw[start:position], tag_bytes))
    return result


def _known(rows: list[_Field], number: int, wire: int, *, required: bool = False) -> _Field | None:
    matches = [row for row in rows if row.number == number]
    if len(matches) > 1 or (required and not matches):
        raise ValueError("图片配置缺少必要字段或含重复字段。")
    if matches and matches[0].wire != wire:
        raise ValueError("图片配置的已知字段类型不匹配。")
    return matches[0] if matches else None


def _identifier(rows: list[_Field], number: int = 1, *, allow_default: bool = False) -> int:
    row = _known(rows, number, 5, required=not allow_default)
    value = int.from_bytes(row.value, "little") if row else 0
    if value == 0 and not allow_default:
        raise ValueError("卡牌、角色或皮肤编号不能为零。")
    return value


def _table_name(table: str) -> str:
    if not isinstance(table, str) or table not in _USAGES:
        raise ValueError("仅支持 Card 卡面与 Skin 立绘配置。")
    return table


def _address(name: str, *, allow_empty: bool = False) -> str:
    if isinstance(name, str):
        if allow_empty and not name:
            return name
        if _RESOURCE_NAME.fullmatch(name) or _MOVIECLIP_URL.fullmatch(name):
            return name
    raise ValueError("图片地址需为资源名或 Common 包内工具自有 jm 动画地址。")


def _record(raw: bytes, table: str) -> _Record:
    rows = _fields(raw)
    record_id = _identifier(rows, 1 if table == "Card" else 3, allow_default=table == "Skin")
    if table == "Skin":
        _known(rows, 1, 5)  # Index 为 0 时可以省略。
    addresses = {}
    for number in _USAGES[table]:
        row = _known(rows, number, 2)
        if row is None:
            continue
        try:
            name = row.value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("图片地址不是有效 UTF-8。") from exc
        addresses[number] = _address(name, allow_empty=True)
    if table == "Skin" and record_id == 0 and not addresses:
        raise ValueError("默认立绘记录缺少图片字段，无法识别此配置结构。")
    return _Record(record_id, raw, addresses)


def _parent(raw: bytes, table: str) -> tuple[int, dict[int, _Record]]:
    if table == "Card":
        record = _record(raw, table)
        return record.record_id, {record.record_id: record}
    rows = _fields(raw)
    parent_id = _identifier(rows)
    records = {}
    for row in rows:
        if row.number != 2:
            continue
        if row.wire != 2:
            raise ValueError("Skin 立绘列表类型不匹配。")
        record = _record(row.value, table)
        if record.record_id in records:
            raise ValueError("Skin 立绘列表含重复皮肤编号。")
        records[record.record_id] = record
        if len(records) > MAX_RECORDS:
            raise ValueError("图片配置记录数量超出处理范围。")
    if not records:
        raise ValueError("Skin 角色缺少立绘子记录，无法识别此配置结构。")
    return parent_id, records


def _inspect(raw: bytes, table: str):
    table = _table_name(table)
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(f"{table} 图片配置为空或超过 16 MB。")
    rows = _fields(raw)
    parents, mirrors, records = {}, {}, {}
    item_ids = set()
    for row in rows:
        if row.number not in (1, 2):
            continue
        if row.wire != 2:
            raise ValueError(f"{table} 图片集合类型不匹配。")
        if row.number == 1:
            parent_id, children = _parent(row.value, table)
            if parent_id in parents:
                raise ValueError(f"{table} 图片列表含重复记录编号。")
            # 默认/怪物皮肤没有道具编号。0 只能在各自父记录内唯一，
            # 其它非零道具编号仍必须在整张表唯一。
            child_ids = {record_id for record_id in children if record_id}
            if item_ids.intersection(child_ids):
                raise ValueError(f"{table} 图片列表含重复记录编号。")
            item_ids.update(child_ids)
            parents[parent_id] = row.value
            records.update({(parent_id, record_id): record for record_id, record in children.items()})
        else:
            entry = _fields(row.value)
            key = _identifier(entry)
            value = _known(entry, 2, 2, required=True)
            parent_id, _ = _parent(value.value, table)
            if key != parent_id or key in mirrors:
                raise ValueError(f"{table} 字典的记录编号不一致或重复。")
            mirrors[key] = value.value
        if len(parents) > MAX_RECORDS or len(mirrors) > MAX_RECORDS or len(records) > MAX_RECORDS:
            raise ValueError("图片配置记录数量超出处理范围。")
    if not parents or parents != mirrors:
        raise ValueError(f"{table} 图片列表与字典镜像不一致，未修改。")
    return rows, records, len(parents)


def inspect_visual_config(raw: bytes, table: str) -> dict:
    """按精确资源名合并可编辑卡面/立绘；头像及其它配置不进入目标列表。"""
    _, records, parent_count = _inspect(raw, table)
    targets = {}
    for (parent_id, record_id), record in records.items():
        for number, name in record.addresses.items():
            if not name:
                continue
            target = targets.setdefault(name, {"ids": set(), "fields": set(), "parents": set()})
            target["ids"].add(record_id if record_id else f"{parent_id}:0")
            target["parents"].add(parent_id)
            target["fields"].add(number)
    return {"table": table, "parent_count": parent_count, "record_count": len(records),
            "targets": [{"name": name,
                         "usage": " / ".join(_USAGES[table][number] for number in sorted(target["fields"])),
                         "record_ids": sorted(i for i in target["ids"] if isinstance(i, int))
                                       + sorted(i for i in target["ids"] if isinstance(i, str)),
                         "parent_ids": sorted(target["parents"]),
                         "field_numbers": sorted(target["fields"])}
                        for name, target in sorted(targets.items())]}


def _replace_message(row: _Field, value: bytes) -> bytes:
    return row.raw if row.value == value else row.tag + _encode_varint(len(value)) + value


def _replace_record(raw: bytes, table: str, old: bytes, new: bytes) -> bytes:
    return b"".join(_replace_message(row, new)
                    if row.number in _USAGES[table] and row.value == old else row.raw
                    for row in _fields(raw))


def _replace_parent(raw: bytes, table: str, old: bytes, new: bytes) -> bytes:
    if table == "Card":
        return _replace_record(raw, table, old, new)
    return b"".join(_replace_message(row, _replace_record(row.value, table, old, new))
                    if row.number == 2 else row.raw for row in _fields(raw))


def replace_visual_address(raw: bytes, table: str, old_name: str, new_name: str) -> bytes:
    """同步修改精确匹配的所有合法叶字段；保留所有非目标字段的原字节。"""
    old_name, new_name = _address(old_name), _address(new_name)
    rows, records, _ = _inspect(raw, table)
    if not any(old_name in record.addresses.values() for record in records.values()):
        raise ValueError(f"{table} 的卡面/立绘字段中找不到此精确资源名。")
    if old_name == new_name:
        return raw
    old, new = old_name.encode("utf-8"), new_name.encode("utf-8")
    output = []
    for row in rows:
        if row.number == 1:
            body = _replace_parent(row.value, table, old, new)
        elif row.number == 2:
            body = b"".join(_replace_message(field, _replace_parent(field.value, table, old, new))
                            if field.number == 2 else field.raw for field in _fields(row.value))
        else:
            output.append(row.raw)
            continue
        output.append(_replace_message(row, body))
    changed = b"".join(output)
    _, saved_records, _ = _inspect(changed, table)
    for record_id, record in records.items():
        expected = {number: new_name if name == old_name else name for number, name in record.addresses.items()}
        if saved_records[record_id].addresses != expected:
            raise ValueError("图片地址回读失败，未修改。")
    return changed


def _config_asset(env, table: str):
    matches = [obj for obj in env.objects if obj.type.name == "TextAsset" and obj.peek_name() == table]
    if len(matches) != 1:
        raise ValueError(f"资源包中 {table} 资源不存在或名称不唯一。")
    return matches[0]


def _objects(env) -> dict:
    result = {}
    for obj in env.objects:
        key = (obj.assets_file.name, obj.path_id)
        if key in result:
            raise ValueError("资源包包含重复对象标识。")
        result[key] = (obj.type.name, obj.get_raw_data())
    return result


@_release_readers
def replace_visual_config_bundle(source: str | Path, out: str | Path, table: str,
                                 old: str, new: str) -> dict:
    """保存前回读验证；仅允许输出草稿，不覆盖输入资源包。"""
    table = _table_name(table)
    source, out = Path(source), Path(out)
    if source.resolve() == out.resolve() or (source.exists() and out.exists() and os.path.samefile(source, out)):
        raise ValueError("图片配置输出不能覆盖输入资源包。")
    env = _load_bundle(source)
    target = _config_asset(env, table)
    original_objects = _objects(env)
    target_key = (target.assets_file.name, target.path_id)
    data = target.read()
    changed = replace_visual_address(text_asset_bytes(data), table, old, new)
    data.m_Script = changed.decode("utf-8", errors="surrogateescape") if isinstance(data.m_Script, str) else changed
    data.save()
    payload = env.file.save()
    restored = UnityPy.load(payload, path=str(source.parent))
    saved = _config_asset(restored, table)
    saved_objects = _objects(restored)
    if (saved.assets_file.name, saved.path_id) != target_key or original_objects.keys() != saved_objects.keys():
        raise ValueError("保存改变了资源对象标识，未写入草稿。")
    if any(original_objects[key] != saved_objects[key] for key in original_objects if key != target_key):
        raise ValueError("保存改变了其它资源对象，未写入草稿。")
    saved_data = saved.read()
    if text_asset_bytes(saved_data) != changed or saved_data.m_Name != data.m_Name:
        raise ValueError(f"{table} 资源保存后不一致，未写入草稿。")
    inspection = inspect_visual_config(changed, table)
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".visual-config-", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, out)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"asset_name": table, "asset_path_id": saved.path_id, **inspection}
