"""Append native MovieClips to a known FairyGUI v6 package.

The writer preserves existing item, sprite, dependency and string records. It
does not write a Unity bundle or alter a texture. Atlas pixels must be prepared
and checked by the caller before these package bytes are saved.

The binary layout follows FairyGUI's MIT-licensed UIPackage reader, and was
checked against the game's Common package. See fairygui_text for attribution.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from .fairygui_text import FairyGUIError, Reader


_NULL_STRING = 65533
_EMPTY_STRING = 65534
_OWN_PATH = "/JiXingModHelper/Animations/"
_OWN_ID = re.compile(r"jm[a-zA-Z0-9]{1,48}\Z")
_MAX_ATLAS_SIZE = 4096
_MAX_FRAMES = 512
_MAX_DURATION_MS = 60000


def _u16(value: int) -> bytes:
    return struct.pack(">H", value)


def _u32(value: int) -> bytes:
    return struct.pack(">I", value)


def _i32(value: int) -> bytes:
    return struct.pack(">i", value)


def _signed(reader: Reader) -> int:
    return struct.unpack(">i", reader.take(4))[0]


def _boolean(reader: Reader) -> bool:
    value = reader.u8()
    if value not in (0, 1):
        raise FairyGUIError("FairyGUI 布尔字段不合法。")
    return bool(value)


def _indexed(blocks: list[bytes]) -> bytes:
    offset = 2 + len(blocks) * 4
    offsets = []
    for block in blocks:
        offsets.append(offset if block else 0)
        offset += len(block)
    return bytes((len(blocks), 0)) + b"".join(_u32(value) for value in offsets) + b"".join(blocks)


def _segments(reader: Reader, expected_count: int) -> list[bytes]:
    """Read ordered, nonoverlapping blocks within the reader's own bounds."""
    table_start = reader.pos
    count = reader.u8()
    short = reader.u8()
    if count != expected_count or short not in (0, 1):
        raise FairyGUIError("此 FairyGUI 分段布局暂不支持安全编辑。")
    offsets = [(reader.u16() if short else reader.u32()) for _ in range(count)]
    table_end = reader.pos
    positions = [table_start + offset for offset in offsets if offset]
    if (not positions or positions != sorted(set(positions))
            or positions[0] != table_end or positions[-1] >= reader.end):
        raise FairyGUIError("FairyGUI 分段位置越界或重叠。")
    blocks = []
    for offset in offsets:
        if not offset:
            blocks.append(b"")
            continue
        start = table_start + offset
        if start < table_end or start >= reader.end:
            raise FairyGUIError("FairyGUI 分段位置越界。")
        end = next((position for position in positions if position > start), reader.end)
        blocks.append(reader.data[start:end])
    return blocks


def _finished(reader: Reader) -> None:
    if reader.pos != reader.end:
        raise FairyGUIError("FairyGUI 记录包含未知尾部，已停止编辑。")


def _read_string(reader: Reader, strings: list[str]) -> str | None:
    index = reader.u16()
    if index == _NULL_STRING:
        return None
    if index == _EMPTY_STRING:
        return ""
    if index >= len(strings):
        raise FairyGUIError("FairyGUI 字符串索引越界。")
    return strings[index]


def _read_tail(reader: Reader, strings: list[str], has_branches: bool) -> dict:
    branch = _read_string(reader, strings)
    count = reader.u8()
    branches = [_read_string(reader, strings) for _ in range(count if has_branches else bool(count))]
    high_resolution = [_read_string(reader, strings) for _ in range(reader.u8())]
    _finished(reader)
    return {"branch": branch, "branches": branches, "high_resolution": high_resolution}


