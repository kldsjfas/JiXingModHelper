"""制作 mod：替换贴图/文本、打包成可分享作品集。"""
from __future__ import annotations

import gc
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import UnityPy
from PIL import Image


def _load_bundle(bundle_path: str | Path):
    # File-backed UnityPy readers can outlive a preview through reference cycles.
    # Read into memory so a second edit can atomically replace the same draft on
    # Windows; retain the parent directory for external resource resolution.
    path = Path(bundle_path)
    return UnityPy.load(path.read_bytes(), path=str(path.parent))


def replace_bundle_texture(
    src_bundle: str | Path,
    image_path: str | Path,
    out_bundle: str | Path,
    target_name: str | None = None,
    match_original_size: bool = True,
    crop_box: tuple[int, int, int, int] | None = None,
) -> str:
    """替换贴图。crop_box 为源图 (l,t,r,b) 像素裁剪，先裁再缩放。"""
    env = _load_bundle(src_bundle)
    new_image = Image.open(image_path).convert("RGBA")
    obj = data = image = None
    try:
        if crop_box:
            new_image = new_image.crop(crop_box)
        replaced: str | None = None

        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            data = obj.read()
            name = str(getattr(data, "m_Name", "") or "")
            if target_name and name != target_name:
                continue

            image = new_image
            if match_original_size:
                width = int(getattr(data, "m_Width", 0) or 0)
                height = int(getattr(data, "m_Height", 0) or 0)
                if width > 0 and height > 0 and (width, height) != new_image.size:
                    image = new_image.resize((width, height), Image.LANCZOS)

            try:
                data.image = image
            except Exception:
                data.set_image(image)
            data.save()
            replaced = name
            break

        if replaced is None:
            raise RuntimeError("这个资源包里没有可替换的贴图。")

        out = Path(out_bundle)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(env.file.save())
        return replaced
    finally:
        obj = data = image = new_image = None
        env = None
        gc.collect()


def replace_bundle_texture_from_bundle(
    src_bundle: str | Path,
    source_bundle: str | Path,
    target_name: str,
    source_name: str,
    out_bundle: str | Path,
) -> str:
    """把 source_bundle 中 source_name 的贴图像素复制到 src_bundle 的 target_name。

    用于“_sfw -> 去掉 _sfw”的快捷贴图替换：`_sfw` 与无后缀贴图可能不在同一个
    资源包里，因此允许从另一个 bundle 取源图。保留 target 的对象名。
    """
    source_env = _load_bundle(source_bundle)
    source_image = None
    for obj in source_env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            data = obj.read()
        except Exception:
            continue
        name = str(getattr(data, "m_Name", "") or "")
        if name == source_name:
            source_image = getattr(data, "image", None)
            break

    if source_image is None:
        raise RuntimeError(f"找不到源贴图：{source_name}（来源包 {Path(source_bundle).name}）")

    env = _load_bundle(src_bundle)
    target_obj = None
    target_data = None
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            data = obj.read()
        except Exception:
            continue
        name = str(getattr(data, "m_Name", "") or "")
        if name == target_name:
            target_obj = obj
            target_data = data
            break

    if target_obj is None or target_data is None:
        raise RuntimeError(f"找不到目标贴图：{target_name}（目标包 {Path(src_bundle).name}）")

    image = source_image
    width = int(getattr(target_data, "m_Width", 0) or 0)
    height = int(getattr(target_data, "m_Height", 0) or 0)
    if width > 0 and height > 0 and image.size != (width, height):
        image = image.resize((width, height), Image.LANCZOS)

    try:
        target_data.image = image
    except Exception:
        target_data.set_image(image)
    target_data.save()

    out = Path(out_bundle)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(env.file.save())
    return target_name


def _text_asset_raw(data) -> bytes | str:
    raw = getattr(data, "m_Script", None)
    if raw is None:
        raw = getattr(data, "script", b"")
    return raw


def decode_text_asset_raw(raw) -> tuple[str | None, str]:
    """Compatibility reader. Only safe editable resources are returned as text."""
    from .text_assets import inspect_text

    document = inspect_text(raw)
    if document.editable:
        return document.text, "text"
    return None, "fgui" if document.format == "fairygui" else "binary"


def is_readable_text_asset(data) -> bool:
    from .text_assets import inspect_text

    return inspect_text(_text_asset_raw(data)).editable


def list_text_assets(bundle_path: str | Path) -> list[tuple[str, str]]:
    """List editable plain text and recognized FairyGUI component text."""
    env = _load_bundle(bundle_path)
    result: list[tuple[str, str]] = []
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        try:
            data = obj.read()
            text, kind = decode_text_asset_raw(_text_asset_raw(data))
        except Exception:
            continue
        if kind == "text" and text is not None:
            name = str(getattr(data, "m_Name", "") or "(未命名)")
            result.append((name, text[:120].replace("\n", " ").replace("\r", "")))
    return result


def _find_text_asset(env, asset_name: str):
    matches = []
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        if str(getattr(data, "m_Name", "") or "") == asset_name:
            matches.append(data)
    if not matches:
        raise RuntimeError(f"找不到文本资源：{asset_name}")
    if len(matches) != 1:
        raise RuntimeError(f"此包有多个同名文本「{asset_name}」，不能安全确定目标。")
    return matches[0]


def read_text_asset_info(bundle_path: str | Path, asset_name: str) -> dict:
    """Return text, edit capability and encoding/format hints for the editor."""
    from .text_assets import inspect_text

    env = _load_bundle(bundle_path)
    data = _find_text_asset(env, asset_name)
    return inspect_text(_text_asset_raw(data)).info()


