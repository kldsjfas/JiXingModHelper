"""为已验证的 Unlit/Sequence 表情生成独立材质与透明序列图。"""
from __future__ import annotations

import bisect
import hashlib
import math
from pathlib import Path

import UnityPy
from PIL import Image

from .animation import _load_bundle, _preview, _read_replacement, _release_readers
from .emote_media import _fit_frame, adapt_video_source, detect_emote_media

MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_ATLAS_EDGE = 4096
MAX_ATLAS_PIXELS = 16_777_216
MAX_FRAMES = 120
MAX_DURATION_MS = 30_000
FRAME_PADDING = 1
# Steam 国服 Unlit/Sequence 的 D3D 字节码已经逐条核对过：
# X=行、Y=列、Z=帧率、W=帧数；从左上角逐行播放，并跳过边框像素。
# 只认已核对的实现，不能仅凭同名 Shader 猜测布局。
_SEQUENCE_SHADERS = {"7269575f8e9c2e8f1086a7dbdecc708529273f37b58ce726e19461dd09a656e0"}
_SEQUENCE_DESCRIPTION = "Rows(X) Columns(Y) FPS(Z) TotalFrame(W)"


def _properties(tree: dict, field: str) -> dict:
    values = tree.get("m_SavedProperties", {}).get(field)
    if not isinstance(values, list):
        raise ValueError("表情材质缺少可验证的属性表。")
    result = {}
    for key, value in values:
        if not isinstance(key, str) or key in result:
            raise ValueError("表情材质属性重名或格式不正确。")
        result[key] = value
    return result


def _sequence_layout(tree: dict, width: int, height: int) -> dict:
    sequence = _properties(tree, "m_Colors").get("_Sequence")
    if not isinstance(sequence, dict) or set(sequence) != {"r", "g", "b", "a"}:
        raise ValueError("表情材质缺少已验证的序列图参数。")
    try:
        rows, columns, fps, count = (float(sequence[key]) for key in ("r", "g", "b", "a"))
        padding = float(_properties(tree, "m_Floats")["_FramePaddingPixels"])
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("表情材质的序列图参数无效。") from exc
    if (not all(math.isfinite(value) for value in (rows, columns, fps, count, padding))
            or any(value != int(value) for value in (rows, columns, count, padding))
            or not 1 <= rows <= 128 or not 1 <= columns <= 128
            or not 2 <= count <= 512 or count > rows * columns or not 0 < fps <= 60
            or not 0 <= padding <= 16 or count * 1000 / fps > MAX_DURATION_MS
            or not 0 < width <= MAX_ATLAS_EDGE or not 0 < height <= MAX_ATLAS_EDGE
            or width * height > MAX_ATLAS_PIXELS or width % int(columns) or height % int(rows)):
        raise ValueError("表情序列图的尺寸、帧数或时长超出支持范围。")
    cell_width, cell_height = width // int(columns), height // int(rows)
    if min(cell_width, cell_height) <= padding * 2:
        raise ValueError("表情序列图边框占满了整个画面。")
    return {"rows": int(rows), "columns": int(columns), "fps": fps, "frames": int(count),
            "total": int(count), "padding": int(padding), "atlas_width": width, "atlas_height": height,
            "width": cell_width - int(padding) * 2, "height": cell_height - int(padding) * 2,
            "duration_ms": count * 1000 / fps}


def _shader_profile(shader) -> dict:
    if shader is None or shader.type.name != "Shader":
        raise ValueError("表情材质没有可读取的 Shader，未猜测共享依赖。")
    digest = hashlib.sha256(shader.get_raw_data()).hexdigest()
    if digest not in _SEQUENCE_SHADERS:
        raise ValueError("当前表情 Shader 尚未验证，暂不改写未知的序列图布局。")
    tree = shader.read_typetree()
    parsed = tree.get("m_ParsedForm", {})
    properties = parsed.get("m_PropInfo", {}).get("m_Props", [])
    sequence = [row for row in properties if row.get("m_Name") == "_Sequence"]
    if (parsed.get("m_Name") != "Unlit/Sequence" or len(sequence) != 1
            or sequence[0].get("m_Description") != _SEQUENCE_DESCRIPTION):
        raise ValueError("表情 Shader 与已验证的序列图参数不一致。")
    return {"shader_name": "Unlit/Sequence", "shader_sha256": digest}


