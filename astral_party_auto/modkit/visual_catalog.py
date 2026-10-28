"""读取卡面所需的 Addressables 地址，不依赖表情视频类型。"""
from __future__ import annotations

import base64
import re
import struct

MAX_CATALOG_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 200_000
_BUNDLE_NAME = re.compile(r"[A-Za-z0-9_.-]+\.bundle\Z")
_ASSETS = {
    "GameData_CN": "UnityEngine.TextAsset",
    "GameData_INT": "UnityEngine.TextAsset",
    "Common_fui": "UnityEngine.TextAsset",
    "Common_atlas0": "UnityEngine.Texture2D",
}


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