def read_text_asset(bundle_path: str | Path, asset_name: str) -> str:
    info = read_text_asset_info(bundle_path, asset_name)
    if not info["editable"]:
        raise RuntimeError(info["reason"])
    return info["text"]


def replace_bundle_text(
    src_bundle: str | Path,
    asset_name: str,
    new_text: str,
    out_bundle: str | Path,
    *,
    reference_bundle: str | Path | None = None,
) -> str:
    """Preserve encoding and validate a complete bundle before atomic replacement."""
    from .text_assets import replacement_bytes

    env = _load_bundle(src_bundle)
    data = _find_text_asset(env, asset_name)
    reference = _load_bundle(reference_bundle) if reference_bundle is not None else env
    # The saved draft may resemble JSON/XML or contain an expanded FGUI table.
    # Keep the original document's format, encoding and table as the baseline.
    raw = _text_asset_raw(_find_text_asset(reference, asset_name))
    encoded = replacement_bytes(raw, new_text)
    _write_text_bytes(env, data, asset_name, encoded, out_bundle)
    return asset_name


def restore_bundle_text(
    draft_bundle: str | Path,
    original_bundle: str | Path,
    asset_name: str,
) -> None:
    """Restore exact TextAsset bytes while preserving other draft objects."""
    from .text_assets import script_bytes

    env = _load_bundle(draft_bundle)
    data = _find_text_asset(env, asset_name)
    original = _load_bundle(original_bundle)
    encoded = script_bytes(_text_asset_raw(_find_text_asset(original, asset_name)))
    _write_text_bytes(env, data, asset_name, encoded, draft_bundle)


def _write_text_bytes(env, data, asset_name: str, encoded: bytes, out_bundle: str | Path) -> None:
    import os
    import tempfile
    from .text_assets import script_bytes

    raw = _text_asset_raw(data)
    field = "m_Script" if hasattr(data, "m_Script") else "script" if hasattr(data, "script") else None
    if field is None:
        raise RuntimeError("无法写入 TextAsset 字段。")
    # UnityPy 1.25 TextAsset uses surrogateescape for arbitrary script bytes.
    payload = encoded.decode("utf-8", errors="surrogateescape") if isinstance(raw, str) else encoded
    setattr(data, field, payload)
    data.save()
    written = env.file.save()
    checked = UnityPy.load(written)
    saved_data = _find_text_asset(checked, asset_name)
    if script_bytes(_text_asset_raw(saved_data)) != encoded:
        raise RuntimeError("文本资源回读校验失败，原文件未修改。")
    out = Path(out_bundle)
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=out.name + ".", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(written)
        os.replace(temp_name, out)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def find_anim_preview_texture(bundle_path: str | Path, anim_name: str) -> str | None:
    """在同包里找动画对应的预览贴图（常见 Walk-001 / Idle_00000）。"""
    env = _load_bundle(bundle_path)
    tex_names: list[str] = []
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            data = obj.read()
        except Exception:
            continue
        n = str(getattr(data, "m_Name", "") or "")
        if n:
            tex_names.append(n)
    if not tex_names:
        return None
    name_set = set(tex_names)
    for cand in (
        f"{anim_name}-001",
        f"{anim_name}_00000",
        f"{anim_name}_0",
        f"{anim_name}-1",
        f"{anim_name}_001",
        anim_name,
    ):
        if cand in name_set:
            return cand
    prefixed = sorted(
        t
        for t in tex_names
        if t.startswith(anim_name + "_") or t.startswith(anim_name + "-") or t.startswith(anim_name)
    )
    return prefixed[0] if prefixed else None


def replace_bundle_animation_raw(
    src_bundle: str | Path,
    asset_name: str,
    raw_bytes: bytes,
    out_bundle: str | Path,
) -> str:
    """用原始字节替换 AnimationClip（需同源结构，风险自担）。"""
    env = _load_bundle(src_bundle)
    replaced = None
    for obj in env.objects:
        if obj.type.name != "AnimationClip":
            continue
        try:
            data = obj.read()
        except Exception:
            continue
        name = str(getattr(data, "m_Name", "") or "")
        if name != asset_name:
            continue
        try:
            obj.set_raw_data(raw_bytes)
        except Exception as exc:
            raise RuntimeError(f"写入动画字节失败：{exc}") from exc
        replaced = name
        break
    if not replaced:
        raise RuntimeError(f"找不到动画：{asset_name}")
    out = Path(out_bundle)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(env.file.save())
    return replaced




def export_mod_pack(
    pack_dir: str | Path,
    zip_path: str | Path | None = None,
    *,
    pack_name: str,
    items: list[dict],
) -> Path:
    """把作品集目录打成 zip（可选），并写 mod_info.json 方便分享。"""
    pack_dir = Path(pack_dir)
    pack_dir.mkdir(parents=True, exist_ok=True)
    info = {
        "name": pack_name,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "tool": "AstralParty Mod Helper",
        "items": items,
        "bundle_count": len(list(pack_dir.glob("*.bundle"))),
    }
    (pack_dir / "mod_info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if zip_path is None:
        return pack_dir
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in pack_dir.rglob("*"):
            if f.is_file():
                zf.write(f, arcname=f.relative_to(pack_dir).as_posix())
    return zip_path


def copy_into_pack(src_bundle: Path, pack_dir: Path) -> Path:
    pack_dir = Path(pack_dir)
    pack_dir.mkdir(parents=True, exist_ok=True)
    dest = pack_dir / src_bundle.name
    shutil.copy2(src_bundle, dest)
    return dest
