"""受限资源内部检查与本机扫描；无法确认时停止工坊操作。"""
from __future__ import annotations

import os
import base64
import io
import json
import lzma
import re
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import lz4.block

MAX_INNER_BYTES = 256 * 1024 * 1024
MAX_INNER_METADATA = 16 * 1024 * 1024
MAX_INNER_OBJECTS = 100_000
MAX_INNER_OBJECT_BYTES = 128 * 1024 * 1024
MAX_TEXT_ASSET_BYTES = 64 * 1024 * 1024
MAX_INNER_NODES = 512
MAX_INNER_BLOCKS = 8192
MAX_TYPE_NODES = 200_000
MAX_CATALOG_REFERENCES = 1_000_000
ASSEMBLY_NAME = re.compile(r"\.(?:dll|exe|pdb|mdb)(?:\.bytes)?$", re.IGNORECASE)


def reject_assembly_bundle_targets(bundle_names, forbidden_bundle_names):
    blocked = {str(name).casefold() for name in forbidden_bundle_names}
    if any(str(name).replace("\\", "/").rsplit("/", 1)[-1].casefold() in blocked for name in bundle_names):
        raise ValueError("此作品覆盖当前游戏的程序集资源包，工坊只支持资源替换，不能投稿或安装。")


class _BoundedReader:
    def __init__(self, source, start, end, *, endian=">", origin=None):
        self.source, self.start, self.end = source, start, end
        self.position, self.endian = start, endian
        self.origin = start if origin is None else origin

    def read(self, size):
        if size < 0 or self.position + size > self.end:
            raise ValueError("Unity 资源内部数据越界或已截断。")
        self.source.seek(self.position)
        result = self.source.read(size)
        if len(result) != size:
            raise ValueError("Unity 资源内部数据不完整。")
        self.position += size
        return result

    def skip(self, size):
        if size < 0 or self.position + size > self.end:
            raise ValueError("Unity 元数据大小越界。")
        self.position += size

    def number(self, kind):
        return struct.unpack(self.endian + kind, self.read(struct.calcsize(kind)))[0]

    def count(self, maximum):
        value = self.number("i")
        if not 0 <= value <= maximum:
            raise ValueError("Unity 内部条目或长度超过限制。")
        return value

    def align(self, width=4):
        self.skip(-(self.position - self.origin) % width)

    def cstring(self, limit=1024):
        result = bytearray()
        while len(result) <= limit:
            value = self.read(1)
            if value == b"\0":
                try:
                    return result.decode("utf-8")
                except UnicodeError as exc:
                    raise ValueError("Unity 内部名称编码不正确。") from exc
            result.extend(value)
        raise ValueError("Unity 内部名称过长。")


def _decompress_block(raw, expected, compression):
    if not 0 < expected <= MAX_INNER_BYTES:
        raise ValueError("UnityFS 展开大小超过限制。")
    try:
        if compression == 0:
            result = raw
        elif compression in (2, 3):
            result = lz4.block.decompress(raw, uncompressed_size=expected)
        elif compression == 1:
            if len(raw) < 6:
                raise ValueError("UnityFS LZMA 数据不完整。")
            props, dictionary = struct.unpack_from("<BI", raw)
            lc, remainder = props % 9, props // 9
            lp, pb = remainder % 5, remainder // 5
            if dictionary > 32 * 1024 * 1024 or lc + lp > 4 or pb > 4:
                raise ValueError("UnityFS LZMA 内存参数超过限制。")
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[
                {"id": lzma.FILTER_LZMA1, "dict_size": max(4096, dictionary), "lc": lc, "lp": lp, "pb": pb}])
            result = decoder.decompress(raw[5:], max_length=expected + 1)
            if not decoder.eof or decoder.unused_data:
                raise ValueError("UnityFS LZMA 数据存在未识别内容。")
        else:
            raise ValueError("UnityFS 使用了不支持的压缩类型。")
    except (lz4.block.LZ4BlockError, lzma.LZMAError) as exc:
        raise ValueError("UnityFS 压缩数据损坏。") from exc
    if len(result) != expected:
        raise ValueError("UnityFS 实际展开大小与声明不一致。")
    return result


