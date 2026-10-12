"""只从当前 Addressables 目录查找表情资源，不新增地址或下载资源。"""
from __future__ import annotations

import base64
import json
import re
import struct
from pathlib import Path

from .bundles import _latest_catalog_path, bundle_file_map

VIDEO_NAME = re.compile(r"VEmoji_[0-9]+(?:_[0-9]+)+\Z")
TEXTURE_NAME = re.compile(r"UT_Item_Emoji_([0-9]+)_([0-9]+)\Z")
MAX_CATALOG_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 200_000


def emote_id_from_texture(name: str) -> int | None:
    match = TEXTURE_NAME.fullmatch(name)
    if not match:
        return None
    character, number = map(int, match.groups())
    if not 0 < character < 10_000 or not 0 < number < 1000:
        return None
    return 120_000_000 + character * 1000 + number


def read_emote_locations(catalog: dict) -> dict:
    """读取紧凑 catalog 的地址、类型及直接 bundle 依赖。"""
    def decode(key):
        value = catalog.get(key)
        if not isinstance(value, str) or len(value) > MAX_CATALOG_BYTES * 2:
            raise ValueError("表情资源目录格式不完整或过大，请更新游戏后重建索引。")
        return base64.b64decode(value, validate=True)

    keys = decode("m_KeyDataString")
    buckets_raw = decode("m_BucketDataString")
    entries_raw = decode("m_EntryDataString")
    internal_ids = catalog.get("m_InternalIds")
    resource_types = catalog.get("m_resourceTypes")
    if not isinstance(internal_ids, list) or not isinstance(resource_types, list):
        raise ValueError("无法读取表情资源的地址或类型。")

    def integer(data, offset):
        if offset < 0 or offset + 4 > len(data):
            raise ValueError("表情资源目录已截断。")
        return struct.unpack_from("<i", data, offset)[0]

    entry_count = integer(entries_raw, 0)
    bucket_count = integer(buckets_raw, 0)
    if not 0 <= entry_count <= MAX_ENTRIES or len(entries_raw) != 4 + entry_count * 28:
        raise ValueError("表情资源条目数量不合法。")
    if not 0 <= bucket_count <= MAX_ENTRIES:
        raise ValueError("表情资源键数量不合法。")
    entries = [struct.unpack_from("<7i", entries_raw, 4 + index * 28) for index in range(entry_count)]
    buckets = []
    offset = 4
    for _ in range(bucket_count):
        key_offset, count = integer(buckets_raw, offset), integer(buckets_raw, offset + 4)
        offset += 8
        if not 0 <= key_offset < len(keys) or not 0 <= count <= entry_count or offset + count * 4 > len(buckets_raw):
            raise ValueError("表情资源键位置越界。")
        ids = [integer(buckets_raw, offset + 4 * index) for index in range(count)]
        if any(not 0 <= value < entry_count for value in ids):
            raise ValueError("表情资源依赖越界。")
        offset += count * 4
        name = None
        if keys[key_offset] in (0, 1):
            length = integer(keys, key_offset + 1)
            start = key_offset + 5
            if not 0 <= length <= 4096 or start + length > len(keys):
                raise ValueError("表情资源名称长度不合法。")
            name = keys[start:start + length].decode("ascii" if keys[key_offset] == 0 else "utf-16le")
        buckets.append((name, ids))
    if offset != len(buckets_raw):
        raise ValueError("表情资源目录存在未识别尾部数据。")

    result = {"config_bundles": set(), "videos": {}}
    for row in entries:
        internal, _, dependency, _, _, primary, resource_type = row
        if not 0 <= primary < len(buckets) or not 0 <= internal < len(internal_ids) or not 0 <= resource_type < len(resource_types):
            raise ValueError("表情资源目录索引越界。")
        name = buckets[primary][0]
        is_config = name in ("GameData_CN", "GameData_INT")
        is_video = isinstance(name, str) and VIDEO_NAME.fullmatch(name)
        if not is_config and not is_video:
            continue
        expected_type = "UnityEngine.TextAsset" if is_config else "CriWare.Assets.CriManaUsmAsset"
        if resource_types[resource_type].get("m_ClassName") != expected_type:
            raise ValueError("表情资源类型与当前游戏不匹配。")
        if not 0 <= dependency < len(buckets):
            raise ValueError("表情资源缺少 bundle 依赖。")
        bundle_names = set()
        for index in buckets[dependency][1]:
            internal_index = entries[index][0]
            if not 0 <= internal_index < len(internal_ids):
                raise ValueError("表情资源包地址越界。")
            address = internal_ids[internal_index]
            if not isinstance(address, str):
                raise ValueError("表情资源包地址无效。")
            basename = address.replace("\\", "/").rsplit("/", 1)[-1]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.bundle", basename):
                raise ValueError("表情资源依赖不是可识别的 bundle。")
            bundle_names.add(basename)
        if is_config:
            result["config_bundles"].update(bundle_names)
        else:
            result["videos"].setdefault(name, set()).update(bundle_names)
    return result


def discover_emote_locations(aa_dirs) -> dict:
    files = bundle_file_map(aa_dirs)
    config_bundles, videos = set(), {}
    seen = set()
    for directory in aa_dirs:
        directory = Path(directory)
        catalog_path = _latest_catalog_path(directory)
        if catalog_path is None:
            catalog_path = directory / "catalog.json"
        if catalog_path in seen or not catalog_path.is_file():
            continue
        seen.add(catalog_path)
        if catalog_path.stat().st_size > MAX_CATALOG_BYTES:
            raise ValueError("表情资源目录超过 32 MB。")
        locations = read_emote_locations(json.loads(catalog_path.read_text(encoding="utf-8-sig")))
        config_bundles.update(name for name in locations["config_bundles"] if name in files)
        for name, dependencies in locations["videos"].items():
            # 热更新目录在前；旧基础目录不得覆盖当前同名地址。
            if name not in videos and dependencies and dependencies <= files.keys():
                videos[name] = sorted(dependencies)
    if len(config_bundles) != 1 or not videos:
        raise ValueError("缺少完整的表情配置或动态视频，请更新游戏资源后重试。")
    return {"config_bundle": next(iter(config_bundles)), "videos": videos}
