"""把旧版本资源包里的贴图迁移到当前 Addressables 资源包。"""
from __future__ import annotations

import gc
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import UnityPy


@dataclass(frozen=True)
class MigrationTexture:
    name: str
    width: int
    height: int
    path_id: int = 0
    texture_format: int = 0


@dataclass(frozen=True)
class TextureMigration:
    texture: MigrationTexture
    target_bundle: str | None
    status: str
    reason: str = ""


@dataclass(frozen=True)
class BundleMigrationPlan:
    source_path: Path
    source_bundle: str
    textures: tuple[MigrationTexture, ...]
    entries: tuple[TextureMigration, ...]

    @property
    def matched(self) -> tuple[TextureMigration, ...]:
        return tuple(entry for entry in self.entries if entry.status == "matched")

    @property
    def ambiguous(self) -> tuple[TextureMigration, ...]:
        return tuple(entry for entry in self.entries if entry.status == "ambiguous")

    @property
    def missing(self) -> tuple[TextureMigration, ...]:
        return tuple(entry for entry in self.entries if entry.status == "missing")


def logical_bundle_name(path: str | Path) -> str:
    """从普通 ``*.bundle`` 或缓存 ``.../<hash>/__data`` 推回逻辑包名。"""
    bundle_path = Path(path)
    if bundle_path.name == "__data" and bundle_path.parent.name:
        return f"{bundle_path.parent.name}.bundle"
    return bundle_path.name


def scan_bundle_textures(bundle_path: str | Path) -> list[MigrationTexture]:
    """读取 Texture2D 元数据；同包重名时保留第一项，与现有按名称替换逻辑一致。"""
    records: list[MigrationTexture] = []
    seen_names: set[str] = set()
    env = UnityPy.load(str(bundle_path))
    obj = data = None
    try:
        for obj in env.objects:
            if obj.type.name != "Texture2D":
                continue
            try:
                data = obj.read()
            except Exception:
                continue
            name = str(getattr(data, "m_Name", "") or getattr(data, "name", "") or "").strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            records.append(
                MigrationTexture(
                    name=name,
                    width=int(getattr(data, "m_Width", 0) or 0),
                    height=int(getattr(data, "m_Height", 0) or 0),
                    path_id=int(getattr(obj, "path_id", 0) or 0),
                    texture_format=int(getattr(data, "m_TextureFormat", 0) or 0),
                )
            )
        return records
    finally:
        obj = data = None
        env = None
        gc.collect()


def plan_bundle_migration(
    source_path: str | Path,
    current_index: dict[str, list[str]],
    resolve_bundle: Callable[[str], Path | None],
    *,
    metadata_cache: dict[str, dict[str, tuple[int, int]]] | None = None,
) -> BundleMigrationPlan:
    """为一个旧包寻找当前版本目标包，不确定的资源只报告、不写入。"""
    source = Path(source_path)
    source_bundle = logical_bundle_name(source)
    textures = tuple(scan_bundle_textures(source))
    old_names = {texture.name for texture in textures}
    name_to_bundles: dict[str, list[str]] = {name: [] for name in old_names}
    overlap_scores: Counter[str] = Counter()

    for bundle_name, names in current_index.items():
        shared = old_names.intersection(names)
        if not shared:
            continue
        overlap_scores[bundle_name] = len(shared)
        for name in shared:
            name_to_bundles[name].append(bundle_name)

    preferred_bundle: str | None = None
    if source_bundle in overlap_scores:
        preferred_bundle = source_bundle
    elif overlap_scores:
        best_score = max(overlap_scores.values())
        best_bundles = [name for name, score in overlap_scores.items() if score == best_score]
        if len(best_bundles) == 1:
            preferred_bundle = best_bundles[0]

    cache = metadata_cache if metadata_cache is not None else {}

    def texture_sizes(bundle_name: str) -> dict[str, tuple[int, int]]:
        cached = cache.get(bundle_name)
        if cached is not None:
            return cached
        path = resolve_bundle(bundle_name)
        if path is None:
            cache[bundle_name] = {}
            return cache[bundle_name]
        try:
            current = scan_bundle_textures(path)
            cache[bundle_name] = {item.name: (item.width, item.height) for item in current}
        except Exception:
            cache[bundle_name] = {}
        return cache[bundle_name]

    entries: list[TextureMigration] = []
    for texture in textures:
        candidates = name_to_bundles.get(texture.name, [])
        if not candidates:
            entries.append(TextureMigration(texture, None, "missing", "当前版本没有同名贴图"))
            continue

        if preferred_bundle in candidates:
            entries.append(TextureMigration(texture, preferred_bundle, "matched", "同包资源组匹配"))
            continue

        same_size = [
            bundle_name
            for bundle_name in candidates
            if texture_sizes(bundle_name).get(texture.name) == (texture.width, texture.height)
        ]
        if len(same_size) == 1:
            entries.append(TextureMigration(texture, same_size[0], "matched", "名称和尺寸匹配"))
        elif len(candidates) == 1:
            entries.append(TextureMigration(texture, candidates[0], "matched", "名称唯一"))
        else:
            matched_bundles = same_size or candidates
            entries.append(
                TextureMigration(
                    texture,
                    None,
                    "ambiguous",
                    f"有 {len(matched_bundles)} 个同名候选",
                )
            )

    return BundleMigrationPlan(source, source_bundle, textures, tuple(entries))
