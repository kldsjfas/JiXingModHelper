"""在资源副本中读取、导出和替换已确认布局的内嵌 CRI 表情视频。

本模块不转码 GIF，也不把压缩视频数据当成已解码的动画预览。
USM 与 UTF 字段依据公开格式读取，所有写入均须回读通过后原子替换。
"""
from __future__ import annotations

import copy
import hashlib
import math
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

import UnityPy

MAX_USM_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_CHUNKS = 20_000
MAX_FRAMES = 7_200
MAX_DURATION = 120
_VIDEO_TAGS = {b"@SFV", b"@ALP"}
_END_MARKERS = {
    b"#HEADER END     ===============\0": "header",
    b"#METADATA END   ===============\0": "metadata",
    b"#CONTENTS END   ===============\0": "contents",
}
_UTF_FORMATS = {0: ">B", 1: ">b", 2: ">H", 3: ">h", 4: ">I", 5: ">i",
                6: ">Q", 7: ">q", 8: ">f", 9: ">d", 10: ">I", 11: ">II"}


def _read_file(path: str | Path, limit: int, label: str) -> bytes:
    source = Path(path)
    if not source.is_file() or not 0 < source.stat().st_size <= limit:
        raise ValueError(f"请选择不超过 {limit // (1024 * 1024)} MB 的{label}文件。")
    with source.open("rb") as stream:
        raw = stream.read(limit + 1)
    if not raw or len(raw) > limit:
        raise ValueError(f"{label}文件为空或超过大小限制。")
    return raw


def _utf_table(raw: bytes | memoryview) -> tuple[str, list[dict]]:
    """有界读取 UTF 表；不接受截断字段、重复列或越界字符串/数据引用。"""
    data = bytes(raw)
    if len(data) < 32 or data[:4] != b"@UTF":
        raise ValueError("USM 元信息不是完整的 UTF 表。")
    size, rows_at, strings_at, bytes_at, name_at, columns, row_size, rows = struct.unpack_from(
        ">IIIIIHHI", data, 4)
    end = size + 8
    rows_at, strings_at, bytes_at = rows_at + 8, strings_at + 8, bytes_at + 8
    if (end != len(data) or not 32 <= rows_at <= strings_at <= bytes_at <= end
            or not 1 <= columns <= 128 or not 1 <= rows <= 4096
            or rows_at + row_size * rows > strings_at):
        raise ValueError("USM UTF 表长度或分段范围无效。")

    def string(offset, *, descriptor=True):
        begin = strings_at + offset
        if not strings_at <= begin < bytes_at:
            raise ValueError("USM UTF 字符串引用越界。")
        finish = data.find(b"\0", begin, bytes_at)
        if finish < 0:
            raise ValueError("USM UTF 字符串没有结束符。")
        if not descriptor:
            # CRI tools may keep local-codepage source filenames. They are
            # descriptive only; table/column names still require strict UTF-8.
            return data[begin:finish].decode("utf-8", errors="replace")
        try:
            return data[begin:finish].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("USM UTF 字符串编码无效。") from exc

    def value(kind, position, boundary):
        fmt = _UTF_FORMATS.get(kind)
        if fmt is None or position + struct.calcsize(fmt) > boundary:
            raise ValueError("USM UTF 字段类型未知或数据截断。")
        values = struct.unpack_from(fmt, data, position)
        result = values[0]
        if kind == 10:
            result = string(result, descriptor=False)
        elif kind == 11:
            start, length = values
            if start > end - bytes_at or length > end - bytes_at - start:
                raise ValueError("USM UTF 字节数据引用越界。")
            result = data[bytes_at + start:bytes_at + start + length]
        elif isinstance(result, float) and not math.isfinite(result):
            raise ValueError("USM UTF 数值无效。")
        return result, struct.calcsize(fmt)

    schema, position, names = [], 32, set()
    for _ in range(columns):
        if position + 5 > rows_at:
            raise ValueError("USM UTF 列定义截断。")
        flags, column_at = struct.unpack_from(">BI", data, position)
        position += 5
        name, kind, storage = string(column_at), flags & 15, flags & 0xE0
        if not name or name in names or flags & 0x10 == 0 or kind not in _UTF_FORMATS:
            raise ValueError("USM UTF 列名重复或类型无效。")
        names.add(name)
        constant = None
        if storage == 0x20:
            constant, length = value(kind, position, rows_at)
            position += length
        elif storage not in (0, 0x40):
            raise ValueError("USM UTF 字段存储方式暂不支持。")
        schema.append((name, kind, storage, constant))
    if position != rows_at:
        raise ValueError("USM UTF 列定义与数据起点不一致。")
    result = []
    for index in range(rows):
        position, boundary = rows_at + index * row_size, rows_at + (index + 1) * row_size
        row = {}
        for name, kind, storage, constant in schema:
            if storage == 0x40:
                row[name], length = value(kind, position, boundary)
                position += length
            else:
                row[name] = constant if storage == 0x20 else ("" if kind == 10 else 0)
        if position != boundary:
            raise ValueError("USM UTF 行长度与字段定义不一致。")
        result.append(row)
    return string(name_at), result


