"""Reviewed resource packs from one repository, with a fail-closed download path.

These checks limit archive and transport risks. They do not establish that a
Unity resource is harmless; review and malware scanning remain separate steps.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import tempfile
import threading
import time
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .catalog_mods import MANIFEST_FORMAT, MAX_MANIFEST_BYTES, OWNED_BUNDLE, validate_patches
from .workshop_security import inspect_resource_bundle, reject_assembly_bundle_targets

REPOSITORY_URL = "https://github.com/kldsjfas/JiXingModHelper"
CATALOG_URL = "https://raw.githubusercontent.com/kldsjfas/JiXingModHelper/workshop/catalog.json"
CATALOG_API_URL = "https://api.github.com/repos/kldsjfas/JiXingModHelper/contents/catalog.json?ref=workshop"
FORMAT = "jixing-workshop"
MAX_CATALOG_BYTES = 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_INFO_BYTES = 256 * 1024
MAX_ZIP_DIRECTORY_BYTES = 1024 * 1024
MAX_INNER_PACKAGE_BYTES = 512 * 1024 * 1024
MAX_INNER_PACKAGE_OBJECTS = 100_000
MAX_FILES = 512
MAX_MODS = 1000
MAX_RATIO = 100
CATEGORIES = frozenset({"texture", "text", "animation", "emote", "audio", "mixed"})
METADATA_FIELDS = frozenset({"id", "name", "author", "description", "category", "version", "game_version"})
SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,115}\.zip\Z")
TAG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PATH_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_. -]{0,119}\Z")
RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
                            *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})
RISK_NOTE = "仅通过来源、摘要和资源包结构检查，不代表无毒；安装前仍需审核与杀毒扫描。"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 包含重复字段。")
        result[key] = value
    return result


def _bad_number(value):
    raise ValueError("JSON 不能包含非有限数值。")


def _json(payload: bytes, limit: int) -> dict:
    if not 0 < len(payload) <= limit:
        raise ValueError("JSON 缺失或超过大小限制。")
    try:
        value = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                           parse_constant=_bad_number)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("JSON 格式不正确。") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON 顶层必须是对象。")
    _json_shape(value)
    return value


def _json_shape(value, depth=0) -> None:
    if depth > 12:
        raise ValueError("JSON 嵌套过深。")
    if isinstance(value, dict):
        for item in value.values():
            _json_shape(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_shape(item, depth + 1)


def _text(value, label: str, limit: int, *, multiline=False, optional=False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ValueError(f"{label}缺失或过长。")
    if any(ord(char) < 32 and not (multiline and char in "\n\t\r") or ord(char) == 127 for char in value):
        raise ValueError(f"{label}包含控制字符。")
    return value.strip()


def validate_metadata(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != METADATA_FIELDS:
        raise ValueError("作品资料字段不正确。")
    identifier = value["id"]
    if not isinstance(identifier, str) or not SLUG.fullmatch(identifier):
        raise ValueError("作品编号必须是小写字母、数字和短横线。")
    category = value["category"]
    if not isinstance(category, str) or category not in CATEGORIES:
        raise ValueError("作品分类不正确。")
    return {"id": identifier, "name": _text(value["name"], "作品名", 80),
            "author": _text(value["author"], "作者", 80),
            "description": _text(value["description"], "说明", 2000, multiline=True),
            "category": category, "version": _text(value["version"], "作品版本", 40),
            "game_version": _text(value["game_version"], "游戏版本", 80)}


def validate_catalog(payload: bytes | dict) -> dict:
    value = _json(payload, MAX_CATALOG_BYTES) if isinstance(payload, bytes) else payload
    if (not isinstance(value, dict) or set(value) != {"format", "version", "mods"}
            or value["format"] != FORMAT or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["mods"], list) or len(value["mods"]) > MAX_MODS):
        raise ValueError("工坊目录格式不正确。")
    entries, identifiers = [], set()
    for entry in value["mods"]:
        if not isinstance(entry, dict) or set(entry) != METADATA_FIELDS | {"asset", "revoked"}:
            raise ValueError("工坊作品字段不正确。")
        checked = validate_metadata({key: entry[key] for key in METADATA_FIELDS})
        if checked["id"] in identifiers:
            raise ValueError("工坊包含重复作品编号。")
        identifiers.add(checked["id"])
        asset = entry["asset"]
        if (not isinstance(asset, dict) or set(asset) != {"tag", "name", "size", "sha256"}
                or not isinstance(asset["tag"], str) or not TAG_NAME.fullmatch(asset["tag"])
                or not isinstance(asset["name"], str) or not ASSET_NAME.fullmatch(asset["name"])
                or type(asset["size"]) is not int or not 0 < asset["size"] <= MAX_PACKAGE_BYTES
                or not isinstance(asset["sha256"], str) or not SHA256.fullmatch(asset["sha256"])
                or type(entry["revoked"]) is not bool):
            raise ValueError("工坊资源地址、大小或摘要不正确。")
        checked.update(asset=dict(asset), revoked=entry["revoked"])
        entries.append(checked)
    return {"format": FORMAT, "version": 1, "mods": entries}


def asset_url(entry: dict) -> str:
    asset = entry["asset"]
    return f"{REPOSITORY_URL}/releases/download/{asset['tag']}/{asset['name']}"


def _https_parts(url: str):
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username is not None or parts.password is not None
                or parts.port is not None or parts.fragment or "\\" in url or any(ord(c) <= 32 for c in url)):
            raise ValueError("下载地址必须使用受信任的 HTTPS 地址。")
    except (TypeError, ValueError) as exc:
        raise ValueError("下载地址不合法。") from exc
    return parts


class _TrustedRedirects(HTTPRedirectHandler):
    max_redirections = 4
    max_repeats = 2

    def __init__(self, initial_url: str):
        super().__init__()
        self.initial_url = initial_url

    def redirect_request(self, request, fp, code, message, headers, new_url):
        source, target = _https_parts(request.full_url), _https_parts(new_url)
        if (self.initial_url in (CATALOG_URL, CATALOG_API_URL) or target.hostname != "release-assets.githubusercontent.com"
                or not target.path.startswith("/")
                or not (request.full_url == self.initial_url or source.hostname == "release-assets.githubusercontent.com")):
            raise ValueError("下载重定向离开了受信任的 GitHub 资源服务。")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def _download(url: str, output, limit: int, *, expected_size=None) -> int:
    parts = _https_parts(url)
    if url not in (CATALOG_URL, CATALOG_API_URL) and (parts.hostname != "github.com" or parts.query
                             or not re.fullmatch(r"/kldsjfas/JiXingModHelper/releases/download/[A-Za-z0-9][A-Za-z0-9_.-]{0,79}/[A-Za-z0-9][A-Za-z0-9_.-]{0,115}\.zip", parts.path)):
        raise ValueError("下载只能来自本项目审核过的 GitHub Release。")
    headers = {"User-Agent": "JiXingModHelper-Workshop/1", "Accept-Encoding": "identity", "Cache-Control": "no-cache"}
    if url == CATALOG_API_URL:
        headers["Accept"] = "application/vnd.github.raw+json"
    request = Request(url, headers=headers)
    started = time.monotonic()
    count = 0
    with build_opener(_TrustedRedirects(url)).open(request, timeout=12) as response:
        final_url = response.geturl()
        if final_url != url:
            final = _https_parts(final_url)
            if url in (CATALOG_URL, CATALOG_API_URL) or final.hostname != "release-assets.githubusercontent.com":
                raise ValueError("下载响应来源不正确。")
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError as exc:
                raise ValueError("下载响应大小不正确。") from exc
            if not 0 < length <= limit or expected_size is not None and length != expected_size:
                raise ValueError("下载大小与审核目录不一致或超过限制。")
        if response.headers.get("Content-Encoding", "identity").lower() not in {"identity", ""}:
            raise ValueError("不支持压缩的下载响应。")
        while True:
            if time.monotonic() - started > 90:
                raise TimeoutError("工坊下载超时。")
            block = response.read(min(64 * 1024, limit - count + 1))
            if not block:
                break
            count += len(block)
            if count > limit or expected_size is not None and count > expected_size:
                raise ValueError("下载超过审核大小，已停止。")
            output.write(block)
    if not count or expected_size is not None and count != expected_size:
        raise ValueError("下载不完整或大小与审核目录不一致。")
    return count


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _regular_file(path: Path, limit: int) -> None:
    if _is_link(path) or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError("资源包必须是大小合法的普通文件，不能是链接。")


def _archive_path(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if (info.orig_filename != name or "\\" in name or len(name) > 240
            or name.startswith("/") or "\x00" in name):
        raise ValueError("资源包包含不安全的文件路径。")
    is_directory = info.is_dir()
    parts = name[:-1].split("/") if is_directory else name.split("/")
    if not parts or any(not PATH_PART.fullmatch(part) or part.endswith((" ", "."))
                        or part.split(".", 1)[0].upper() in RESERVED_NAMES for part in parts):
        raise ValueError("资源包包含路径穿越、Windows 保留名称或非法路径。")
    normalized = PurePosixPath(*parts).as_posix()
    if info.flag_bits & 1:
        raise ValueError("不能使用加密资源包。")
    mode = info.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or kind == stat.S_IFDIR and not is_directory:
        raise ValueError("资源包包含链接或特殊文件。")
    if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise ValueError("资源包使用了不支持的压缩格式。")
    if is_directory:
        if info.file_size:
            raise ValueError("资源包目录条目不能包含数据。")
        return normalized + "/"
    if name not in {"mod_info.json", "emote_addresses.json"} and not name.endswith(".bundle"):
        raise ValueError("工坊只允许 Unity 资源包和规定格式的元数据文件，不能包含程序或脚本。")
    return normalized


def _bundle_header(header: bytes, size: int) -> None:
    if not header.startswith(b"UnityFS\x00") or len(header) < 30:
        raise ValueError("资源不是受支持的 UnityFS 包。")
    version = struct.unpack_from(">I", header, 8)[0]
    if version not in (6, 7, 8):
        raise ValueError("资源包 UnityFS 版本不受支持。")
    offset = 12
    for _ in range(2):
        end = header.find(b"\x00", offset, offset + 100)
        if end <= offset or any(byte < 32 or byte > 126 for byte in header[offset:end]):
            raise ValueError("资源包头部版本字段不正确。")
        offset = end + 1
    if len(header) < offset + 20:
        raise ValueError("资源包头部不完整。")
    declared, compressed, expanded, flags = struct.unpack_from(">QIII", header, offset)
    if (declared != size or not 0 < compressed <= min(size, 16 * 1024 * 1024)
            or not 0 < expanded <= 16 * 1024 * 1024
            or flags & 0x3F not in (0, 1, 2, 3) or flags & ~0x3FF):
        raise ValueError("资源包头部大小或压缩类型不正确。")


def _mod_info(value: dict, bundle_count: int) -> dict:
    allowed = {"name", "created_at", "tool", "items", "bundle_count"}
    if (not set(value) <= allowed or "name" not in value or "items" not in value
            or not isinstance(value["items"], list) or len(value["items"]) > 2000
            or any(not isinstance(item, dict) for item in value["items"])):
        raise ValueError("mod_info.json 的作品元数据格式不正确。")
    name = _text(value["name"], "作品名", 80)
    for field in ("created_at", "tool"):
        if field in value:
            _text(value[field], field, 120, optional=True)
    if "bundle_count" in value and (type(value["bundle_count"]) is not int or value["bundle_count"] != bundle_count):
        raise ValueError("作品元数据的资源包数量不一致。")
    return {"name": name, "bundle_count": bundle_count, "items": value["items"]}


def _digest(handle) -> str:
    handle.seek(0)
    result = hashlib.file_digest(handle, "sha256").hexdigest()
    handle.seek(0)
    return result


def _zip_preflight(source, size: int) -> int:
    """Bound the central directory before ZipFile allocates all its entries."""
    source.seek(0)
    if source.read(4) != b"PK\x03\x04":
        raise ValueError("只接受普通 ZIP 资源包，不能包含自解压程序或前置内容。")
    tail_start = max(0, size - (65535 + 22))
    source.seek(tail_start)
    tail = source.read(65535 + 22)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or len(tail) - offset < 22:
        raise ValueError("ZIP 目录记录缺失。")
    _, disk, directory_disk, disk_count, count, directory_size, directory_offset, comment_size = struct.unpack_from("<4s4H2LH", tail, offset)
    if (disk != 0 or directory_disk != 0 or disk_count != count or not 1 <= count <= MAX_FILES
            or not 0 < directory_size <= MAX_ZIP_DIRECTORY_BYTES
            or directory_offset + directory_size != tail_start + offset
            or offset + 22 + comment_size != len(tail)
            or tail[max(0, offset - 20):offset].startswith(b"PK\x06\x07")):
        raise ValueError("ZIP 目录太大、条目过多或使用了不受支持的分卷/ZIP64 格式。")
    source.seek(0)
    return count


def inspect_package(path: str | Path, *, forbidden_bundle_names=()) -> dict:
    """Check archive paths and bounded Unity contents without loading assemblies."""
    path = Path(path)
    _regular_file(path, MAX_PACKAGE_BYTES)
    try:
        with path.open("rb") as source:
            size = os.fstat(source.fileno()).st_size
            expected_count = _zip_preflight(source, size)
            package_digest = _digest(source)
            with zipfile.ZipFile(source) as archive:
                members = archive.infolist()
                if not 1 <= len(members) <= MAX_FILES or len(members) != expected_count:
                    raise ValueError("资源包文件数量超过限制。")
                paths, basenames, checked, total = set(), set(), [], 0
                for member in members:
                    name = _archive_path(member)
                    key = name.rstrip("/").casefold()
                    if key in paths:
                        raise ValueError("资源包存在重名或大小写冲突路径。")
                    paths.add(key)
                    if member.is_dir():
                        continue
                    basename = PurePosixPath(name).name.casefold()
                    if basename in basenames:
                        raise ValueError("不同目录存在同名资源包，不能确定安装目标。")
                    basenames.add(basename)
                    file_limit = MAX_BUNDLE_BYTES if name.endswith(".bundle") else MAX_INFO_BYTES
                    if name == "emote_addresses.json":
                        file_limit = MAX_MANIFEST_BYTES
                    total += member.file_size
                    if (not 0 < member.file_size <= file_limit or total > MAX_EXPANDED_BYTES
                            or member.file_size >= 1024 * 1024 and member.file_size > max(1, member.compress_size) * MAX_RATIO):
                        raise ValueError("资源包文件太大、展开总量过大或压缩比例异常。")
                    checked.append((member, name))
                file_paths = {name.casefold() for _, name in checked}
                for name in paths:
                    if any(parent.as_posix().casefold() in file_paths for parent in PurePosixPath(name).parents if parent.as_posix() != "."):
                        raise ValueError("资源包文件与目录路径冲突。")
                reject_assembly_bundle_targets((name for _, name in checked if name.endswith(".bundle")), forbidden_bundle_names)
                files, documents, bundle_checks, inner_total, inner_objects = [], {}, [], 0, 0
                for member, name in checked:
                    digest, count, prefix, json_data = hashlib.sha256(), 0, b"", bytearray()
                    temporary = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) if name.endswith(".bundle") else None
                    try:
                        with archive.open(member) as content:
                            while block := content.read(64 * 1024):
                                count += len(block)
                                if count > member.file_size:
                                    raise ValueError("资源包展开大小与 ZIP 记录不一致。")
                                digest.update(block)
                                if len(prefix) < 512:
                                    prefix += block[:512 - len(prefix)]
                                if temporary is not None:
                                    temporary.write(block)
                                else:
                                    json_data.extend(block)
                        if count != member.file_size:
                            raise ValueError("资源包文件不完整。")
                        if temporary is not None:
                            _bundle_header(prefix, count)
                            internal = inspect_resource_bundle(temporary, count, max_expanded_bytes=MAX_INNER_PACKAGE_BYTES - inner_total)
                            inner_total += internal["expanded_size"]
                            inner_objects += internal["object_count"]
                            if inner_objects > MAX_INNER_PACKAGE_OBJECTS:
                                raise ValueError("资源包内部对象总数超过限制。")
                            bundle_checks.append({"path": name, **internal})
                        else:
                            documents[name] = _json(bytes(json_data), MAX_MANIFEST_BYTES if name == "emote_addresses.json" else MAX_INFO_BYTES)
                    finally:
                        if temporary is not None:
                            temporary.close()
                    files.append({"path": name, "size": count, "sha256": digest.hexdigest()})
                bundles = [file for file in files if file["path"].endswith(".bundle")]
                if not bundles:
                    raise ValueError("资源包没有 Unity 资源。")
                info = _mod_info(documents["mod_info.json"], len(bundles)) if "mod_info.json" in documents else None
                patches = []
                if "emote_addresses.json" in documents:
                    manifest = documents["emote_addresses.json"]
                    if (set(manifest) != {"format", "version", "patches"} or manifest["format"] != MANIFEST_FORMAT
                            or type(manifest["version"]) is not int or manifest["version"] != 1):
                        raise ValueError("表情地址清单格式不正确。")
                    patches = validate_patches(manifest["patches"])
                    sizes = {file["path"]: file["size"] for file in bundles}
                    for patch in patches:
                        if sizes.get(patch["bundle_name"]) != patch["bundle_size"]:
                            raise ValueError("独立表情资源的根目录位置或大小与清单不一致。")
                declared = {patch["bundle_name"] for patch in patches}
                if any(OWNED_BUNDLE.fullmatch(PurePosixPath(file["path"]).name) and file["path"] not in declared for file in bundles):
                    raise ValueError("独立表情资源缺少对应的地址清单。")
            if _digest(source) != package_digest or os.fstat(source.fileno()).st_size != size:
                raise ValueError("资源包在检查期间改变，请重新导入。")
    except (zipfile.BadZipFile, RuntimeError, EOFError, struct.error, OverflowError, zlib.error) as exc:
        raise ValueError("资源包 ZIP 数据损坏或不受支持。") from exc
    return {"format": "jixing-resource-pack", "sha256": package_digest, "size": size,
            "total_size": total, "expanded_size": total, "file_count": len(files),
            "bundle_count": len(bundles), "files": files, "bundles": bundles,
            "inner_expanded_size": inner_total, "inner_object_count": inner_objects, "bundle_checks": bundle_checks,
            "mod_info": info, "emote_patches": patches, "risk_note": RISK_NOTE}


def _ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if _is_link(path) or not path.is_dir():
        raise ValueError("工坊工作目录不能是链接或重解析点。")
    return path.resolve()


def _extract_snapshot(path: Path, inspection: dict, destination: Path) -> None:
    """Copy approved bytes from the same open ZIP and verify every output digest."""
    destination.mkdir()
    try:
        with path.open("rb") as source:
            if _digest(source) != inspection["sha256"]:
                raise ValueError("资源包在校验后被修改，已停止准备。")
            with zipfile.ZipFile(source) as archive:
                for file in inspection["files"]:
                    target = destination / PurePosixPath(file["path"]).name
                    digest, count = hashlib.sha256(), 0
                    with archive.open(file["path"]) as content, target.open("xb") as output:
                        while block := content.read(64 * 1024):
                            count += len(block)
                            if count > file["size"]:
                                raise ValueError("资源包展开大小改变。")
                            digest.update(block)
                            output.write(block)
                    if count != file["size"] or digest.hexdigest() != file["sha256"]:
                        raise ValueError("资源包内容在校验后改变，已停止准备。")
            if _digest(source) != inspection["sha256"]:
                raise ValueError("资源包在展开期间改变，已停止准备。")
    except Exception:
        # Only files this function just created are eligible for removal.
        for file in inspection["files"]:
            target = destination / PurePosixPath(file["path"]).name
            if target.exists() and not _is_link(target) and target.is_file():
                target.unlink()
        destination.rmdir()
        raise


class WorkshopClient:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).resolve()
        self.work_dir = self.data_dir / "workshop"
        self._lock = threading.RLock()
        self._cached = None
        self._cached_at = 0.0
        self._prepared: dict[str, dict] = {}

    def catalog(self, force=False) -> dict:
        with self._lock:
            if self._cached is not None and not force and time.monotonic() - self._cached_at < 300:
                return json.loads(json.dumps(self._cached, ensure_ascii=False))
            import io

            buffer = io.BytesIO()
            source_url = CATALOG_URL
            try:
                try:
                    _download(CATALOG_URL, buffer, MAX_CATALOG_BYTES)
                except (OSError, URLError, TimeoutError):
                    # The same branch and file, served by GitHub's fixed API.
                    # Never retry malformed content or an untrusted redirect.
                    buffer.seek(0)
                    buffer.truncate()
                    source_url = CATALOG_API_URL
                    _download(CATALOG_API_URL, buffer, MAX_CATALOG_BYTES)
                catalog = validate_catalog(buffer.getvalue())
                revoked = [entry["id"] for entry in catalog["mods"] if entry["revoked"]]
                result = {**catalog, "mods": [entry for entry in catalog["mods"] if not entry["revoked"]],
                          "revoked_ids": revoked, "status": "ready", "message": "目录仅包含审核上架的作品。"}
            except HTTPError as exc:
                message = "工坊目录还没有上架，暂时没有可下载作品。" if exc.code == 404 else "无法读取工坊目录，请稍后刷新。"
                result = {"format": FORMAT, "version": 1, "mods": [], "revoked_ids": [], "status": "unavailable", "message": message}
            except (ValueError, OSError, URLError, TimeoutError):
                result = {"format": FORMAT, "version": 1, "mods": [], "revoked_ids": [], "status": "unavailable",
                          "message": "目录获取或验证失败；已停止下载，请检查网络后刷新。"}
            result.update(source=source_url, fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), risk_note=RISK_NOTE)
            self._cached, self._cached_at = result, time.monotonic()
            return json.loads(json.dumps(result, ensure_ascii=False))

    def prepare(self, mod_id: str, expected_sha256="", *, forbidden_bundle_names=()) -> dict:
        if not isinstance(mod_id, str) or not SLUG.fullmatch(mod_id):
            raise ValueError("作品编号不正确。")
        if not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256):
            raise ValueError("作品摘要不正确。")
        with self._lock:
            catalog = self.catalog(force=True)
            if catalog["status"] != "ready":
                raise ValueError(catalog["message"])
            entry = next((item for item in catalog["mods"] if item["id"] == mod_id), None)
            if entry is None:
                raise ValueError("此作品已下架或尚未通过审核。")
            if entry["asset"]["sha256"] != expected_sha256:
                raise ValueError("作品版本或摘要已变更，请刷新工坊后重新确认。")
            root = _ensure_directory(self.work_dir)
            stage = Path(tempfile.mkdtemp(prefix="prepared-", dir=root))
            pending, package = stage / "package.part", stage / "package.zip"
            inspection = None
            try:
                with pending.open("xb") as output:
                    _download(asset_url(entry), output, MAX_PACKAGE_BYTES, expected_size=entry["asset"]["size"])
                inspection = inspect_package(pending, forbidden_bundle_names=forbidden_bundle_names)
                if inspection["sha256"] != entry["asset"]["sha256"] or inspection["size"] != entry["asset"]["size"]:
                    raise ValueError("下载资源与审核摘要不一致，已隔离并拒绝使用。")
                pending.replace(package)
                _extract_snapshot(package, inspection, stage / "content")
                result = {"path": str(package), "mod_dir": str(stage / "content"), "entry": entry, "inspection": inspection}
                self._prepared[str(package)] = json.loads(json.dumps(result))
                return result
            except Exception:
                for candidate in (pending, package):
                    if candidate.exists() and not _is_link(candidate) and candidate.is_file():
                        candidate.unlink()
                if not any(stage.iterdir()):
                    stage.rmdir()
                raise

    def verify_prepared(self, prepared: dict, *, forbidden_bundle_names=()) -> None:
        """Recheck the exact ZIP and flat resource snapshot before installation."""
        with self._lock:
            key = prepared.get("path") if isinstance(prepared, dict) else None
            owned = self._prepared.get(key)
            if owned is None:
                raise ValueError("此资源不是本次工坊校验产生的候选包。")
            reject_assembly_bundle_targets((file["path"] for file in owned["inspection"]["bundles"]), forbidden_bundle_names)
            package, content = Path(owned["path"]), Path(owned["mod_dir"])
            stage = package.parent
            if (_is_link(stage) or _is_link(content) or stage.parent != self.work_dir.resolve()
                    or {path.name for path in stage.iterdir()} != {"package.zip", "content"}):
                raise ValueError("工坊暂存目录已改变。")
            files = {PurePosixPath(file["path"]).name: file for file in owned["inspection"]["files"]}
            if {path.name for path in content.iterdir()} != set(files):
                raise ValueError("工坊资源出现缺失或未知文件。")
            expected = {package: owned["inspection"]["sha256"], **{content / name: item["sha256"] for name, item in files.items()}}
            for path, digest in expected.items():
                _regular_file(path, MAX_PACKAGE_BYTES)
                with path.open("rb") as source:
                    if _digest(source) != digest:
                        raise ValueError("工坊资源在检查后被修改或隔离，已停止安装。")

    def cleanup_prepared(self, prepared: dict) -> dict:
        """Remove this client's unchanged staging files; preserve unknown contents."""
        with self._lock:
            key = prepared.get("path") if isinstance(prepared, dict) else None
            owned = self._prepared.get(key)
            if owned is None:
                return {"removed": False, "bytes": 0, "message": "未识别为本次工坊暂存，未清理。"}
            package, content = Path(owned["path"]), Path(owned["mod_dir"])
            stage = package.parent
            try:
                if (_is_link(stage) or _is_link(content) or stage.parent != self.work_dir.resolve()
                        or {path.name for path in stage.iterdir()} != {"package.zip", "content"}):
                    return {"removed": False, "bytes": 0, "message": "暂存目录包含未知内容，已保留。"}
                files = {PurePosixPath(file["path"]).name: file for file in owned["inspection"]["files"]}
                if {path.name for path in content.iterdir()} != set(files):
                    return {"removed": False, "bytes": 0, "message": "暂存资源出现未知文件，已保留。"}
            except OSError:
                return {"removed": False, "bytes": 0, "message": "暂存目录已缺失或不可读取，已保留可用部分。"}
            paths = [package, *(content / name for name in files)]
            expected = {package: owned["inspection"]["sha256"], **{content / name: item["sha256"] for name, item in files.items()}}
            try:
                for path in paths:
                    _regular_file(path, MAX_PACKAGE_BYTES)
                    with path.open("rb") as source:
                        if _digest(source) != expected[path]:
                            raise ValueError("工坊暂存被修改。")
                size = sum(path.stat().st_size for path in paths)
                for path in paths:
                    path.unlink()
                content.rmdir()
                stage.rmdir()
            except (OSError, ValueError):
                return {"removed": False, "bytes": 0, "message": "暂存被修改或正在占用，已保留未删除的文件。"}
            self._prepared.pop(key, None)
            return {"removed": True, "bytes": size, "message": "已清理本次下载和展开暂存。"}

    def discard(self, prepared: dict) -> dict:
        return self.cleanup_prepared(prepared)