def _read_movieclip(data: bytes, strings: list[str]) -> dict:
    blocks = _segments(Reader(data), 2)
    if not blocks[0] or not blocks[1]:
        raise FairyGUIError("FairyGUI 动画帧数据缺失。")
    timing = Reader(blocks[0])
    interval = _signed(timing)
    swing = _boolean(timing)
    repeat_delay = _signed(timing)
    _finished(timing)
    if interval <= 0 or repeat_delay < 0:
        raise FairyGUIError("FairyGUI 动画时长不合法。")
    frames_reader = Reader(blocks[1])
    count = frames_reader.u16()
    if not 1 <= count <= 32767:
        raise FairyGUIError("FairyGUI 动画帧数不合法。")
    frames = []
    for index in range(count):
        frame = frames_reader.sized_record()
        rect = [_signed(frame) for _ in range(4)]
        add_delay = _signed(frame)
        sprite_id = _read_string(frame, strings)
        _finished(frame)
        if add_delay < 0 or not sprite_id or rect[2] < 0 or rect[3] < 0:
            raise FairyGUIError("FairyGUI 动画帧记录不合法。")
        frames.append({"index": index, "rect": rect, "add_delay_ms": add_delay,
                       "duration_ms": interval + add_delay, "sprite_id": sprite_id})
    _finished(frames_reader)
    return {"interval_ms": interval, "swing": swing, "repeat_delay_ms": repeat_delay, "frames": frames}


@dataclass
class _Package:
    header: bytes
    blocks: list[bytes]
    strings: list[str]
    string_records: bytes
    package_id: str
    package_name: str
    items: list[dict]
    sprites: list[dict]
    has_branches: bool


def _parse(raw: bytes) -> _Package:
    if not isinstance(raw, bytes) or len(raw) > 128 * 1024 * 1024:
        raise FairyGUIError("FairyGUI 包不是有效字节数据，或超出编辑大小限制。")
    reader = Reader(raw)
    if reader.take(4) != b"FGUI" or reader.u32() != 6 or reader.u8() != 0:
        raise FairyGUIError("仅支持已验证的未压缩 FairyGUI v6 包。")
    try:
        package_id, package_name = reader.string(), reader.string()
    except UnicodeError as exc:
        raise FairyGUIError("FairyGUI 包名称不是有效 UTF-8。") from exc
    reader.skip(20)
    header = raw[:reader.pos]
    blocks = _segments(reader, 6)
    if not blocks[0] or not blocks[1] or not blocks[4] or blocks[5]:
        raise FairyGUIError("FairyGUI 表缺失或含扩展字符串表，暂不支持编辑。")
    # Keeping the original string records also preserves duplicate strings.
    string_reader = Reader(blocks[4])
    string_count = string_reader.u32()
    if string_count > _NULL_STRING:
        raise FairyGUIError("FairyGUI 字符串表超出安全范围。")
    try:
        strings = [string_reader.string() for _ in range(string_count)]
    except UnicodeError as exc:
        raise FairyGUIError("FairyGUI 字符串不是有效 UTF-8。") from exc
    _finished(string_reader)
    dependencies = Reader(blocks[0])
    dependency_count = dependencies.u16()
    if dependency_count > 32767:
        raise FairyGUIError("FairyGUI 依赖表不合法。")
    for _ in range(dependency_count):
        _read_string(dependencies, strings)
        _read_string(dependencies, strings)
    branch_count = dependencies.u16()
    if branch_count > 32767:
        raise FairyGUIError("FairyGUI 分支表不合法。")
    for _ in range(branch_count):
        _read_string(dependencies, strings)
    _finished(dependencies)

    item_reader = Reader(blocks[1])
    item_count = item_reader.u16()
    if item_count > 32767:
        raise FairyGUIError("FairyGUI 资源数量超出安全范围。")
    items = []
    item_ids = set()
    for _ in range(item_count):
        record = item_reader.sized_record(wide=True)
        record_bytes = record.data[record.start:record.end]
        kind = record.u8()
        item_id, name, path, file = [_read_string(record, strings) for _ in range(4)]
        exported = _boolean(record)
        width, height = _signed(record), _signed(record)
        if not item_id or item_id in item_ids or width < 0 or height < 0:
            raise FairyGUIError("FairyGUI 资源编号重复或尺寸不合法。")
        item_ids.add(item_id)
        item = {"type": kind, "id": item_id, "name": name, "path": path, "file": file,
                "exported": exported, "width": width, "height": height, "_record": record_bytes}
        if kind == 0:
            scale = record.u8()
            if scale not in (0, 1, 2):
                raise FairyGUIError("FairyGUI 图片缩放方式无法识别。")
            if scale == 1:
                record.skip(20)
            item["smoothing"] = _boolean(record)
        elif kind == 1:
            item["smoothing"] = _boolean(record)
            movie_data = record.sized_record(wide=True)
            item.update(_read_movieclip(movie_data.take(movie_data.end - movie_data.pos), strings))
        elif kind in (2, 3):
            if kind == 3:
                record.skip(1)
            record.sized_record(wide=True)
        elif kind in (8, 9):
            record.skip(8)
        elif kind not in (4, 5, 7):
            raise FairyGUIError("FairyGUI 含未知资源类型，已停止编辑。")
        item.update(_read_tail(record, strings, bool(branch_count)))
        items.append(item)
    _finished(item_reader)

    sprites = []
    sprite_ids = set()
    if blocks[2]:
        sprite_reader = Reader(blocks[2])
        count = sprite_reader.u16()
        if count > 32767:
            raise FairyGUIError("FairyGUI 精灵数量超出安全范围。")
        for _ in range(count):
            record = sprite_reader.sized_record()
            record_bytes = record.data[record.start:record.end]
            sprite_id, atlas_id = [_read_string(record, strings) for _ in range(2)]
            rect = [_signed(record) for _ in range(4)]
            rotated, trimmed = _boolean(record), _boolean(record)
            offset = [_signed(record), _signed(record)] if trimmed else [0, 0]
            original_size = [_signed(record), _signed(record)] if trimmed else (rect[2:][::-1] if rotated else rect[2:])
            _finished(record)
            if not sprite_id or sprite_id in sprite_ids or not atlas_id or min(rect) < 0:
                raise FairyGUIError("FairyGUI 精灵编号或矩形不合法。")
            sprite_ids.add(sprite_id)
            sprites.append({"id": sprite_id, "atlas_id": atlas_id, "rect": rect, "rotated": rotated,
                            "trimmed": trimmed, "offset": offset, "original_size": original_size, "_record": record_bytes})
        _finished(sprite_reader)
    atlases = {item["id"]: item for item in items if item["type"] == 4}
    sprite_map = {sprite["id"]: sprite for sprite in sprites}
    for sprite in sprites:
        atlas = atlases.get(sprite["atlas_id"])
        x, y, width, height = sprite["rect"]
        if atlas is None or x + width > atlas["width"] or y + height > atlas["height"]:
            raise FairyGUIError("FairyGUI 精灵的图集引用或范围不合法。")
    for item in items:
        if item["type"] == 1:
            for frame in item["frames"]:
                if frame["sprite_id"] not in sprite_map:
                    raise FairyGUIError("FairyGUI 动画引用了不存在的精灵。")
    return _Package(header, blocks, strings, blocks[4][4:], package_id, package_name, items, sprites, bool(branch_count))