@dataclass(frozen=True)
class _Chunk:
    offset: int
    tag: bytes
    channel: int
    kind: int
    time: int
    rate: int
    payload: memoryview


def _chunks(raw: bytes):
    if not 32 <= len(raw) <= MAX_USM_BYTES or raw[:4] != b"CRID":
        raise ValueError("请选择完整的 CRI USM 文件；GIF、APNG、WebP 和普通视频不能直接导入。")
    position, count = 0, 0
    while position < len(raw):
        if len(raw) - position < 32:
            raise ValueError("USM 块头截断，文件未通过完整性检查。")
        tag = raw[position:position + 4]
        size = struct.unpack_from(">I", raw, position + 4)[0]
        payload_at, padding = raw[position + 9], struct.unpack_from(">H", raw, position + 10)[0]
        flags = raw[position + 15]
        end = position + 8 + size
        if (tag not in _VIDEO_TAGS | {b"CRID"} or size < 24 or end > len(raw)
                or payload_at != 24 or padding > size - payload_at
                or flags not in (0, 1, 2, 3) or raw[position + 8] != 0
                or any(raw[position + 13:position + 15]) or any(raw[position + 24:position + 32])
                or (padding and any(raw[end - padding:end]))):
            raise ValueError("USM 块类型、长度或块头布局暂不兼容。")
        count += 1
        if count > MAX_CHUNKS:
            raise ValueError("USM 块数量超过工作台范围。")
        time, rate = struct.unpack_from(">II", raw, position + 16)
        yield _Chunk(position, tag, raw[position + 12], flags, time, rate,
                     memoryview(raw)[position + 32:end - padding])
        position = end


def _positive(row: dict, field: str, maximum: int) -> int:
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= maximum:
        raise ValueError(f"USM 视频字段 {field} 无效或超出范围。")
    return value


