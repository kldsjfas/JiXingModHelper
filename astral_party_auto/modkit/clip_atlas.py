"""AnimationClip 关联图集替换：只接收原尺寸、原布局的完整静态图集。"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from .animation import MAX_FILE_BYTES, _atomic_save, _load_bundle, _release_readers, _set_image, _size_limit
from .clip_preview import _read_clip, _render_data


def _atlas_details(env, name: str):
    # Capture the bundle's own objects before dereferencing external pointers.
    local_objects = {(id(obj.assets_file), obj.path_id) for obj in env.objects}
    frames, _, _, _ = _read_clip(env, name)
    textures = {}
    for frame in frames:
        if frame is None:
            continue
        render_data = _render_data(frame.sprite)
        if getattr(getattr(render_data, "alphaTexture", None), "m_PathID", 0):
            raise ValueError("此动画还使用独立透明度图集，不能只用一张图片替换；请使用同源 .animbin。")
        pointer = render_data.texture
        reader = pointer.deref()
        key = (id(reader.assets_file), reader.path_id)
        if reader.type.name != "Texture2D" or key not in local_objects:
            raise ValueError("动画图集不在当前资源包内，不能直接用图片替换；请使用同源 .animbin。")
        if key not in textures:
            textures[key] = reader.read()
    if len(textures) != 1:
        raise ValueError(
            f"此动画关联 {len(textures)} 张图集，不能用一张图片替换整个动画；请使用同源 .animbin。"
        )
    texture = next(iter(textures.values()))
    atlas_name = str(texture.m_Name or "")
    matching = [obj for obj in env.objects if obj.type.name == "Texture2D" and obj.peek_name() == atlas_name]
    if not atlas_name or len(matching) != 1:
        raise ValueError("动画图集名称为空或不唯一，不能安全替换；请使用同源 .animbin。")
    return texture, {
        "atlas_name": atlas_name,
        "atlas_width": int(texture.m_Width),
        "atlas_height": int(texture.m_Height),
        "atlas_editable": True,
        "atlas_reason": "请使用原布局的完整静态图集，不能使用角色单帧或帧目录。",
    }


@_release_readers
def inspect_clip_atlas(bundle_path: str | Path, name: str) -> dict:
    try:
        _, details = _atlas_details(_load_bundle(bundle_path), name)
        return details
    except Exception as exc:
        return {"atlas_name": "", "atlas_width": 0, "atlas_height": 0,
                "atlas_editable": False, "atlas_reason": str(exc)}


def _read_atlas_image(path: str | Path, details: dict) -> Image.Image:
    source = Path(path)
    if not source.is_file() or source.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("请选择不超过 64 MB 的完整静态图集文件，不能选择帧目录。")
    with Image.open(source) as image:
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("图集替换只能导入静态图片，不能导入 GIF、APNG 或 WebP 多帧动画。")
        if image.format not in {"PNG", "JPEG", "WEBP", "BMP"}:
            raise ValueError("完整静态图集支持 PNG、JPG、WebP 或 BMP，建议使用透明 PNG。")
        expected = (details["atlas_width"], details["atlas_height"])
        _size_limit(*expected)
        if image.size != expected:
            raise ValueError(
                f"此动画需要整张图集「{details['atlas_name']}」({expected[0]} × {expected[1]})，"
                f"所选图片是 {image.width} × {image.height}。不能使用角色单帧，"
                "请在「浏览资源 → 贴图」导出同名图集，按原布局修改后导入。"
            )
        return image.convert("RGBA")


@_release_readers
def replace_clip_atlas_checked(src_bundle: str | Path, name: str, image_path: str | Path,
                               out_bundle: str | Path) -> str:
    """预览和保存共用校验；拒绝导入时不创建或改写输出包。"""
    env = _load_bundle(src_bundle)
    texture, details = _atlas_details(env, name)
    image = _read_atlas_image(image_path, details)
    try:
        _set_image(texture, image)
    finally:
        image.close()

    def validate(saved):
        saved_texture, saved_details = _atlas_details(saved, name)
        if saved_details != details or saved_texture.image.size != (details["atlas_width"], details["atlas_height"]):
            raise ValueError("图集写回校验失败，作品集未修改。")

    _atomic_save(env, out_bundle, validate)
    return details["atlas_name"]