def _reject_pe_data(source, start, size, deadline):
    """Check valid PE signatures without loading a library or executing payloads."""
    end, position, suspects = start + size, start, 0
    while position < end:
        if time.monotonic() > deadline:
            raise ValueError("Unity 资源安全检查超时。")
        source.seek(position)
        chunk = source.read(min(64 * 1024, end - position))
        if not chunk:
            raise ValueError("Unity 资源内容已截断。")
        cursor = 0
        while True:
            found = chunk.find(b"MZ", cursor)
            if found < 0:
                break
            suspects += 1
            if suspects > 8192:
                raise ValueError("Unity 资源包含过多可执行文件候选标记。")
            absolute = position + found
            if absolute + 64 <= end:
                source.seek(absolute + 0x3C)
                offset = struct.unpack("<I", source.read(4))[0]
                if 2 <= offset <= end - absolute - 4:
                    source.seek(absolute + offset)
                    if source.read(4) == b"PE\0\0":
                        raise ValueError("工坊资源包内含 PE 可执行数据或程序集，不能投稿或安装。")
            cursor = found + 2
        # Retain one byte so an MZ marker across chunk boundaries is checked.
        position += len(chunk) - 1 if position + len(chunk) < end else len(chunk)


def _skip_type(reader, version, type_tree, *, reference=False, budget=None):
    class_id = reader.number("i")
    stripped = reader.number("B")
    script_index = reader.number("h")
    if stripped not in (0, 1):
        raise ValueError("Unity 类型元数据不合法。")
    if class_id == 114 or reference and script_index >= 0:
        reader.skip(16)
    reader.skip(16)
    if type_tree:
        nodes = reader.count(MAX_TYPE_NODES)
        string_bytes = reader.count(MAX_INNER_METADATA)
        budget[0] += nodes
        if not nodes or budget[0] > MAX_TYPE_NODES:
            raise ValueError("Unity 类型树节点超过限制。")
        reader.skip(nodes * (32 if version >= 19 else 24) + string_bytes)
        if version >= 21:
            if reference:
                for _ in range(3):
                    reader.cstring()
            else:
                reader.skip(reader.count(MAX_TYPE_NODES) * 4)
    return class_id


def _inspect_serialized(source, start, size, deadline):
    header = _BoundedReader(source, start, start + size)
    metadata_size, declared, version, data_offset = [header.number("I") for _ in range(4)]
    if not 17 <= version <= 22:
        raise ValueError("工坊暂不支持此 Unity serialized 版本，不能安全检查内部对象。")
    endian = header.number("B")
    if endian not in (0, 1):
        raise ValueError("Unity serialized 字节顺序不合法。")
    header.skip(3)
    if version >= 22:
        metadata_size, declared, data_offset = header.number("I"), header.number("q"), header.number("q")
        header.skip(8)
    if (declared != size or not 0 < metadata_size <= MAX_INNER_METADATA
            or not header.position + metadata_size <= start + data_offset <= start + size):
        raise ValueError("Unity serialized 头部大小不合法。")
    reader = _BoundedReader(source, header.position, header.position + metadata_size,
                            endian=">" if endian else "<", origin=start)
    reader.cstring(100)
    platform = reader.number("i")
    if platform == -2:
        raise ValueError("工坊只支持游戏运行时资源，不能安全检查编辑器资源布局。")
    type_tree = reader.number("B")
    if type_tree not in (0, 1):
        raise ValueError("Unity 类型树标记不合法。")
    budget = [0]
    type_count = reader.count(4096)
    types = [_skip_type(reader, version, type_tree, budget=budget) for _ in range(type_count)]
    object_count = reader.count(MAX_INNER_OBJECTS)
    objects, ids = [], set()
    for _ in range(object_count):
        if time.monotonic() > deadline:
            raise ValueError("Unity 对象安全检查超时。")
        reader.align()
        path_id = reader.number("q")
        offset = reader.number("q" if version >= 22 else "I")
        byte_size, type_id = reader.number("I"), reader.number("i")
        if (path_id in ids or not 0 <= type_id < len(types) or offset < 0
                or not 0 < byte_size <= MAX_INNER_OBJECT_BYTES or data_offset + offset + byte_size > size):
            raise ValueError("Unity 对象记录重名、越界或超过大小限制。")
        ids.add(path_id)
        objects.append((start + data_offset + offset, byte_size, types[type_id]))
    previous_end = start + data_offset
    for offset, byte_size, _ in sorted(objects):
        if offset < previous_end:
            raise ValueError("Unity 内部对象数据重叠。")
        previous_end = offset + byte_size
    for _ in range(reader.count(MAX_INNER_OBJECTS)):
        reader.skip(4)
        reader.align()
        reader.skip(8)
    for _ in range(reader.count(4096)):
        reader.cstring()
        reader.skip(20)
        reader.cstring(4096)
    if version >= 20:
        for _ in range(reader.count(4096)):
            _skip_type(reader, version, type_tree, reference=True, budget=budget)
    reader.cstring(4096)
    if reader.position != reader.end:
        raise ValueError("Unity 元数据存在未识别的尾部字段。")
    text_count = 0
    for offset, byte_size, class_id in objects:
        if class_id != 49:
            continue
        text_count += 1
        text = _BoundedReader(source, offset, offset + byte_size, endian=reader.endian, origin=start)
        name_size = text.count(4096)
        try:
            name = text.read(name_size).decode("utf-8")
        except UnicodeError as exc:
            raise ValueError("Unity TextAsset 名称编码不正确。") from exc
        if any(ord(char) < 32 for char in name):
            raise ValueError("Unity TextAsset 名称含控制字符，无法安全确认资源类型。")
        if ASSEMBLY_NAME.search(name.rstrip(" .")) or name.lower().rstrip(" .") == "astralparty.runtime":
            raise ValueError("工坊资源包包含程序集或可执行 TextAsset，不能投稿或安装。")
        text.align()
        payload_size = text.count(MAX_TEXT_ASSET_BYTES)
        text.skip(payload_size)
        if text.end - text.position > 3 or any(text.read(text.end - text.position)):
            raise ValueError("Unity TextAsset 存在未识别的布局或尾部数据。")
    return {"object_count": object_count, "text_asset_count": text_count}