def inspect_usm_bytes(raw: bytes) -> dict:
    """验证容器和已确认的 MPEG 双流布局；没有视频解码器参与。"""
    headers, streams, metadata, stages, directory = {}, {}, {}, {}, None
    for chunk in _chunks(raw):
        key = (chunk.tag, chunk.channel)
        if chunk.tag == b"CRID":
            if directory is not None or chunk.offset != 0 or chunk.kind != 1:
                raise ValueError("USM 必须以唯一的 CRID 目录块开头。")
            table, directory = _utf_table(chunk.payload)
            if table != "CRIUSF_DIR_STREAM":
                raise ValueError("USM 目录表类型不正确。")
            continue
        stage = stages.get(key, "new")
        if chunk.kind == 1:
            if stage != "new":
                raise ValueError("USM 视频头重复或顺序错误。")
            table, rows = _utf_table(chunk.payload)
            if table != "VIDEO_HDRINFO" or len(rows) != 1:
                raise ValueError("USM 视频头不是唯一的 VIDEO_HDRINFO。")
            headers[key], streams[key], metadata[key] = rows[0], [], []
            stages[key] = "header"
        elif chunk.kind == 2:
            marker = _END_MARKERS.get(bytes(chunk.payload))
            expected = {"header": "header", "metadata": "metadata", "contents": "stream"}.get(marker)
            if stage != expected:
                raise ValueError("USM 分段结束标记缺失或顺序错误。")
            stages[key] = {"header": "header_end", "metadata": "ready", "contents": "done"}[marker]
        elif chunk.kind == 3:
            if stage not in ("header_end", "metadata"):
                raise ValueError("USM 索引出现于错误的分段。")
            table, rows = _utf_table(chunk.payload)
            if table != "VIDEO_SEEKINFO":
                raise ValueError("USM 视频索引类型暂不支持。")
            metadata[key].extend(rows)
            stages[key] = "metadata"
        else:
            if stage not in ("ready", "stream") or not chunk.payload:
                raise ValueError("USM 视频流缺少元信息、结束标记或有效数据。")
            if (streams[key] and chunk.time <= streams[key][-1].time) or (not streams[key] and chunk.time != 0):
                raise ValueError("USM 视频块时间没有按顺序排列。")
            streams[key].append(chunk)
            stages[key] = "stream"
    if directory is None or not headers or any(stage != "done" for stage in stages.values()):
        raise ValueError("USM 视频流不完整，缺少内容结束标记。")
    if set(headers) != {(b"@SFV", 0), (b"@ALP", 0)}:
        raise ValueError("目前仅支持已确认的一个颜色流和一个透明度流；普通 RGB 视频不能替代透明表情。")
    directory_keys, container_rows = [], []
    for row in directory:
        channel, stream_id = row.get("chno"), row.get("stmid")
        if channel in (-1, 65535, 4294967295):
            container_rows.append(row)
            continue
        if not isinstance(stream_id, int) or not 0 <= stream_id <= 0xFFFFFFFF or not isinstance(channel, int):
            raise ValueError("USM 目录缺少流编号。")
        directory_keys.append((stream_id.to_bytes(4, "big"), channel))
    if len(directory_keys) != len(set(directory_keys)) or set(directory_keys) != set(headers):
        raise ValueError("USM 目录与颜色/透明度流不一致。")
    if len(container_rows) != 1 or container_rows[0].get("filesize") != len(raw):
        raise ValueError("USM 目录文件长度与实际文件不一致。")
    profiles = []
    for key in ((b"@SFV", 0), (b"@ALP", 0)):
        header = headers[key]
        width, height = _positive(header, "width", 4096), _positive(header, "height", 4096)
        numerator = _positive(header, "framerate_n", 120_000)
        denominator = _positive(header, "framerate_d", 120_000)
        frames = _positive(header, "total_frames", MAX_FRAMES)
        rate = numerator / denominator
        if not 1 <= rate <= 60 or frames / rate > MAX_DURATION or width * height > 4_194_304:
            raise ValueError("USM 视频尺寸、帧率或时长超过表情工作台范围。")
        # The header's codec ID identifies the MPEG family. The elementary
        # stream, rather than that ID, distinguishes MPEG-1 from MPEG-2.
        if header.get("mpeg_codec") != 1:
            raise ValueError("此 USM 编码暂未验证；目前仅接收原生 MPEG 颜色与透明度双流。")
        if header.get("alpha_type") != (0 if key[0] == b"@SFV" else 1):
            raise ValueError("USM 透明度布局暂未验证。")
        if any(header.get(field) != expected for field, expected in (
                ("mat_width", width), ("mat_height", height), ("disp_width", width), ("disp_height", height))):
            raise ValueError("USM 视频裁切或材质尺寸布局暂未验证。")
        first = bytes(streams[key][0].payload)
        sequence = first.find(b"\0\0\1\xb3")
        if sequence < 0 or sequence + 7 > len(first):
            raise ValueError("USM 视频不是可确认的未加密 MPEG 序列。")
        geometry = int.from_bytes(first[sequence + 4:sequence + 7], "big")
        if (geometry >> 12, geometry & 4095) != (width, height):
            raise ValueError("USM 视频序列尺寸与视频头不一致。")
        if len(streams[key]) != frames or any(bytes(part.payload).count(b"\0\0\1\0") != 1 for part in streams[key]):
            raise ValueError("USM MPEG 帧数据与声明帧数不一致；此分包布局暂不支持。")
        codec = "MPEG-1"
        for part in streams[key]:
            payload = bytes(part.payload)
            extension = payload.find(b"\0\0\1\xb5")
            while extension >= 0:
                if extension + 4 < len(payload) and payload[extension + 4] >> 4 == 1:
                    codec = "MPEG-2"
                    break
                extension = payload.find(b"\0\0\1\xb5", extension + 4)
            if codec == "MPEG-2":
                break
        if any(part.rate != round(rate * 100) for part in streams[key]):
            raise ValueError("USM 视频块帧率与视频头不一致。")
        offsets = {part.offset: index for index, part in enumerate(streams[key])}
        previous_index = -1
        for row in metadata[key]:
            if row.get("ofs_byte") not in offsets:
                raise ValueError("USM 视频索引没有指向当前流的完整数据块。")
            frame_index = row.get("ofs_frmid")
            if (not isinstance(frame_index, int) or not previous_index < frame_index < frames
                    or offsets[row["ofs_byte"]] != frame_index):
                raise ValueError("USM 视频索引帧号无效。")
            previous_index = frame_index
        if not metadata[key]:
            raise ValueError("USM 缺少已确认的视频索引。")
        profiles.append({"stream": key[0].decode("ascii"), "channel": key[1],
                         "width": width, "height": height, "frames": frames,
                         "fps_n": numerator, "fps_d": denominator, "fps": rate,
                         "codec": codec, "codec_id": header["mpeg_codec"],
                         "alpha_type": header["alpha_type"], "stream_chunks": len(streams[key]),
                         "mat_width": header.get("mat_width"), "mat_height": header.get("mat_height"),
                         "disp_width": header.get("disp_width"), "disp_height": header.get("disp_height")})
    color, alpha = profiles
    if any(color[field] != alpha[field] for field in ("width", "height", "frames", "fps_n", "fps_d", "codec")):
        raise ValueError("USM 颜色流与透明度流尺寸、帧数或时序不一致。")
    if [(part.time, part.rate) for part in streams[(b"@SFV", 0)]] != [
            (part.time, part.rate) for part in streams[(b"@ALP", 0)]]:
        raise ValueError("USM 颜色流与透明度流的块时间不一致。")
    return {"format": "usm", "codec": color["codec"], "width": color["width"], "height": color["height"],
            "frames": color["frames"], "fps": color["fps"], "duration_ms": round(color["frames"] / color["fps"] * 1000),
            "video_channels": 1, "alpha_channels": 1, "has_alpha": True, "streams": profiles,
            "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "preview_available": False,
            "notice": "USM 结构和双流布局已校验；播放预览还需实际解码，游戏内效果仍需测试。"}


