"""Replace verified rectangular Sprite frames while preserving their atlas neighbours."""
from __future__ import annotations

import math

from PIL import Image
from UnityPy.enums import SpriteMeshType, SpritePackingMode, SpritePackingRotation
from UnityPy.export.SpriteHelper import SpriteSettings


NOTICE = "矩形 Sprite 帧按原图集位置写入，保留原帧数、尺寸、引用和播放时序。共享这些帧的动作会一起改变。"


def _key(reader) -> tuple[str, int]:
    return reader.assets_file.name, reader.path_id


def _rectangle(rect) -> tuple[int, int, int, int]:
    values = [float(getattr(rect, field)) for field in ("x", "y", "width", "height")]
    if any(not math.isfinite(value) or abs(value - round(value)) > 0.001 for value in values):
        raise ValueError("Sprite 图集区域包含非整数坐标，暂不能安全替换。")
    x, y, width, height = map(round, values)
    if min(x, y) < 0 or min(width, height) <= 0:
        raise ValueError("Sprite 图集区域无效。")
    return x, y, width, height


def _overlaps(first, second) -> bool:
    x, y, width, height = first
    other_x, other_y, other_width, other_height = second
    return (x < other_x + other_width and other_x < x + width
            and y < other_y + other_height and other_y < y + height)


def _render_data(sprite):
    # The existing preview resolves SpriteAtlas overrides in the same way as UnityPy.
    from .clip_preview import _render_data as resolve_render_data
    return resolve_render_data(sprite)


def inspect_layout(env, rows) -> dict:
    """Reject ambiguous pixels before decoding a replacement or changing any object."""
    local_objects = {_key(obj): obj for obj in env.objects}
    frames = []
    atlases = {}
    selected = {_key(obj) for _, obj, _ in rows}
    for name, obj, sprite in rows:
        if obj.type.name != "Sprite":
            raise ValueError("混合 Sprite 与独立贴图的序列暂不能整体替换。")
        data = _render_data(sprite)
        settings = SpriteSettings(data.settingsRaw)
        if settings.packingRotation != SpritePackingRotation.kSPRNone:
            raise ValueError(f"Sprite「{name}」使用了旋转或翻转图集，暂不能替换。")
        if (settings.packingMode != SpritePackingMode.kSPMRectangle
                or settings.meshType != SpriteMeshType.kSpriteMeshTypeFullRect):
            raise ValueError(f"Sprite「{name}」使用了紧密裁剪或网格，暂不能替换。")
        if getattr(getattr(data, "alphaTexture", None), "m_PathID", 0):
            raise ValueError(f"Sprite「{name}」使用独立透明度图集，暂不能替换。")
        rect = _rectangle(data.textureRect)
        offset = data.textureRectOffset
        dimensions = [float(offset.x), float(offset.y), float(sprite.m_Rect.width), float(sprite.m_Rect.height)]
        expected = (0, 0, rect[2], rect[3])
        if any(not math.isfinite(value) or abs(value - wanted) > 0.001
               for value, wanted in zip(dimensions, expected)):
            raise ValueError(f"Sprite「{name}」的画面被裁剪，暂不能替换。")
        texture_reader = data.texture.deref()
        atlas_key = _key(texture_reader)
        if (texture_reader.type.name != "Texture2D" or atlas_key not in local_objects
                or texture_reader.assets_file is not local_objects[atlas_key].assets_file):
            raise ValueError(f"Sprite「{name}」的图集在其他资源包内，暂不能替换。")
        if atlas_key not in atlases:
            texture = texture_reader.read()
            if (texture.m_MipCount != 1 or texture.m_ImageCount != 1
                    or texture.m_TextureDimension != 2):
                raise ValueError("Sprite 图集包含多级纹理或特殊维度，暂不能保持其它像素完整。")
            atlases[atlas_key] = {"reader": texture_reader, "texture": texture,
                                  "size": (int(texture.m_Width), int(texture.m_Height))}
        width, height = atlases[atlas_key]["size"]
        if min(width, height) <= 0 or width * height > 16_777_216:
            raise ValueError("Sprite 图集尺寸无效或超过 1600 万像素，暂不能替换。")
        x, y, frame_width, frame_height = rect
        if x + frame_width > width or y + frame_height > height:
            raise ValueError(f"Sprite「{name}」的图集区域越界。")
        if any(frame["atlas"] == atlas_key and _overlaps(frame["rect"], rect) for frame in frames):
            raise ValueError("所选 Sprite 帧共用了重叠图集区域，暂不能分别替换。")
        frames.append({"name": name, "atlas": atlas_key, "rect": rect,
                       "box": (x, height - y - frame_height, x + frame_width, height - y),
                       "size": (frame_width, frame_height)})

    if sum(width * height for width, height in (atlas["size"] for atlas in atlases.values())) > 64_000_000:
        raise ValueError("Sprite 图集总像素超过 6400 万，暂不能替换。")

    # Unknown/overlapping neighbours cannot be safely preserved by painting a rectangle.
    for obj in env.objects:
        if obj.type.name != "Sprite" or _key(obj) in selected:
            continue
        try:
            data = _render_data(obj.read())
            atlas_keys = {_key(data.texture.deref())}
            if getattr(getattr(data, "alphaTexture", None), "m_PathID", 0):
                atlas_keys.add(_key(data.alphaTexture.deref()))
            affected = atlas_keys & atlases.keys()
            if not affected:
                continue
            rect = _rectangle(data.textureRect)
        except Exception as exc:
            raise ValueError("其他 Sprite 的图集引用无法核对，暂不能安全替换此序列。") from exc
        if any(frame["atlas"] in affected and _overlaps(frame["rect"], rect) for frame in frames):
            raise ValueError(f"Sprite「{obj.peek_name()}」与所选帧共用图集区域，暂不能单独替换。")

    # An atlas can carry additional Sprite definitions whose Sprite object is in another bundle.
    for obj in env.objects:
        if obj.type.name != "SpriteAtlas":
            continue
        selected_keys = []
        for _, _, sprite in rows:
            pointer = getattr(sprite, "m_SpriteAtlas", None)
            if pointer and _key(pointer.deref()) == _key(obj):
                selected_keys.append(sprite.m_RenderDataKey)
            elif not pointer and obj.peek_name() in (getattr(sprite, "m_AtlasTags", None) or []):
                selected_keys.append(sprite.m_RenderDataKey)
        for render_key, data in obj.read().m_RenderDataMap:
            if render_key in selected_keys:
                continue
            atlas_keys = {_key(data.texture.deref())}
            if getattr(getattr(data, "alphaTexture", None), "m_PathID", 0):
                atlas_keys.add(_key(data.alphaTexture.deref()))
            affected = atlas_keys & atlases.keys()
            if affected:
                rect = _rectangle(data.textureRect)
                if any(frame["atlas"] in affected and _overlaps(frame["rect"], rect) for frame in frames):
                    raise ValueError("SpriteAtlas 内还有其它帧共用所选区域，暂不能单独替换。")
    return {"frames": frames, "atlases": atlases}


