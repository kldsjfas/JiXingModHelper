"""读取 Unity 中的 Wwise 音频包，并在副本中替换已确认的内嵌 WEM。

游戏把 bank 存在 MonoBehaviour.RawData，而不是 Unity AudioClip 中。
这里按已验证的序列化布局读取字节，避免把几十 MB 的 byte[] 展成 Python 整数。
"""
from __future__ import annotations

import hashlib
import gc
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

import UnityPy

MAX_MEDIA_BYTES = 128 * 1024 * 1024
SUPPORTED_BANK_VERSION = 145
_CODECS = {1: "PCM", 2: "ADPCM", 0x11: "IMA ADPCM", 0xFFFF: "Wwise Vorbis", 0x3041: "Wwise Opus", 0x3039: "Wwise Opus"}


def _uint(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError("音频数据不完整。")
    return struct.unpack_from("<I", data, offset)[0]


def _aligned(offset: int, alignment: int = 4) -> int:
    return (offset + alignment - 1) // alignment * alignment


def _string(data: bytes, offset: int) -> tuple[str, int]:
    size = _uint(data, offset)
    end = offset + 4 + size
    if size > 1024 * 1024 or end > len(data):
        raise ValueError("音频资源名称损坏。")
    return data[offset + 4:end].decode("utf-8"), _aligned(end)


@dataclass
class _BankObject:
    obj: object
    raw: bytes
    name: str
    bank: bytes
    bank_start: int
    hash_start: int
    language: str
    events: list[str]

    def updated(self, bank: bytes) -> bytes:
        end = _aligned(self.bank_start + len(self.bank))
        tail = bytearray(self.raw[end:])
        # The game's RawData hash is MD5; it was checked when opening the object.
        tail[4:20] = hashlib.md5(bank).digest()
        prefix = self.raw[:self.bank_start - 4] + struct.pack("<I", len(bank))
        padding = b"\0" * (_aligned(len(prefix) + len(bank)) - len(prefix) - len(bank))
        return prefix + bank + padding + tail


def _read_object(obj) -> _BankObject | None:
    if obj.type.name != "MonoBehaviour":
        return None
    raw = obj.get_raw_data()
    if len(raw) < 52:
        return None
    try:
        name, at = _string(raw, 28)
        size = _uint(raw, at)
        start = at + 4
        if raw[start:start + 4] != b"BKHD":
            return None
        if size > len(raw) - start:
            raise ValueError("Wwise 音频包长度损坏。")
        bank = raw[start:start + size]
        at = _aligned(start + size)
        hash_start = at
        if _uint(raw, at) != 16 or at + 20 > len(raw):
            raise ValueError("不支持该音频资源的 hash 布局。")
        if raw[at + 4:at + 20] != hashlib.md5(bank).digest():
            raise ValueError("音频资源校验不匹配，请恢复原版或重新扫描。")
        language, at = _string(raw, at + 20)
        count = _uint(raw, at)
        at += 4
        if count > 100000:
            raise ValueError("音频事件数量异常。")
        events = []
        for _ in range(count):
            event, at = _string(raw, at)
            events.append(event)
        if at != len(raw):
            raise ValueError("不支持该音频资源的附加字段。")
        return _BankObject(obj, raw, name, bank, start, hash_start, language, events)
    except (UnicodeError, struct.error):
        return None
    except ValueError:
        # Most MonoBehaviours are unrelated components. Only reject recognized banks.
        if b"BKHD" in raw[28:256]:
            raise
        return None


def _chunks(bank: bytes) -> list[tuple[bytes, bytes]]:
    chunks = []
    seen = set()
    at = 0
    while at < len(bank):
        if at + 8 > len(bank):
            raise ValueError("Wwise 音频包区块头不完整。")
        tag, size = bank[at:at + 4], _uint(bank, at + 4)
        end = at + 8 + size
        if end > len(bank) or tag in seen:
            raise ValueError("Wwise 音频包区块长度或数量异常。")
        seen.add(tag)
        chunks.append((tag, bank[at + 8:end]))
        at = end
    if not chunks or chunks[0][0] != b"BKHD" or len(chunks[0][1]) < 16:
        raise ValueError("不是有效的 Wwise 音频包。")
    return chunks


def inspect_wem(data: bytes) -> dict:
    """只检查媒体结构；是否能完整解码由试听解码器验证。"""
    if not data or len(data) > MAX_MEDIA_BYTES:
        raise ValueError("音频为空或超过 128 MB。")
    if len(data) < 28 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("请选择 Wwise 导出的 .wem 音频，不能把 MP3 或 WAV 改后缀导入。")
    if _uint(data, 4) + 8 != len(data):
        raise ValueError("WEM 文件长度不完整或包含多余数据。")
    chunks = {}
    at = 12
    while at < len(data):
        if at + 8 > len(data):
            raise ValueError("WEM 区块头不完整。")
        tag, size = data[at:at + 4], _uint(data, at + 4)
        end = at + 8 + size
        if end > len(data) or tag in chunks:
            raise ValueError("WEM 区块长度或数量异常。")
        chunks[tag] = data[at + 8:end]
        # Wwise RIFF has unpadded odd chunks in some versions. Current game uses
        # the ordinary RIFF layout, with the last odd data chunk left unpadded.
        at = end if end == len(data) else _aligned(end, 2)
    fmt = chunks.get(b"fmt ", b"")
    if len(fmt) < 16 or not chunks.get(b"data"):
        raise ValueError("WEM 缺少音频格式或声音数据。")
    codec, channels, rate = struct.unpack_from("<HHI", fmt)
    if not 1 <= channels <= 8 or not 8000 <= rate <= 192000:
        raise ValueError("WEM 声道数或采样率无效。")
    duration = None
    sample_count = None
    channel_config = None
    if codec == 0xFFFF and len(fmt) >= 66:
        channel_config = _uint(fmt, 20)
        sample_count = _uint(fmt, 24)
        duration = sample_count / rate
    elif codec == 1:
        byte_rate = _uint(fmt, 8)
        if byte_rate:
            duration = len(chunks[b"data"]) / byte_rate
    loop_points = []
    if b"smpl" in chunks:
        sampler = chunks[b"smpl"]
        if len(sampler) < 36:
            raise ValueError("WEM 循环信息不完整。")
        count = _uint(sampler, 28)
        if count > 64 or 36 + 24 * count > len(sampler):
            raise ValueError("WEM 循环点数量或长度异常。")
        for at in range(36, 36 + 24 * count, 24):
            point = list(struct.unpack_from("<IIIIII", sampler, at))
            if point[2] > point[3] or (sample_count and point[3] >= sample_count):
                raise ValueError("WEM 循环点超出音频长度。")
            loop_points.append(point)
    cue_signature = None
    if b"cue " in chunks:
        cue_signature = hashlib.sha256(chunks[b"cue "] + chunks.get(b"LIST", b"")).hexdigest()
    return {"codec_id": codec, "codec": _CODECS.get(codec, f"Wwise 0x{codec:04x}"),
            "channels": channels, "sample_rate": rate, "size": len(data),
            "duration": duration, "sha256": hashlib.sha256(data).hexdigest(),
            "sample_count": sample_count, "channel_config": channel_config,
            "loop_points": loop_points, "cue_signature": cue_signature,
            "is_wwise_vorbis": codec == 0xFFFF and len(fmt) == 66 and len(chunks.get(b"hash", b"")) == 16}


def _sources(hirc: bytes, version: int) -> tuple[dict[int, list[dict]], str]:
    """Read only the source headers whose layout is known for Wwise 2022.1."""
    if version != SUPPORTED_BANK_VERSION:
        return {}, f"音频包版本 {version} 暂不支持安全替换（已验证版本 145）。"
    if len(hirc) < 4:
        return {}, "缺少 HIRC 播放规则，无法确认内嵌音频的引用。"
    sources: dict[int, list[dict]] = {}
    at = 4
    count = _uint(hirc, 0)
    if count > 100000:
        return {}, "HIRC 对象数量异常。"
    for _ in range(count):
        if at + 5 > len(hirc):
            return {}, "HIRC 播放规则不完整。"
        kind, size = hirc[at], _uint(hirc, at + 1)
        start, end = at + 5, at + 5 + size
        if size < 4 or end > len(hirc):
            return {}, "HIRC 对象长度异常。"
        if kind == 2:
            source_start, source_count = start + 4, 1
        elif kind == 11:
            if size < 9:
                return {}, "音乐轨道头不完整。"
            source_start, source_count = start + 9, _uint(hirc, start + 5)
        else:
            at = end
            continue
        for _ in range(source_count):
            if source_start + 14 > end:
                return {}, "音频来源结构不完整。"
            plugin = _uint(hirc, source_start)
            if plugin & 0x0F != 1:
                return {}, "含合成器或未知来源布局，暂不支持安全替换。"
            stream_type = hirc[source_start + 4]
            media_id = _uint(hirc, source_start + 5)
            media_size = _uint(hirc, source_start + 9)
            flags = hirc[source_start + 13]
            sources.setdefault(media_id, []).append({"size_offset": source_start + 9,
                "size": media_size, "stream_type": stream_type, "prefetch": bool(flags & 2),
                "plugin": plugin, "music_track": kind == 11})
            source_start += 14
        at = end
    if at != len(hirc):
        return {}, "HIRC 尾部数据异常。"
    return sources, ""


def _media(bank: bytes) -> tuple[list[dict], list[tuple[bytes, bytes]], dict[int, list[dict]]]:
    chunks = _chunks(bank)
    parts = dict(chunks)
    version = _uint(parts[b"BKHD"], 0)
    index, data = parts.get(b"DIDX", b""), parts.get(b"DATA", b"")
    if len(index) % 12:
        raise ValueError("Wwise 媒体索引长度异常。")
    sources, source_error = _sources(parts.get(b"HIRC", b""), version)
    result, seen = [], set()
    occupied = []
    for at in range(0, len(index), 12):
        media_id, offset, size = struct.unpack_from("<III", index, at)
        if media_id in seen or size == 0 or offset + size > len(data):
            raise ValueError("Wwise 媒体索引越界、为空或存在重复编号。")
        seen.add(media_id)
        occupied.append((offset, offset + size))
        wem = data[offset:offset + size]
        try:
            info = inspect_wem(wem)
            reason = source_error
        except ValueError as exc:
            info = {"codec_id": None, "codec": "未知/不完整音频", "channels": None,
                    "sample_rate": None, "duration": None, "size": size,
                    "sha256": hashlib.sha256(wem).hexdigest(), "is_wwise_vorbis": False}
            reason = str(exc)
        references = sources.get(media_id, [])
        if not reason and not references:
            reason = "没有找到可验证的音频来源引用。"
        if not reason and any(r["stream_type"] != 0 or r["prefetch"] for r in references):
            reason = "该声音为流式或预加载片段，暂不支持安全替换。"
        if not reason and any(r["size"] != size for r in references):
            reason = "播放规则中的媒体大小与索引不一致。"
        if not reason and not info["is_wwise_vorbis"]:
            reason = "当前仅验证了 Wwise Vorbis WEM 的安全替换。"
        if not reason and any(r["plugin"] != 0x00040001 for r in references):
            reason = "音频解码插件与 Wwise Vorbis 不匹配。"
        info.update(media_id=media_id, offset=offset, editable=not reason, reason=reason,
                    music_track=any(r["music_track"] for r in references))
        result.append(info)
    occupied.sort()
    if any(right[0] < left[1] for left, right in zip(occupied, occupied[1:])):
        raise ValueError("Wwise 媒体片段重叠。")
    return result, chunks, sources


def scan_audio_objects(env) -> list[dict]:
    """Reuse an already opened Unity environment when building the main index."""
    result = []
    for obj in env.objects:
        bank = _read_object(obj)
        if bank is None:
            continue
        media, chunks, _ = _media(bank.bank)
        result.append({"name": bank.name, "object_id": obj.path_id, "language": bank.language,
            "event_names": bank.events, "media_count": len(media),
            "bank_version": _uint(dict(chunks)[b"BKHD"], 0),
            "sha256": hashlib.sha256(bank.bank).hexdigest(), "media": media})
    return result


def _load(path):
    path = Path(path)
    return UnityPy.load(path.read_bytes(), path=str(path.parent))


def _select(env, bank_name: str | int) -> _BankObject:
    matches = []
    for obj in env.objects:
        if isinstance(bank_name, int) and obj.path_id != bank_name:
            continue
        bank = _read_object(obj)
        if bank and (isinstance(bank_name, int) or bank.name == bank_name):
            matches.append(bank)
    if len(matches) != 1:
        raise ValueError("音频包不存在或名称重复，请重新扫描后选择。")
    return matches[0]


def list_audio_banks(bundle_path) -> list[dict]:
    env = _load(bundle_path)
    try:
        return scan_audio_objects(env)
    finally:
        env = None
        gc.collect(0)


def list_bank_media(bundle_path, bank_name: str | int) -> list[dict]:
    bank = _select(_load(bundle_path), bank_name)
    return _media(bank.bank)[0]


def extract_bank_media(bundle_path, bank_name: str | int, media_id: int) -> bytes:
    bank = _select(_load(bundle_path), bank_name)
    media, chunks, _ = _media(bank.bank)
    entry = next((m for m in media if m["media_id"] == int(media_id)), None)
    if entry is None:
        raise ValueError("所选声音不在该音频包中，请重新扫描。")
    data = dict(chunks)[b"DATA"]
    return data[entry["offset"]:entry["offset"] + entry["size"]]


def replace_bank_bytes(bank: bytes, media_id: int, replacement: bytes, *, expected_sha256: str | None = None) -> tuple[bytes, dict]:
    """Rebuild DIDX/DATA and update only verified source-size fields in HIRC."""
    media, chunks, sources = _media(bank)
    entry = next((m for m in media if m["media_id"] == int(media_id)), None)
    if entry is None:
        raise ValueError("所选声音不在该音频包中。")
    if expected_sha256 and entry["sha256"] != expected_sha256:
        raise ValueError("音频已发生变化，请重新打开试听后再替换。")
    if not entry["editable"]:
        raise ValueError(entry["reason"])
    candidate = inspect_wem(replacement)
    if not candidate["is_wwise_vorbis"]:
        raise ValueError("请先用 Wwise 将声音转换为 Vorbis .wem，不能将普通 WAV/MP3 直接写入该音频包。")
    for key, label in (("codec_id", "编码"), ("channels", "声道数"), ("sample_rate", "采样率")):
        if candidate[key] != entry[key]:
            raise ValueError(f"替换音频的{label}不一致：原版 {entry[key]}，导入 {candidate[key]}。")
    if candidate["channel_config"] != entry["channel_config"]:
        raise ValueError("替换音频的声道布局与原版不同，请使用相同的 Wwise 声道配置。")
    if candidate["loop_points"] != entry["loop_points"]:
        raise ValueError("替换音频的循环点与原版不同，请保留相同的循环起止位置和次数。")
    if candidate["cue_signature"] != entry["cue_signature"]:
        raise ValueError("替换音频的 cue 标记与原版不同，请保留原有播放标记。")
    if entry["music_track"] and candidate["sample_count"] != entry["sample_count"]:
        raise ValueError(f"音乐轨道使用固定时间线，请导入同长度 WEM：原版 {entry['duration']:.6f} 秒，导入 {candidate['duration']:.6f} 秒。")
    parts = dict(chunks)
    alignment = _uint(parts[b"BKHD"], 12)
    if alignment not in (1, 2, 4, 8, 16, 32, 64, 128, 256):
        raise ValueError("未知的 Wwise 媒体对齐方式，无法安全重建。")
    original_data = parts[b"DATA"]
    rebuilt_data = bytearray()
    rebuilt_index = bytearray()
    for item in media:
        rebuilt_data.extend(b"\0" * (_aligned(len(rebuilt_data), alignment) - len(rebuilt_data)))
        wem = replacement if item["media_id"] == int(media_id) else original_data[item["offset"]:item["offset"] + item["size"]]
        rebuilt_index.extend(struct.pack("<III", item["media_id"], len(rebuilt_data), len(wem)))
        rebuilt_data.extend(wem)
    rebuilt_hirc = bytearray(parts[b"HIRC"])
    for reference in sources[int(media_id)]:
        struct.pack_into("<I", rebuilt_hirc, reference["size_offset"], len(replacement))
    changed = {b"DIDX": bytes(rebuilt_index), b"DATA": bytes(rebuilt_data), b"HIRC": bytes(rebuilt_hirc)}
    payload = b"".join(tag + struct.pack("<I", len(changed.get(tag, content))) + changed.get(tag, content)
                       for tag, content in chunks)
    verified, _, _ = _media(payload)
    verified_entry = next(m for m in verified if m["media_id"] == int(media_id))
    if not verified_entry["editable"] or verified_entry["sha256"] != candidate["sha256"]:
        raise ValueError("重建音频包未通过校验。")
    return payload, verified_entry


def replace_bank_media(bundle_path, bank_name: str | int, media_id: int, replacement: bytes,
                       output_path, *, expected_sha256: str | None = None) -> dict:
    env = _load(bundle_path)
    target = _select(env, bank_name)
    bank, entry = replace_bank_bytes(target.bank, media_id, replacement, expected_sha256=expected_sha256)
    target.obj.set_raw_data(target.updated(bank))
    payload = env.file.save()
    reopened = UnityPy.load(payload, path=str(Path(bundle_path).parent))
    checked = _select(reopened, int(target.obj.path_id))
    if checked.bank != bank or checked.name != target.name or checked.events != target.events or checked.language != target.language:
        raise ValueError("音频草稿重新读取校验失败。")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".audio-", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, out)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"bank_name": target.name, "object_id": target.obj.path_id, **entry}


def restore_bank_media(output_path, original_path, bank_name: str | int, media_id: int) -> dict:
    original = extract_bank_media(original_path, bank_name, media_id)
    return replace_bank_media(output_path, bank_name, media_id, original, output_path)
