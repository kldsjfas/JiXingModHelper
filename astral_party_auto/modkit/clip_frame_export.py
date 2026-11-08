"""按 AnimationClip 的真实时间线导出完整 PNG 帧包。"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import tempfile
import zipfile
from pathlib import Path

from PIL import Image

from .animation import MAX_FRAME_PIXELS, MAX_SOURCE_FRAMES, _load_bundle, _release_readers
from .bundles import bundle_file_map, read_bundle_asset_names
from .catalog_mods import active_catalog_path
from .categories import resource_display_name
from .clip_preview import _read_clip, iter_clip_canvases
from .emote_addressables import _catalog_parts


MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_CLIP_PACKAGE_PIXELS = 128_000_000
FRAME_PACKAGE_SCHEMA = "jixing-animation-frames"


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _character_locations(aa_dirs):
    catalog = json.loads(active_catalog_path(aa_dirs).read_text(encoding="utf-8-sig"))
    _, _, buckets, entries = _catalog_parts(catalog)
    for _offset, key, ids in buckets:
        if not isinstance(key, str):
            continue
        for entry_id in ids:
            entry = entries[entry_id]
            kind = catalog["m_resourceTypes"][entry[6]].get("m_ClassName")
            if kind not in {"Core.CharacterAsset", "UnityEngine.AnimationClip"} or entry[2] < 0:
                continue
            bundles = {
                catalog["m_InternalIds"][entries[dependency][0]].replace("\\", "/").rsplit("/", 1)[-1]
                for dependency in buckets[entry[2]][2]
            }
            yield key, kind, bundles


def resolve_clip_identity(aa_dirs, bundle_name: str, asset_name: str) -> dict:
    """只使用当前 catalog 中指向目标包的明确角色地址识别归属。"""
    result = {"role_id": None, "action": asset_name, "target_kind": "unknown", "catalog_keys": []}
    hero_ids, monster_ids = set(), set()
    hero_asset = re.compile(r"^Hero([1-9]\d*)(?:_\d+|_Max)?_PC$")
    hero_clip = re.compile(r"^Hero([1-9]\d*)(?:_\d+|_Max)?_" + re.escape(asset_name) + r"_PC$")
    monster_asset = re.compile(r"^Monster([1-9]\d*)(?:_\d+)?_PC$")
    for key, kind, bundles in _character_locations(aa_dirs):
        if bundle_name not in bundles:
            continue
        match = hero_asset.fullmatch(key) if kind == "Core.CharacterAsset" else hero_clip.fullmatch(key)
        monster = monster_asset.fullmatch(key) if kind == "Core.CharacterAsset" else None
        if match:
            hero_ids.add(int(match[1]))
            result["catalog_keys"].append(key)
        if monster:
            monster_ids.add(int(monster[1]))
            result["catalog_keys"].append(key)
    result["catalog_keys"] = sorted(set(result["catalog_keys"]))
    if len(hero_ids) == 1 and not monster_ids:
        role_id = next(iter(hero_ids))
        if role_id <= 2_147_483_647:
            result.update(role_id=role_id, target_kind="hero")
    elif monster_ids and not hero_ids:
        result["target_kind"] = "monster"
    elif hero_ids or monster_ids:
        result["target_kind"] = "ambiguous"
    return result


def find_character_clip_targets(aa_dirs, role_id: int, action: str, *, typed_index=None,
                                preferred_bundle: str = "") -> list[dict]:
    """定位同角色同动作的原版来源，保留各套外观的实际包与动作名。"""
    if type(role_id) is not int or not 1 <= role_id <= 2_147_483_647:
        raise ValueError("角色 ID 无效。")
    if not isinstance(action, str):
        return []
    hero_asset = re.compile(r"^Hero" + str(role_id) + r"(?:_(?P<variant>\d+|Max))?_PC$")
    clip_pattern = re.escape(action) if action else r"(?P<clip>.+)"
    hero_clip = re.compile(r"^Hero" + str(role_id) + r"(?:_(?P<variant>\d+|Max))?_" + clip_pattern + r"_PC$")
    known_actions = (typed_index.get("anim") or {}) if isinstance(typed_index, dict) else {}
    paths = bundle_file_map(aa_dirs) if typed_index is None else {}
    inspected = {}
    candidates = {}
    for address, kind, bundles in _character_locations(aa_dirs):
        match = hero_asset.fullmatch(address) if kind == "Core.CharacterAsset" else hero_clip.fullmatch(address)
        if not match:
            continue
        variant = match["variant"] or ""
        label = "默认外观" if not variant else ("Max 变体" if variant == "Max" else f"外观 {variant}")
        for bundle in sorted(bundles):
            if typed_index is None:
                if bundle not in inspected:
                    try:
                        inspected[bundle] = read_bundle_asset_names(paths[bundle]).get("anim", []) if bundle in paths else []
                    except Exception:
                        inspected[bundle] = []
                names = inspected[bundle]
            else:
                names = known_actions.get(bundle, [])
            if action:
                matched_actions = [action] if action in names else []
            elif kind == "UnityEngine.AnimationClip":
                matched_actions = [match["clip"]] if match["clip"] in names else []
            else:
                matched_actions = names
            for clip_name in matched_actions:
                key = bundle, variant, clip_name
                if key in candidates and kind != "UnityEngine.AnimationClip":
                    continue
                candidate = {"bundle": bundle, "name": clip_name, "role_id": role_id, "action": clip_name,
                             "address": address, "label": label}
                if not action:
                    candidate["action_label"] = resource_display_name(clip_name, "anim")
                candidates[key] = candidate

    def order(item):
        (bundle, variant, clip_name), _candidate = item
        variant_order = (0, 0) if not variant else ((2, 0) if variant == "Max" else (1, int(variant)))
        return bundle != preferred_bundle, variant_order, bundle, clip_name.casefold()

    return [candidate for _key, candidate in sorted(candidates.items(), key=order)]


def read_clip_frame_directory(directory: str | Path) -> tuple[list[Image.Image], list[float], dict]:
    """读取解压后的完整动作帧包，允许修改 PNG 像素并沿用原时间线。"""
    root = Path(directory).resolve()
    path = root / "clip-frames.json"
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("完整动作帧包缺少有效的 clip-frames.json。")
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if metadata.get("schema") != FRAME_PACKAGE_SCHEMA or metadata.get("schema_version") != 1:
        raise ValueError("不支持此动作帧包清单版本。")
    if (not isinstance(metadata.get("source_bundle_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", metadata["source_bundle_sha256"])
            or not isinstance(metadata.get("action"), str) or not 1 <= len(metadata["action"]) <= 200):
        raise ValueError("动作帧包的来源哈希或动作名无效。")
    role_id = metadata.get("role_id")
    if role_id is not None and (type(role_id) is not int or not 1 <= role_id <= 2_147_483_647):
        raise ValueError("动作帧包的角色归属无效。")
    rows, canvas = metadata.get("frames"), metadata.get("canvas")
    count = metadata.get("frame_count")
    if (type(count) is not int or not 1 <= count <= MAX_SOURCE_FRAMES or not isinstance(rows, list)
            or len(rows) != count or not isinstance(canvas, dict)):
        raise ValueError("动作帧包的帧数或画布信息不完整。")
    width, height = canvas.get("width"), canvas.get("height")
    if (type(width) is not int or type(height) is not int or min(width, height) < 1 or max(width, height) > 16384
            or width * height > MAX_FRAME_PIXELS or width * height * count > MAX_CLIP_PACKAGE_PIXELS):
        raise ValueError("完整动作帧包超过支持的画布或总像素范围。")
    expected_names = {f"{index:04d}.png" for index in range(count)}
    actual_names = {file.name for file in root.iterdir() if file.is_file() and file.suffix.lower() == ".png"}
    if actual_names != expected_names:
        raise ValueError("目录中的 PNG 帧文件与清单数量或文件名不一致。")
    if sum((root / name).stat().st_size for name in expected_names) > MAX_PACKAGE_BYTES:
        raise ValueError("动作帧目录总大小超过 256 MiB。")
    frames, durations, changed = [], [], 0
    try:
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or row.get("path") != f"{index:04d}.png":
                raise ValueError("动作帧包中的路径或帧序无效。")
            frame_path = root / row["path"]
            if frame_path.is_symlink() or frame_path.resolve().parent != root:
                raise ValueError("动作帧图路径越界。")
            duration = row.get("duration_ms")
            if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 60000:
                raise ValueError("动作帧包中的逐帧时长无效。")
            if not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
                raise ValueError("动作帧包的原帧哈希无效。")
            if not 0 < frame_path.stat().st_size <= MAX_PACKAGE_BYTES:
                raise ValueError("动作帧文件为空或过大。")
            with Image.open(frame_path) as image:
                if image.format != "PNG" or image.size != (width, height) or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("修改后的 PNG 需保持原画布尺寸，并为单张静态帧。")
                frames.append(image.convert("RGBA"))
            durations.append(float(duration))
            if isinstance(row.get("sha256"), str) and _digest(frame_path) != row["sha256"]:
                changed += 1
        declared_duration = metadata.get("duration_ms")
        if (type(declared_duration) not in (int, float) or not math.isfinite(declared_duration)
                or abs(sum(durations) - declared_duration) > max(0.01, declared_duration * 0.000001)):
            raise ValueError("动作帧包的总时长与逐帧时长不一致。")
        metadata["modified_frame_count"] = changed
        return frames, durations, metadata
    except Exception:
        for frame in frames:
            frame.close()
        raise


@_release_readers
def export_clip_frames(bundle_path: str | Path, asset_name: str, output_zip: str | Path, *,
                       bundle_name: str = "", role_id: int | None = None, identity: dict | None = None) -> dict:
    source = Path(bundle_path)
    destination = Path(output_zip)
    if source.resolve() == destination.resolve():
        raise ValueError("帧包输出路径不能覆盖原始资源包。")
    if role_id is not None and (type(role_id) is not int or not 1 <= role_id <= 2_147_483_647):
        raise ValueError("角色 ID 无效，无法标记帧包归属。")
    source_hash = _digest(source)
    frames, durations, info, origin = _read_clip(_load_bundle(source), asset_name)
    if not 1 <= len(frames) <= MAX_SOURCE_FRAMES or len(frames) != len(durations):
        raise ValueError("完整动作超过 512 帧或时长信息不完整，未导出抽样帧代替。")
    width, height = info["width"], info["height"]
    if (min(width, height) < 1 or max(width, height) > 16384 or width * height > MAX_FRAME_PIXELS
            or width * height * len(frames) > MAX_CLIP_PACKAGE_PIXELS):
        raise ValueError("完整动作超过单帧或 1.28 亿总像素范围，请选择尺寸更小的动作。")
    if any(not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0 for duration in durations):
        raise ValueError("原动作的逐帧时长无效。")
    ppu, left, top = origin
    metadata = {
        "schema": FRAME_PACKAGE_SCHEMA, "schema_version": 1,
        "source_bundle": bundle_name or source.name, "source_bundle_sha256": source_hash,
        "source_animation": asset_name, "role_id": role_id, "action": info.get("runtime_action") or asset_name,
        "runtime_action": info.get("runtime_action"),
        "runtime_binding_reason": info.get("runtime_binding_reason", ""),
        "identity": identity or {}, "frame_count": len(frames), "duration_ms": sum(durations),
        "sample_rate": info.get("sample_rate", info.get("fps")), "average_fps": info.get("fps"),
        "fps": info.get("sample_rate", info.get("fps")),
        "loop": bool(info.get("loop", False)),
        "canvas": {"width": width, "height": height, "pixels_per_unit": ppu,
                   "left": left, "top": top, "origin_pixels": [-left, top],
                   "pivot": [-left / width, 1 - top / height]},
        "frames": [],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".clip-frames-", suffix=".tmp", dir=destination.parent)
    os.close(handle)
    try:
        encoded_bytes = 0
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, image in enumerate(iter_clip_canvases(frames, info, origin)):
                try:
                    if index >= len(frames) or image.size != (width, height):
                        raise ValueError("全帧渲染的数量或尺寸与原动画不一致。")
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    payload = buffer.getvalue()
                finally:
                    image.close()
                encoded_bytes += len(payload)
                if encoded_bytes > MAX_PACKAGE_BYTES:
                    raise ValueError("PNG 帧总大小超过 256 MiB，未写入输出包。")
                relative = f"{index:04d}.png"
                metadata["frames"].append({"path": relative, "name": info["frame_names"][index],
                                           "duration_ms": durations[index], "sha256": hashlib.sha256(payload).hexdigest()})
                archive.writestr(relative, payload)
            if len(metadata["frames"]) != len(frames):
                raise ValueError("全帧渲染结果不完整。")
            archive.writestr("clip-frames.json", json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False))
            archive.writestr("README.txt", "原动画完整 PNG 帧包\n\n"
                             "帧文件按真实动作时间线导出，透明画布与脚底原点已统一。没有使用预览抽样图片。\n"
                             "clip-frames.json 保存原始精灵名、逐帧时长、角色归属与原资源哈希。\n"
                             "修改图片时请保留每张 PNG 的画布尺寸、透明区域和文件名。\n"
                             "纯 PNG 目录只能表示固定帧率，存在驻帧时需连同清单导入才能保持原时序。\n")
        if Path(temporary).stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError("帧包 ZIP 超过 256 MiB。")
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() or json.loads(archive.read("clip-frames.json")) != metadata:
                raise ValueError("导出帧包的清单或完整性回读失败。")
            for frame in metadata["frames"]:
                payload = archive.read(frame["path"])
                if hashlib.sha256(payload).hexdigest() != frame["sha256"]:
                    raise ValueError("导出帧图回读哈希不一致。")
                with Image.open(io.BytesIO(payload)) as image:
                    if image.format != "PNG" or image.size != (width, height):
                        raise ValueError("导出帧图回读格式或尺寸不一致。")
                    image.verify()
        if _digest(source) != source_hash:
            raise ValueError("原动画资源在导出期间发生变化，请重新导出。")
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"path": str(destination), "name": destination.name, "frame_count": len(frames),
            "duration_ms": sum(durations), "role_id": role_id, "action": metadata["action"],
            "source_animation": asset_name, "runtime_action": metadata["runtime_action"],
            "runtime_binding_reason": metadata["runtime_binding_reason"],
            "width": width, "height": height, "loop": metadata["loop"],
            "source_bundle_sha256": source_hash, "sha256": _digest(destination),
            "size": destination.stat().st_size, "notice": "已导出完整动作帧，清单保留真实逐帧时长与原点布局。"}
