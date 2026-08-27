"""读取游戏的 Unity 资源包（Addressable bundle）：列出资源、导出预览、建立索引。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import UnityPy

from .dynamic import (
    classify_text_asset,
    extract_fairygui_dynamic_names,
    sequence_groups_from_names,
    text_asset_bytes,
)


# 资源包相对客户端 exe 所在目录的位置
AA_SUBPATH = ("StreamingAssets", "aa", "StandaloneWindows64")
ADDRESSABLES_CACHE_SUBPATH = ("AppData", "LocalLow", "feimo")
UNITY_CACHE_DATA_FILE = "__data"

_TEXTURE_TYPES = {"Texture2D", "Sprite"}
# Unity 类型名 → 我们的选项卡
_TYPE_MAP = {
    "Texture2D": "texture",
    "Sprite": "texture",
    "TextAsset": "text",
    "Mesh": "mesh",
    "AnimationClip": "anim",
}

ASSET_TYPE_KEYS = ("texture", "text", "mesh", "anim", "dynamic")
INDEX_VERSION = 7

BundleDirectories = str | Path | Iterable[str | Path]


@dataclass(frozen=True)
class TextureInfo:
    name: str
    width: int
    height: int


@dataclass(frozen=True)
class BundleEntry:
    """逻辑包名与磁盘上的实际文件。

    旧版二者都是 ``xxx.bundle``；新版 Unity 缓存则是
    ``<缓存键>/<包哈希>/__data``，需要保留原包名供 Mod 匹配。
    """

    name: str
    path: Path


def hot_update_dir_for_exe(
    exe_path: str | Path,
    *,
    user_profile: str | Path | None = None,
) -> Path:
    """返回新版 Addressables 热更新缓存根目录。"""
    exe = Path(exe_path)
    profile_value = user_profile or os.environ.get("USERPROFILE") or Path.home()
    profile = Path(profile_value)
    return profile.joinpath(
        *ADDRESSABLES_CACHE_SUBPATH,
        exe.stem,
        "com.unity.addressables",
        "AssetBundles",
    )


def _legacy_aa_dir_for_exe(exe_path: str | Path) -> Path:
    exe = Path(exe_path)
    data_dir = exe.parent / f"{exe.stem}_Data"
    return data_dir.joinpath(*AA_SUBPATH)


def bundle_dirs_for_exe(
    exe_path: str | Path,
    *,
    user_profile: str | Path | None = None,
) -> tuple[Path, ...]:
    """按优先级返回资源目录：热更新缓存优先，Steam/TapTap 基础包兜底。"""
    hot_update_dir = hot_update_dir_for_exe(exe_path, user_profile=user_profile)
    legacy_dir = _legacy_aa_dir_for_exe(exe_path)
    directories = []
    if hot_update_dir.is_dir():
        directories.append(hot_update_dir)
    if legacy_dir.is_dir():
        directories.append(legacy_dir)
    return tuple(directories) or (legacy_dir,)


def aa_dir_for_exe(
    exe_path: str | Path,
    *,
    user_profile: str | Path | None = None,
) -> Path:
    """返回最高优先级资源目录；多目录读取请使用 ``bundle_dirs_for_exe``。"""
    return bundle_dirs_for_exe(exe_path, user_profile=user_profile)[0]


def read_bundle_textures(bundle_path: str | Path) -> list[TextureInfo]:
    """读出一个资源包里的所有贴图信息（名字 + 尺寸），不导出像素，尽量快。"""
    textures: list[TextureInfo] = []
    env = UnityPy.load(str(bundle_path))
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        try:
            data = obj.read()
        except Exception:
            continue
        name = getattr(data, "m_Name", "") or getattr(data, "name", "") or "(未命名)"
        width = int(getattr(data, "m_Width", 0) or 0)
        height = int(getattr(data, "m_Height", 0) or 0)
        textures.append(TextureInfo(name=str(name), width=width, height=height))
    return textures


def read_bundle_asset_names(bundle_path: str | Path) -> dict[str, list[str]]:
    """一次扫包，按类型收集资源名。

    返回 texture/text/mesh/anim/dynamic → [name,...]，并额外返回：
    - sprite：Sprite 对象名字
    - atlas：被 Sprite 引用的 Texture2D 图集名（动画源图，不是动画帧）
    - sequence_frames：可用于序列帧组的非图集贴图/精灵图名字

    - TextAsset 只收录可读文本到 text；FairyGUI（*_fui）等二进制不会混入 text。
    - dynamic 收集视频、Live2D/Spine/GIF/FairyGUI 等动态 2D 候选，以及序列帧组名。
    本游戏音效不在 Addressable bundle 的 AudioClip 里，故不索引音频。
    """
    from .maker import is_readable_text_asset

    out: dict[str, list[str]] = {k: [] for k in ASSET_TYPE_KEYS}
    texture_names: set[str] = set()
    sprite_names: set[str] = set()
    texture_path_names: dict[int, str] = {}
    sprite_texture_paths: set[int] = set()
    dynamic_seen: set[str] = set()
    env = UnityPy.load(str(bundle_path))
    for obj in env.objects:
        tn = obj.type.name
        kind = _TYPE_MAP.get(tn)

        # 视频 / 动画控制器不落在 _TYPE_MAP，单独收进 dynamic
        if tn in ("VideoClip", "VideoPlayer", "MovieTexture", "AnimatorController"):
            try:
                data = obj.read()
            except Exception:
                continue
            name = str(getattr(data, "m_Name", "") or getattr(data, "name", "") or "")
            if name and name not in dynamic_seen:
                dynamic_seen.add(name)
                out["dynamic"].append(name)
            continue

        if not kind:
            continue

        try:
            data = obj.read()
        except Exception:
            continue
        name = str(getattr(data, "m_Name", "") or getattr(data, "name", "") or "(未命名)")

        if kind == "texture":
            # Sprite 与 Texture2D 可能重名，贴图侧去重
            if name not in texture_names:
                texture_names.add(name)
                out["texture"].append(name)
            if tn == "Sprite":
                sprite_names.add(name)
                rd = getattr(data, "m_RD", None)
                tex = getattr(rd, "texture", None) if rd is not None else None
                if tex is not None:
                    pid = getattr(tex, "m_PathID", None)
                    if pid:
                        sprite_texture_paths.add(pid)
            elif tn == "Texture2D":
                texture_path_names[obj.path_id] = name
        elif kind == "text":
            raw = text_asset_bytes(data)
            dynamic_kind = classify_text_asset(name, raw)
            if dynamic_kind and name not in dynamic_seen:
                dynamic_seen.add(name)
                out["dynamic"].append(name)
            # FairyGUI 包内的动效/组件名也作为动态候选，方便找到卡牌、横幅等
            if dynamic_kind == "fairygui":
                for item in extract_fairygui_dynamic_names(raw):
                    full = f"{name}/{item}"
                    if full not in dynamic_seen:
                        dynamic_seen.add(full)
                        out["dynamic"].append(full)
            if is_readable_text_asset(data):
                out["text"].append(name)
        elif kind == "anim":
            out["anim"].append(name)
            if name not in dynamic_seen:
                dynamic_seen.add(name)
                out["dynamic"].append(name)
        else:
            out[kind].append(name)

    # 识别图集：被 Sprite 引用的 Texture2D 是动画源图，不是动画帧。
    atlas_names = {
        texture_path_names[pid]
        for pid in sprite_texture_paths
        if pid in texture_path_names
    }
    sequence_frame_names = [name for name in texture_names if name not in atlas_names]

    # 序列帧贴图：同包同名前缀 + 数字帧号成组，组名作为 dynamic 资源项。
    # 只用非图集贴图/精灵图组成动画帧，避免把 Cry-001 这类图集混进序列帧。
    for group in sequence_groups_from_names(sequence_frame_names):
        if group.base and group.base not in dynamic_seen:
            dynamic_seen.add(group.base)
            out["dynamic"].append(group.base)

    out["sprite"] = sorted(sprite_names)
    out["atlas"] = sorted(atlas_names)
    out["sequence_frames"] = sorted(sequence_frame_names)
    return out


def extract_texture_png(
    bundle_path: str | Path,
    out_png: str | Path,
    target_name: str | None = None,
) -> TextureInfo | None:
    """导出资源包里的贴图/精灵图为 PNG。target_name 为空时取第一张。"""
    env = UnityPy.load(str(bundle_path))
    for obj in env.objects:
        if obj.type.name not in ("Texture2D", "Sprite"):
            continue
        data = obj.read()
        name = str(getattr(data, "m_Name", "") or getattr(data, "name", "") or "(未命名)")
        if target_name and name != target_name:
            continue
        image = data.image
        if image is None:
            continue
        width = int(getattr(data, "m_Width", 0) or image.width)
        height = int(getattr(data, "m_Height", 0) or image.height)
        out = Path(out_png)
        out.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGBA").save(out)
        return TextureInfo(name=name, width=width, height=height)
    return None


def _latest_catalog_path(cache_root: Path) -> Path | None:
    catalogs = list(cache_root.parent.glob("catalog_*.json"))
    if not catalogs:
        return None
    try:
        return max(catalogs, key=lambda path: path.stat().st_mtime_ns)
    except OSError:
        return None


def _active_catalog_bundle_names(cache_root: Path) -> set[str] | None:
    """读取当前 catalog 的包名；读取失败时返回 None，让调用方安全降级。"""
    catalog_path = _latest_catalog_path(cache_root)
    if catalog_path is None:
        return None
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    internal_ids = catalog.get("m_InternalIds") if isinstance(catalog, dict) else None
    if not isinstance(internal_ids, list):
        return None

    names: set[str] = set()
    for internal_id in internal_ids:
        value = str(internal_id).replace("\\", "/")
        name = value.rsplit("/", 1)[-1]
        if name.lower().endswith(".bundle"):
            names.add(name)
    return names


def _normalize_bundle_dirs(directories: BundleDirectories) -> tuple[Path, ...]:
    if isinstance(directories, (str, Path)):
        return (Path(directories),)
    return tuple(Path(directory) for directory in directories)


def _active_names_for_dirs(directories: tuple[Path, ...]) -> set[str] | None:
    for directory in directories:
        active_names = _active_catalog_bundle_names(directory)
        if active_names is not None:
            return active_names
    return None


def _entries_from_dir(aa: Path, active_names: set[str] | None) -> dict[str, Path]:
    if not aa.is_dir():
        return {}

    paths_by_name: dict[str, Path] = {}
    for path in aa.glob("*.bundle"):
        if active_names is None or path.name in active_names:
            paths_by_name[path.name] = path

    cached_data_files = aa.glob(f"*/*/{UNITY_CACHE_DATA_FILE}")
    if cached_data_files:
        for data_file in cached_data_files:
            bundle_name = f"{data_file.parent.name}.bundle"
            if active_names is not None and bundle_name not in active_names:
                continue
            previous = paths_by_name.get(bundle_name)
            if previous is None:
                paths_by_name[bundle_name] = data_file
                continue
            try:
                if data_file.stat().st_mtime_ns > previous.stat().st_mtime_ns:
                    paths_by_name[bundle_name] = data_file
            except OSError:
                continue
    return paths_by_name


def iter_bundle_entries(aa_dirs: BundleDirectories) -> Iterable[BundleEntry]:
    """合并资源目录；同名包使用靠前目录中的版本。

    每个目录只使用自己目录下的 catalog 过滤：
    - 热更新缓存目录按自己的 catalog 过滤过期项；
    - 基础包/旧版目录没有 catalog 时保留全部 bundle，避免漏掉未进热更 catalog 的资源。
    """
    directories = _normalize_bundle_dirs(aa_dirs)
    paths_by_name: dict[str, Path] = {}
    for directory in directories:
        active_names = _active_catalog_bundle_names(directory)
        for bundle_name, path in _entries_from_dir(directory, active_names).items():
            paths_by_name.setdefault(bundle_name, path)

    return [BundleEntry(name, paths_by_name[name]) for name in sorted(paths_by_name)]


def bundle_file_map(aa_dirs: BundleDirectories) -> dict[str, Path]:
    return {entry.name: entry.path for entry in iter_bundle_entries(aa_dirs)}


def iter_bundle_files(aa_dirs: BundleDirectories) -> Iterable[Path]:
    """兼容旧调用：只返回磁盘实际路径。"""
    return [entry.path for entry in iter_bundle_entries(aa_dirs)]


def bundle_source_key(aa_dirs: BundleDirectories) -> str:
    """生成轻量资源版本标识，用于自动丢弃过期索引。"""
    parts = []
    for aa in _normalize_bundle_dirs(aa_dirs):
        catalog_path = _latest_catalog_path(aa)
        if catalog_path is not None:
            hash_path = catalog_path.with_suffix(".hash")
            try:
                catalog_hash = hash_path.read_text(encoding="ascii").strip()
            except OSError:
                try:
                    catalog_hash = str(catalog_path.stat().st_mtime_ns)
                except OSError:
                    catalog_hash = "missing"
            parts.append(f"cache|{aa}|{catalog_path.name}|{catalog_hash}")
            continue
        try:
            directory_stamp = aa.stat().st_mtime_ns
        except OSError:
            directory_stamp = 0
        parts.append(f"directory|{aa}|{directory_stamp}")
    return "||".join(parts)


def _empty_typed_index() -> dict[str, dict[str, list[str]]]:
    return {k: {} for k in ASSET_TYPE_KEYS}


def build_asset_index(
    aa_dirs: BundleDirectories,
    cache_path: str | Path,
    progress: Callable[[int, int, str], bool] | None = None,
) -> dict[str, dict[str, list[str]]]:
    """扫描所有资源包，建立多类型索引并缓存。

    返回 {texture|text|mesh|anim: {bundle文件名: [资源名...]}}
    progress(done, total, current) 返回 False 可中断。
    """
    bundles = list(iter_bundle_entries(aa_dirs))
    total = len(bundles)
    typed = _empty_typed_index()
    for done, bundle in enumerate(bundles, start=1):
        try:
            names_by_type = read_bundle_asset_names(bundle.path)
        except Exception:
            names_by_type = {k: [] for k in ASSET_TYPE_KEYS}
        for kind, names in names_by_type.items():
            if kind not in ASSET_TYPE_KEYS or not names:
                continue
            typed[kind][bundle.name] = names
        if progress is not None and not progress(done, total, bundle.name):
            break
    _save_typed_index(cache_path, typed, source=bundle_source_key(aa_dirs))
    return typed


def _save_typed_index(
    cache_path: str | Path,
    typed: dict[str, dict[str, list[str]]],
    *,
    source: str,
) -> None:
    cache = Path(cache_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": INDEX_VERSION,
        "source": source,
        **{k: typed.get(k, {}) for k in ASSET_TYPE_KEYS},
    }
    cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_asset_index(
    cache_path: str | Path,
    source_dir: BundleDirectories | None = None,
) -> dict[str, dict[str, list[str]]]:
    """加载当前资源版本的索引；旧布局索引会自动失效。"""
    cache = Path(cache_path)
    typed = _empty_typed_index()
    if not cache.exists():
        return typed
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return typed
    if not isinstance(data, dict):
        return typed
    if data.get("version") != INDEX_VERSION:
        return typed
    if source_dir is not None and data.get("source") != bundle_source_key(source_dir):
        return typed
    for kind in ASSET_TYPE_KEYS:
        block = data.get(kind)
        if isinstance(block, dict):
            typed[kind] = {
                str(bundle_name): [str(name) for name in names]
                for bundle_name, names in block.items()
                if isinstance(names, list)
            }
    return typed
