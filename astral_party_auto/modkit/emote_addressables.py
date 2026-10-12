"""为选中的表情生成独立视频资源，并追加本地 Addressables 地址。"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import struct
from pathlib import Path, PureWindowsPath

import UnityPy

from .animation import _release_readers
from .cri_emote import (
    MAX_BUNDLE_BYTES, MAX_USM_BYTES, _check_movie_cache, _compatible,
    _embedded_object, _load_bundle, _read_file, inspect_usm_bytes,
)
from .emote_catalog import MAX_CATALOG_BYTES, MAX_ENTRIES, read_emote_locations

_BUNDLE_PROVIDER = "UnityEngine.ResourceManagement.ResourceProviders.AssetBundleProvider"
_ASSET_PROVIDER = "UnityEngine.ResourceManagement.ResourceProviders.BundledAssetProvider"
_VIDEO_TYPE = "CriWare.Assets.CriManaUsmAsset"
_OPTIONS_TYPE = "UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions"
_BUNDLE_NAME = re.compile(r"jixing_emote_([0-9]+)\.bundle\Z")


def independent_emote_identity(emote_id: int) -> dict:
    if isinstance(emote_id, bool) or not isinstance(emote_id, int):
        raise ValueError("表情编号无效。")
    character, number = divmod(emote_id - 120_000_000, 1000)
    if not 0 < character < 10_000 or not 0 < number < 1000:
        raise ValueError("表情编号不属于当前支持的角色表情。")
    return {"video_name": f"VEmoji_{character}_{number:02d}_999",
            "bundle_name": f"jixing_emote_{emote_id}.bundle",
            "internal_id": f"jixing/emote/{emote_id}"}


def _integer(raw: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(raw):
        raise ValueError("Addressables 目录字段被截断。")
    return struct.unpack_from("<i", raw, offset)[0]


def _decode_section(catalog: dict, key: str) -> bytes:
    value = catalog.get(key)
    if not isinstance(value, str) or len(value) > MAX_CATALOG_BYTES * 2:
        raise ValueError("Addressables 目录字段缺失或过大。")
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise ValueError("Addressables 目录字段编码无效。") from exc


def _catalog_parts(catalog: dict):
    if not isinstance(catalog, dict):
        raise ValueError("Addressables 目录格式无效。")
    # Reuse the existing emote/type validation before editing the compact tables.
    read_emote_locations(catalog)
    keys = _decode_section(catalog, "m_KeyDataString")
    bucket_data = _decode_section(catalog, "m_BucketDataString")
    entry_data = _decode_section(catalog, "m_EntryDataString")
    extra = _decode_section(catalog, "m_ExtraDataString")
    bucket_count, entry_count = _integer(bucket_data, 0), _integer(entry_data, 0)
    if _integer(keys, 0) != bucket_count:
        raise ValueError("Addressables 键数量与索引不一致。")
    providers = catalog.get("m_ProviderIds")
    if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
        raise ValueError("Addressables 加载器列表无效。")
    buckets, cursor = [], 4
    for _ in range(bucket_count):
        offset, count = _integer(bucket_data, cursor), _integer(bucket_data, cursor + 4)
        cursor += 8
        ids = list(struct.unpack_from(f"<{count}i", bucket_data, cursor))
        cursor += count * 4
        name = None
        if keys[offset] in (0, 1):
            size = _integer(keys, offset + 1)
            try:
                name = keys[offset + 5:offset + 5 + size].decode("ascii" if keys[offset] == 0 else "utf-16le")
            except UnicodeError as exc:
                raise ValueError("Addressables 资源名称编码无效。") from exc
        buckets.append((offset, name, ids))
    entries = [list(struct.unpack_from("<7i", entry_data, 4 + index * 28)) for index in range(entry_count)]
    for row in entries:
        if not 0 <= row[1] < len(providers) or row[2] < -1 or row[2] >= bucket_count:
            raise ValueError("Addressables 加载器或依赖索引越界。")
        if row[4] < -1 or row[4] >= len(extra):
            raise ValueError("Addressables 加载参数索引越界。")
    return keys, extra, buckets, entries


def _read_options(raw: bytes, offset: int):
    if not 0 <= offset < len(raw) or raw[offset] != 7:
        raise ValueError("原视频缺少可识别的资源包加载参数。")
    cursor = offset + 1

    def short_name():
        nonlocal cursor
        if cursor >= len(raw):
            raise ValueError("资源包加载参数被截断。")
        size = raw[cursor]
        cursor += 1
        if cursor + size > len(raw):
            raise ValueError("资源包类型名称被截断。")
        value = raw[cursor:cursor + size]
        cursor += size
        return value

    assembly, class_name = short_name(), short_name()
    length = _integer(raw, cursor)
    cursor += 4
    if not 0 < length <= 16384 or cursor + length > len(raw):
        raise ValueError("资源包加载参数长度无效。")
    try:
        options = json.loads(raw[cursor:cursor + length].decode("utf-16le"))
        if class_name.decode("ascii") != _OPTIONS_TYPE or not isinstance(options, dict):
            raise ValueError("原视频资源包加载参数类型不受支持。")
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("资源包加载参数编码无效。") from exc
    return assembly, class_name, options


def _encode_options(assembly: bytes, class_name: bytes, options: dict) -> bytes:
    raw = json.dumps(options, ensure_ascii=False, separators=(",", ":")).encode("utf-16le")
    return (bytes([7, len(assembly)]) + assembly + bytes([len(class_name)]) + class_name
            + struct.pack("<i", len(raw)) + raw)


def apply_catalog_address_patch(catalog_dict: dict, template_video_key: str, new_name: str,
                                bundle_name: str, bundle_internal_id: str, *, bundle_size: int) -> dict:
    """返回追加新地址的目录副本；原地址、原参数和版本标识保持原样。"""
    match = _BUNDLE_NAME.fullmatch(bundle_name) if isinstance(bundle_name, str) else None
    if not match:
        raise ValueError("独立表情资源包名称无效。")
    identity = independent_emote_identity(int(match.group(1)))
    if new_name != identity["video_name"] or bundle_name != identity["bundle_name"]:
        raise ValueError("独立视频地址与表情编号不一致。")
    if (not isinstance(bundle_internal_id, str) or len(bundle_internal_id) > 4096
            or any(c in bundle_internal_id for c in "\0\r\n{}")
            or "://" in bundle_internal_id
            or not (Path(bundle_internal_id).is_absolute() or PureWindowsPath(bundle_internal_id).is_absolute())
            or bundle_internal_id.replace("\\", "/").rsplit("/", 1)[-1] != bundle_name):
        raise ValueError("独立表情资源包必须使用对应文件的本地绝对路径。")
    if isinstance(bundle_size, bool) or not isinstance(bundle_size, int) or not 0 < bundle_size <= MAX_BUNDLE_BYTES:
        raise ValueError("独立表情资源包大小无效。")
    keys_raw, extra_raw, buckets, entries = _catalog_parts(catalog_dict)
    names = {name for _, name, _ in buckets if name is not None}
    dependency_name = f"jixing_emote_dependencies_{match.group(1)}"
    if any(name in names for name in (new_name, bundle_name, dependency_name)):
        raise ValueError("此表情的独立视频地址已存在，请先移除旧作品，避免覆盖。")
    if bundle_internal_id in catalog_dict["m_InternalIds"] or identity["internal_id"] in catalog_dict["m_InternalIds"]:
        raise ValueError("独立表情的本地路径或内部资源地址已被占用。")
    template_buckets = [ids for _, name, ids in buckets if name == template_video_key]
    if len(template_buckets) != 1 or len(template_buckets[0]) != 1:
        raise ValueError("参考动态表情不存在或地址不唯一。")
    template = entries[template_buckets[0][0]]
    providers, types = catalog_dict["m_ProviderIds"], catalog_dict["m_resourceTypes"]
    if providers[template[1]] != _ASSET_PROVIDER or types[template[6]].get("m_ClassName") != _VIDEO_TYPE:
        raise ValueError("参考表情的加载器或资源类型不受支持。")
    if template[2] < 0 or len(buckets[template[2]][2]) < 2:
        raise ValueError("参考视频缺少视频包或共享脚本包依赖。")
    dependencies = buckets[template[2]][2]
    if len(set(dependencies)) != len(dependencies):
        raise ValueError("参考视频包含重复资源包依赖。")
    primary_bundle_id = dependencies[0]
    primary_bundle = entries[primary_bundle_id]
    if providers[primary_bundle[1]] != _BUNDLE_PROVIDER or primary_bundle[2] != -1:
        raise ValueError("参考视频的首个依赖不是独立资源包。")
    assembly, class_name, options = _read_options(extra_raw, primary_bundle[4])
    if len(entries) + 2 > MAX_ENTRIES or len(buckets) + 3 > MAX_ENTRIES:
        raise ValueError("Addressables 目录过大，无法追加表情。")
    catalog = copy.deepcopy(catalog_dict)
    key_data, extra_data = bytearray(keys_raw), bytearray(extra_raw)
    options.update(m_Hash="", m_Crc=0, m_BundleName=bundle_name[:-7], m_BundleSize=bundle_size,
                   m_UseCrcForCachedBundles=False, m_UseUWRForLocalBundles=False,
                   m_ClearOtherCachedVersionsWhenLoaded=False)
    options_offset = len(extra_data)
    extra_data.extend(_encode_options(assembly, class_name, options))
    bundle_entry_id, video_entry_id = len(entries), len(entries) + 1

    def add_bucket(name, ids):
        raw = name.encode("ascii")
        offset = len(key_data)
        key_data.extend(bytes([0]) + struct.pack("<i", len(raw)) + raw)
        buckets.append((offset, name, ids))
        return len(buckets) - 1

    bundle_key = add_bucket(bundle_name, [bundle_entry_id])
    dependency_key = add_bucket(dependency_name, [bundle_entry_id, *dependencies[1:]])
    video_key = add_bucket(new_name, [video_entry_id])
    bundle_index = len(catalog["m_InternalIds"])
    catalog["m_InternalIds"].extend([bundle_internal_id, identity["internal_id"]])
    # The dependency hash identifies ResourceManager operations, not file integrity.
    dependency_hash = int.from_bytes(hashlib.sha256(dependency_name.encode()).digest()[:4], "little", signed=True) or 1
    entries.extend([
        [bundle_index, primary_bundle[1], -1, 0, options_offset, bundle_key, primary_bundle[6]],
        [bundle_index + 1, template[1], dependency_key, dependency_hash, -1, video_key, template[6]],
    ])
    struct.pack_into("<i", key_data, 0, len(buckets))
    bucket_data = bytearray(struct.pack("<i", len(buckets)))
    for offset, _, ids in buckets:
        bucket_data.extend(struct.pack("<ii", offset, len(ids)))
        bucket_data.extend(struct.pack(f"<{len(ids)}i", *ids))
    entry_data = struct.pack("<i", len(entries)) + b"".join(struct.pack("<7i", *row) for row in entries)
    catalog.update(m_KeyDataString=base64.b64encode(key_data).decode(), m_BucketDataString=base64.b64encode(bucket_data).decode(),
                   m_EntryDataString=base64.b64encode(entry_data).decode(), m_ExtraDataString=base64.b64encode(extra_data).decode())
    if len(json.dumps(catalog, ensure_ascii=False).encode("utf-8")) > MAX_CATALOG_BYTES:
        raise ValueError("追加后的 Addressables 目录超过 32 MB。")
    before, after = read_emote_locations(catalog_dict), read_emote_locations(catalog)
    if (any(after["videos"].get(name) != deps for name, deps in before["videos"].items())
            or before["config_bundles"] != after["config_bundles"]
            or bundle_name not in after["videos"].get(new_name, set())):
        raise ValueError("新增表情地址回读校验失败。")
    return catalog


def _write_new_file(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("独立表情资源包已存在，请先移除旧作品，避免覆盖。") from exc
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@_release_readers
def create_independent_emote_bundle(source: str | Path, template_video_key: str,
                                    replacement_usm_path: str | Path, out: str | Path, *, emote_id: int) -> dict:
    """克隆已确认的双对象视频包；不写参考包、不复用原视频地址。"""
    identity = independent_emote_identity(emote_id)
    destination = Path(out)
    if destination.name != identity["bundle_name"] or destination.exists():
        raise ValueError("独立表情资源包目标名称不正确或文件已存在。")
    replacement = _read_file(replacement_usm_path, MAX_USM_BYTES, "USM 视频")
    profile = inspect_usm_bytes(replacement)
    env = _load_bundle(source)
    target, tree, holder, original_usm = _embedded_object(env, template_video_key)
    _compatible(profile, inspect_usm_bytes(original_usm))
    _check_movie_cache(tree, replacement, original_usm)
    objects = list(env.objects)
    bundles = [obj for obj in objects if obj.type.name == "AssetBundle"]
    if len(objects) != 2 or len(bundles) != 1 or len(env.file.files) != 1:
        raise ValueError("参考视频包不是已确认的独立双对象布局。")
    script = tree["m_Script"]
    if not 0 < script.get("m_FileID", 0) <= len(target.assets_file.externals):
        raise ValueError("参考视频缺少外部共享脚本引用。")
    original_tree = copy.deepcopy(tree)
    bundle_obj = bundles[0]
    bundle_tree = bundle_obj.read_typetree()
    containers = bundle_tree.get("m_Container")
    if (not isinstance(containers, list) or len(containers) != 1
            or containers[0][1].get("asset") != {"m_FileID": 0, "m_PathID": target.path_id}):
        raise ValueError("参考视频包的资源入口与对象不一致。")
    original_bundle_tree = copy.deepcopy(bundle_tree)
    old_cab = target.assets_file.name
    cab_hash = hashlib.sha256((identity["video_name"] + profile["sha256"]).encode()).hexdigest()[:32]
    new_cab = "CAB-" + cab_hash
    if new_cab == old_cab or old_cab not in env.file.files:
        raise ValueError("无法为独立表情建立唯一资源身份。")
    tree["m_Name"] = identity["video_name"]
    holder["data"] = list(replacement)
    target.save_typetree(tree)
    bundle_tree["m_Name"] = identity["bundle_name"]
    bundle_tree["m_AssetBundleName"] = identity["bundle_name"]
    bundle_tree["m_Container"][0] = (identity["internal_id"], bundle_tree["m_Container"][0][1])
    bundle_obj.save_typetree(bundle_tree)
    serialized_file = env.file.files.pop(old_cab)
    serialized_file.name = new_cab
    env.file.files[new_cab] = serialized_file
    payload = env.file.save()
    if len(payload) > MAX_BUNDLE_BYTES:
        raise ValueError("独立表情资源包超过 128 MB。")
    restored = UnityPy.load(payload)
    saved, saved_tree, saved_holder, saved_usm = _embedded_object(restored, identity["video_name"])
    if saved_usm != replacement or saved.path_id != target.path_id or saved.assets_file.name != new_cab:
        raise ValueError("独立视频包内容或对象身份回读失败。")
    saved_holder["data"] = list(original_usm)
    saved_tree["m_Name"] = original_tree["m_Name"]
    if saved_tree != original_tree:
        raise ValueError("克隆视频包改变了播放元信息或脚本引用。")
    before_externals = [(x.path, x.type, x.guid) for x in target.assets_file.externals]
    after_externals = [(x.path, x.type, x.guid) for x in saved.assets_file.externals]
    if before_externals != after_externals:
        raise ValueError("克隆视频包改变了共享脚本依赖。")
    saved_bundle = next(obj for obj in restored.objects if obj.type.name == "AssetBundle").read_typetree()
    if (saved_bundle["m_Name"] != identity["bundle_name"] or saved_bundle["m_AssetBundleName"] != identity["bundle_name"]
            or saved_bundle["m_Container"][0][0] != identity["internal_id"]):
        raise ValueError("独立视频包地址入口回读失败。")
    saved_bundle["m_Name"] = original_bundle_tree["m_Name"]
    saved_bundle["m_AssetBundleName"] = original_bundle_tree["m_AssetBundleName"]
    saved_bundle["m_Container"][0] = (original_bundle_tree["m_Container"][0][0], saved_bundle["m_Container"][0][1])
    if saved_bundle != original_bundle_tree:
        raise ValueError("克隆视频包改变了其它资源引用。")
    _write_new_file(destination, payload)
    return {**identity, "emote_id": emote_id, "template_video_key": template_video_key,
            "bundle_size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
            "serialized_cab": new_cab, "profile": profile, "independent": True}
