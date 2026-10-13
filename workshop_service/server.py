"""A bounded upload inbox. Acceptance never publishes a workshop entry.

Run behind a TLS proxy in production. This process must use a dedicated account
and a private data directory outside every web root and game installation.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from bottle import Bottle, HTTPResponse, request

from astral_party_auto.modkit.workshop import MAX_PACKAGE_BYTES, _json, inspect_package, validate_metadata

ID = re.compile(r"[0-9a-f]{32}\Z")
IDEMPOTENCY = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")
METADATA_ENCODING = re.compile(r"[A-Za-z0-9_-]{1,24576}\Z")
MAX_ADMIN_JSON = 16 * 1024


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    admin_secret: str
    quota_bytes: int = 2 * 1024 * 1024 * 1024
    max_package_bytes: int = MAX_PACKAGE_BYTES
    max_submissions: int = 1000
    max_workers: int = 4
    read_timeout: float = 10.0
    upload_timeout: float = 180.0
    uploads_per_hour: int = 10
    status_per_minute: int = 60
    forbidden_bundle_names: tuple[str, ...] = ()
    scanner_command: tuple[str, ...] = ()
    scan_timeout: float = 120.0
    trusted_proxy_ips: tuple[str, ...] = ()

    def __post_init__(self):
        if (not isinstance(self.admin_secret, str) or not 32 <= len(self.admin_secret) <= 256
                or not self.admin_secret.isascii() or any(ord(char) <= 32 or ord(char) >= 127 for char in self.admin_secret)):
            raise ValueError("维护者密钥至少需要 32 个字符。")
        if not 1 <= self.max_package_bytes <= MAX_PACKAGE_BYTES:
            raise ValueError("上传包限制必须在 1 到 256 MiB 之间。")
        if self.quota_bytes < self.max_package_bytes or self.max_submissions < 1:
            raise ValueError("磁盘配额或投稿数量设置不正确。")
        if not 1 <= self.max_workers <= 16 or self.read_timeout <= 0 or self.upload_timeout <= 0:
            raise ValueError("并发数或超时设置不正确。")
        if self.uploads_per_hour < 1 or self.status_per_minute < 1 or self.scan_timeout <= 0:
            raise ValueError("限速或扫描超时设置不正确。")
        if self.scanner_command and (not any("{path}" in arg for arg in self.scanner_command)
                                     or any("\0" in arg for arg in self.scanner_command)):
            raise ValueError("扫描命令必须包含 {path}，且不能含空字符。")
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}\.bundle", name)
               for name in self.forbidden_bundle_names):
            raise ValueError("程序集资源包黑名单格式不正确。")
        for peer in self.trusted_proxy_ips:
            ipaddress.ip_address(peer)


class IntakeError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


def _response(value, status=200):
    return HTTPResponse(json.dumps(value, ensure_ascii=False), status=status, headers={
        "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"})


def _private_path(path: Path):
    for current in (path, *path.parents):
        if current.exists():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("投稿存储路径不能包含符号链接或重解析点。")
    return path


class Inbox:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = _private_path(Path(settings.data_dir).absolute())
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner_lock = _private_path(self.root / "service.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.owner_lock.seek(0)
                if not self.owner_lock.read(1):
                    self.owner_lock.write(b"0")
                    self.owner_lock.flush()
                self.owner_lock.seek(0)
                msvcrt.locking(self.owner_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.owner_lock.close()
            raise ValueError("投稿目录已经被另一服务占用。") from error
        self.packages = self.root / "quarantine"
        self.records = self.root / "reviewed"
        for directory in (self.packages, self.records):
            _private_path(directory).mkdir(exist_ok=True, mode=0o700)
        self.database = _private_path(self.root / "inbox.sqlite3")
        secret_path = _private_path(self.root / "status-key")
        if not secret_path.exists():
            with secret_path.open("xb") as output:
                os.chmod(secret_path, 0o600)
                output.write(secrets.token_bytes(32))
        self.status_key = secret_path.read_bytes()
        if len(self.status_key) != 32:
            raise ValueError("查询密钥文件损坏。")
        self.rate_lock = threading.Lock()
        self.rate_windows = {}
        self.scan_slots = threading.BoundedSemaphore(1)
        with self.connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                metadata TEXT NOT NULL, status TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, message TEXT NOT NULL DEFAULT '',
                inspection TEXT NOT NULL DEFAULT '{}', scan TEXT NOT NULL DEFAULT '{}',
                review TEXT NOT NULL DEFAULT '{}', github_issue_number INTEGER)""")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(submissions)")}
            if "github_issue_number" not in columns:
                connection.execute("ALTER TABLE submissions ADD COLUMN github_issue_number INTEGER")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS submissions_github_issue ON submissions(github_issue_number) WHERE github_issue_number IS NOT NULL")
            # A single service owns this directory. A previous interrupted stream
            # cannot be resumed and must not keep its reserved quota forever.
            interrupted = connection.execute("SELECT id FROM submissions WHERE status='uploading'").fetchall()
            for row in interrupted:
                path = self.package_path(row["id"], partial=True)
                path.unlink(missing_ok=True)
                connection.execute("DELETE FROM submissions WHERE id=?", (row["id"],))
        os.chmod(self.database, 0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def close(self):
        self.owner_lock.close()

    def package_path(self, identifier, *, partial=False):
        if not ID.fullmatch(identifier):
            raise IntakeError(404, "not_found", "投稿不存在。")
        return _private_path(self.packages / (identifier + (".part" if partial else ".zip")))

    def token(self, identifier):
        digest = hmac.new(self.status_key, identifier.encode("ascii"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def throttle(self, bucket, peer, count, seconds):
        now = time.monotonic()
        with self.rate_lock:
            # Keep hostile source addresses from growing the limiter unbounded.
            self.rate_windows = {key: value for key, value in self.rate_windows.items()
                                 if value[0] > now - 3600}
            key = (bucket, peer)
            start, used = self.rate_windows.get(key, (now, 0))
            if now - start >= seconds:
                start, used = now, 0
            if used >= count or key not in self.rate_windows and len(self.rate_windows) >= 4096:
                raise IntakeError(429, "rate_limited", "操作过于频繁，请稍后再试。")
            self.rate_windows[key] = (start, used + 1)

    def peer(self, environment):
        peer = environment.get("REMOTE_ADDR", "unknown")
        if peer in self.settings.trusted_proxy_ips:
            supplied = environment.get("HTTP_X_REAL_IP", "")
            try:
                return str(ipaddress.ip_address(supplied))
            except ValueError as error:
                raise IntakeError(400, "bad_proxy_peer", "代理来源信息不正确。") from error
        return peer

    def reserve(self, key, metadata, size):
        encoded = json.dumps(metadata, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        request_key = hashlib.sha256(key.encode("ascii")).hexdigest()
        identifier = secrets.token_hex(16)
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM submissions WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                if existing["metadata"] != encoded or existing["size"] != size:
                    raise IntakeError(409, "idempotency_conflict", "这次上传与之前的投稿不一致，请重新投稿。")
                if existing["status"] == "uploading":
                    raise IntakeError(409, "upload_in_progress", "同一投稿还在上传，请稍后查询。")
                return dict(existing), True
            occupied = connection.execute("SELECT COUNT(*) AS count, COALESCE(SUM(size),0) AS size FROM submissions").fetchone()
            if occupied["count"] >= self.settings.max_submissions or occupied["size"] + size > self.settings.quota_bytes:
                raise IntakeError(503, "inbox_full", "投稿箱已满，请稍后再试。")
            connection.execute("INSERT INTO submissions(id,request_key,metadata,status,size,created_at,updated_at) VALUES(?,?,?,'uploading',?,?,?)",
                               (identifier, request_key, encoded, size, now, now))
        return {"id": identifier, "size": size}, False

    def row(self, identifier):
        if not ID.fullmatch(identifier):
            raise IntakeError(404, "not_found", "投稿不存在。")
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM submissions WHERE id=?", (identifier,)).fetchone()
        if row is None or row["status"] == "uploading":
            raise IntakeError(404, "not_found", "投稿不存在。")
        return dict(row)

    def receipt(self, row):
        return {"submission_id": row["id"], "status": row["status"], "status_token": self.token(row["id"]),
                "sha256": row["sha256"], "size": row["size"]}

    def visible(self, row, *, admin=False, compact=False):
        value = {"submission_id": row["id"], "status": row["status"], "metadata": json.loads(row["metadata"]),
                 "sha256": row["sha256"], "size": row["size"], "created_at": row["created_at"],
                 "updated_at": row["updated_at"], "message": row["message"]}
        if admin:
            inspection = json.loads(row["inspection"])
            if compact:
                # Queue pages only need counts. Full file lists and embedded
                # mod_info can be large and stay behind the detail endpoint.
                summary_fields = ("bundle_count", "file_count", "expanded_size",
                                  "inner_expanded_size", "inner_object_count")
                inspection = {key: inspection[key] for key in summary_fields
                              if type(inspection.get(key)) is int and inspection[key] >= 0}
            value.update(inspection=inspection, scan=json.loads(row["scan"]),
                         review=json.loads(row["review"]), github_issue_number=row["github_issue_number"])
        return value

    def bind_github_issue(self, identifier, number):
        if type(number) is not int or not 1 <= number <= 2147483647:
            raise IntakeError(400, "bad_issue_number", "GitHub 审核单编号不正确。")
        self.row(identifier)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT github_issue_number FROM submissions WHERE id=?", (identifier,)).fetchone()[0]
            if current is not None and current != number:
                raise IntakeError(409, "issue_already_bound", "投稿已经关联其它 GitHub 审核单。")
            other = connection.execute("SELECT id FROM submissions WHERE github_issue_number=? AND id!=?", (number, identifier)).fetchone()
            if other:
                raise IntakeError(409, "issue_already_bound", "GitHub 审核单已经关联其它投稿。")
            connection.execute("UPDATE submissions SET github_issue_number=?,updated_at=? WHERE id=?", (number, int(time.time()), identifier))
        return self.visible(self.row(identifier), admin=True)

    def accept(self, source, metadata, key, size):
        row, repeated = self.reserve(key, metadata, size)
        identifier = row["id"]
        temporary = None if repeated else self.package_path(identifier, partial=True)
        digest = hashlib.sha256()
        started = time.monotonic()
        received = 0
        output = None
        try:
            if temporary:
                output = temporary.open("xb")
                os.chmod(temporary, 0o600)
            while received < size:
                if time.monotonic() - started > self.settings.upload_timeout:
                    raise IntakeError(408, "upload_timeout", "上传耗时过长，请重试。")
                block = source.read(min(64 * 1024, size - received))
                if not block:
                    raise IntakeError(400, "incomplete_upload", "上传未完成，请重试。")
                received += len(block)
                digest.update(block)
                if output:
                    output.write(block)
            if output:
                output.flush()
                os.fsync(output.fileno())
                output.close()
                output = None
            if repeated:
                if digest.hexdigest() != row["sha256"]:
                    raise IntakeError(409, "idempotency_conflict", "这次上传与之前的投稿不一致，请重新投稿。")
                return self.receipt(row), 200
            try:
                inspection = inspect_package(temporary, forbidden_bundle_names=self.settings.forbidden_bundle_names)
            except ValueError as error:
                raise IntakeError(400, "unsafe_package", str(error)) from error
            if inspection["sha256"] != digest.hexdigest() or inspection["size"] != size:
                raise IntakeError(400, "changed_package", "上传资源发生变化，请重试。")
            destination = self.package_path(identifier)
            # Each path uses an unpredictable server ID; never a user filename.
            if destination.exists():
                raise IntakeError(409, "storage_conflict", "投稿存储发生冲突。")
            temporary.rename(destination)
            with self.connect() as connection:
                connection.execute("UPDATE submissions SET status='pending',sha256=?,inspection=?,updated_at=? WHERE id=?",
                                   (digest.hexdigest(), json.dumps(inspection, ensure_ascii=False), int(time.time()), identifier))
            return self.receipt(self.row(identifier)), 201
        except (TimeoutError, socket.timeout) as error:
            raise IntakeError(408, "upload_timeout", "上传读取超时，请重试。") from error
        finally:
            if output:
                output.close()
            if not repeated:
                with self.connect() as connection:
                    state = connection.execute("SELECT status FROM submissions WHERE id=?", (identifier,)).fetchone()
                    if state and state["status"] == "uploading":
                        temporary.unlink(missing_ok=True)
                        self.package_path(identifier).unlink(missing_ok=True)
                        connection.execute("DELETE FROM submissions WHERE id=?", (identifier,))

    def scan(self, identifier):
        if not self.settings.scanner_command:
            raise IntakeError(503, "scanner_unavailable", "服务器未配置独立杀毒扫描，不能批准投稿。")
        if not self.scan_slots.acquire(blocking=False):
            raise IntakeError(429, "scanner_busy", "扫描器正在处理其它投稿。")
        try:
            row = self.row(identifier)
            if row["status"] != "pending":
                raise IntakeError(409, "review_closed", "只能扫描待审核投稿。")
            package = self.package_path(identifier)
            with package.open("rb") as content:
                package_digest = hashlib.file_digest(content, "sha256").hexdigest()
            if package_digest != row["sha256"]:
                raise IntakeError(409, "changed_package", "隔离包摘要已变化，不能批准。")
            command = [arg.replace("{path}", str(package)) for arg in self.settings.scanner_command]
            try:
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=self.settings.scan_timeout,
                                        shell=False, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise IntakeError(503, "scan_failed", "独立扫描失败或超时，不能批准。") from error
            with package.open("rb") as content:
                current_digest = hashlib.file_digest(content, "sha256").hexdigest()
            if current_digest != row["sha256"]:
                raise IntakeError(409, "changed_package", "扫描期间隔离包发生变化。")
            record = {"scan_id": secrets.token_hex(16), "sha256": current_digest,
                      "engine": Path(command[0]).name, "scanned_at": int(time.time()),
                      "clean": result.returncode == 0, "exit_code": result.returncode}
            with self.connect() as connection:
                connection.execute("UPDATE submissions SET scan=?,updated_at=? WHERE id=? AND status='pending'",
                                   (json.dumps(record), int(time.time()), identifier))
            return record
        finally:
            self.scan_slots.release()

    def review(self, identifier, payload):
        if not isinstance(payload, dict) or set(payload) != {"decision", "reviewer", "message", "scan_id", "game_test"}:
            raise IntakeError(400, "bad_review", "审核资料字段不正确。")
        decision, reviewer, message = payload["decision"], payload["reviewer"], payload["message"]
        if decision not in ("approve", "reject", "revoke") or not _plain_text(reviewer, 80) or not _plain_text(message, 2000):
            raise IntakeError(400, "bad_review", "审核结论、审核人或说明不正确。")
        row = self.row(identifier)
        previous = json.loads(row["review"])
        expected = "approved" if decision == "approve" else "rejected"
        same_review = (previous.get("decision") == decision and previous.get("reviewer") == reviewer
                       and previous.get("message") == message)
        if decision == "approve":
            same_review = (same_review and previous.get("game_test") == payload["game_test"]
                           and previous.get("scan", {}).get("scan_id") == payload["scan_id"])
        elif payload["scan_id"] != "" or payload["game_test"] is not None:
            raise IntakeError(400, "bad_review", "拒绝或下架不应附带扫描和游戏验证字段。")
        if row["status"] == expected and same_review:
            return self.visible(row, admin=True)
        if (decision == "revoke" and row["status"] != "approved"
                or decision != "revoke" and row["status"] != "pending"):
            raise IntakeError(409, "review_closed", "投稿已经完成审核。")
        review = {"decision": decision, "reviewer": reviewer, "message": message,
                  "reviewed_at": int(time.time())}
        if decision == "revoke":
            review["previous_review"] = previous
        if decision == "approve":
            scan = json.loads(row["scan"])
            game_test = payload["game_test"]
            if (not scan.get("clean") or scan.get("scan_id") != payload["scan_id"]
                    or scan.get("sha256") != row["sha256"] or scan.get("scanned_at", 0) < time.time() - 48 * 3600):
                raise IntakeError(409, "scan_required", "批准前必须通过服务器独立扫描。")
            if (not isinstance(game_test, dict) or set(game_test) != {"passed", "version", "evidence", "notes"}
                    or game_test["passed"] is not True or not _plain_text(game_test["version"], 80)
                    or not _plain_text(game_test["evidence"], 500) or not _plain_text(game_test["notes"], 2000, minimum=20)):
                raise IntakeError(400, "game_test_required", "批准前必须记录游戏版本、测试证据和详细结论。")
            inspection = inspect_package(self.package_path(identifier), forbidden_bundle_names=self.settings.forbidden_bundle_names)
            if inspection["sha256"] != row["sha256"]:
                raise IntakeError(409, "changed_package", "审核时资源包摘要发生变化。")
            review.update(scan=scan, game_test=game_test)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute("UPDATE submissions SET status=?,review=?,message=?,updated_at=? WHERE id=? AND status=?",
                                         ("approved" if decision == "approve" else "rejected", json.dumps(review, ensure_ascii=False),
                                          message, int(time.time()), identifier, row["status"]))
            if changed.rowcount != 1:
                raise IntakeError(409, "review_closed", "投稿已经完成审核。")
            if decision == "approve":
                publication = {"format": "jixing-workshop-reviewed", "version": 1,
                               "submission_id": identifier, "metadata": json.loads(row["metadata"]),
                               "sha256": row["sha256"], "size": row["size"], "review": review,
                               "publication_status": "not_published"}
                record_path = _private_path(self.records / (identifier + ".json"))
                with record_path.open("x", encoding="utf-8") as output:
                    os.chmod(record_path, 0o600)
                    json.dump(publication, output, ensure_ascii=False, indent=2)
            elif decision == "revoke":
                record_path = _private_path(self.records / (identifier + ".revoked.json"))
                with record_path.open("x", encoding="utf-8") as output:
                    os.chmod(record_path, 0o600)
                    json.dump({"submission_id": identifier, "sha256": row["sha256"], "review": review,
                               "publication_status": "revoked"}, output, ensure_ascii=False, indent=2)
        return self.visible(self.row(identifier), admin=True)


def _plain_text(value, limit, *, minimum=1):
    return (isinstance(value, str) and minimum <= len(value.strip()) <= limit
            and not any(ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127 for char in value))


def create_app(settings: Settings):
    inbox = Inbox(settings)
    app = Bottle()
    app.inbox = inbox

    def guarded(action):
        try:
            if request.environ.get("workshop.bad_headers") or request.query_string:
                raise IntakeError(400, "bad_headers", "请求头或地址格式不正确。")
            if request.get_header("Transfer-Encoding"):
                raise IntakeError(400, "unsupported_transfer", "上传必须提供明确的文件长度。")
            return action()
        except IntakeError as error:
            return _response({"error": {"code": error.code, "message": str(error)}}, error.status)
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return _response({"error": {"code": "invalid_request", "message": "请求资料格式不正确。"}}, 400)
        except Exception:
            # No framework traceback, disk path, token or scanner output is sent.
            return _response({"error": {"code": "service_error", "message": "投稿服务暂时不可用，请稍后重试。"}}, 503)

    def length(limit):
        raw = request.get_header("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,10}", raw):
            raise IntakeError(411, "length_required", "请求必须提供有效文件长度。")
        size = int(raw)
        if size <= 0:
            raise IntakeError(400, "empty_body", "请求内容不能为空。")
        if size > limit:
            raise IntakeError(413, "too_large", "请求内容超过大小限制。")
        return size

    def admin():
        supplied = request.get_header("Authorization", "")
        if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + settings.admin_secret).encode("utf-8")):
            raise IntakeError(401, "unauthorized", "维护者验证失败。")

    def read_json():
        if request.get_header("Content-Type", "").split(";")[0].strip() != "application/json":
            raise IntakeError(415, "json_required", "审核请求必须使用 JSON。")
        size = length(MAX_ADMIN_JSON)
        content = request.environ["wsgi.input"].read(size)
        if len(content) != size:
            raise IntakeError(400, "incomplete_upload", "请求内容不完整。")
        return _json(content, MAX_ADMIN_JSON)

    def stream_package(identifier):
        row = inbox.row(identifier)
        if row["status"] == "rejected":
            raise IntakeError(403, "rejected_package", "被拒绝的投稿不能下载。")
        path = inbox.package_path(identifier)
        with path.open("rb") as content:
            digest = hashlib.file_digest(content, "sha256").hexdigest()
        if digest != row["sha256"]:
            raise IntakeError(409, "changed_package", "隔离包摘要已变化。")
        return HTTPResponse(path.open("rb"), headers={"Content-Type": "application/zip",
                            "Content-Length": str(row["size"]), "Content-Disposition": f'attachment; filename="workshop-{identifier}.zip"',
                            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


    @app.get("/v1/health")
    def health():
        return guarded(lambda: _response({"service": "jixing-workshop-upload", "version": 1,
                                          "max_package_bytes": settings.max_package_bytes, "accepting_uploads": True}))


    @app.post("/v1/submissions")
    def upload():
        def action():
            inbox.throttle("upload", inbox.peer(request.environ), settings.uploads_per_hour, 3600)
            if request.get_header("Content-Type", "") != "application/zip":
                raise IntakeError(415, "zip_required", "投稿必须使用 ZIP 资源包。")
            size = length(settings.max_package_bytes)
            encoded = request.get_header("X-Submission-Metadata", "")
            key = request.get_header("Idempotency-Key", "")
            if not IDEMPOTENCY.fullmatch(key) or not METADATA_ENCODING.fullmatch(encoded):
                raise IntakeError(400, "bad_metadata", "投稿资料或重试标识不正确。")
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            metadata = validate_metadata(_json(raw, 16 * 1024))
            receipt, status = inbox.accept(request.environ["wsgi.input"], metadata, key, size)
            return _response(receipt, status)
        return guarded(action)

    @app.get("/v1/submissions/<identifier>")
    def status(identifier):
        def action():
            inbox.throttle("status", inbox.peer(request.environ), settings.status_per_minute, 60)
            supplied = request.get_header("Authorization", "")
            expected = "Bearer " + inbox.token(identifier) if ID.fullmatch(identifier) else ""
            if not expected or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
                raise IntakeError(404, "not_found", "投稿不存在或查询凭证不正确。")
            return _response(inbox.visible(inbox.row(identifier)))
        return guarded(action)

    def list_submissions(page=0, *, pending=False):
        def action():
            admin()
            if page < 0 or page > 100:
                raise IntakeError(400, "bad_page", "页码不正确。")
            state = "status='pending'" if pending else "status!='uploading'"
            with inbox.connect() as connection:
                rows = connection.execute("SELECT * FROM submissions WHERE " + state + " ORDER BY created_at,id LIMIT 100 OFFSET ?", (page * 100,)).fetchall()
                total = connection.execute("SELECT COUNT(*) FROM submissions WHERE " + state).fetchone()[0]
            return _response({"submissions": [inbox.visible(dict(row), admin=True, compact=True) for row in rows],
                              "total": total, "next_page": page + 1 if total > (page + 1) * 100 else None})
        return guarded(action)

    @app.get("/v1/admin/submissions")
    @app.get("/v1/admin/submissions/page/<page:int>")
    def admin_list(page=0):
        return list_submissions(page)

    @app.get("/v1/admin/submissions/pending")
    @app.get("/v1/admin/submissions/pending/page/<page:int>")
    def admin_pending(page=0):
        return list_submissions(page, pending=True)

    @app.get("/v1/admin/submissions/<identifier>")
    def admin_detail(identifier):
        def action():
            admin()
            return _response(inbox.visible(inbox.row(identifier), admin=True))
        return guarded(action)

    @app.get("/v1/admin/submissions/<identifier>/package")
    def admin_package(identifier):
        def action():
            admin()
            return stream_package(identifier)
        return guarded(action)

    @app.post("/v1/admin/submissions/<identifier>/scan")
    def admin_scan(identifier):
        def action():
            admin()
            return _response(inbox.scan(identifier))
        return guarded(action)

    @app.post("/v1/admin/submissions/<identifier>/review")
    def admin_review(identifier):
        def action():
            admin()
            return _response(inbox.review(identifier, read_json()))
        return guarded(action)

    @app.post("/v1/admin/submissions/<identifier>/github-issue")
    def admin_bind_github_issue(identifier):
        def action():
            admin()
            value = read_json()
            if set(value) != {"issue_number"}:
                raise IntakeError(400, "bad_issue_number", "GitHub 审核单资料字段不正确。")
            return _response(inbox.bind_github_issue(identifier, value["issue_number"]))
        return guarded(action)

    @app.error(404)
    def missing(_):
        return _response({"error": {"code": "not_found", "message": "接口不存在。"}}, 404)

    return app


class QuietHandler(WSGIRequestHandler):
    def handle(self):
        self.connection.settimeout(self.server.settings.read_timeout)
        try:
            super().handle()
        except (TimeoutError, socket.timeout, ConnectionError):
            pass

    def get_environ(self):
        value = super().get_environ()
        header_bytes = sum(len(key) + len(item) + 4 for key, item in self.headers.items())
        protected = ("Content-Length", "Transfer-Encoding", "Authorization", "X-Submission-Metadata", "Idempotency-Key", "X-Real-IP")
        value["workshop.bad_headers"] = header_bytes > 32 * 1024 or any(len(self.headers.get_all(key, [])) > 1 for key in protected)
        return value

    def log_message(self, format, *args):
        # URL and headers can contain capability tokens in hostile requests.
        pass


class BoundedServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True

    def configure(self, settings):
        self.settings = settings
        self.worker_slots = threading.BoundedSemaphore(settings.max_workers)

    def process_request(self, incoming, address):
        if not self.worker_slots.acquire(blocking=False):
            try:
                incoming.settimeout(1)
                incoming.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(incoming)
            return
        try:
            super().process_request(incoming, address)
        except Exception:
            self.worker_slots.release()
            raise

    def process_request_thread(self, incoming, address):
        try:
            super().process_request_thread(incoming, address)
        finally:
            self.worker_slots.release()

    def server_close(self):
        super().server_close()
        application = self.get_app()
        if application is not None and hasattr(application, "inbox"):
            application.inbox.close()


def make_http_server(settings, host="127.0.0.1", port=0):
    server = make_server(host, port, create_app(settings), server_class=BoundedServer, handler_class=QuietHandler)
    server.configure(settings)
    return server


def main():
    parser = argparse.ArgumentParser(description="JiXing Mod 工坊隔离投稿箱")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    root = os.environ.get("WORKSHOP_DATA_DIR")
    if not root:
        parser.error("需要设置 WORKSHOP_DATA_DIR 为独立的私有存储目录。")
    try:
        settings = Settings(Path(root), os.environ.get("WORKSHOP_ADMIN_SECRET", ""),
                            max_package_bytes=int(os.environ.get("WORKSHOP_MAX_UPLOAD_BYTES", str(MAX_PACKAGE_BYTES))),
                            quota_bytes=int(os.environ.get("WORKSHOP_QUOTA_BYTES", str(2 * 1024**3))),
                            max_submissions=int(os.environ.get("WORKSHOP_MAX_SUBMISSIONS", "1000")),
                            max_workers=int(os.environ.get("WORKSHOP_MAX_WORKERS", "4")),
                            read_timeout=float(os.environ.get("WORKSHOP_READ_TIMEOUT", "10")),
                            upload_timeout=float(os.environ.get("WORKSHOP_UPLOAD_TIMEOUT", "180")),
                            uploads_per_hour=int(os.environ.get("WORKSHOP_UPLOADS_PER_HOUR", "10")),
                            status_per_minute=int(os.environ.get("WORKSHOP_STATUS_PER_MINUTE", "60")),
                            scan_timeout=float(os.environ.get("WORKSHOP_SCAN_TIMEOUT", "120")),
                            trusted_proxy_ips=tuple(json.loads(os.environ.get("WORKSHOP_TRUSTED_PROXY_IPS", "[]"))),
                            forbidden_bundle_names=tuple(json.loads(os.environ.get("WORKSHOP_FORBIDDEN_BUNDLES", "[]"))),
                            scanner_command=tuple(json.loads(os.environ.get("WORKSHOP_SCANNER_COMMAND", "[]"))))
        server = make_http_server(settings, args.host, args.port)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"工坊投稿箱监听 {args.host}:{server.server_port}；投稿不会自动上架。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