def _inspect(environment, name: str):
    objects = list(environment.objects)
    materials = [obj for obj in objects if obj.type.name == "Material" and obj.peek_name() == name]
    textures = [obj for obj in objects if obj.type.name == "Texture2D"]
    bundles = [obj for obj in objects if obj.type.name == "AssetBundle"]
    shaders = [obj for obj in objects if obj.type.name == "Shader"]
    if (len(materials) != 1 or len(textures) != 1 or len(bundles) != 1 or len(shaders) > 1
            or len(objects) != 3 + len(shaders)):
        raise ValueError("表情包不是已验证的单材质、单图集布局。")
    material, texture, bundle = materials[0], textures[0], bundles[0]
    if material.assets_file is not texture.assets_file or bundle.assets_file is not material.assets_file:
        raise ValueError("表情对象分散在多份序列化文件中，暂不制作独立资源。")
    tree = material.read_typetree()
    maps = _properties(tree, "m_TexEnvs")
    base = maps.get("_BaseMap", {})
    if (base.get("m_Texture") != {"m_FileID": 0, "m_PathID": texture.path_id}
            or base.get("m_Scale") != {"x": 1.0, "y": 1.0}
            or base.get("m_Offset") != {"x": 0.0, "y": 0.0}
            or maps.get("_MainTex", {}).get("m_Texture") != {"m_FileID": 0, "m_PathID": 0}):
        raise ValueError("表情材质的贴图引用或坐标变换尚未验证。")
    pointer = tree.get("m_Shader", {})
    if pointer.get("m_FileID") == 0:
        shader = next((obj for obj in shaders if obj.path_id == pointer.get("m_PathID")), None)
    else:
        file_id = pointer.get("m_FileID")
        if (not isinstance(file_id, int) or not 0 < file_id <= len(material.assets_file.externals)
                or shaders):
            raise ValueError("表情材质的共享 Shader 引用不正确。")
        try:
            shader = material.read().m_Shader.deref()
        except Exception as exc:
            raise ValueError("无法核实表情的共享 Shader，已保留原依赖。") from exc
    shader_info = _shader_profile(shader)
    texture_tree = texture.read_typetree()
    profile = _sequence_layout(tree, texture_tree["m_Width"], texture_tree["m_Height"])
    cab = material.assets_file.name
    stream = texture_tree.get("m_StreamData") or {}
    stream_path = stream.get("path", "")
    expected_files = {cab}
    if stream_path:
        resource_name = cab + ".resS"
        if (stream_path != f"archive:/{cab}/{resource_name}" or stream.get("offset") != 0
                or not 0 < stream.get("size", 0) <= MAX_ATLAS_PIXELS * 4):
            raise ValueError("表情图集引用了未经验证的外部资源流。")
        expected_files.add(resource_name)
    if set(environment.file.files) != expected_files:
        raise ValueError("表情包含其它内部资源，暂不制作独立资源。")
    container = bundle.read_typetree().get("m_Container")
    if (not isinstance(container, list) or len(container) != 1
            or container[0][1].get("asset") != {"m_FileID": 0, "m_PathID": material.path_id}):
        raise ValueError("表情资源入口没有唯一指向当前材质。")
    profile.update(shader_info, asset_name=name, resource_type="UnityEngine.Material",
                   internal_id=container[0][0], serialized_cab=material.assets_file.name)
    return profile, material, texture, bundle


def _checked_environment(path: str | Path):
    source = Path(path)
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_BUNDLE_BYTES:
        raise ValueError("表情资源包为空或超过 128 MB。")
    from .workshop_security import inspect_resource_bundle
    with source.open("rb") as stream:
        inspect_resource_bundle(stream, source.stat().st_size, max_expanded_bytes=96 * 1024 * 1024)
    return _load_bundle(source)


@_release_readers
def inspect_material_emote_bundle(path: str | Path, name: str) -> dict:
    profile, _, _, _ = _inspect(_checked_environment(path), name)
    return profile