def _load_bundle(path):
    source = Path(path)
    return UnityPy.load(_read_file(source, MAX_BUNDLE_BYTES, "视频资源包"), path=str(source.parent))


def _embedded_object(env, asset_name):
    matches = [obj for obj in env.objects if obj.type.name == "MonoBehaviour" and obj.peek_name() == asset_name]
    if len(matches) != 1:
        raise ValueError(f"CRI 表情视频不存在或名称不唯一：{asset_name}")
    obj = matches[0]
    tree = obj.read_typetree()
    script = tree.get("m_Script", {})
    implementation = tree.get("implementation", {})
    references = tree.get("references", {})
    if (tree.get("m_Name") != asset_name or not isinstance(script, dict) or not script.get("m_PathID")
            or not isinstance(tree.get("movieInfo"), dict) or not isinstance(tree.get("assetInfo"), dict)
            or not isinstance(implementation, dict) or not isinstance(references, dict)
            or references.get("version") != 2):
        raise ValueError("CRI 表情视频序列化结构暂不支持。")
    rows = references.get("RefIds", [])
    if not isinstance(rows, list) or not isinstance(implementation.get("rid"), int):
        raise ValueError("CRI 表情视频缺少内嵌实现引用。")
    selected = [row for row in rows if isinstance(row, dict) and row.get("rid") == implementation["rid"]]
    if len(selected) != 1 or selected[0].get("type") != {
        "class": "CriSerializedBytesAssetImpl", "ns": "CriWare.Assets", "asm": "CriMw.CriWare.Assets.Runtime"
    }:
        raise ValueError("此视频不是已确认的内嵌 CRI 字节资源；外部视频暂不支持替换。")
    holder = selected[0].get("data")
    values = holder.get("data") if isinstance(holder, dict) else None
    if (not isinstance(values, (list, bytes, bytearray)) or len(values) > MAX_USM_BYTES
            or not values or any(not isinstance(value, int) or not 0 <= value <= 255 for value in values)):
        raise ValueError("CRI 内嵌视频字节字段损坏或超过 64 MB。")
    return obj, tree, holder, bytes(values)


