"""读取卡面所需的 Addressables 地址，不依赖表情视频类型。"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MAX_CATALOG_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 200_000
_BUNDLE_NAME = re.compile(r"[A-Za-z0-9_.-]+\.bundle\Z")
_ASSETS = {
    "GameData_CN": "UnityEngine.TextAsset",
    "GameData_INT": "UnityEngine.TextAsset",
    "Common_fui": "UnityEngine.TextAsset",
    "Common_atlas0": "UnityEngine.Texture2D",
}


def _checked_message(message: str, checked_paths: tuple[str, ...]) -> str:
    return message + "\n已检查：\n" + "\n".join(checked_paths)


def _json_document(raw: bytes, label: str) -> dict:
    if not 0 < len(raw) <= MAX_CATALOG_BYTES:
        raise ValueError(f"{label}为空或超过 32 MB。")
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}不是可读取的 UTF-8 JSON。") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label}的根节点不是对象。")
    return document


def _read_catalog_bundle(path: Path) -> bytes:
    from .animation import _load_bundle, _release_readers
    from .dynamic import text_asset_bytes
    from .workshop_security import inspect_resource_bundle

    @_release_readers
    def read():
        size = path.stat().st_size
        if not 0 < size <= MAX_CATALOG_BYTES:
            raise ValueError("内置 catalog.bundle 为空或超过 32 MB。")
        with path.open("rb") as stream:
            inspect_resource_bundle(stream, size, max_expanded_bytes=MAX_CATALOG_BYTES + 16 * 1024 * 1024)
        environment = _load_bundle(path)
        assets = [obj for obj in environment.objects if obj.type.name == "TextAsset"]
        if len(assets) != 1 or assets[0].peek_name() != "catalog":
            raise ValueError("内置 catalog.bundle 必须只含一份名为 catalog 的文本目录。")
        raw = text_asset_bytes(assets[0].read())
        if not 0 < len(raw) <= MAX_CATALOG_BYTES:
            raise ValueError("内置 catalog 解包后为空或超过 32 MB。")
        return raw

    return read()


@dataclass(frozen=True)
class VisualCatalogSnapshot:
    path: Path
    kind: str
    locations: dict[str, str]
    source_key: str
    checked_paths: tuple[str, ...]


@dataclass(frozen=True)
class VisualCatalogSource:
    path: Path
    kind: str
    checked_paths: tuple[str, ...]
    settings_path: Path | None = None
    settings_problem: str = ""

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(json.dumps((self.kind, self.settings_problem), ensure_ascii=False).encode("utf-8"))
        official_hash = self.path.with_suffix(".hash") if self.kind == "hot_json" else None
        if official_hash is not None and not official_hash.is_file():
            official_hash = None
        for path in (self.path, self.settings_path, official_hash):
            if path is not None:
                limit = MAX_CATALOG_BYTES if path == self.path else (4096 if path == official_hash else 1024 * 1024)
                if not 0 < path.stat().st_size <= limit:
                    raise ValueError(_checked_message(f"资源目录来源为空或超过允许大小：{path}", self.checked_paths))
                digest.update(str(path.resolve()).encode("utf-8"))
                with path.open("rb") as stream:
                    digest.update(hashlib.file_digest(stream, "sha256").digest())
        return digest.hexdigest()

    def read_document(self, *, purpose: str = "资源") -> dict:
        """共享来源校验；各制作流程再核对自己需要的地址和实际资源。"""
        try:
            if not 0 < self.path.stat().st_size <= MAX_CATALOG_BYTES:
                raise ValueError("catalog 文件为空或超过 32 MB。")
            if self.settings_problem:
                raise ValueError(self.settings_problem)
            raw = _read_catalog_bundle(self.path) if self.kind == "base_bundle" else self.path.read_bytes()
            return _json_document(raw, f"{purpose} catalog")
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError(_checked_message(f"{purpose}制作暂不可用：{exc}", self.checked_paths)) from exc

    def read(self, bundle_path: Callable[[str], Path | None]) -> VisualCatalogSnapshot:
        catalog = self.read_document(purpose="卡面")
        try:
            locations = visual_bundle_locations(catalog)
            config_names = {locations[key] for key in ("GameData_CN", "GameData_INT") if key in locations}
            if len(config_names) != 1 or any(key not in locations for key in ("Common_fui", "Common_atlas0")):
                raise ValueError("卡面 catalog 缺少唯一的 GameData、Common 界面或图集地址。")
            required = config_names | {locations["Common_fui"], locations["Common_atlas0"]}
            missing = [name for name in sorted(required) if (path := bundle_path(name)) is None or not Path(path).is_file()]
            if missing:
                raise ValueError("卡面 catalog 引用的资源包尚未在当前游戏目录找到：" + "、".join(missing))
            return VisualCatalogSnapshot(self.path, self.kind, locations, self.fingerprint(), self.checked_paths)
        except (OSError, ValueError, KeyError) as exc:
            raise ValueError(_checked_message(f"卡面制作暂不可用：{exc}", self.checked_paths)) from exc


def _base_settings_problem(settings_path: Path, catalog_path: Path, kind: str) -> str:
    if not settings_path.is_file():
        return "找到内置 catalog，但缺少 settings.json，无法确认它是当前游戏使用的目录。"
    try:
        if not 0 < settings_path.stat().st_size <= 1024 * 1024:
            raise ValueError("游戏 settings.json 为空或过大。")
        settings = _json_document(settings_path.read_bytes(), "游戏 settings.json")
    except (OSError, ValueError) as exc:
        return str(exc)
    locations = settings.get("m_CatalogLocations")
    if not isinstance(locations, list):
        return "settings.json 没有可核对的 catalog 加载来源。"
    primary = [item for item in locations if isinstance(item, dict) and isinstance(item.get("m_Keys"), list)
               and "AddressablesMainContentCatalog" in item["m_Keys"]]
    expected = "{UnityEngine.AddressableAssets.Addressables.RuntimePath}/" + catalog_path.name
    if (len(primary) != 1 or primary[0].get("m_InternalId") != expected
            or primary[0].get("m_Provider") != "UnityEngine.AddressableAssets.ResourceProviders.ContentCatalogProvider"
            or settings.get("m_DisableCatalogUpdateOnStart") is not True
            or settings.get("m_IsLocalCatalogInBundle") is not (kind == "base_bundle")):
        return "找到内置 catalog，但 settings.json 不能确认当前仅从这份本地目录加载，未使用可能过时的卡面配置。"
    if len(locations) != 1 or primary[0].get("m_Dependencies"):
        return "找到内置 catalog，但游戏还声明了远程或缓存目录来源，无法确认内置版本当前生效；未使用可能过时的卡面配置。"
    return ""


def find_catalog_source(aa_dirs: tuple[Path, ...], executable: str | Path | None = None,
                        *, purpose: str = "资源") -> VisualCatalogSource:
    """按当前安装身份查元数据，资源目录是否存在不影响 catalog 定位。"""
    from .bundles import _latest_catalog_path, unity_catalog_roots_for_exe

    directories = tuple(Path(directory) for directory in aa_dirs)
    roots: list[Path] = []
    base_roots: list[Path] = []
    if executable is not None:
        executable = Path(executable)
        data_root = executable.parent / f"{executable.stem}_Data"
        roots.append(data_root / "com.unity.addressables")
        base_roots.append(data_root / "StreamingAssets" / "aa")
        try:
            roots.extend(unity_catalog_roots_for_exe(executable))
        except (OSError, ValueError) as exc:
            raise ValueError(_checked_message(
                f"{purpose}制作无法核实当前游戏身份或系统 LocalLow 位置，未使用可能属于其它安装的目录。" + str(exc),
                (str(data_root / "app.info"),))) from exc
    for directory in directories:
        if executable is not None:
            continue
        if directory.name == "StandaloneWindows64":
            base_roots.append(directory.parent)
        else:
            roots.append(directory.parent)
    roots = list(dict.fromkeys(path.resolve() for path in roots))
    base_roots = list(dict.fromkeys(path.resolve() for path in base_roots))
    checked = [str(root / "catalog_*.json") for root in roots]
    checked.extend(str(root / "catalog.json") for root in roots)
    for root in roots:
        candidate = _latest_catalog_path(root / "AssetBundles")
        if candidate is None and (root / "catalog.json").is_file():
            candidate = root / "catalog.json"
        if candidate is not None:
            return VisualCatalogSource(candidate.resolve(), "hot_json", tuple(checked))
    # 没有客户端身份的旧接口兼容目录内 JSON；生产环境始终按 exe 身份查找。
    if executable is None:
        for directory in directories:
            candidate = directory / "catalog.json"
            if directory.name != "StandaloneWindows64":
                checked.append(str(candidate))
                if candidate.is_file():
                    return VisualCatalogSource(candidate.resolve(), "hot_json", tuple(checked))
    cached_hashes = [path for root in roots for path in root.glob("catalog_*.hash") if path.is_file()]
    bases = [path for root in base_roots for path in (root / "catalog.bundle", root / "catalog.json") if path.is_file()]
    checked.extend(str(path) for root in base_roots for path in (root / "catalog.bundle", root / "catalog.json", root / "settings.json"))
    if len(bases) == 1:
        path = bases[0]
        kind = "base_bundle" if path.suffix == ".bundle" else "base_json"
        settings_path = path.parent / "settings.json"
        problem = _base_settings_problem(settings_path, path, kind)
        if cached_hashes:
            checked.extend(str(path) for path in cached_hashes)
            problem = "检测到热更新版本 hash，但对应 catalog JSON 缺失，无法确认内置版本生效；未使用可能过时的卡面配置。"
        return VisualCatalogSource(path.resolve(), kind, tuple(checked), settings_path if settings_path.is_file() else None, problem)
    message = f"{purpose}制作未找到可读取的 catalog。请保留现有缓存，并核对助手检测的游戏位置。"
    if len(bases) > 1:
        message = "发现多份内置 catalog，无法确认当前游戏使用哪一份，未使用可能过时的卡面配置。"
    raise ValueError(_checked_message(message, tuple(checked)))


def find_visual_catalog_source(aa_dirs: tuple[Path, ...], executable: str | Path | None = None) -> VisualCatalogSource:
    return find_catalog_source(aa_dirs, executable, purpose="卡面")


def visual_bundle_locations(catalog: dict) -> dict[str, str]:
    """将配置标签及公共资源映射到唯一的直接资源包依赖。"""
    if not isinstance(catalog, dict):
        raise ValueError("卡面资源目录格式无效。")

    def section(key):
        value = catalog.get(key)
        if not isinstance(value, str) or len(value) > MAX_CATALOG_BYTES * 2:
            raise ValueError("卡面资源目录字段缺失或过大。")
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("卡面资源目录编码无效。") from exc

    def integer(raw, offset):
        if offset < 0 or offset + 4 > len(raw):
            raise ValueError("卡面资源目录已截断。")
        return struct.unpack_from("<i", raw, offset)[0]

    keys = section("m_KeyDataString")
    bucket_data = section("m_BucketDataString")
    entry_data = section("m_EntryDataString")
    internal_ids, resource_types = catalog.get("m_InternalIds"), catalog.get("m_resourceTypes")
    if not isinstance(internal_ids, list) or not isinstance(resource_types, list):
        raise ValueError("卡面资源目录缺少地址或类型。")
    entry_count, bucket_count = integer(entry_data, 0), integer(bucket_data, 0)
    if not 0 <= entry_count <= MAX_ENTRIES or len(entry_data) != 4 + entry_count * 28:
        raise ValueError("卡面资源条目数量不合法。")
    if not 0 <= bucket_count <= MAX_ENTRIES or integer(keys, 0) != bucket_count:
        raise ValueError("卡面资源键数量不合法。")
    entries = [struct.unpack_from("<7i", entry_data, 4 + index * 28) for index in range(entry_count)]
    buckets, cursor = [], 4
    for _ in range(bucket_count):
        offset, count = integer(bucket_data, cursor), integer(bucket_data, cursor + 4)
        cursor += 8
        if not 0 <= offset < len(keys) or not 0 <= count <= entry_count or cursor + count * 4 > len(bucket_data):
            raise ValueError("卡面资源键位置越界。")
        ids = list(struct.unpack_from(f"<{count}i", bucket_data, cursor))
        cursor += count * 4
        if any(not 0 <= index < entry_count for index in ids):
            raise ValueError("卡面资源条目索引越界。")
        name = None
        if keys[offset] in (0, 1):
            length = integer(keys, offset + 1)
            if not 0 <= length <= 4096 or offset + 5 + length > len(keys):
                raise ValueError("卡面资源名称长度不合法。")
            try:
                name = keys[offset + 5:offset + 5 + length].decode("ascii" if keys[offset] == 0 else "utf-16le")
            except UnicodeError as exc:
                raise ValueError("卡面资源名称编码无效。") from exc
        buckets.append((name, ids))
    if cursor != len(bucket_data):
        raise ValueError("卡面资源目录存在未识别尾部数据。")
    for internal, _, dependency, _, _, primary, resource_type in entries:
        if (not 0 <= internal < len(internal_ids) or not 0 <= primary < len(buckets)
                or not 0 <= resource_type < len(resource_types) or not -1 <= dependency < len(buckets)):
            raise ValueError("卡面资源目录索引越界。")

    result = {}
    for key, ids in buckets:
        if key not in _ASSETS:
            continue
        if key in result or not ids:
            raise ValueError(f"卡面资源地址不唯一或没有条目：{key}")
        names = set()
        for index in ids:
            row = entries[index]
            resource_type = resource_types[row[6]]
            if not isinstance(resource_type, dict) or resource_type.get("m_ClassName") != _ASSETS[key] or row[2] < 0:
                raise ValueError(f"卡面资源类型或加载方式已变化：{key}")
            dependencies = buckets[row[2]][1]
            if not dependencies:
                raise ValueError(f"卡面资源缺少直接资源包依赖：{key}")
            for dependency in dependencies:
                address = internal_ids[entries[dependency][0]]
                if not isinstance(address, str):
                    raise ValueError("卡面资源包地址无效。")
                name = address.replace("\\", "/").rsplit("/", 1)[-1]
                if not _BUNDLE_NAME.fullmatch(name):
                    raise ValueError("卡面资源依赖不是可识别的资源包。")
                names.add(name)
        if len(names) != 1:
            raise ValueError(f"卡面资源包含未验证的额外依赖：{key}")
        result[key] = next(iter(names))
    return result