def _frames_from_atlas(atlas: Image.Image, profile: dict):
    cell_width = profile["atlas_width"] // profile["columns"]
    cell_height = profile["atlas_height"] // profile["rows"]
    padding = profile["padding"]
    for index in range(profile["frames"]):
        # Shader 的 V 坐标先翻到最上行；Pillow 图像本身已是左上原点。
        left = (index % profile["columns"]) * cell_width + padding
        top = (index // profile["columns"]) * cell_height + padding
        yield atlas.crop((left, top, left + profile["width"], top + profile["height"]))


@_release_readers
def read_material_emote_preview(path: str | Path, name: str) -> dict:
    profile, _, texture, _ = _inspect(_checked_environment(path), name)
    atlas = texture.read().image.convert("RGBA")
    indices = list(range(min(profile["frames"], MAX_FRAMES)))
    frames = _frames_from_atlas(atlas, profile)
    # 原版超过 120 帧时均匀取样；新制作的资源本身至多 120 帧。
    if profile["frames"] > MAX_FRAMES:
        wanted = {index * profile["frames"] // MAX_FRAMES for index in indices}
        frames = (frame for index, frame in enumerate(frames) if index in wanted)
    durations = [profile["duration_ms"] / len(indices)] * len(indices)
    return _preview(frames, [f"第 {index + 1} 帧" for index in indices], durations, profile["frames"],
                    info={**profile, "animation_kind": "material", "editable": True,
                          "notice": "从最终材质图集回读；游戏内播放与显示仍需实际测试。"})


def _output_plan(width: int, height: int, duration_ms: float) -> dict:
    if (isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int)
            or not isinstance(height, int) or not 0 < width <= MAX_ATLAS_EDGE
            or not 0 < height <= MAX_ATLAS_EDGE):
        raise ValueError("表情画面尺寸无效。")
    if not math.isfinite(duration_ms) or not 1000 / 30 <= duration_ms <= MAX_DURATION_MS:
        raise ValueError("表情素材时长须在 1/30 秒到 30 秒之间。")
    count = max(2, min(MAX_FRAMES, round(duration_ms * 30 / 1000)))
    columns = max(1, min(count, math.ceil(math.sqrt(count * height / width))))
    rows = math.ceil(count / columns)
    scale = min(1.0, 512 / max(width, height),
                (MAX_ATLAS_EDGE / columns - 2 * FRAME_PADDING) / width,
                (MAX_ATLAS_EDGE / rows - 2 * FRAME_PADDING) / height)
    output_width, output_height = max(1, int(width * scale)), max(1, int(height * scale))
    atlas_width = columns * (output_width + 2 * FRAME_PADDING)
    atlas_height = rows * (output_height + 2 * FRAME_PADDING)
    if atlas_width * atlas_height > MAX_ATLAS_PIXELS or max(atlas_width, atlas_height) > MAX_ATLAS_EDGE:
        raise ValueError("无法在允许的图集尺寸内容纳表情。")
    return {"width": output_width, "height": output_height, "fps": count * 1000 / duration_ms,
            "frames": count, "rows": rows, "columns": columns,
            "atlas_width": atlas_width, "atlas_height": atlas_height, "padding": FRAME_PADDING,
            "duration_ms": duration_ms}


def _resample(frames: list, durations: list, plan: dict) -> list:
    if len(frames) != len(durations) or len(frames) < 2:
        raise ValueError("表情素材至少需要两帧有效画面。")
    ends, elapsed = [], 0.0
    for duration in durations:
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise ValueError("表情素材包含无效的帧时长。")
        elapsed += duration
        ends.append(elapsed)
    output = []
    for index in range(plan["frames"]):
        position = index * elapsed / plan["frames"]
        source_index = min(len(frames) - 1, bisect.bisect_right(ends, position))
        output.append(_fit_frame(frames[source_index], plan["width"], plan["height"]))
    return output


def _sprite_sheet(frames: list, plan: dict) -> Image.Image:
    atlas = Image.new("RGBA", (plan["atlas_width"], plan["atlas_height"]), (0, 0, 0, 0))
    for index, frame in enumerate(frames):
        left = index % plan["columns"] * (plan["width"] + 2 * FRAME_PADDING) + FRAME_PADDING
        top = index // plan["columns"] * (plan["height"] + 2 * FRAME_PADDING) + FRAME_PADDING
        atlas.paste(frame, (left, top))
    return atlas


@_release_readers
def prepare_material_emote(source: str | Path, reference_bundle: str | Path, template_name: str,
                           output_bundle: str | Path, *, emote_id: int, work_dir: str | Path) -> dict:
    from .emote_addressables import _write_new_file, independent_emote_identity
    identity = independent_emote_identity(emote_id)
    destination = Path(output_bundle)
    if destination.name != identity["bundle_name"] or destination.exists():
        raise ValueError("独立表情资源包目标名称不正确或文件已存在。")
    environment = _checked_environment(reference_bundle)
    reference, material, texture, bundle = _inspect(environment, template_name)
    source_kind = detect_emote_media(source)
    if source_kind == "usm":
        raise ValueError("当前游戏表情使用材质序列图，请导入 GIF、APNG、动画 WebP 或普通视频。")
    if source_kind == "video":
        frames, video_info = adapt_video_source(source, reference, work_dir=work_dir,
            output_planner=lambda duration: _output_plan(reference["width"], reference["height"], duration))
        plan = _output_plan(reference["width"], reference["height"], video_info["source_duration_ms"])
        if len(frames) != plan["frames"]:
            raise ValueError("视频输出帧数与序列图规划不一致。")
    else:
        frames, durations, source_kind = _read_replacement(source)
        plan = _output_plan(reference["width"], reference["height"], sum(durations))
        frames = _resample(frames, durations, plan)
        video_info = {"format": source_kind, "source_duration_ms": sum(durations),
                      "duration_ms": plan["duration_ms"], "fps": plan["fps"], "frames": len(frames)}
    atlas = _sprite_sheet(frames, plan)
    original_objects = {obj.path_id: obj.get_raw_data() for obj in environment.objects}
    original_externals = [(item.path, item.type, item.guid) for item in material.assets_file.externals]
    tree = material.read_typetree()
    tree["m_Name"] = identity["video_name"]
    tree["m_SavedProperties"]["m_Colors"] = [(name, {"r": float(plan["rows"]), "g": float(plan["columns"]),
        "b": plan["fps"], "a": float(len(frames))} if name == "_Sequence" else value)
        for name, value in tree["m_SavedProperties"]["m_Colors"]]
    tree["m_SavedProperties"]["m_Floats"] = [(name, float(FRAME_PADDING) if name == "_FramePaddingPixels"
        else 0.0 if name == "_StartTime" else value) for name, value in tree["m_SavedProperties"]["m_Floats"]]
    material.save_typetree(tree)
    image = texture.read()
    old_stream = image.m_StreamData.path if image.m_StreamData is not None else ""
    image.m_Name = identity["video_name"]
    image.set_image(atlas, target_format=4, mipmap_count=1)
    image.save()
    bundle_tree = bundle.read_typetree()
    bundle_tree["m_Name"] = identity["bundle_name"]
    bundle_tree["m_AssetBundleName"] = identity["bundle_name"]
    bundle_tree["m_Container"][0] = (identity["internal_id"], bundle_tree["m_Container"][0][1])
    bundle.save_typetree(bundle_tree)
    old_cab = material.assets_file.name
    content_hash = hashlib.sha256(atlas.tobytes() + repr(plan).encode()).hexdigest()
    new_cab = "CAB-" + hashlib.sha256((identity["video_name"] + content_hash).encode()).hexdigest()[:32]
    expected_files = {old_cab}
    if old_stream:
        expected_files.add(old_stream.rsplit("/", 1)[-1])
    if set(environment.file.files) != expected_files or new_cab == old_cab:
        raise ValueError("表情包含有其它内部资源，无法安全建立独立身份。")
    # 新图集已经内嵌；移除只被旧图集使用的资源流，避免携带原图副本。
    if old_stream:
        environment.file.files.pop(old_stream.rsplit("/", 1)[-1])
    serialized = environment.file.files.pop(old_cab)
    serialized.name = new_cab
    environment.file.files[new_cab] = serialized
    payload = environment.file.save(packer="lz4")
    if len(payload) > MAX_BUNDLE_BYTES:
        raise ValueError("独立表情资源包超过 128 MB。")
    restored = UnityPy.load(payload)
    saved, saved_material, saved_texture, saved_bundle = _inspect(restored, identity["video_name"])
    if (saved["serialized_cab"] != new_cab or saved["internal_id"] != identity["internal_id"]
            or saved["frames"] != len(frames) or not math.isclose(saved["fps"], plan["fps"], rel_tol=1e-6)
            or saved_texture.read().image.convert("RGBA").tobytes() != atlas.tobytes()):
        raise ValueError("独立表情的身份、时序或图集像素回读失败。")
    changed = {material.path_id, texture.path_id, bundle.path_id}
    if ({obj.path_id for obj in restored.objects} != set(original_objects)
            or any(obj.get_raw_data() != original_objects[obj.path_id]
                   for obj in restored.objects if obj.path_id not in changed)
            or [(item.path, item.type, item.guid) for item in saved_material.assets_file.externals] != original_externals):
        raise ValueError("制作表情改变了其它对象或共享引用。")
    if saved_bundle.read_typetree()["m_Name"] != identity["bundle_name"]:
        raise ValueError("独立表情的资源包名称回读失败。")
    _write_new_file(destination, payload)
    try:
        animation = read_material_emote_preview(destination, identity["video_name"])
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    notice = "已生成独立的透明序列图表情，不占用原表情资源；按素材时长重采样，最多 120 帧。游戏内效果仍需测试。"
    return {**identity, "path": destination, "animation": animation, "size": len(payload),
            "bundle_size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "serialized_cab": new_cab,
            "notice": notice, "video_info": video_info, "resource_type": "UnityEngine.Material",
            "emote_id": emote_id, "template_video_key": template_name, "independent": True}
