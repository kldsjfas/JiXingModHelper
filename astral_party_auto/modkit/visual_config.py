"""编辑卡面和立绘的图片地址，保留玩法字段及配置镜像。"""
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
    "Skin": {7: "立绘", 8: "立绘（SFW）", 9: "局内立绘", 10: "局内立绘（SFW）",
             17: "准备界面卡面", 18: "准备界面卡面（SFW）", 19: "细卡面", 20: "细卡面（SFW）"},
    "CardAltArt": {3: "异画卡面", 4: "异画卡面（SFW）", 7: "异画卡面前板"},
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
    kind: str


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
    if not isinstance(table, str) or table not in ("Card", "Skin"):
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
    identifier_field = {"Card": 1, "Skin": 3, "CardAltArt": 2}[table]
    record_id = _identifier(rows, identifier_field, allow_default=table == "Skin")
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
    if table == "CardAltArt":
        for number in (5, 6):
            _known(rows, number, 2)
    return _Record(record_id, raw, addresses, table)


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
            raise ValueError("卡面或立绘子列表类型不匹配。")
        record = _record(row.value, table)
        if record.record_id in records:
            raise ValueError("卡面或立绘子列表含重复编号。")
        records[record.record_id] = record
        if len(records) > MAX_RECORDS:
            raise ValueError("图片配置记录数量超出处理范围。")
    if not records:
        raise ValueError("卡面或立绘缺少子记录，无法识别此配置结构。")
    return parent_id, records


def _inspect(raw: bytes, table: str):
    table = _table_name(table)
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(f"{table} 图片配置为空或超过 16 MB。")
    rows = _fields(raw)
    records, parent_count = {}, 0
    sections = [(1, 2, table)] + ([(7, 8, "CardAltArt")] if table == "Card" else [])
    for list_field, dict_field, kind in sections:
        parents, mirrors, item_ids = {}, {}, set()
        for row in rows:
            if row.number not in (list_field, dict_field):
                continue
            if row.wire != 2:
                raise ValueError(f"{table} 图片集合类型不匹配。")
            if row.number == list_field:
                parent_id, children = _parent(row.value, kind)
                if parent_id in parents:
                    raise ValueError(f"{table} 图片列表含重复记录编号。")
                child_ids = {record_id for record_id in children if record_id}
                if item_ids.intersection(child_ids):
                    raise ValueError(f"{table} 图片列表含重复记录编号。")
                item_ids.update(child_ids)
                parents[parent_id] = row.value
                for record_id, record in children.items():
                    key = (parent_id, record_id) if kind == table else (kind, parent_id, record_id)
                    records[key] = record
            else:
                entry = _fields(row.value)
                key = _identifier(entry)
                value = _known(entry, 2, 2, required=True)
                parent_id, _ = _parent(value.value, kind)
                if key != parent_id or key in mirrors:
                    raise ValueError(f"{table} 字典的记录编号不一致或重复。")
                mirrors[key] = value.value
            if len(parents) > MAX_RECORDS or len(mirrors) > MAX_RECORDS or len(records) > MAX_RECORDS:
                raise ValueError("图片配置记录数量超出处理范围。")
        if parents != mirrors or (kind == table and not parents):
            raise ValueError(f"{table} 图片列表与字典镜像不一致，未修改。")
        parent_count += len(parents)
    return rows, records, parent_count


def inspect_visual_config(raw: bytes, table: str) -> dict:
    """按精确资源名合并可编辑卡面/立绘；头像及其它配置不进入目标列表。"""
    _, records, parent_count = _inspect(raw, table)
    targets = {}
    for key, record in records.items():
        parent_id, record_id = key[-2:]
        for number, name in record.addresses.items():
            if not name:
                continue
            target = targets.setdefault(name, {"ids": set(), "fields": set(), "parents": set(), "usages": set()})
            target["ids"].add(record_id if record_id else f"{parent_id}:0")
            target["parents"].add(parent_id)
            target["fields"].add(number)
            target["usages"].add(_USAGES[record.kind][number])
    return {"table": table, "parent_count": parent_count, "record_count": len(records),
            "targets": [{"name": name,
                         "usage": " / ".join(sorted(target["usages"])),
                         "record_ids": sorted(i for i in target["ids"] if isinstance(i, int))
                                       + sorted(i for i in target["ids"] if isinstance(i, str)),
                         "parent_ids": sorted(target["parents"]),
                         "field_numbers": sorted(target["fields"])}
                        for name, target in sorted(targets.items())]}