def inspect_resource_bundle(source, size, *, max_expanded_bytes=MAX_INNER_BYTES):
    """Bounded UnityFS/object-table inspection; never executes game assemblies."""
    deadline = time.monotonic() + 30
    reader = _BoundedReader(source, 0, size)
    if reader.cstring(16) != "UnityFS":
        raise ValueError("工坊仅支持 UnityFS 资源。")
    version = reader.number("I")
    reader.cstring(100)
    engine = reader.cstring(100)
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", engine)
    if version not in (6, 7, 8) or match is None:
        raise ValueError("UnityFS 版本无法验证。")
    engine_version = tuple(map(int, match.groups()))
    new_flags = (engine_version[0] > 2022 or engine_version[0] == 2022 and engine_version >= (2022, 1, 1)
                 or engine_version[0] == 2021 and engine_version >= (2021, 3, 2)
                 or engine_version[0] == 2020 and engine_version >= (2020, 3, 34))
    declared, compressed, expanded, flags = reader.number("q"), reader.number("I"), reader.number("I"), reader.number("I")
    if (declared != size or not 0 < compressed <= MAX_INNER_METADATA or not 0 < expanded <= MAX_INNER_METADATA
            or not flags & 0x40 or flags & ~0x3FF or flags & 0x200 and not new_flags):
        raise ValueError("UnityFS 包头、加密标记或目录布局不受支持。")
    if version >= 7 or engine_version >= (2019, 4, 15) and engine_version[0] == 2019:
        reader.align(16)
    content_start = reader.position
    if flags & 0x80:
        source.seek(size - compressed)
        metadata_raw = source.read(compressed)
        content_end = size - compressed
    else:
        metadata_raw = reader.read(compressed)
        content_start = reader.position
        content_end = size
    metadata = _decompress_block(metadata_raw, expanded, flags & 0x3F)
    info = _BoundedReader(io.BytesIO(metadata), 0, len(metadata))
    info.skip(16)
    block_count = info.count(MAX_INNER_BLOCKS)
    if not block_count:
        raise ValueError("UnityFS 缺少数据块。")
    blocks, total = [], 0
    for _ in range(block_count):
        uncompressed_size, compressed_size, block_flags = info.number("I"), info.number("I"), info.number("H")
        total += uncompressed_size
        if (not 0 < uncompressed_size <= MAX_INNER_BYTES or not 0 < compressed_size <= size
                or total > min(MAX_INNER_BYTES, max_expanded_bytes) or block_flags & ~0x7F):
            raise ValueError("UnityFS 内部展开体积或块标记超过限制。")
        blocks.append((uncompressed_size, compressed_size, block_flags & 0x3F))
    node_count = info.count(MAX_INNER_NODES)
    if not node_count:
        raise ValueError("UnityFS 缺少内部文件。")
    nodes, paths = [], set()
    for _ in range(node_count):
        offset, node_size, node_flags = info.number("q"), info.number("q"), info.number("I")
        name = info.cstring(1024)
        if (offset < 0 or node_size <= 0 or offset + node_size > total or name.casefold() in paths
                or node_flags not in (0, 4)
                or ASSEMBLY_NAME.search(name)):
            raise ValueError("UnityFS 内部文件重名、越界或包含程序集文件。")
        paths.add(name.casefold())
        nodes.append((offset, node_size, node_flags, name))
    if info.position != info.end:
        raise ValueError("UnityFS 块目录包含未识别内容。")
    previous_end = 0
    for offset, node_size, _, _ in sorted(nodes):
        if offset < previous_end:
            raise ValueError("UnityFS 内部文件数据重叠。")
        previous_end = offset + node_size
    if flags & 0x200:
        content_start += -content_start % 16
    if content_start + sum(block[1] for block in blocks) != content_end:
        raise ValueError("UnityFS 压缩数据长度与块目录不一致。")
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as body:
        position = content_start
        for uncompressed_size, compressed_size, compression in blocks:
            if time.monotonic() > deadline:
                raise ValueError("UnityFS 展开检查超时。")
            source.seek(position)
            raw = source.read(compressed_size)
            if len(raw) != compressed_size:
                raise ValueError("UnityFS 数据块不完整。")
            body.write(_decompress_block(raw, uncompressed_size, compression))
            position += compressed_size
        _reject_pe_data(body, 0, total, deadline)
        result = {"expanded_size": total, "object_count": 0, "text_asset_count": 0}
        for offset, node_size, node_flags, name in nodes:
            if name.lower().endswith((".ress", ".resource")) and not node_flags & 4:
                continue
            checked = _inspect_serialized(body, offset, node_size, deadline)
            result["object_count"] += checked["object_count"]
            result["text_asset_count"] += checked["text_asset_count"]
            if result["object_count"] > MAX_INNER_OBJECTS:
                raise ValueError("UnityFS 总对象数量超过限制。")
        return result