def prepare_submission(path: str | Path, metadata: dict, destination: str | Path, *, forbidden_bundle_names=()) -> dict:
    """Create a reviewable ZIP and a path-free report. No upload credentials needed."""
    checked = validate_metadata(metadata)
    inspection = inspect_package(path, forbidden_bundle_names=forbidden_bundle_names)
    package = Path(destination)
    if not ASSET_NAME.fullmatch(package.name):
        raise ValueError("投稿文件名只能用英文字母、数字、下划线、点和短横线，并以 .zip 结尾。")
    root = _ensure_directory(package.parent)
    package = root / package.name
    report_path = package.with_suffix(".submission.json")
    if package.exists() or package.is_symlink() or report_path.exists() or report_path.is_symlink():
        raise ValueError("投稿输出已经存在，请另选文件名以免覆盖原作品。")
    source_path = Path(path)
    handle, temporary_name = tempfile.mkstemp(prefix=".workshop-submission-", suffix=".tmp", dir=root)
    os.close(handle)
    temporary_package = Path(temporary_name)
    temporary_report = None
    published = False
    final = None
    try:
        with source_path.open("rb") as source:
            if _digest(source) != inspection["sha256"]:
                raise ValueError("投稿来源在检查后改变。")
            with zipfile.ZipFile(source) as archive, zipfile.ZipFile(temporary_package, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for item in inspection["files"]:
                    if item["path"] == "mod_info.json":
                        continue
                    digest, count = hashlib.sha256(), 0
                    target_name = PurePosixPath(item["path"]).name
                    info = zipfile.ZipInfo(target_name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    with archive.open(item["path"]) as content, output.open(info, "w") as target:
                        while block := content.read(64 * 1024):
                            count += len(block)
                            if count > item["size"]:
                                raise ValueError("投稿来源大小改变。")
                            digest.update(block)
                            target.write(block)
                    if count != item["size"] or digest.hexdigest() != item["sha256"]:
                        raise ValueError("投稿来源在复制期间改变。")
                # Retain the reload guard without forwarding local edit history.
                source_items = (inspection["mod_info"] or {}).get("items", [])
                needs_ui_reload = any(item.get("kind") == "visual_animation" for item in source_items)
                info = {"name": checked["name"], "tool": "AstralParty Mod Helper",
                        "items": [{"kind": "visual_animation"}] if needs_ui_reload else [],
                        "bundle_count": inspection["bundle_count"]}
                metadata_entry = zipfile.ZipInfo("mod_info.json")
                metadata_entry.compress_type = zipfile.ZIP_DEFLATED
                metadata_entry.external_attr = (stat.S_IFREG | 0o644) << 16
                output.writestr(metadata_entry, json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8"))
            if _digest(source) != inspection["sha256"]:
                raise ValueError("投稿来源在复制期间改变。")
        final = inspect_package(temporary_package, forbidden_bundle_names=forbidden_bundle_names)
        asset = {"tag": "workshop-pending", "name": package.name, "size": final["size"], "sha256": final["sha256"]}
        report = {"format": "jixing-workshop-submission", "version": 1, "metadata": checked,
                  "asset": asset, "files": final["files"], "risk_note": RISK_NOTE,
                  "review_status": "pending", "checks": ["resource_only", "archive_paths", "size_limits", "sha256", "unityfs_objects", "no_embedded_pe_or_assemblies"]}
        handle, temporary_name = tempfile.mkstemp(prefix=".workshop-report-", suffix=".tmp", dir=root)
        temporary_report = Path(temporary_name)
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(json.dumps(report, ensure_ascii=False, indent=2))
            output.flush()
            os.fsync(output.fileno())
        # Same-volume linking publishes complete bytes without replacing a file
        # that another writer created after the initial absence check.
        os.link(temporary_package, package)
        published = True
        os.link(temporary_report, report_path)
        return {"path": str(package), "report_path": str(report_path), "metadata": checked, "inspection": final, "report": report}
    except Exception:
        if published and package.exists() and not _is_link(package) and package.is_file():
            with package.open("rb") as source:
                unchanged = final is not None and _digest(source) == final["sha256"]
            if unchanged:
                package.unlink()
        raise
    finally:
        temporary_package.unlink(missing_ok=True)
        if temporary_report is not None:
            temporary_report.unlink(missing_ok=True)
