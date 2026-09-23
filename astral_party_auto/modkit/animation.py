"""有界动画预览与独立序列帧替换；不改变游戏的帧引用和播放规则。"""
from __future__ import annotations

import base64
import bisect
import functools
import gc
import io
import math
import os
import re
import tempfile
from pathlib import Path

import UnityPy
from PIL import Image

from .dynamic import classify_text_asset, sequence_groups_from_names, sorted_sequence_names, text_asset_bytes

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_FRAMES = 512
MAX_FRAME_PIXELS = 16_777_216
MAX_TOTAL_PIXELS = 64_000_000
MAX_PREVIEW_FRAMES = 120
MAX_PREVIEW_EDGE = 512
MAX_PREVIEW_PIXELS = 20_000_000
IMAGE_KINDS = {"gif", "webp", "apng"}
SEQUENCE_NOTICE = "序列按名称推测分组，预览默认 30 帧/秒；替换保留原帧数、尺寸和游戏内播放时序。"
SPRITE_NOTICE = "此组含 Sprite 图集/网格帧，可播放预览；当前版本不支持直接替换，以免破坏共享图集和角色网格。"


def _release_readers(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        finally:
            # UnityPy environments contain cycles retaining bundles and atlases.
            gc.collect()
    return wrapped


def _load_bundle(path: str | Path):
    # Path-backed UnityPy readers keep Windows files open through object cycles.
    # Loading bytes permits atomic replacement of the current draft immediately.
    source = Path(path)
    if source.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("资源包超过 512 MB，暂不在动画工作台中打开。")
    return UnityPy.load(source.read_bytes(), path=str(source.parent))


def _size_limit(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or width * height > MAX_FRAME_PIXELS:
        raise ValueError("动画单帧尺寸无效或超过 1600 万像素，请先缩小素材。")


def _fps(value: float) -> float:
    rate = float(value)
    if not math.isfinite(rate) or not 1 <= rate <= 120:
        raise ValueError("预览帧率需要在 1 到 120 之间。")
    return rate


def _duration(value) -> int:
    try:
        value = float(value)
        return round(min(60_000, max(10, value))) if math.isfinite(value) else 100
    except (TypeError, ValueError):
        return 100


def _sequence(env, asset_name: str):
    """与 bundles 索引使用相同分组规则，优先保留 Sprite 而排除其源图集。"""
    candidates: dict[str, list] = {}
    textures: dict[int, str] = {}
    sprite_textures: set[int] = set()
    for obj in env.objects:
        if obj.type.name not in ("Texture2D", "Sprite"):
            continue
        try:
            data = obj.read()
        except Exception:
            continue
        name = str(getattr(data, "m_Name", "") or "(未命名)")
        candidates.setdefault(name, []).append((obj, data))
        if obj.type.name == "Texture2D":
            textures[obj.path_id] = name
        else:
            pointer = getattr(getattr(data, "m_RD", None), "texture", None)
            if pointer is not None and getattr(pointer, "m_PathID", None):
                sprite_textures.add(pointer.m_PathID)
    atlas_names = {textures[pid] for pid in sprite_textures if pid in textures}
    eligible = [name for name in candidates if name not in atlas_names]
    group = next((group for group in sequence_groups_from_names(eligible) if group.base == asset_name), None)
    if group is None:
        raise ValueError(f"找不到序列帧组：{asset_name}，请刷新资源索引。")
    rows = []
    for name in sorted_sequence_names(group.names):
        options = candidates[name]
        sprites = [row for row in options if row[0].type.name == "Sprite"]
        options = sprites or options
        if len(options) != 1:
            raise ValueError(f"序列内有重复资源名 {name}，暂不支持处理。")
        rows.append((name, *options[0]))
    return rows


def _text_asset(env, asset_name: str):
    matches = []
    for obj in env.objects:
        if obj.type.name == "TextAsset":
            data = obj.read()
            if str(getattr(data, "m_Name", "")) == asset_name:
                matches.append((obj, data))
    if len(matches) != 1:
        raise ValueError(f"动画资源不存在或名称不唯一：{asset_name}")
    return matches[0]


def _sequence_info(rows) -> dict:
    editable = all(obj.type.name == "Texture2D" for _, obj, _ in rows)
    data = rows[0][2]
    rect = getattr(data, "m_Rect", None)
    width = int(getattr(data, "m_Width", 0) or getattr(rect, "width", 0))
    height = int(getattr(data, "m_Height", 0) or getattr(rect, "height", 0))
    notice = SEQUENCE_NOTICE if editable else SPRITE_NOTICE + " " + SEQUENCE_NOTICE
    return {"animation_kind": "sequence", "editable": editable,
            "reason": "" if editable else SPRITE_NOTICE, "notice": notice, "message": notice,
            "frame_names": [row[0] for row in rows], "width": width, "height": height, "total": len(rows)}


@_release_readers
def inspect_animation(bundle_path: str | Path, asset_name: str, kind: str = "sequence") -> dict:
    env = _load_bundle(bundle_path)
    if kind == "sequence":
        return _sequence_info(_sequence(env, asset_name))
    if kind not in IMAGE_KINDS:
        return {"animation_kind": kind, "editable": False, "reason": "此动画格式暂不支持直接替换。"}
    _, data = _text_asset(env, asset_name)
    raw = text_asset_bytes(data)
    actual = classify_text_asset(asset_name, raw)
    if actual != kind:
        raise ValueError("资源实际格式与所选动画类型不一致，请刷新资源索引。")
    with _open_image(raw) as image:
        count = getattr(image, "n_frames", 1) - int(bool(image.info.get("default_image")))
        return {"animation_kind": kind, "editable": count > 1, "reason": "" if count > 1 else "这是一张静态图片。",
                "notice": "仅接受同格式动画文件；完整保留导入文件的帧时长。", "width": image.width,
                "height": image.height, "total": count, "frame_names": [asset_name]}


def _open_image(raw: bytes):
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("动画文件超过 64 MB，请先缩小素材。")
    image = Image.open(io.BytesIO(raw))
    _size_limit(*image.size)
    count = getattr(image, "n_frames", 1)
    if count > MAX_SOURCE_FRAMES or image.width * image.height * count > MAX_TOTAL_PIXELS:
        image.close()
        raise ValueError("动画超过 512 帧或总像素超过 6400 万，请先缩小或减少帧数。")
    return image


def _decode_animation(raw: bytes) -> tuple[list[Image.Image], list[int], str]:
    with _open_image(raw) as image:
        if image.format not in ("GIF", "WEBP", "PNG"):
            raise ValueError("请选择 GIF、APNG、动画 WebP 或 PNG 帧目录。")
        frames, durations = [], []
        kind = {"GIF": "gif", "WEBP": "webp", "PNG": "apng"}[image.format]
        start = int(bool(image.info.get("default_image")))
        for index in range(start, getattr(image, "n_frames", 1)):
            image.seek(index)
            frames.append(image.convert("RGBA"))
            durations.append(_duration(image.info.get("duration", 100)))
        if len(frames) < 2:
            raise ValueError("所选文件没有多帧动画，请选择动态图或至少两张 PNG 的帧目录。")
        return frames, durations, kind


def _natural_key(path: Path):
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", path.name)]


def _read_replacement(path: str | Path, fps: float = 30):
    source = Path(path)
    if not source.is_dir():
        if source.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("动画文件超过 64 MB，请先缩小素材。")
        return _decode_animation(source.read_bytes())
    files = sorted((file for file in source.iterdir() if file.is_file() and file.suffix.lower() == ".png"), key=_natural_key)
    if not 2 <= len(files) <= MAX_SOURCE_FRAMES:
        raise ValueError("帧目录需要包含 2 到 512 张 PNG 图片。")
    frames, total_pixels = [], 0
    for file in files:
        if file.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"图片过大：{file.name}")
        with Image.open(file) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                raise ValueError(f"帧目录只能放静态 PNG：{file.name}")
            _size_limit(*image.size)
            total_pixels += image.width * image.height
            if total_pixels > MAX_TOTAL_PIXELS:
                raise ValueError("帧目录总像素超过 6400 万，请先缩小素材。")
            frames.append(image.convert("RGBA"))
    return frames, [1000 / _fps(fps)] * len(frames), "sequence"


def _preview(frames, names, durations, total: int, *, info: dict | None = None) -> dict:
    """frames 为有界图像迭代器；预览缩略图不反过来充当写回素材。"""
    encoded = []
    original_size = (0, 0)
    edge = min(MAX_PREVIEW_EDGE, int(math.sqrt(MAX_PREVIEW_PIXELS / max(1, len(names)))))
    for frame in frames:
        _size_limit(*frame.size)
        if original_size == (0, 0):
            original_size = frame.size
        frame = frame.convert("RGBA")
        frame.thumbnail((edge, edge), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        frame.save(buffer, format="PNG")
        encoded.append("data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
    result = dict(info or {})
    result.update({"frames": encoded, "names": names, "durations": durations, "fps": 1000 * total / sum(durations),
                   "width": original_size[0], "height": original_size[1], "total": total,
                   "truncated": len(encoded) < total})
    if result["truncated"]:
        result["notice"] = (result.get("notice", "") + " 预览均匀抽样至 120 帧以内；替换使用全部原帧。").strip()
    return result


def _preview_indices(total: int, durations):
    indices = sorted({int(index * total / min(total, MAX_PREVIEW_FRAMES)) for index in range(min(total, MAX_PREVIEW_FRAMES))})
    merged = [sum(durations[start:end]) for start, end in zip(indices, indices[1:] + [total])]
    return indices, merged


@_release_readers
def read_replacement_animation(path: str | Path, fps: float = 30) -> dict:
    frames, durations, kind = _read_replacement(path, fps)
    indices, sampled_durations = _preview_indices(len(frames), durations)
    return _preview((frames[index] for index in indices), [f"第 {index + 1} 帧" for index in indices],
                    sampled_durations, len(frames), info={"animation_kind": kind, "editable": True,
                                                        "notice": "显示导入素材；写入序列帧时按原帧数均匀采样并匹配每帧尺寸。"})


@_release_readers
def read_animation_preview(bundle_path: str | Path, asset_name: str, kind: str = "sequence", fps: float = 30) -> dict:
    env = _load_bundle(bundle_path)
    if kind == "sequence":
        rows = _sequence(env, asset_name)
        info = _sequence_info(rows)
        indices, durations = _preview_indices(len(rows), [1000 / _fps(fps)] * len(rows))
        # UnityPy caches the shared Sprite atlas in this one environment.
        return _preview((rows[index][2].image for index in indices), [rows[index][0] for index in indices],
                        durations, len(rows), info=info)
    if kind not in IMAGE_KINDS:
        raise ValueError("此格式需要对应游戏运行时，暂不支持播放预览。")
    _, data = _text_asset(env, asset_name)
    frames, durations, actual = _decode_animation(text_asset_bytes(data))
    if actual != kind:
        raise ValueError("资源实际格式与所选动画类型不一致。")
    indices, sampled_durations = _preview_indices(len(frames), durations)
    return _preview((frames[index] for index in indices), [f"{asset_name} / {index + 1}" for index in indices],
                    sampled_durations, len(frames), info={"animation_kind": kind, "editable": True,
                                                        "notice": "按动画文件内的逐帧时长播放。"})


def _set_image(data, image: Image.Image) -> None:
    image = image.convert("RGBA")
    mipmaps = max(1, int(getattr(data, "m_MipCount", 1) or 1))
    data.set_image(image, mipmap_count=mipmaps)
    data.save()


def _set_text_bytes(data, raw: bytes) -> None:
    # UnityPy's TextAsset field is a surrogateescaped string on current versions.
    current = getattr(data, "m_Script", None)
    field = "m_Script" if current is not None else "script"
    current = getattr(data, field)
    setattr(data, field, raw.decode("utf-8", errors="surrogateescape") if isinstance(current, str) else raw)
    data.save()


def _atomic_save(env, out_bundle: str | Path, validate) -> None:
    out = Path(out_bundle)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = env.file.save()
    # Verify the serialized bundle, not just the in-memory edited objects.
    validate(UnityPy.load(payload))
    handle, name = tempfile.mkstemp(prefix=out.name + ".", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, out)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _validate_sequence(env, asset_name, expected_sizes):
    rows = _sequence(env, asset_name)
    if [row[0] for row in rows] != list(expected_sizes):
        raise ValueError("写回校验失败：动画帧名或数量发生变化。")
    for name, obj, data in rows:
        if obj.type.name != "Texture2D" or data.image.size != expected_sizes[name]:
            raise ValueError(f"写回校验失败：{name} 无法按原尺寸解码。")


def _sequence_sizes(rows):
    info = _sequence_info(rows)
    if not info["editable"]:
        raise ValueError(info["reason"])
    sizes = {name: (int(data.m_Width), int(data.m_Height)) for name, _, data in rows}
    if len(rows) > MAX_SOURCE_FRAMES or sum(w * h for w, h in sizes.values()) > MAX_TOTAL_PIXELS:
        raise ValueError("目标序列超过 512 帧或总像素超过 6400 万，本次不写入。")
    for size in sizes.values():
        _size_limit(*size)
    return sizes


def _resample_indices(durations, count: int) -> list[int]:
    boundaries, running = [], 0.0
    for duration in durations:
        running += duration
        boundaries.append(running)
    return [min(bisect.bisect_right(boundaries, running * (index + 0.5) / count), len(durations) - 1)
            for index in range(count)]


@_release_readers
def read_adapted_replacement(bundle_path: str | Path, asset_name: str, replacement_path: str | Path,
                             kind: str = "sequence", fps: float = 30) -> dict:
    """预览真正写入的帧数、尺寸和采样位置，不触碰草稿文件。"""
    rate = _fps(fps)
    env = _load_bundle(bundle_path)
    if kind == "sequence":
        rows = _sequence(env, asset_name)
        sizes = _sequence_sizes(rows)
        frames, durations, _ = _read_replacement(replacement_path, rate)
        sampled = _resample_indices(durations, len(rows))
        indices, preview_durations = _preview_indices(len(rows), [1000 / rate] * len(rows))
        adapted = (frames[sampled[index]].resize(sizes[rows[index][0]], Image.Resampling.LANCZOS)
                   for index in indices)
        info = _sequence_info(rows)
        info.update({"source_frame_count": len(frames), "source_duration": sum(durations),
                     "notice": f"已按目标的 {len(rows)} 帧和每帧尺寸适配，展示写入前的采样效果；游戏内播放时序保持原样。"})
        return _preview(adapted, [rows[index][0] for index in indices], preview_durations, len(rows), info=info)
    if kind not in IMAGE_KINDS or not Path(replacement_path).is_file():
        raise ValueError("该资源只能选择同格式的动画文件。")
    _, data = _text_asset(env, asset_name)
    actual = classify_text_asset(asset_name, text_asset_bytes(data))
    frames, durations, imported = _read_replacement(replacement_path, rate)
    if actual != kind or imported != kind:
        raise ValueError(f"原资源为 {actual or '未知格式'}，只能导入相同格式的动画。")
    indices, preview_durations = _preview_indices(len(frames), durations)
    return _preview((frames[index] for index in indices), [f"{asset_name} / {index + 1}" for index in indices],
                    preview_durations, len(frames), info={"animation_kind": kind, "editable": True,
                                                        "notice": "格式校验通过；写入时保留导入动画的尺寸和逐帧时长。"})


@_release_readers
def replace_bundle_sequence(src_bundle: str | Path, asset_name: str, replacement_path: str | Path,
                            out_bundle: str | Path, fps: float = 30) -> dict:
    rate = _fps(fps)
    env = _load_bundle(src_bundle)
    rows = _sequence(env, asset_name)
    info = _sequence_info(rows)
    expected_sizes = _sequence_sizes(rows)
    frames, durations, _ = _read_replacement(replacement_path, rate)
    sampled = _resample_indices(durations, len(rows))
    for index, (name, _, data) in enumerate(rows):
        frame = frames[sampled[index]].resize(expected_sizes[name], Image.Resampling.LANCZOS)
        _set_image(data, frame)
    _atomic_save(env, out_bundle, lambda saved: _validate_sequence(saved, asset_name, expected_sizes))
    info.update({"source_frame_count": len(frames), "source_duration": sum(durations), "fps": rate,
                 "frame_count": len(rows), "names": info["frame_names"]})
    return info


@_release_readers
def replace_bundle_animated_image(src_bundle: str | Path, asset_name: str,
                                  replacement_path: str | Path, out_bundle: str | Path) -> dict:
    path = Path(replacement_path)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("请选择不超过 64 MB 的同格式动画文件。")
    raw = path.read_bytes()
    frames, durations, kind = _decode_animation(raw)
    env = _load_bundle(src_bundle)
    _, data = _text_asset(env, asset_name)
    old_kind = classify_text_asset(asset_name, text_asset_bytes(data))
    if old_kind != kind:
        raise ValueError(f"原资源为 {old_kind or '未知格式'}，只能导入相同格式的动画。")
    _set_text_bytes(data, raw)
    def validate(saved):
        _, restored = _text_asset(saved, asset_name)
        if text_asset_bytes(restored) != raw:
            raise ValueError("写回校验失败：动画文件字节不一致。")
    _atomic_save(env, out_bundle, validate)
    return {"animation_kind": kind, "frame_names": [asset_name], "frame_count": len(frames),
            "width": frames[0].width, "height": frames[0].height, "durations": durations,
            "notice": "已保留导入动画的完整字节与逐帧时长。"}


@_release_readers
def restore_bundle_animation(draft_bundle: str | Path, original_bundle: str | Path,
                              asset_name: str, kind: str = "sequence") -> None:
    draft = _load_bundle(draft_bundle)
    original = _load_bundle(original_bundle)
    if kind == "sequence":
        current_rows = _sequence(draft, asset_name)
        source_rows = _sequence(original, asset_name)
        if not _sequence_info(current_rows)["editable"] or not _sequence_info(source_rows)["editable"]:
            raise ValueError(SPRITE_NOTICE)
        sources = {name: (obj, data) for name, obj, data in source_rows}
        if set(sources) != {name for name, _, _ in current_rows}:
            raise ValueError("原包和草稿帧名不一致，未执行恢复。")
        sizes = {}
        for name, target_obj, _ in current_rows:
            source_obj, source_data = sources[name]
            sizes[name] = (source_data.m_Width, source_data.m_Height)
            # Copy original compressed bytes, without another lossy encoding.
            # Inline streamed pixels so offsets never depend on draft layout.
            source_data.image_data = source_data.get_image_data()
            if source_data.m_StreamData is not None:
                source_data.m_StreamData.path = ""
                source_data.m_StreamData.offset = 0
                source_data.m_StreamData.size = 0
            source_data.save()
            # get_raw_data() reads the original serialized buffer in UnityPy;
            # .data contains the newly serialized inline pixels after save().
            target_obj.set_raw_data(source_obj.data)
        _atomic_save(draft, draft_bundle, lambda saved: _validate_sequence(saved, asset_name, sizes))
    elif kind in IMAGE_KINDS:
        _, source = _text_asset(original, asset_name)
        _, target = _text_asset(draft, asset_name)
        raw = text_asset_bytes(source)
        _set_text_bytes(target, raw)
        def validate(saved):
            if text_asset_bytes(_text_asset(saved, asset_name)[1]) != raw:
                raise ValueError("恢复动画时字节校验失败。")
        _atomic_save(draft, draft_bundle, validate)
    else:
        raise ValueError("此动画格式暂不支持恢复。")