def forbidden_workshop_bundle_names(aa_dirs):
    """Read current local catalog dependencies of assembly addresses, fail closed."""
    from .bundles import _latest_catalog_path

    blocked, seen = set(), set()
    for directory in aa_dirs:
        directory = Path(directory)
        path = _latest_catalog_path(directory) or directory / "catalog.json"
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        if not 0 < path.stat().st_size <= 32 * 1024 * 1024:
            raise ValueError("当前游戏 catalog 缺失或超过安全读取范围。")
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("当前游戏 catalog 含重复字段。")
                value[key] = item
            return value
        try:
            catalog = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique)
            blocked.update(_assembly_catalog_targets(catalog))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("无法验证当前游戏的程序集资源目录，已停止工坊操作。") from exc
    if not seen:
        raise ValueError("缺少当前游戏 catalog，无法确认程序集资源目标，已停止工坊安装。")
    return blocked


def _assembly_catalog_targets(catalog):
    if not isinstance(catalog, dict):
        raise ValueError("当前游戏 catalog 格式不正确。")
    internal_ids = catalog.get("m_InternalIds")
    if not isinstance(internal_ids, list) or len(internal_ids) > 200_000:
        raise ValueError("当前游戏 catalog 资源列表无效。")
    def decode(key):
        value = catalog.get(key)
        if not isinstance(value, str) or len(value) > 64 * 1024 * 1024:
            raise ValueError("当前游戏 catalog 索引不完整。")
        try:
            return base64.b64decode(value, validate=True)
        except ValueError as exc:
            raise ValueError("当前游戏 catalog 索引编码无效。") from exc
    keys = decode("m_KeyDataString")
    bucket_data, entry_data = decode("m_BucketDataString"), decode("m_EntryDataString")
    bucket_reader = _BoundedReader(io.BytesIO(bucket_data), 0, len(bucket_data), endian="<")
    bucket_count = bucket_reader.count(200_000)
    buckets, total_references = [], 0
    for _ in range(bucket_count):
        offset, count = bucket_reader.number("i"), bucket_reader.count(200_000)
        total_references += count
        if total_references > MAX_CATALOG_REFERENCES:
            raise ValueError("当前游戏 catalog 依赖总数超过读取限制。")
        if not 0 <= offset < len(keys):
            raise ValueError("当前游戏 catalog 键位置越界。")
        ids = [bucket_reader.number("i") for _ in range(count)]
        name = None
        if keys[offset] in (0, 1):
            if offset + 5 > len(keys):
                raise ValueError("当前游戏 catalog 键被截断。")
            length = struct.unpack_from("<i", keys, offset + 1)[0]
            if not 0 <= length <= 4096 or offset + 5 + length > len(keys):
                raise ValueError("当前游戏 catalog 名称长度不合法。")
            try:
                name = keys[offset + 5:offset + 5 + length].decode("ascii" if keys[offset] == 0 else "utf-16le")
            except UnicodeError as exc:
                raise ValueError("当前游戏 catalog 名称编码无效。") from exc
        buckets.append((name, ids))
    if bucket_reader.position != bucket_reader.end or len(entry_data) < 4:
        raise ValueError("当前游戏 catalog 条目大小不正确。")
    entry_count = struct.unpack_from("<i", entry_data)[0]
    if not 0 <= entry_count <= 200_000 or len(entry_data) != 4 + entry_count * 28:
        raise ValueError("当前游戏 catalog 条目数量不合法。")
    entries = [struct.unpack_from("<7i", entry_data, 4 + index * 28) for index in range(entry_count)]
    for _, ids in buckets:
        if any(not 0 <= index < entry_count for index in ids):
            raise ValueError("当前游戏 catalog 依赖条目越界。")
    for row in entries:
        if not 0 <= row[0] < len(internal_ids) or not -1 <= row[2] < bucket_count:
            raise ValueError("当前游戏 catalog 依赖索引越界。")
    pending = [index for name, ids in buckets if isinstance(name, str) and ASSEMBLY_NAME.search(name) for index in ids]
    visited, blocked = set(), set()
    while pending:
        index = pending.pop()
        if index in visited:
            continue
        visited.add(index)
        row = entries[index]
        address = internal_ids[row[0]]
        if not isinstance(address, str) or len(address) > 4096:
            raise ValueError("当前游戏 catalog 资源地址无效。")
        name = address.replace("\\", "/").rsplit("/", 1)[-1]
        if re.fullmatch(r"[A-Za-z0-9_.-]+\.bundle", name):
            blocked.add(name.casefold())
        if row[2] >= 0:
            pending.extend(buckets[row[2]][1])
    return blocked


