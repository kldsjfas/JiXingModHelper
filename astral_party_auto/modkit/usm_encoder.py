"""Encode adapted RGBA frames as the reference's clear MPEG RGB/alpha USM.

Only layout metadata is taken from the reference; every picture is newly encoded.
The container is checked before it leaves this module. CriMana playback still
needs a separate game test.
"""
from __future__ import annotations

import math
import re
import struct
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from . import cri_emote

MAX_FRAME_PIXELS = 64_000_000
_START_CODE = re.compile(b"\x00\x00\x01(.)", re.DOTALL)
_TAGS = (b"@SFV", b"@ALP")


def _table_schema(payload: bytes) -> tuple[str, list[dict], list[tuple[str, int]]]:
    name, rows = cri_emote._utf_table(payload)
    strings_at = struct.unpack_from(">I", payload, 12)[0] + 8
    count = struct.unpack_from(">H", payload, 24)[0]
    position, schema = 32, []
    for _ in range(count):
        flags, offset = struct.unpack_from(">BI", payload, position)
        position += 5
        begin = strings_at + offset
        finish = payload.index(b"\0", begin)
        schema.append((payload[begin:finish].decode("utf-8"), flags & 15))
        if flags & 0xE0 == 0x20:
            position += struct.calcsize(cri_emote._UTF_FORMATS[flags & 15])
    return name, rows, schema


def _utf_table(name: str, rows: list[dict], schema: list[tuple[str, int]]) -> bytes:
    if not rows or len(rows) > 4096:
        raise ValueError("USM 索引数量超过范围。")
    strings, binary, offsets = bytearray(), bytearray(), {}

    def string(value):
        if value not in offsets:
            offsets[value] = len(strings)
            strings.extend(value.encode("utf-8") + b"\0")
        return offsets[value]

    table_name = string(name)
    definitions, values = bytearray(), bytearray()
    for field, kind in schema:
        definitions.extend(struct.pack(">BI", 0x50 | kind, string(field)))
    for row in rows:
        for field, kind in schema:
            value = row[field]
            if kind == 10:
                value = string(value)
            if kind == 11:
                values.extend(struct.pack(">II", len(binary), len(value)))
                binary.extend(value)
            else:
                values.extend(struct.pack(cri_emote._UTF_FORMATS[kind], value))
    row_size = sum(struct.calcsize(cri_emote._UTF_FORMATS[kind]) for _, kind in schema)
    rows_at = 32 + len(definitions)
    strings_at = rows_at + len(values)
    bytes_at = strings_at + len(strings)
    end = bytes_at + len(binary)
    return (b"@UTF" + struct.pack(">IIIIIHHI", end - 8, rows_at - 8, strings_at - 8,
                                 bytes_at - 8, table_name, len(schema), row_size, len(rows))
            + definitions + values + strings + binary)


def _chunk(tag: bytes, kind: int, payload: bytes, *, time=0, rate=30, align=32) -> bytes:
    padding = (-32 - len(payload)) % align
    if padding > 65535:
        raise ValueError("USM 块填充超出范围。")
    return (tag + struct.pack(">I", 24 + len(payload) + padding) + bytes((0, 24))
            + struct.pack(">H", padding) + bytes((0, 0, 0, kind))
            + struct.pack(">II", time, rate) + bytes(8) + payload + bytes(padding))


def _picture_packets(raw: bytes, expected_frames: int) -> list[bytes]:
    codes = [(match.start(), match[1][0]) for match in _START_CODE.finditer(raw)]
    pictures = [offset for offset, code in codes if code == 0]
    if len(pictures) != expected_frames or not pictures:
        raise ValueError("FFmpeg 输出帧数与输入不同，未生成表情视频。")
    boundaries, seen_picture, preamble = [0], False, None
    for offset, code in codes:
        if code in (0xB3, 0xB8) and seen_picture and preamble is None:
            preamble = offset
        if code == 0:
            if seen_picture:
                boundaries.append(preamble if preamble is not None else offset)
            seen_picture, preamble = True, None
    packets = [raw[begin:end] for begin, end in zip(boundaries, boundaries[1:] + [len(raw)])]
    # All-intra encoding makes every packet independently seekable. Reject an
    # unexpected B/P picture instead of emitting an incorrect seek table.
    for packet in packets:
        picture = packet.find(b"\0\0\1\0")
        if picture < 0 or picture + 6 > len(packet) or ((int.from_bytes(packet[picture + 4:picture + 6], "big") >> 3) & 7) != 1:
            raise ValueError("FFmpeg 未输出预期的逐帧关键帧布局。")
    if not packets[-1].endswith(b"\0\0\1\xb7"):
        packets[-1] += b"\0\0\1\xb7"
    return packets


