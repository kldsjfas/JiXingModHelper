"""从 AnimationClip 的 Sprite 引用曲线重建二维动作预览。"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from UnityPy.classes import PPtr
from UnityPy.enums import SpritePackingMode
from UnityPy.export.SpriteHelper import SpriteSettings
from UnityPy.helpers.MeshHelper import MeshHandler

from .animation import _load_bundle, _preview, _preview_indices, _release_readers, _size_limit
from .clip_edit import _clip

MAX_KEYS = 20_000
MAX_SPRITES = 512
MAX_DURATION = 3600
NOTICE = "按 AnimationClip 内的 Sprite 引用顺序、逐帧时长和角色原点播放；循环仅用于预览。"
UNSUPPORTED = "此动画包含骨骼、变换或多条渲染轨道，需要游戏运行时才能完整预览。"


@dataclass
class SpriteFrame:
    name: str
    sprite: object
    left: float
    bottom: float
    right: float
    top: float
    pixels_per_unit: float


def _number(value, label: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label}不是有效数值。")
    return number


def _streamed_keys(stream: dict) -> list[tuple[float, float]]:
    """构建后的 Unity 曲线用 uint32 数组存时间和 (索引, 三次系数)。"""
    words = stream.get("data", [])
    if len(words) > MAX_KEYS * 7 + 2:
        raise ValueError("动画关键帧过多，暂不生成预览。")
    raw = struct.pack(f"<{len(words)}I", *words)
    keys, offset = [], 0
    while offset < len(raw):
        if offset + 8 > len(raw):
            raise ValueError("动画流关键帧不完整。")
        time, count = struct.unpack_from("<fi", raw, offset)
        offset += 8
        if count < 0 or count > 1 or offset + count * 20 > len(raw):
            raise ValueError("动画流不是受支持的单条 Sprite 曲线。")
        for _ in range(count):
            index, _, _, _, value = struct.unpack_from("<i4f", raw, offset)
            offset += 20
            if index != 0:
                raise ValueError(UNSUPPORTED)
            keys.append((time, value))
    return keys


def _compiled_keys(tree: dict) -> tuple[list, float, float]:
    bindings = tree.get("m_ClipBindingConstant", {})
    tracks = bindings.get("genericBindings", [])
    if (len(tracks) != 1 or tracks[0].get("typeID") != 212
            or not tracks[0].get("isPPtrCurve")):
        raise ValueError(UNSUPPORTED)
    mapping = bindings.get("pptrCurveMapping", [])
    muscle = tree.get("m_MuscleClip", {})
    start = _number(muscle.get("m_StartTime", 0), "动画起点")
    stop = _number(muscle.get("m_StopTime", 0), "动画终点")
    clip = muscle.get("m_Clip", {}).get("data", {})
    stream = clip.get("m_StreamedClip", {})
    dense = clip.get("m_DenseClip", {})
    constant = clip.get("m_ConstantClip", {}).get("data", [])
    counts = (stream.get("curveCount", 0), dense.get("m_CurveCount", 0), len(constant))
    if counts == (1, 0, 0):
        indices = _streamed_keys(stream)
    elif counts == (0, 1, 0):
        rate = _number(dense.get("m_SampleRate", 0), "动画采样率")
        begin = _number(dense.get("m_BeginTime", 0), "动画采样起点")
        values = dense.get("m_SampleArray", [])
        if not 0 < rate <= 1000 or len(values) > MAX_KEYS or len(values) != dense.get("m_FrameCount"):
            raise ValueError("动画采样数据不完整或超过预览范围。")
        indices = [(begin + index / rate, value) for index, value in enumerate(values)]
    elif counts == (0, 0, 1):
        indices = [(start, constant[0])]
    else:
        raise ValueError(UNSUPPORTED)
    keys = []
    for time, value in indices:
        if math.isnan(time):
            raise ValueError("动画关键帧时间无效。")
        if time == math.inf:
            continue
        value = _number(value, "Sprite 引用索引")
        index = round(value)
        if abs(value - index) > 0.001 or not 0 <= index < len(mapping):
            raise ValueError("动画引用的 Sprite 索引无效。")
        keys.append((max(start, time), mapping[index]))
    return keys, start, stop


def _timeline(tree: dict) -> tuple[list[dict], list[float], float]:
    for field in ("m_RotationCurves", "m_CompressedRotationCurves", "m_EulerCurves",
                  "m_PositionCurves", "m_ScaleCurves", "m_FloatCurves"):
        if tree.get(field):
            raise ValueError(UNSUPPORTED)
    curves = tree.get("m_PPtrCurves", [])
    if curves:
        if len(curves) != 1 or curves[0].get("classID") != 212 or curves[0].get("attribute") != "m_Sprite":
            raise ValueError(UNSUPPORTED)
        rows = curves[0].get("curve", [])
        keys = [(_number(row["time"], "动画关键帧时间"), row["value"]) for row in rows]
        start = 0.0
        muscle = tree.get("m_MuscleClip", {})
        stop = _number(muscle.get("m_StopTime", 0), "动画终点")
        if stop <= start and keys:
            rate = _number(tree.get("m_SampleRate", 0), "动画采样率")
            if not 0 < rate <= 1000:
                raise ValueError("动画采样率无效。")
            stop = max(time for time, _ in keys) + 1 / rate
    else:
        keys, start, stop = _compiled_keys(tree)
    if not 0 < stop - start <= MAX_DURATION or not keys or len(keys) > MAX_KEYS:
        raise ValueError("动画没有可播放的时间范围，或超过一小时。")
    # Unity's first streamed key is usually at -float.max and holds at t=0.
    # Identical timestamps use the final value, matching a stepped PPtr curve.
    events: list[tuple[float, dict]] = []
    previous = -math.inf
    for time, pointer in keys:
        if time < previous:
            raise ValueError("动画关键帧时间没有按顺序排列。")
        previous = time
        if time >= stop:
            continue
        time = max(start, time)
        if events and abs(events[-1][0] - time) < 1e-8:
            events[-1] = (time, pointer)
        else:
            events.append((time, pointer))
    if not events or events[0][0] > start + 1e-6:
        raise ValueError("动画起始 Sprite 依赖场景状态，暂不能独立预览。")
    durations = [(end - time) * 1000 for (time, _), end in zip(events, [row[0] for row in events[1:]] + [stop])]
    return [pointer for _, pointer in events], durations, stop - start


def _render_data(sprite):
    atlas = None
    if sprite.m_SpriteAtlas:
        atlas = sprite.m_SpriteAtlas.deref_parse_as_object()
    elif sprite.m_AtlasTags:
        for obj in sprite.assets_file.objects.values():
            if obj.type.name == "SpriteAtlas" and obj.peek_name() == sprite.m_AtlasTags[0]:
                atlas = obj.read()
                break
    if atlas:
        return next(value for key, value in atlas.m_RenderDataMap if key == sprite.m_RenderDataKey)
    return sprite.m_RD


def _sprite_frame(sprite, reader) -> SpriteFrame:
    ppu = _number(sprite.m_PixelsToUnits, "Sprite 像素比例")
    if not 0 < ppu <= 1_000_000:
        raise ValueError("Sprite 像素比例无效。")
    data = _render_data(sprite)
    settings = SpriteSettings(data.settingsRaw)
    bounds = None
    if settings.packingMode == SpritePackingMode.kSPMTight:
        mesh = MeshHandler(sprite.m_RD, reader.version)
        mesh.process()
        if mesh.m_UV0 and any(u or v for u, v in mesh.m_UV0) and mesh.m_Vertices:
            axes = [[vertex[index] for vertex in mesh.m_Vertices] for index in range(3)]
            flat_axis = next((index for index in (2, 1, 0) if len(set(axes[index])) == 1), None)
            if flat_axis is None:
                raise ValueError("此 Sprite 是三维网格，需要游戏运行时预览。")
            axes.pop(flat_axis)
            # UnityPy reconstructs mesh sprites in their real local coordinates.
            # The game's tiled sprites have a nominal pivot that differs from
            # these vertices, so centering each cropped image would visibly jump.
            bounds = (min(axes[0]), min(axes[1]), max(axes[0]), max(axes[1]))
    if bounds is None:
        rect = sprite.m_Rect
        pivot = sprite.m_Pivot
        offset = data.textureRectOffset
        left = (offset.x - pivot.x * rect.width) / ppu
        bottom = (offset.y - pivot.y * rect.height) / ppu
        bounds = (left, bottom, left + data.textureRect.width / ppu, bottom + data.textureRect.height / ppu)
    left, bottom, right, top = (_number(value, "Sprite 边界") for value in bounds)
    _size_limit(round((right - left) * ppu), round((top - bottom) * ppu))
    return SpriteFrame(str(sprite.m_Name), sprite, left, bottom, right, top, ppu)


def _read_clip(env, name: str):
    clip = _clip(env, name)
    pointers, durations, duration = _timeline(clip.read_typetree())
    unique, frames = {}, []
    for pointer in pointers:
        key = (int(pointer["m_FileID"]), int(pointer["m_PathID"]))
        if key[1] == 0:
            frames.append(None)
            continue
        if key not in unique:
            if len(unique) >= MAX_SPRITES:
                raise ValueError("动画使用超过 512 个 Sprite，暂不生成预览。")
            try:
                reader = PPtr(m_FileID=key[0], m_PathID=key[1], assetsfile=clip.assets_file).deref()
            except Exception as exc:
                raise ValueError("动画引用的 Sprite 不在当前资源包中，暂不能独立预览。") from exc
            if reader.type.name != "Sprite":
                raise ValueError("动画曲线引用的对象不是 Sprite。")
            unique[key] = _sprite_frame(reader.read(), reader)
        frames.append(unique[key])
    visible = list(unique.values())
    if not visible:
        raise ValueError("动画全部引用空 Sprite，没有可显示的画面。")
    ppu = visible[0].pixels_per_unit
    # Tiny float errors in Unity's serialized vertices must not add a border.
    left = math.floor(min(row.left for row in visible) * ppu + 0.001)
    bottom = math.floor(min(row.bottom for row in visible) * ppu + 0.001)
    right = math.ceil(max(row.right for row in visible) * ppu - 0.001)
    top = math.ceil(max(row.top for row in visible) * ppu - 0.001)
    width, height = right - left, top - bottom
    _size_limit(width, height)
    info = {"animation_kind": "sprite_clip", "playable": True, "reason": "", "notice": NOTICE,
            "width": width, "height": height, "total": len(frames), "duration": duration,
            "fps": len(frames) / duration, "frame_names": [row.name if row else "空白帧" for row in frames]}
    return frames, durations, info, (ppu, left, top)


@_release_readers
def inspect_clip_preview(bundle_path: str | Path, name: str) -> dict:
    try:
        _, _, info, _ = _read_clip(_load_bundle(bundle_path), name)
        return info
    except Exception as exc:
        return {"animation_kind": "sprite_clip", "playable": False, "reason": str(exc),
                "notice": str(exc), "width": 0, "height": 0, "total": 0, "duration": 0}


@_release_readers
def read_clip_preview(bundle_path: str | Path, name: str) -> dict:
    frames, durations, info, (ppu, left, top) = _read_clip(_load_bundle(bundle_path), name)
    indices, sampled_durations = _preview_indices(len(frames), durations)

    def images():
        for index in indices:
            canvas = Image.new("RGBA", (info["width"], info["height"]))
            frame = frames[index]
            if frame is not None:
                sprite_image = frame.sprite.image.convert("RGBA")
                size = (round((frame.right - frame.left) * ppu), round((frame.top - frame.bottom) * ppu))
                if sprite_image.size != size:
                    sprite_image = sprite_image.resize(size, Image.Resampling.LANCZOS)
                position = (round(frame.left * ppu - left), round(top - frame.top * ppu))
                # alpha_composite also clears the atlas RGB hidden under alpha=0.
                canvas.alpha_composite(sprite_image, dest=position)
            yield canvas

    return _preview(images(), [info["frame_names"][index] for index in indices],
                    sampled_durations, len(frames), info=info)