def _defender_executable() -> Path:
    # 只寻找 Windows 自带扫描器，不能从 Mod 或 PATH 加载同名程序。
    if os.name != "nt":
        raise RuntimeError("工坊自动安装需要 Windows Defender 完成扫描。")
    platform = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows Defender/Platform"
    candidates = sorted(platform.glob("*/MpCmdRun.exe"), reverse=True) if platform.is_dir() else []
    candidates.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender/MpCmdRun.exe")
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise RuntimeError("未找到 Windows Defender 扫描器，工坊已停止自动安装。请先检查系统防病毒保护。")


def scan_workshop_package(directory: Path) -> dict:
    directory = Path(directory).resolve(strict=True)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("工坊待扫描目录无效。")
    scanner = _defender_executable()
    try:
        result = subprocess.run(
            [str(scanner), "-Scan", "-ScanType", "3", "-File", str(directory), "-DisableRemediation"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("防病毒扫描超时，未安装。请稍后重试。") from exc
    except OSError as exc:
        raise RuntimeError("无法启动防病毒扫描，未安装。请检查 Windows Defender。") from exc
    if result.returncode != 0:
        raise RuntimeError("Windows Defender 发现威胁或未能完成扫描，工坊已停止安装。请检查防病毒保护状态后重试；本次只扫描，不处置文件。")
    return {"scanner": "Windows Defender", "status": "passed", "message": "扫描完成；这不代表资源包绝对安全。"}