def _reference_layout(reference_raw: bytes) -> dict:
    profile = cri_emote.inspect_usm_bytes(reference_raw)
    chunks = list(cri_emote._chunks(reference_raw))
    directory = _table_schema(bytes(chunks[0].payload))
    headers, indexes, codecs = {}, {}, {}
    for tag in _TAGS:
        header = next(chunk for chunk in chunks if chunk.tag == tag and chunk.kind == 1)
        seek = next(chunk for chunk in chunks if chunk.tag == tag and chunk.kind == 3)
        first = next(chunk for chunk in chunks if chunk.tag == tag and chunk.kind == 0)
        headers[tag] = _table_schema(bytes(header.payload))
        indexes[tag] = _table_schema(bytes(seek.payload))
        # CRI's mpeg_codec=1 also appears in the game's actual MPEG-1 streams.
        # Check the sequence extension rather than relying on that label.
        codes = list(_START_CODE.finditer(bytes(first.payload)))
        codecs[tag] = "mpeg2video" if any(match[1][0] == 0xB5 and
            match.end() < len(first.payload) and first.payload[match.end()] >> 4 == 1 for match in codes) else "mpeg1video"
    if codecs[_TAGS[0]] != codecs[_TAGS[1]]:
        raise ValueError("参考视频两路 MPEG 版本不一致，暂不支持自动转换。")
    return {"profile": profile, "directory": directory, "headers": headers, "indexes": indexes,
            "codec": codecs[_TAGS[0]]}


