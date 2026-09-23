"""从 bundle 导出资源：贴图 / 文本 / 网格 / 动画。"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import UnityPy
from PIL import Image

from .bundles import extract_texture_png, read_bundle_asset_names
from .dynamic import (
    find_sequence_preview_texture,
    sequence_groups_from_names,
    sorted_sequence_names,
    text_asset_bytes,
)


def _safe_stem(name: str) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (name or "asset").strip()) or "asset"
    return s[:80]


def _find_object(env, unity_types: set[str], target_name: str):
    for obj in env.objects:
        if obj.type.name not in unity_types:
            continue
        try:
            data = obj.read()
        except Exception:
            continue
        name = str(getattr(data, "m_Name", "") or getattr(data, "name", "") or "")
        if name == target_name:
            return obj, data
    return None, None


def export_texture(
    bundle_path: str | Path,
    asset_name: str,
    dest: str | Path,
    *,
    fmt: str = "png",
    quality: int = 92,
) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fmt_u = fmt.upper().replace("JPG", "JPEG")
    if fmt_u not in ("PNG", "JPEG"):
        raise RuntimeError("贴图只支持 png 或 jpg。")

    tmp = dest.with_suffix(".tmp.png")
    info = extract_texture_png(bundle_path, tmp, target_name=asset_name)
    if not info:
        raise RuntimeError(f"找不到贴图：{asset_name}")

    img = Image.open(tmp).convert("RGBA")
    try:
        if fmt_u == "JPEG":
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            if dest.suffix.lower() not in (".jpg", ".jpeg"):
                dest = dest.with_suffix(".jpg")
            bg.save(dest, format="JPEG", quality=quality)
        else:
            if dest.suffix.lower() != ".png":
                dest = dest.with_suffix(".png")
            img.save(dest, format="PNG")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return dest


def export_text(
    bundle_path: str | Path,
    asset_name: str,
    dest: str | Path,
) -> Path:
    from .maker import decode_text_asset_raw

    env = UnityPy.load(str(bundle_path))
    _obj, data = _find_object(env, {"TextAsset"}, asset_name)
    if data is None:
        raise RuntimeError(f"找不到文本：{asset_name}")
    raw = getattr(data, "m_Script", None)
    if raw is None:
        raw = getattr(data, "script", b"")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    text, kind = decode_text_asset_raw(raw)
    if kind == "text" and text is not None:
        if dest.suffix.lower() not in (".txt", ".json", ".xml", ".csv"):
            # 地图节点多半是 JSON
            dest = dest.with_suffix(".json" if text.lstrip().startswith("{") else ".txt")
        dest.write_text(text, encoding="utf-8")
        return dest

    # 二进制 / FGUI：按原样导出 bytes，不伪装成文本
    if isinstance(raw, str):
        payload = raw.encode("utf-8", errors="surrogateescape")
    else:
        payload = bytes(raw)
    if dest.suffix.lower() not in (".bytes", ".bin", ".fui"):
        dest = dest.with_suffix(".fui" if kind == "fgui" else ".bytes")
    dest.write_bytes(payload)
    return dest


def export_mesh(
    bundle_path: str | Path,
    asset_name: str,
    dest: str | Path,
) -> Path:
    env = UnityPy.load(str(bundle_path))
    _obj, data = _find_object(env, {"Mesh"}, asset_name)
    if data is None:
        raise RuntimeError(f"找不到 3D 模型：{asset_name}")
    try:
        obj_text = data.export(format="obj")
    except Exception as exc:
        raise RuntimeError(f"3D 模型导出失败：{exc}") from exc
    if not obj_text or obj_text is False:
        raise RuntimeError("该模型没有可导出的顶点数据。")
    dest = Path(dest)
    if dest.suffix.lower() != ".obj":
        dest = dest.with_suffix(".obj")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(obj_text, encoding="utf-8")
    return dest


def _curve_summary(curves, limit: int = 40) -> list:
    out = []
    if not curves:
        return out
    for c in list(curves)[:limit]:
        try:
            path = getattr(c, "path", None) or getattr(c, "attribute", None) or ""
            out.append(str(path)[:120])
        except Exception:
            continue
    return out


def export_animation(
    bundle_path: str | Path,
    asset_name: str,
    dest: str | Path,
) -> Path:
    """导出动画：默认 JSON 摘要 + 同目录 raw 二进制（可选合并到一个 .json 引用）。"""
    env = UnityPy.load(str(bundle_path))
    obj, data = _find_object(env, {"AnimationClip"}, asset_name)
    if data is None:
        raise RuntimeError(f"找不到动画：{asset_name}")

    raw = b""
    try:
        if obj is not None:
            raw = obj.get_raw_data() or b""
    except Exception:
        raw = b""

    meta = {
        "name": asset_name,
        "sample_rate": getattr(data, "m_SampleRate", None),
        "legacy": getattr(data, "m_Legacy", None),
        "wrap_mode": getattr(data, "m_WrapMode", None),
        "compressed": getattr(data, "m_Compressed", None),
        "position_curves": len(getattr(data, "m_PositionCurves", None) or []),
        "rotation_curves": len(getattr(data, "m_RotationCurves", None) or []),
        "scale_curves": len(getattr(data, "m_ScaleCurves", None) or []),
        "float_curves": len(getattr(data, "m_FloatCurves", None) or []),
        "euler_curves": len(getattr(data, "m_EulerCurves", None) or []),
        "events": len(getattr(data, "m_Events", None) or []),
        "position_paths": _curve_summary(getattr(data, "m_PositionCurves", None)),
        "rotation_paths": _curve_summary(getattr(data, "m_RotationCurves", None)),
        "raw_bytes": len(raw),
        "note": "JSON 为可读摘要；同名 .animbin 为 Unity 对象原始字节，便于对照。",
    }

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix.lower() not in (".json", ".animbin"):
        dest = dest.with_suffix(".json")

    if dest.suffix.lower() == ".animbin":
        if not raw:
            raise RuntimeError("无法读取动画原始字节。")
        dest.write_bytes(raw)
        return dest

    dest.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    if raw:
        bin_path = dest.with_suffix(".animbin")
        bin_path.write_bytes(raw)
    return dest


def default_export_name(asset_type: str, asset_name: str) -> str:
    stem = _safe_stem(asset_name)
    ext = {
        "texture": ".png",
        "text": ".txt",
        "mesh": ".obj",
        "anim": ".json",
        "dynamic": ".png",
    }.get(asset_type, ".bin")
    return stem + ext


def export_sequence_apng(
    bundle_path: str | Path,
    asset_name: str,
    dest: str | Path,
    *,
    fps: int = 30,
) -> Path:
    """导出序列帧为无损 APNG 动画（30fps 默认）。"""
    names_by_type = read_bundle_asset_names(bundle_path)
    texture_names = names_by_type.get("sequence_frames") or names_by_type.get("texture") or []

    groups = [
        group for group in sequence_groups_from_names(texture_names)
        if group.base == asset_name
    ]
    if not groups:
        raise RuntimeError(f"不是序列帧动画组：{asset_name}")
    frame_names = sorted_sequence_names(groups[0].names)
    if not frame_names:
        raise RuntimeError(f"序列帧组没有可用帧：{asset_name}")

    out = Path(dest)
    if out.suffix.lower() != ".apng":
        out = out.with_suffix(".apng")
    out.parent.mkdir(parents=True, exist_ok=True)

    duration = max(1, round(1000 / max(1, fps)))
    with tempfile.TemporaryDirectory(prefix="apng_") as tmp:
        tmp_dir = Path(tmp)
        frame_paths: list[Path] = []
        for index, frame_name in enumerate(frame_names):
            frame_png = tmp_dir / f"frame_{index:04d}.png"
            info = extract_texture_png(bundle_path, frame_png, target_name=frame_name)
            if info is None:
                raise RuntimeError(f"无法提取帧：{frame_name}")
            frame_paths.append(frame_png)

        images = [Image.open(p) for p in frame_paths]
        try:
            first = images[0].convert("RGBA")
            rest = [im.convert("RGBA") for im in images[1:]]
            first.save(
                out,
                format="PNG",
                save_all=True,
                append_images=rest,
                duration=duration,
                loop=0,
                disposal=2,
            )
        finally:
            for im in images:
                im.close()
    return out


def export_dynamic(
    bundle_path: str | Path,
    asset_name: str,
    dest: str | Path,
    *,
    fmt: str | None = None,
) -> Path:
    """导出动态 2D 资源：序列帧优先导出第一帧 PNG，否则导出原始字节。"""
    names_by_type = read_bundle_asset_names(bundle_path)
    texture_names = names_by_type.get("sequence_frames") or names_by_type.get("texture") or []

    # 序列帧：按 fmt 导出 APNG 或第一帧 PNG
    if any(group.base == asset_name for group in sequence_groups_from_names(texture_names)):
        if (fmt or "").lower() == "apng":
            return export_sequence_apng(bundle_path, asset_name, dest, fps=30)
        preview = find_sequence_preview_texture(texture_names, asset_name)
        if preview:
            out = Path(dest)
            if out.suffix.lower() != ".png":
                out = out.with_suffix(".png")
            info = extract_texture_png(bundle_path, out, target_name=preview)
            if info:
                return out

    # TextAsset / 视频：导出原始字节
    env = UnityPy.load(str(bundle_path))
    for obj in env.objects:
        if obj.type.name == "TextAsset":
            try:
                data = obj.read()
            except Exception:
                continue
            name = str(getattr(data, "m_Name", "") or "")
            # FairyGUI 包内组件名使用“包名/组件名”，导出时仍导出整个 fui 包
            if name != asset_name and not asset_name.startswith(name + "/"):
                continue
            raw = text_asset_bytes(data)
            out = Path(dest)
            if out.suffix.lower() not in (".bin", ".bytes", ".json", ".txt", ".fui"):
                out = out.with_suffix(".bin")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw)
            return out
        if obj.type.name in ("VideoClip", "VideoPlayer", "MovieTexture"):
            try:
                data = obj.read()
            except Exception:
                continue
            name = str(getattr(data, "m_Name", "") or "")
            if name != asset_name:
                continue
            out = Path(dest)
            if out.suffix.lower() not in (".bin", ".bytes"):
                out = out.with_suffix(".bin")
            out.parent.mkdir(parents=True, exist_ok=True)
            try:
                raw = obj.get_raw_data() or b""
            except Exception:
                raw = b""
            if not raw:
                raise RuntimeError(f"无法读取视频原始字节：{asset_name}")
            out.write_bytes(raw)
            return out

    raise RuntimeError(f"找不到可导出的动态资源：{asset_name}")


def export_by_type(
    asset_type: str,
    bundle_path: str | Path,
    asset_name: str,
    dest: str | Path,
    *,
    fmt: str | None = None,
) -> Path:
    if asset_type == "texture":
        return export_texture(bundle_path, asset_name, dest, fmt=fmt or "png")
    if asset_type == "text":
        return export_text(bundle_path, asset_name, dest)
    if asset_type == "mesh":
        return export_mesh(bundle_path, asset_name, dest)
    if asset_type == "anim":
        return export_animation(bundle_path, asset_name, dest)
    if asset_type == "dynamic":
        return export_dynamic(bundle_path, asset_name, dest, fmt=fmt)
    raise RuntimeError(f"不支持导出类型：{asset_type}")