def _replace_message(row: _Field, value: bytes) -> bytes:
    return row.raw if row.value == value else row.tag + _encode_varint(len(value)) + value


def _replace_record(raw: bytes, kind: str, old: bytes, new: bytes, original: _Record | None = None) -> bytes:
    rows = _fields(raw)
    changed_fields = {row.number for row in rows if row.number in _USAGES[kind] and row.value == old}
    videos = {5, 6} if kind == "CardAltArt" and changed_fields.intersection((3, 4)) else set()
    original_fields = {row.number: row for row in _fields(original.raw)} if original else {}
    remaining_animation = any(row.number in (3, 4) and row.number not in changed_fields and row.value.startswith(b"ui://")
                              for row in rows)
    if videos and not new.startswith(b"ui://") and not remaining_animation and original is not None:
        if ({row.number for row in rows if row.number in videos} != set(original_fields).intersection(videos)
                or any(original.addresses.get(number, "").encode("utf-8") != new for number in changed_fields)):
            raise ValueError("异画视频的恢复来源与当前动画不一致，未修改。")
    output = []
    for row in rows:
        if row.number in changed_fields:
            output.append(_replace_message(row, new))
        elif row.number in videos:
            if new.startswith(b"ui://"):
                # 普通侧视频字段同时决定 SFW 的播放路径，切成逐帧需停用两侧。
                output.append(_replace_message(row, b""))
            elif remaining_animation:
                if row.value:
                    raise ValueError("异画动画仍在使用，但视频配置已被外部改变。")
                output.append(row.raw)
            elif original is not None:
                saved_video = original_fields.get(row.number)
                if saved_video is None or row.value:
                    raise ValueError("异画视频的恢复来源与当前动画不一致，未修改。")
                output.append(saved_video.raw)
            elif row.value:
                output.append(row.raw)
            else:
                raise ValueError("恢复异画卡面需要原始视频配置，未修改。")
        else:
            output.append(row.raw)
    return b"".join(output)


def _replace_parent(raw: bytes, kind: str, old: bytes, new: bytes, originals: dict) -> bytes:
    parent_id, _ = _parent(raw, kind)
    if kind == "Card":
        return _replace_record(raw, kind, old, new, originals.get((parent_id, parent_id)))
    output = []
    for row in _fields(raw):
        if row.number == 2:
            record = _record(row.value, kind)
            key = (parent_id, record.record_id) if kind == "Skin" else (kind, parent_id, record.record_id)
            output.append(_replace_message(row, _replace_record(row.value, kind, old, new, originals.get(key))))
        else:
            output.append(row.raw)
    return b"".join(output)


def replace_visual_address(raw: bytes, table: str, old_name: str, new_name: str, *, original_raw: bytes | None = None) -> bytes:
    """同步修改精确匹配的所有合法叶字段；保留所有非目标字段的原字节。"""
    old_name, new_name = _address(old_name), _address(new_name)
    rows, records, _ = _inspect(raw, table)
    if not any(old_name in record.addresses.values() for record in records.values()):
        raise ValueError(f"{table} 的卡面/立绘字段中找不到此精确资源名。")
    if old_name == new_name:
        return raw
    old, new = old_name.encode("utf-8"), new_name.encode("utf-8")
    originals = _inspect(original_raw, table)[1] if original_raw is not None else {}
    sections = {1: (table, False), 2: (table, True)}
    if table == "Card":
        sections.update({7: ("CardAltArt", False), 8: ("CardAltArt", True)})
    output = []
    for row in rows:
        section = sections.get(row.number)
        if section is None:
            output.append(row.raw)
            continue
        kind, mirrored = section
        if mirrored:
            body = b"".join(_replace_message(field, _replace_parent(field.value, kind, old, new, originals))
                            if field.number == 2 else field.raw for field in _fields(row.value))
        else:
            body = _replace_parent(row.value, kind, old, new, originals)
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
                                 old: str, new: str, *, restore_from: str | Path | None = None) -> dict:
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
    original_raw = None
    if restore_from is not None:
        original_env = _load_bundle(Path(restore_from))
        original_raw = text_asset_bytes(_config_asset(original_env, table).read())
    changed = replace_visual_address(text_asset_bytes(data), table, old, new, original_raw=original_raw)
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
