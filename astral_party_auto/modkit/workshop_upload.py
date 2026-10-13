"""固定工坊服务的流式投稿；私有查询凭证只留在本机。"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import http.client
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .workshop import MAX_PACKAGE_BYTES, validate_metadata
from ..core.http_transport import trusted_https_connection


# 发布时由维护者固定为已部署的 HTTPS 服务，界面不接受任意服务器地址。
UPLOAD_SERVICE_URL = "https://xn--killer-gx2m.top/jixing-workshop"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_METADATA_HEADER = 24 * 1024
SOCKET_TIMEOUT = 20
STATUS_SOCKET_TIMEOUT = 12
REQUEST_DEADLINE = 300
BLOCK_BYTES = 64 * 1024
MAX_RECEIPTS = 100
SUBMISSION_ID = re.compile(r"[a-f0-9]{32}\Z")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
STATUS_MESSAGES = {
    "awaiting_confirmation": "上传结果尚未确认，可用相同作品和资料重试。",
    "pending": "上传已确认，等待人工审核。",
    "approved": "审核已通过；上架后可从工坊目录下载。",
    "rejected": "本次投稿未通过审核，请修改作品后重新投稿。",
}
ERROR_MESSAGES = {
    400: "上传服务拒绝了投稿格式，请重新检查作品。",
    401: "无法验证这份投稿的查询凭证。",
    403: "上传服务拒绝了本次请求。",
    404: "上传服务暂时找不到这份投稿。",
    409: "本次投稿与之前的上传记录不一致，请重新选择作品。",
    413: "作品超过上传服务的大小限制。",
    429: "投稿过于频繁，请稍后重试。",
    503: "上传服务暂时停止收稿，请稍后重试。",
}


def _strict_json(raw: bytes) -> dict:
    def fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("上传服务返回了重复字段。")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=fields)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("上传服务返回了无效响应。") from None
    if not isinstance(value, dict):
        raise ValueError("上传服务返回了无效响应。")
    return value


def _service_origin(url: str, allow_local_http: bool):
    try:
        parts = urlsplit(url)
        if (parts.username is not None or parts.password is not None or parts.query or parts.fragment
                or not parts.hostname or "\\" in url
                or any(ord(char) <= 32 for char in url)):
            raise ValueError
        local = parts.hostname in ("localhost", "127.0.0.1", "::1")
        if parts.scheme != "https" and not (allow_local_http and local and parts.scheme == "http"):
            raise ValueError
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        prefix = parts.path.rstrip("/")
        if prefix and (not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", prefix)
                       or any(segment in (".", "..") for segment in prefix.split("/"))):
            raise ValueError
        return parts.scheme, parts.hostname, port, prefix
    except (ValueError, TypeError):
        raise ValueError("投稿服务地址必须是维护者配置的 HTTPS 来源。") from None


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _private_bytes(payload: bytes, *, decrypt: bool = False) -> bytes:
    """Windows 使用当前账户的 DPAPI；其它平台依赖 0600 文件权限。"""
    if os.name != "nt":
        return payload

    class Blob(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    source_buffer = ctypes.create_string_buffer(payload)
    source = Blob(len(payload), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    operation = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    operation.restype = ctypes.c_int
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise OSError("无法保护本机投稿凭证，请检查当前 Windows 账户。")
    try:
        return ctypes.string_at(output.data, output.length)
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
        ctypes.windll.kernel32.LocalFree(output.data)


class WorkshopUploadClient:
    def __init__(self, data_dir: str | Path, *, service_url: str | None = None,
                 allow_local_http: bool = False):
        self._service_url = UPLOAD_SERVICE_URL if service_url is None else service_url
        self._origin = _service_origin(self._service_url, allow_local_http) if self._service_url else None
        self._directory = Path(data_dir) / "workshop-upload-receipts"
        self._receipt_path = self._directory / "receipts.dat"
        self._lock = threading.RLock()
        self._records = None
        self._accepting = False
        self._upload_limit = MAX_PACKAGE_BYTES
        self._status_offset = 0
        self._message = "投稿服务尚未部署，当前不能上传；无需操作 GitHub。" if not self._origin else "请刷新投稿服务状态。"

    def _connection(self):
        scheme, host, port, _prefix = self._origin
        if scheme == "https":
            return trusted_https_connection(host, port=port, timeout=SOCKET_TIMEOUT)
        return http.client.HTTPConnection(host, port=port, timeout=SOCKET_TIMEOUT)

    def _request(self, method: str, endpoint: str, headers: dict | None = None,
                 *, source=None, size: int = 0, expected_sha256: str = "") -> tuple[int, dict]:
        if self._origin is None:
            raise RuntimeError(self._message)
        connection = None
        deadline = time.monotonic() + REQUEST_DEADLINE
        try:
            connection = self._connection()
            if source is None:
                connection.timeout = STATUS_SOCKET_TIMEOUT
            connection.putrequest(method, self._origin[3] + endpoint)
            connection.putheader("Accept", "application/json")
            connection.putheader("User-Agent", "JiXing-Mod-Helper")
            connection.putheader("Connection", "close")
            for key, value in (headers or {}).items():
                connection.putheader(key, value)
            connection.endheaders()
            if source is not None:
                count, digest = 0, hashlib.sha256()
                while block := source.read(BLOCK_BYTES):
                    count += len(block)
                    if count > size or time.monotonic() > deadline:
                        raise ValueError("投稿包大小改变或上传时间超过限制。")
                    digest.update(block)
                    connection.send(block)
                if count != size or digest.hexdigest() != expected_sha256:
                    raise ValueError("投稿包在上传时发生改变，请重新选择。")
            response = connection.getresponse()
            # 不跟随任何跳转，包与凭证只发到固定来源。
            if 300 <= response.status <= 399:
                raise RuntimeError("投稿服务返回了跳转，已停止传输。")
            if response.status not in (200, 201):
                raise RuntimeError(ERROR_MESSAGES.get(response.status, "上传服务暂时无法处理请求，请稍后重试。"))
            if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ValueError("上传服务返回了无效响应类型。")
            body = bytearray()
            while block := response.read(min(BLOCK_BYTES, MAX_RESPONSE_BYTES + 1 - len(body))):
                body.extend(block)
                if len(body) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                    raise ValueError("上传服务响应超过限制。")
            return response.status, _strict_json(bytes(body))
        except (OSError, http.client.HTTPException):
            raise RuntimeError("无法连接投稿服务或连接中断；上传结果尚未确认，可稍后重试。") from None
        finally:
            if connection is not None:
                connection.close()

    def _load_records(self) -> list[dict]:
        if self._records is not None:
            return self._records
        if not self._receipt_path.exists():
            self._records = []
            return self._records
        if (_is_link(self._directory) or _is_link(self._receipt_path) or not self._receipt_path.is_file()
                or self._receipt_path.stat().st_size > 1024 * 1024):
            raise ValueError("本机投稿凭证文件不安全，已停止读取。")
        try:
            payload = _private_bytes(self._receipt_path.read_bytes(), decrypt=True)
            document = _strict_json(payload)
            records = document["receipts"]
            if document.get("version") != 1 or not isinstance(records, list) or len(records) > MAX_RECEIPTS:
                raise ValueError
            for record in records:
                if (not isinstance(record, dict) or not TOKEN.fullmatch(record.get("idempotency_key", ""))
                        or not SHA256.fullmatch(record.get("fingerprint", ""))
                        or not SHA256.fullmatch(record.get("sha256", ""))
                        or type(record.get("size")) is not int or not 0 < record["size"] <= MAX_PACKAGE_BYTES
                        or record.get("status") not in STATUS_MESSAGES
                        or record.get("service_url") != self._service_url):
                    raise ValueError
                record["metadata"] = validate_metadata(record["metadata"])
                if record["status"] != "awaiting_confirmation" and (
                        not SUBMISSION_ID.fullmatch(record.get("submission_id", ""))
                        or not TOKEN.fullmatch(record.get("status_token", ""))):
                    raise ValueError
            self._records = records
            return records
        except (OSError, ValueError, KeyError, TypeError):
            self._records = None
            raise ValueError("本机投稿凭证无法读取；为避免丢失查询记录，已停止上传。") from None

    def _save_records(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        if _is_link(self._directory) or (self._receipt_path.exists() and _is_link(self._receipt_path)):
            raise ValueError("本机投稿凭证目录不安全。")
        os.chmod(self._directory, 0o700)
        payload = _private_bytes(json.dumps({"version": 1, "receipts": self._records}, ensure_ascii=False).encode("utf-8"))
        handle, filename = tempfile.mkstemp(prefix=".receipt-", dir=self._directory)
        temporary = Path(filename)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._receipt_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _public_receipt(record: dict) -> dict:
        return {"submission_id": record.get("submission_id", ""), "name": record["metadata"]["name"],
                "status": record["status"], "sha256": record["sha256"], "size": record["size"],
                "message": STATUS_MESSAGES[record["status"]]}

    def snapshot(self, refresh: bool = False) -> dict:
        with self._lock:
            if self._origin is None:
                return {"configured": False, "accepting_uploads": False, "message": self._message,
                        "max_package_bytes": MAX_PACKAGE_BYTES, "submissions": []}
            records = self._load_records()
            if refresh:
                self._accepting = False
                try:
                    _, health = self._request("GET", "/v1/health")
                    if (set(health) != {"service", "version", "max_package_bytes", "accepting_uploads"}
                            or health["service"] != "jixing-workshop-upload" or type(health["version"]) is not int
                            or health["version"] != 1 or type(health["max_package_bytes"]) is not int
                            or not 0 < health["max_package_bytes"] <= MAX_PACKAGE_BYTES
                            or type(health["accepting_uploads"]) is not bool):
                        raise ValueError("投稿服务状态响应不正确。")
                    self._accepting = health["accepting_uploads"]
                    self._upload_limit = health["max_package_bytes"]
                    self._message = "可在软件内上传，作品审核通过后才上架。" if self._accepting else "投稿服务暂时停止收稿。"
                    changed = False
                    pending = [item for item in reversed(records) if item["status"] in ("pending", "approved")]
                    if pending:
                        start = self._status_offset % len(pending)
                        pending = pending[start:] + pending[:start]
                        self._status_offset = (start + min(3, len(pending))) % len(pending)
                    for record in pending[:3]:
                        _, result = self._request("GET", "/v1/submissions/" + record["submission_id"],
                                                  {"Authorization": "Bearer " + record["status_token"]})
                        if (result.get("submission_id") != record["submission_id"]
                                or result.get("status") not in ("pending", "approved", "rejected")
                                or result.get("sha256") != record["sha256"] or type(result.get("size")) is not int
                                or result["size"] != record["size"]
                                or validate_metadata(result.get("metadata")) != record["metadata"]):
                            raise ValueError("审核状态与本机投稿记录不一致。")
                        if result["status"] != record["status"]:
                            record["status"] = result["status"]
                            changed = True
                    if changed:
                        self._save_records()
                except (RuntimeError, ValueError, OSError) as exc:
                    self._message = str(exc)
            return {"configured": True, "accepting_uploads": self._accepting, "message": self._message,
                    "max_package_bytes": self._upload_limit,
                    "submissions": [self._public_receipt(item) for item in reversed(records)]}

    def upload(self, path: str | Path, metadata: dict, *, expected_sha256: str, expected_size: int) -> dict:
        with self._lock:
            if self._origin is None:
                raise RuntimeError(self._message)
            checked = validate_metadata(metadata)
            if (not isinstance(expected_sha256, str) or not SHA256.fullmatch(expected_sha256) or type(expected_size) is not int
                    or not 0 < expected_size <= MAX_PACKAGE_BYTES):
                raise ValueError("投稿包摘要或大小无效。")
            package = Path(path)
            if _is_link(package) or not package.is_file() or package.stat().st_size != expected_size:
                raise ValueError("投稿包已改变，请重新选择。")
            encoded_metadata = json.dumps(checked, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            metadata_header = base64.urlsafe_b64encode(encoded_metadata).rstrip(b"=").decode("ascii")
            if len(metadata_header) > MAX_METADATA_HEADER:
                raise ValueError("投稿说明超过上传服务限制。")
            fingerprint = hashlib.sha256(encoded_metadata + b"\0" + expected_sha256.encode("ascii")).hexdigest()
            records = self._load_records()
            record = next((item for item in records if item["fingerprint"] == fingerprint), None)
            if record is not None and record["status"] != "awaiting_confirmation":
                return self._public_receipt(record)
            health = self.snapshot(refresh=True)
            if not health["accepting_uploads"]:
                raise RuntimeError(health["message"])
            if expected_size > self._upload_limit:
                raise ValueError("作品超过上传服务的大小限制，请减小投稿包。")
            if record is None:
                if len(records) >= MAX_RECEIPTS:
                    raise ValueError("本机投稿记录已达到上限，请维护者处理后再上传。")
                record = {"service_url": self._service_url, "fingerprint": fingerprint,
                          "idempotency_key": secrets.token_urlsafe(32), "metadata": checked,
                          "sha256": expected_sha256, "size": expected_size, "status": "awaiting_confirmation"}
                records.append(record)
                # 在联网前保存重试凭证，超时不会让再次点击创建重复投稿。
            self._save_records()
            with package.open("rb") as source:
                digest = hashlib.sha256()
                while block := source.read(BLOCK_BYTES):
                    digest.update(block)
                if digest.hexdigest() != expected_sha256 or source.tell() != expected_size:
                    raise ValueError("投稿包在上传前发生改变，请重新选择。")
                source.seek(0)
                _, result = self._request("POST", "/v1/submissions",
                                          {"Content-Type": "application/zip", "Content-Length": str(expected_size),
                                           "X-Submission-Metadata": metadata_header,
                                           "Idempotency-Key": record["idempotency_key"]},
                                          source=source, size=expected_size, expected_sha256=expected_sha256)
            if (set(result) != {"submission_id", "status", "status_token", "sha256", "size"}
                    or not isinstance(result.get("submission_id"), str)
                    or not SUBMISSION_ID.fullmatch(result["submission_id"])
                    or result.get("status") not in ("pending", "approved", "rejected") or not isinstance(result.get("status_token"), str)
                    or not TOKEN.fullmatch(result["status_token"])
                    or result.get("sha256") != expected_sha256 or type(result.get("size")) is not int
                    or result["size"] != expected_size):
                raise ValueError("服务未确认这份投稿，不能视为上传成功；请稍后重试。")
            record.update(submission_id=result["submission_id"], status=result["status"], status_token=result["status_token"])
            try:
                self._save_records()
            except (OSError, ValueError):
                record["status"] = "awaiting_confirmation"
                record.pop("status_token", None)
                record.pop("submission_id", None)
                raise RuntimeError("服务已接收投稿，但本机查询凭证未能保存；请勿更换作品，稍后重试。") from None
            return self._public_receipt(record)