def frame_sizes(layout) -> dict[str, tuple[int, int]]:
    return {frame["name"]: frame["size"] for frame in layout["frames"]}


def atlas_images(layout) -> dict:
    return {key: atlas["texture"].image.convert("RGBA") for key, atlas in layout["atlases"].items()}


def write_frames(layout, images, frames) -> None:
    for target, replacement in zip(layout["frames"], frames, strict=True):
        fitted = replacement.resize(target["size"], Image.Resampling.LANCZOS)
        images[target["atlas"]].paste(fitted, target["box"])
    for key, image in images.items():
        texture = layout["atlases"][key]["texture"]
        # Recompressing a shared atlas would change neighbouring pixels as well.
        texture.set_image(image, target_format=4, mipmap_count=1)
        texture.save()


def object_snapshot(env, excluded) -> dict:
    return {_key(obj): obj.get_raw_data() for obj in env.objects if _key(obj) not in excluded}


def validate_write(env, rows, expected_layout, expected_images, unchanged_objects) -> None:
    layout = inspect_layout(env, rows)
    if layout["frames"] != expected_layout["frames"] or layout["atlases"].keys() != expected_images.keys():
        raise ValueError("Sprite 写回后图集布局发生变化，未保存作品集。")
    if object_snapshot(env, expected_images) != unchanged_objects:
        raise ValueError("Sprite 写回改变了其它对象，未保存作品集。")
    for key, expected in expected_images.items():
        restored = layout["atlases"][key]["texture"].image.convert("RGBA")
        if restored.size != expected.size or restored.tobytes() != expected.tobytes():
            raise ValueError("Sprite 图集像素回读不一致，未保存作品集。")


def restore_frames(layout, original_layout) -> dict:
    sizes = {key: atlas["size"] for key, atlas in layout["atlases"].items()}
    original_sizes = {key: atlas["size"] for key, atlas in original_layout["atlases"].items()}
    if layout["frames"] != original_layout["frames"] or sizes != original_sizes:
        raise ValueError("原版与草稿的 Sprite 图集布局不同，未执行恢复。")
    images = atlas_images(layout)
    source_images = atlas_images(original_layout)
    for target in layout["frames"]:
        patch = source_images[target["atlas"]].crop(target["box"])
        images[target["atlas"]].paste(patch, target["box"])
    for key, image in images.items():
        texture = layout["atlases"][key]["texture"]
        texture.set_image(image, target_format=4, mipmap_count=1)
        texture.save()
    return images