def _encode_stream(frames: list[Image.Image], folder: Path, ffmpeg: Path,
                   fps_n: int, fps_d: int, codec: str, *, alpha: bool, dc_precision=8) -> list[bytes]:
    stem = "alpha" if alpha else "color"
    source, output = folder / f"{stem}.rgb", folder / f"{stem}.m1v"
    with source.open("wb") as stream:
        for frame in frames:
            if alpha:
                plane = frame.getchannel("A")
                picture = Image.merge("RGB", (plane, plane, plane))
            else:
                picture = frame.convert("RGB")
            stream.write(picture.tobytes())
    width, height = frames[0].size
    command = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{width}x{height}",
               "-framerate", f"{fps_n}/{fps_d}", "-i", str(source), "-an", "-c:v", codec,
               "-pix_fmt", "yuv420p", "-q:v", "1", "-g", "1", "-bf", "0", "-threads", "1"]
    if codec == "mpeg2video":
        command.extend(("-dc", str(dc_precision)))
    command.extend(("-fs", str(cri_emote.MAX_USM_BYTES // 2), "-f", codec, str(output)))
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=120,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise ValueError("表情视频编码超时，请缩短动画或缩小尺寸。") from exc
    except OSError as exc:
        raise ValueError("无法启动 FFmpeg，请检查视频组件。") from exc
    if result.returncode or not output.is_file():
        detail = result.stderr.decode("utf-8", errors="replace")[-1000:]
        raise ValueError(f"FFmpeg 表情编码失败：{detail}")
    if not 0 < output.stat().st_size <= cri_emote.MAX_USM_BYTES // 2:
        raise ValueError("编码视频超过大小限制，请缩短动画。")
    packets = _picture_packets(output.read_bytes(), len(frames))
    # The demuxer's inferred r_frame_rate can report field frequency for a
    # timestamp-free MPEG stream. Check the actual sequence frame-rate code.
    rates = {1: 24000 / 1001, 2: 24, 3: 25, 4: 30000 / 1001,
             5: 30, 6: 50, 7: 60000 / 1001, 8: 60}
    first = packets[0]
    sequence = first.find(b"\0\0\1\xb3")
    if sequence < 0 or sequence + 8 > len(first):
        raise ValueError("FFmpeg 输出缺少 MPEG 序列帧率。")
    encoded_rate = rates.get(first[sequence + 7] & 15)
    if encoded_rate is None or not math.isclose(encoded_rate, fps_n / fps_d, rel_tol=0.00001):
        raise ValueError("FFmpeg 输出 MPEG 帧率与参考视频不同。")
    return packets


def _mux(layout: dict, packets: dict[bytes, list[bytes]]) -> bytes:
    profile = layout["profile"]
    fps = profile["fps"]
    count = len(packets[_TAGS[0]])
    seek_frames = list(range(0, count, 16))
    markers = {value: marker for marker, value in cri_emote._END_MARKERS.items()}

    def build(file_size, positions):
        directory_name, original_rows, directory_schema = layout["directory"]
        rows = [dict(row) for row in original_rows]
        for row in rows:
            tag = row["stmid"].to_bytes(4, "big")
            if row["chno"] in (-1, 65535, 4294967295):
                row.update(filename="custom-emote.usm", filesize=file_size, datasize=0,
                           minbuf=sum(max(len(part) for part in packets[key]) for key in _TAGS) + 64,
                           avbps=math.ceil(sum(map(len, packets[_TAGS[0]] + packets[_TAGS[1]])) * 8 * fps / count))
            else:
                parts = packets[tag]
                row.update(filename="custom-alpha.m1v" if tag == b"@ALP" else "custom-color.m1v",
                           filesize=sum(map(len, parts)), datasize=0,
                           minbuf=max(map(len, parts)) + 64,
                           avbps=math.ceil(sum(map(len, parts)) * 8 * fps / count))
        output = bytearray(_chunk(b"CRID", 1, _utf_table(directory_name, rows, directory_schema), align=2048))
        seeks = {}
        for tag in _TAGS:
            name, original_index, schema = layout["indexes"][tag]
            seek_rows = []
            for index in seek_frames:
                row = dict(original_index[0])
                row.update(ofs_byte=positions.get((tag, index), 0), ofs_frmid=index, num_skip=0, resv=0)
                seek_rows.append(row)
            seeks[tag] = _chunk(tag, 3, _utf_table(name, seek_rows, schema))
        for tag in _TAGS:
            name, original_header, schema = layout["headers"][tag]
            header = dict(original_header[0])
            header.update(total_frames=count, metadata_count=1, metadata_size=len(seeks[tag]),
                          ixsize=max(len(_chunk(tag, 0, part)) for part in packets[tag]))
            output.extend(_chunk(tag, 1, _utf_table(name, [header], schema), align=512))
        for tag in _TAGS:
            output.extend(_chunk(tag, 2, markers["header"]))
        for tag in _TAGS:
            output.extend(seeks[tag])
        for tag in _TAGS:
            output.extend(_chunk(tag, 2, markers["metadata"]))
        found = {}
        for index in range(count):
            for tag in _TAGS:
                found[tag, index] = len(output)
                output.extend(_chunk(tag, 0, packets[tag][index], time=index * 100, rate=round(fps * 100)))
        for tag in _TAGS:
            output.extend(_chunk(tag, 2, markers["contents"]))
        return bytes(output), found

    first, positions = build(0, {})
    raw, _ = build(len(first), positions)
    candidate = cri_emote.inspect_usm_bytes(raw)
    cri_emote._compatible(candidate, profile)
    return raw


def encode_emote_frames(frames: list[Image.Image], reference_raw: bytes,
                        ffmpeg_path: str | Path, *, work_dir: str | Path | None = None) -> bytes:
    """Return a checked USM from already adapted, constant-rate RGBA frames.

    The caller owns animation decoding, letterboxing, duration resampling and
    the decoded output preview. This function owns and removes its temporary
    raw-video files, including on encoder failure.
    """
    layout = _reference_layout(reference_raw)
    profile = layout["profile"]
    if not isinstance(frames, (list, tuple)) or not 1 <= len(frames) <= cri_emote.MAX_FRAMES:
        raise ValueError("表情需要 1 到 7200 帧。")
    if len(frames) / profile["fps"] > cri_emote.MAX_DURATION:
        raise ValueError("表情动画不能超过 120 秒。")
    size = (profile["width"], profile["height"])
    if any(not isinstance(frame, Image.Image) or frame.mode != "RGBA" or frame.size != size for frame in frames):
        raise ValueError("编码前需要把每帧适配为参考尺寸的 RGBA 图像。")
    if size[0] % 2 or size[1] % 2 or size[0] * size[1] * len(frames) > MAX_FRAME_PIXELS:
        raise ValueError("转换帧像素超过 6400 万或尺寸不支持 MPEG，请缩小或缩短动画。")
    ffmpeg = Path(ffmpeg_path).resolve()
    if not ffmpeg.is_file():
        raise ValueError("未找到 FFmpeg 视频组件。")
    parent = Path(work_dir) if work_dir is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    stream = profile["streams"][0]
    with tempfile.TemporaryDirectory(prefix="emote-encode-", dir=parent) as temporary:
        folder = Path(temporary)
        packets = {tag: _encode_stream(frames, folder, ffmpeg, stream["fps_n"], stream["fps_d"],
                                      layout["codec"], alpha=tag == b"@ALP",
                                      dc_precision=layout["headers"][tag][1][0].get("mpeg_dcprec", 8)) for tag in _TAGS}
        return _mux(layout, packets)