def _public(record: dict) -> dict:
    return {key: value for key, value in record.items() if not key.startswith("_")}


def inspect_movieclip_package(raw: bytes) -> dict:
    """Return exact item, atlas, sprite and MovieClip associations."""
    package = _parse(raw)
    return {"package_id": package.package_id, "package_name": package.package_name, "version": 6,
            "items": [_public(item) for item in package.items],
            "atlases": [_public(item) for item in package.items if item["type"] == 4],
            "movieclips": [_public(item) for item in package.items if item["type"] == 1],
            "sprites": [_public(sprite) for sprite in package.sprites]}


def _integer(value, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise FairyGUIError(f"{label}必须为 {minimum}–{maximum} 范围内的整数。")
    return value


def _write(package: _Package, items: list[bytes], sprites: list[bytes], new_strings: list[str] | None = None) -> bytes:
    new_strings = new_strings or []
    if len(items) > 32767 or len(sprites) > 32767 or len(package.strings) + len(new_strings) > _NULL_STRING:
        raise FairyGUIError("FairyGUI 表已满，不能再追加动画。")
    blocks = package.blocks.copy()
    blocks[1] = _u16(len(items)) + b"".join(_u32(len(item)) + item for item in items)
    blocks[2] = _u16(len(sprites)) + b"".join(_u16(len(sprite)) + sprite for sprite in sprites)
    additions = []
    for value in new_strings:
        encoded = value.encode("utf-8")
        if len(encoded) > 65535:
            raise FairyGUIError("FairyGUI 字符串过长。")
        additions.append(_u16(len(encoded)) + encoded)
    blocks[4] = _u32(len(package.strings) + len(new_strings)) + package.string_records + b"".join(additions)
    return package.header + _indexed(blocks)


def append_movieclip(raw: bytes, *, item_id: str, atlas_id: str, width: int, height: int,
                     frames: list[dict], atlas_width: int, atlas_height: int) -> bytes:
    """Add a tool-owned MovieClip and extend an existing atlas's dimensions.

    ``atlas_rect`` locates the full, untrimmed frame pixels in the atlas. ``rect``
    is the frame's placement on the MovieClip canvas, defaulting to the whole
    canvas. Its width/height must match the pixel rectangle: FairyGUI uses this
    field for trimming, not arbitrary frame scaling. A GLoader can scale the
    resulting MovieClip to its target UI size.
    """
    package = _parse(raw)
    if not isinstance(item_id, str) or not _OWN_ID.fullmatch(item_id):
        raise FairyGUIError("新增动画必须使用工具自有的 jm 资源编号。")
    if any(item["id"] == item_id or item["name"] == item_id for item in package.items):
        raise FairyGUIError("动画资源编号已存在，请先移除该工具动画。")
    width = _integer(width, 1, _MAX_ATLAS_SIZE, "动画宽度")
    height = _integer(height, 1, _MAX_ATLAS_SIZE, "动画高度")
    atlas_width = _integer(atlas_width, 1, _MAX_ATLAS_SIZE, "图集宽度")
    atlas_height = _integer(atlas_height, 1, _MAX_ATLAS_SIZE, "图集高度")
    atlas = next((item for item in package.items if item["type"] == 4 and item["id"] == atlas_id), None)
    if atlas is None:
        raise FairyGUIError("找不到动画要使用的既有图集。")
    if atlas_width < atlas["width"] or atlas_height < atlas["height"]:
        raise FairyGUIError("动画写入不能缩小既有图集。")
    if not isinstance(frames, list) or not 1 <= len(frames) <= _MAX_FRAMES:
        raise FairyGUIError(f"动画必须包含 1–{_MAX_FRAMES} 帧。")
    sprite_ids = {sprite["id"] for sprite in package.sprites}
    clean_frames = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise FairyGUIError("动画帧必须是结构化数据。")
        rect = frame.get("rect", [0, 0, width, height])
        atlas_rect = frame.get("atlas_rect", rect)
        for value in (rect, atlas_rect):
            if not isinstance(value, (list, tuple)) or len(value) != 4:
                raise FairyGUIError("动画帧矩形必须包含 x、y、宽、高四个整数。")
            for coordinate in value:
                _integer(coordinate, 0, _MAX_ATLAS_SIZE, "动画帧矩形")
        x, y, frame_width, frame_height = rect
        ax, ay, pixel_width, pixel_height = atlas_rect
        if (not frame_width or not frame_height or x + frame_width > width or y + frame_height > height
                or ax + pixel_width > atlas_width or ay + pixel_height > atlas_height
                or (frame_width, frame_height) != (pixel_width, pixel_height)):
            raise FairyGUIError("动画帧超出画布或图集范围；帧矩形尺寸必须与像素尺寸相同。")
        duration = _integer(frame.get("duration_ms"), 1, _MAX_DURATION_MS, "动画帧时长")
        sprite_id = f"{item_id}_{index}"
        if sprite_id in sprite_ids:
            raise FairyGUIError("新增动画的精灵编号已存在。")
        clean_frames.append({"rect": list(rect), "atlas_rect": list(atlas_rect), "duration_ms": duration, "sprite_id": sprite_id})

    additions = []
    indexes = {value: index for index, value in enumerate(package.strings)}

    def reference(value: str | None) -> bytes:
        if value is None:
            return _u16(_NULL_STRING)
        if value not in indexes:
            index = len(package.strings) + len(additions)
            if index >= _NULL_STRING:
                raise FairyGUIError("FairyGUI 字符串表已满。")
            indexes[value] = index
            additions.append(value)
        return _u16(indexes[value])

    interval = min(frame["duration_ms"] for frame in clean_frames)
    movie_frames = []
    new_sprites = []
    for frame in clean_frames:
        body = b"".join(_i32(value) for value in frame["rect"]) + _i32(frame["duration_ms"] - interval) + reference(frame["sprite_id"])
        movie_frames.append(_u16(len(body)) + body)
        sprite = reference(frame["sprite_id"]) + reference(atlas_id) + b"".join(_i32(value) for value in frame["atlas_rect"]) + b"\x00\x00"
        new_sprites.append(sprite)
    movie_data = _indexed([_i32(interval) + b"\x00" + _i32(0), _u16(len(clean_frames)) + b"".join(movie_frames)])
    tail = _u16(_NULL_STRING) + b"\x00\x00"
    movie_item = (b"\x01" + reference(item_id) + reference(item_id) + reference(_OWN_PATH) + reference(None)
                  + b"\x01" + _i32(width) + _i32(height) + b"\x01" + _u32(len(movie_data)) + movie_data + tail)
    items = []
    for item in package.items:
        record = item["_record"]
        if item is atlas:
            record = record[:10] + _i32(atlas_width) + _i32(atlas_height) + record[18:]
        items.append(record)
    written = _write(package, items + [movie_item], [sprite["_record"] for sprite in package.sprites] + new_sprites, additions)
    checked = _parse(written)
    expected_durations = [frame["duration_ms"] for frame in clean_frames]
    movie = next(item for item in checked.items if item["id"] == item_id)
    if [frame["duration_ms"] for frame in movie["frames"]] != expected_durations:
        raise FairyGUIError("动画写回校验失败。")
    return written


def remove_movieclip(raw: bytes, *, item_id: str) -> bytes:
    """Remove only an identifiable clip generated by this writer.

    Atlas dimensions and shared strings stay intact. A missing tool-owned ID is
    an idempotent no-op; official items and shared sprite records are rejected.
    """
    if not isinstance(item_id, str) or not _OWN_ID.fullmatch(item_id):
        raise FairyGUIError("只能移除工具自有的 jm 动画。")
    package = _parse(raw)
    movie = next((item for item in package.items if item["id"] == item_id), None)
    if movie is None:
        return raw
    if (movie["type"] != 1 or movie["name"] != item_id or movie["path"] != _OWN_PATH
            or movie["file"] is not None or not movie["exported"] or not movie["smoothing"]
            or movie["branch"] is not None or movie["branches"] or movie["high_resolution"]
            or movie["swing"] or movie["repeat_delay_ms"]):
        raise FairyGUIError("该资源无法确认为工具生成的动画，已停止移除。")
    owned_ids = {f"{item_id}_{index}" for index in range(len(movie["frames"]))}
    if [frame["sprite_id"] for frame in movie["frames"]] != [f"{item_id}_{index}" for index in range(len(movie["frames"]))]:
        raise FairyGUIError("工具动画的精灵编号不完整，已停止移除。")
    for item in package.items:
        if item is not movie and (item["id"] in owned_ids or any(frame["sprite_id"] in owned_ids for frame in item.get("frames", []))):
            raise FairyGUIError("动画精灵仍被其他资源共享，已停止移除。")
    for sprite in package.sprites:
        if sprite["id"] in owned_ids and (sprite["rotated"] or sprite["trimmed"]):
            raise FairyGUIError("动画精灵已被外部修改，已停止移除。")
    items = [item["_record"] for item in package.items if item is not movie]
    sprites = [sprite["_record"] for sprite in package.sprites if sprite["id"] not in owned_ids]
    written = _write(package, items, sprites)
    checked = _parse(written)
    if any(item["id"] == item_id for item in checked.items) or any(sprite["id"] in owned_ids for sprite in checked.sprites):
        raise FairyGUIError("动画移除回读校验失败。")
    return written
