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


def replace_bundle_texture(
    src_bundle: str | Path,
    image_path: str | Path,
    out_bundle: str | Path,
    target_name: str | None = None,
    match_original_size: bool = True,
    crop_box: tuple[int, int, int, int] | None = None,
) -> str:
    """替换贴图。crop_box 为源图 (l,t,r,b) 像素裁剪，先裁再缩放。"""
    env = UnityPy.load(str(src_bundle))
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


def _text_asset_raw(data) -> bytes | str:
    raw = getattr(data, "m_Script", None)
    if raw is None:
        raw = getattr(data, "script", b"")
    return raw


def _extract_readable_runs(s: str, *, min_len: int = 2, limit: int = 8000) -> str:
    """从夹杂控制符的串里抽出可读片段（敏感词库等 protobuf 风格资源）。"""
    import re

    runs = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_./\-]{%d,}" % min_len, s)
    if not runs:
        return ""
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for w in runs:
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return "\n".join(out)


def decode_text_asset_raw(raw) -> tuple[str | None, str]:
    """把 TextAsset 内容解成可读字符串。

    返回 (text, kind)：
    - text 成功时为解码结果，失败为 None
    - kind: 'text' | 'fgui' | 'binary' | 'empty'
    大量 *_fui 是 FairyGUI 二进制包，Unity 也挂在 TextAsset 下，绝不能当乱码显示。
    """
    if raw is None:
        return None, "empty"
    if isinstance(raw, str):
        if not raw:
            return None, "empty"
        head = raw[:64]
        if raw.startswith("FGUI") or head.startswith("FGUI"):
            return None, "fgui"
        # 代理对 = UnityPy 把非文本字节硬塞进 str
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in raw[:512]):
            return None, "binary"

        sample = raw[:400]
        nulls = sample.count("\x00")
        cjk = sum(1 for ch in raw[:4000] if "\u4e00" <= ch <= "\u9fff")
        printable = sum(1 for ch in sample if ch.isprintable() or ch in "\r\n\t")

        # 纯 JSON / 配置：几乎无 null
        if nulls == 0:
            if sample and printable < max(8, int(len(sample) * 0.75)):
                return None, "binary"
            return raw, "text"

        # 含 null：可能是敏感词等结构化资源，已带正确中文
        if cjk >= 8 or printable >= int(len(sample) * 0.45):
            extracted = _extract_readable_runs(raw)
            if extracted:
                note = "（已从二进制结构中提取可读字符串，非完整原始文件）\n"
                return note + extracted, "text"
        return None, "binary"

    try:
        payload = bytes(raw)
    except Exception:
        return None, "binary"
    if not payload:
        return None, "empty"
    if payload.startswith(b"FGUI"):
        return None, "fgui"

    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = payload.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        # 尝试从原始字节里抠 UTF-8 / GBK 中文片段
        try:
            loose = payload.decode("utf-8", errors="ignore")
            extracted = _extract_readable_runs(loose)
            if extracted.count("\n") >= 5 or sum(1 for ch in extracted if "\u4e00" <= ch <= "\u9fff") >= 8:
                return "（已从二进制中提取可读字符串）\n" + extracted, "text"
        except Exception:
            pass
        return None, "binary"

    if b"\x00" in payload[:256]:
        cjk = sum(1 for ch in text[:4000] if "\u4e00" <= ch <= "\u9fff")
        if cjk >= 8:
            extracted = _extract_readable_runs(text)
            if extracted:
                return "（已从二进制结构中提取可读字符串）\n" + extracted, "text"
        return None, "binary"

    sample = text[:160]
    ok = sum(1 for ch in sample if ch.isprintable() or ch in "\r\n\t")
    if sample and ok < max(8, int(len(sample) * 0.8)):
        return None, "binary"
    return text, "text"


def is_readable_text_asset(data) -> bool:
    text, kind = decode_text_asset_raw(_text_asset_raw(data))
    return kind == "text" and bool(text)


def list_text_assets(bundle_path: str | Path) -> list[tuple[str, str]]:
    """列出资源包里可当文本编辑的 TextAsset：(name, preview)。跳过二进制 / FGUI。"""
    env = UnityPy.load(str(bundle_path))
    result: list[tuple[str, str]] = []
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        try:
            data = obj.read()
        except Exception:
            continue
        name = str(getattr(data, "m_Name", "") or "(未命名)")
        text, kind = decode_text_asset_raw(_text_asset_raw(data))
        if kind != "text" or not text:
            continue
        preview = text[:120].replace("\n", " ").replace("\r", "")
        result.append((name, preview))
    return result


def read_text_asset(bundle_path: str | Path, asset_name: str) -> str:
    """读取可读文本。二进制 / FGUI 抛错，避免界面显示乱码。"""
    env = UnityPy.load(str(bundle_path))
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        name = str(getattr(data, "m_Name", "") or "")
        if name != asset_name:
            continue
        text, kind = decode_text_asset_raw(_text_asset_raw(data))
        if kind == "fgui":
            raise RuntimeError(
                f"「{asset_name}」是 FairyGUI 界面包（*_fui），二进制不是可读文本。"
            )
        if kind != "text" or text is None:
            raise RuntimeError(f"「{asset_name}」不是可读文本（可能是二进制资源）。")
        return text
    raise RuntimeError(f"找不到文本资源：{asset_name}")


def replace_bundle_text(
    src_bundle: str | Path,
    asset_name: str,
    new_text: str,
    out_bundle: str | Path,
) -> str:
    env = UnityPy.load(str(src_bundle))
    replaced = None
    for obj in env.objects:
        if obj.type.name != "TextAsset":
            continue
        data = obj.read()
        name = str(getattr(data, "m_Name", "") or "")
        if name != asset_name:
            continue
        # UnityPy 不同版本：m_Script 可能是 str 或 bytes
        cur = getattr(data, "m_Script", None)
        if cur is None:
            cur = getattr(data, "script", None)
        if isinstance(cur, (bytes, bytearray)) or cur is None:
            payload: str | bytes = new_text.encode("utf-8")
        else:
            payload = new_text
        if hasattr(data, "m_Script"):
            data.m_Script = payload
        elif hasattr(data, "script"):
            data.script = payload
        else:
            raise RuntimeError("无法写入 TextAsset 字段。")
        try:
            data.save()
        except Exception:
            # 再试另一种类型
            alt = new_text if isinstance(payload, (bytes, bytearray)) else new_text.encode("utf-8")
            if hasattr(data, "m_Script"):
                data.m_Script = alt
            elif hasattr(data, "script"):
                data.script = alt
            data.save()
        replaced = name
        break
    if not replaced:
        raise RuntimeError(f"找不到文本资源：{asset_name}")
    out = Path(out_bundle)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(env.file.save())
    return replaced


def find_anim_preview_texture(bundle_path: str | Path, anim_name: str) -> str | None:
    """在同包里找动画对应的预览贴图（常见 Walk-001 / Idle_00000）。"""
    env = UnityPy.load(str(bundle_path))
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
    env = UnityPy.load(str(src_bundle))
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