def inspect_cri_emote_bundle(path: str | Path, asset_name: str) -> dict:
    obj, _, _, raw = _embedded_object(_load_bundle(path), asset_name)
    return {**inspect_usm_bytes(raw), "asset_name": asset_name, "path_id": obj.path_id, "embedded": True}


def _compatible(candidate, reference):
    fields = ("stream", "channel", "width", "height", "codec", "codec_id", "alpha_type",
              "mat_width", "mat_height", "disp_width", "disp_height", "fps_n", "fps_d")
    for imported, original in zip(candidate["streams"], reference["streams"]):
        if any(imported.get(field) != original.get(field) for field in fields):
            raise ValueError("导入 USM 必须与原视频保持相同编码、尺寸、帧率及颜色/透明度布局；请使用同源导出文件。")


def _check_movie_cache(tree, replacement, original):
    if replacement != original and any(value != 0 for value in tree["movieInfo"].values()):
        raise ValueError("此视频保存了额外影片缓存元信息，目前只能原样导入同源 USM，避免留下不一致的播放数据。")


def validate_usm_file(path: str | Path, reference_bundle: str | Path, asset_name: str) -> dict:
    replacement = _read_file(path, MAX_USM_BYTES, "USM 视频")
    candidate = inspect_usm_bytes(replacement)
    _, tree, _, original = _embedded_object(_load_bundle(reference_bundle), asset_name)
    reference = inspect_usm_bytes(original)
    _compatible(candidate, reference)
    _check_movie_cache(tree, replacement, original)
    return {**candidate, "asset_name": asset_name, "compatible": True,
            "same_source_bytes": candidate["sha256"] == reference["sha256"]}


def _write_atomic(out, payload):
    destination = Path(out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".cri-emote-", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def export_cri_emote_bundle(path: str | Path, asset_name: str, out: str | Path) -> Path:
    _, _, _, raw = _embedded_object(_load_bundle(path), asset_name)
    inspect_usm_bytes(raw)
    destination = Path(out)
    if destination.suffix.lower() != ".usm":
        destination = destination.with_suffix(".usm")
    _write_atomic(destination, raw)
    return destination


def replace_cri_emote_bundle(source: str | Path, asset_name: str, replacement_path: str | Path,
                            out: str | Path) -> dict:
    """只替换 implementation.rid 所指字节字段，其余对象与字段保持原样。"""
    replacement = _read_file(replacement_path, MAX_USM_BYTES, "USM 视频")
    candidate = inspect_usm_bytes(replacement)
    env = _load_bundle(source)
    obj, tree, holder, original = _embedded_object(env, asset_name)
    _compatible(candidate, inspect_usm_bytes(original))
    _check_movie_cache(tree, replacement, original)
    expected = copy.deepcopy(tree)
    original_objects = {(reader.assets_file.name, reader.path_id): reader.get_raw_data()
                 for reader in env.objects if reader is not obj}
    holder["data"] = list(replacement)
    obj.save_typetree(tree)
    payload = env.file.save()
    written = UnityPy.load(payload, path=str(Path(source).parent))
    saved, saved_tree, saved_holder, saved_bytes = _embedded_object(written, asset_name)
    if saved.path_id != obj.path_id or saved_bytes != replacement:
        raise ValueError("CRI 视频写回字节或对象编号校验失败，作品集未修改。")
    saved_holder["data"] = list(original)
    if saved_tree != expected:
        raise ValueError("CRI 视频写回影响了其他字段，作品集未修改。")
    # Object identity changes when reloading; path IDs remain local to a file.
    saved_objects = {(reader.assets_file.name, reader.path_id): reader.get_raw_data()
                     for reader in written.objects if reader is not saved}
    if original_objects != saved_objects:
        raise ValueError("CRI 视频写回影响了其他对象，作品集未修改。")
    inspect_usm_bytes(saved_bytes)
    _write_atomic(out, payload)
    return {**candidate, "asset_name": asset_name, "path_id": saved.path_id,
            "embedded": True, "compatible": True}
