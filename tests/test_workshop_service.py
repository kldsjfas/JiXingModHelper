"""Real HTTP streams into an isolated inbox; antivirus uses a QA stub only."""
import base64
import hashlib
import http.client
import json
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

from tests.test_workshop import metadata, pack_bytes
from tests.workshop_fixtures import pe_payload, resource_bundle
from workshop_service.server import Inbox, Settings, make_http_server


class WorkshopServiceTests(unittest.TestCase):
    def setUp(self):
        cache = os.environ.get("WORKSHOP_TEST_CACHE")
        if cache:
            Path(cache).mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=cache)
        self.root = Path(self.temporary.name)
        self.settings = Settings(self.root / "inbox", "a" * 48, max_package_bytes=128 * 1024,
                                 quota_bytes=256 * 1024, uploads_per_hour=100, status_per_minute=100,
                                 read_timeout=0.5, upload_timeout=3)
        self.servers = []
        self.server, self.thread = self.start(self.settings)
        self.payload = pack_bytes()

    def start(self, settings):
        server = make_http_server(settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server, thread

    def tearDown(self):
        for server, thread in self.servers:
            server.shutdown()
            thread.join(3)
            server.server_close()
        self.temporary.cleanup()

    def call(self, method, path, payload=None, headers=None, *, server=None):
        connection = http.client.HTTPConnection("127.0.0.1", (server or self.server).server_port, timeout=5)
        try:
            connection.request(method, path, body=payload, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            content_type = response.getheader("Content-Type", "")
            value = json.loads(body) if content_type.startswith("application/json") else body
            return response.status, value
        finally:
            connection.close()

    def upload_headers(self, value=None, key=None):
        encoded = base64.urlsafe_b64encode(json.dumps(value or metadata(), ensure_ascii=False).encode()).decode().rstrip("=")
        return {"Content-Type": "application/zip", "X-Submission-Metadata": encoded,
                "Idempotency-Key": key or secrets.token_urlsafe(32)}

    def upload(self, payload=None, headers=None, *, server=None):
        return self.call("POST", "/v1/submissions", self.payload if payload is None else payload,
                         headers or self.upload_headers(), server=server)

    def admin_headers(self, *, json_body=False):
        result = {"Authorization": "Bearer " + self.settings.admin_secret}
        if json_body:
            result["Content-Type"] = "application/json"
        return result

    def review(self, receipt, decision="approve", scan_id="", game_test=None):
        value = {"decision": decision, "reviewer": "QA maintainer", "message": "人工审核测试结论",
                 "scan_id": scan_id, "game_test": game_test}
        return self.call("POST", f"/v1/admin/submissions/{receipt['submission_id']}/review",
                         json.dumps(value).encode(), self.admin_headers(json_body=True))

    def test_account_free_upload_is_pending_and_private(self):
        status, receipt = self.upload()
        self.assertEqual(status, 201)
        self.assertEqual(receipt["status"], "pending")
        self.assertRegex(receipt["submission_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(receipt["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertEqual(receipt["size"], len(self.payload))
        token = receipt["status_token"]
        path = "/v1/submissions/" + receipt["submission_id"]
        self.assertEqual(self.call("GET", path)[0], 404)
        self.assertEqual(self.call("GET", path, headers={"Authorization": "Bearer wrong"})[0], 404)
        status, detail = self.call("GET", path, headers={"Authorization": "Bearer " + token})
        self.assertEqual(status, 200)
        self.assertEqual(detail["metadata"], metadata())
        self.assertNotIn("status_token", detail)
        self.assertNotIn("inspection", detail)
        self.assertEqual(self.call("GET", path + "/package", headers={"Authorization": "Bearer " + token})[0], 404)
        self.assertEqual(list(self.server.get_app().inbox.records.iterdir()), [])

    def test_identical_retry_is_idempotent_and_keeps_secret(self):
        headers = self.upload_headers()
        first_status, first = self.upload(headers=headers)
        second_status, second = self.upload(headers=headers)
        self.assertEqual((first_status, second_status), (201, 200))
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.settings.data_dir / "quarantine").iterdir())), 1)

    def test_retry_cannot_change_metadata_or_same_sized_payload(self):
        headers = self.upload_headers()
        _, receipt = self.upload(headers=headers)
        changed = bytearray(self.payload)
        changed[10] ^= 1
        self.assertEqual(self.upload(payload=bytes(changed), headers=headers)[0], 409)
        replacement = self.upload_headers({**metadata(), "name": "另一个作品"}, headers["Idempotency-Key"])
        self.assertEqual(self.upload(headers=replacement)[0], 409)
        self.assertEqual((self.settings.data_dir / "quarantine" / (receipt["submission_id"] + ".zip")).read_bytes(), self.payload)

    def test_distinct_submissions_cannot_overwrite_each_other(self):
        _, first = self.upload()
        _, second = self.upload()
        self.assertNotEqual(first["submission_id"], second["submission_id"])
        self.assertEqual(len(list((self.settings.data_dir / "quarantine").iterdir())), 2)

    def test_content_type_and_declared_limits(self):
        headers = self.upload_headers()
        headers["Content-Type"] = "multipart/form-data"
        self.assertEqual(self.upload(headers=headers)[0], 415)
        headers = self.upload_headers()
        headers["Content-Length"] = str(self.settings.max_package_bytes + 1)
        self.assertEqual(self.upload(headers=headers)[0], 413)
        headers["Content-Length"] = "-1"
        self.assertEqual(self.upload(headers=headers)[0], 411)
        headers["Content-Length"] = "0"
        self.assertEqual(self.upload(headers=headers)[0], 400)

    def test_missing_length_and_chunked_transfer_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("POST", "/v1/submissions")
        for key, value in self.upload_headers().items():
            connection.putheader(key, value)
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 411)
        response.read()
        connection.close()
        headers = self.upload_headers()
        headers["Transfer-Encoding"] = "chunked"
        self.assertEqual(self.upload(headers=headers)[0], 400)

    def test_duplicate_length_header_rejected(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("POST", "/v1/submissions")
        for key, value in self.upload_headers().items():
            connection.putheader(key, value)
        connection.putheader("Content-Length", str(len(self.payload)))
        connection.putheader("Content-Length", str(len(self.payload)))
        connection.endheaders(self.payload)
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.close()

    def test_metadata_is_exact_bounded_json(self):
        cases = [dict(metadata(), unexpected="x"), {**metadata(), "id": "../../x"},
                 {**metadata(), "description": "x" * 2001}]
        for value in cases:
            with self.subTest(value=value.get("id")):
                self.assertEqual(self.upload(headers=self.upload_headers(value))[0], 400)
        headers = self.upload_headers()
        headers["X-Submission-Metadata"] = "a" * 24577
        self.assertEqual(self.upload(headers=headers)[0], 400)
        headers["X-Submission-Metadata"] = "%%%"
        self.assertEqual(self.upload(headers=headers)[0], 400)
        raw = json.dumps(metadata())[:-1] + ',"name":"duplicate"}'
        headers["X-Submission-Metadata"] = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        self.assertEqual(self.upload(headers=headers)[0], 400)

    def test_query_tokens_not_accepted_in_url(self):
        self.assertEqual(self.call("GET", "/v1/health?secret=do-not-log")[0], 400)
        headers = self.upload_headers()
        headers["Idempotency-Key"] = "short"
        self.assertEqual(self.upload(headers=headers)[0], 400)

    def test_archive_attacks_and_hidden_programs_rejected(self):
        packages = [b"not-a-zip", pack_bytes([( "../escape.bundle", resource_bundle())]),
                    pack_bytes([("tool.exe", b"MZ dangerous")]),
                    pack_bytes([("texture.bundle", resource_bundle(pe_payload()))]),
                    pack_bytes([("texture.bundle", resource_bundle(b"data", name="Runtime.dll"))])]
        for payload in packages:
            with self.subTest(size=len(payload)):
                status, error = self.upload(payload=payload)
                self.assertEqual(status, 400)
                self.assertEqual(error["error"]["code"], "unsafe_package")
        self.assertEqual(list((self.settings.data_dir / "quarantine").iterdir()), [])
        with self.server.get_app().inbox.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 0)

    def test_assembly_target_blacklist_applies_on_server(self):
        other, _ = self.start(replace(self.settings, data_dir=self.root / "blocked", forbidden_bundle_names=("texture.bundle",)))
        self.assertEqual(self.upload(server=other)[0], 400)

    def test_short_stream_rolls_back_reserved_quota_and_partial_file(self):
        stream = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        headers = self.upload_headers()
        headers["Content-Length"] = str(len(self.payload))
        head = "POST /v1/submissions HTTP/1.0\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
        stream.sendall(head.encode("ascii") + self.payload[:20])
        stream.shutdown(socket.SHUT_WR)
        response = b""
        while chunk := stream.recv(4096):
            response += chunk
        stream.close()
        self.assertIn(b"400", response.split(b"\r\n", 1)[0])
        self.assertEqual(list((self.settings.data_dir / "quarantine").iterdir()), [])
        self.assertEqual(self.upload()[0], 201)

    def test_slow_stream_timeout_rolls_back(self):
        stream = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        headers = self.upload_headers()
        headers["Content-Length"] = str(len(self.payload))
        head = "POST /v1/submissions HTTP/1.0\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers.items()) + "\r\n"
        stream.sendall(head.encode("ascii"))
        response = stream.recv(4096)
        stream.close()
        self.assertIn(b"408", response.split(b"\r\n", 1)[0])
        self.assertEqual(list((self.settings.data_dir / "quarantine").iterdir()), [])

    def test_quota_and_pending_count_fail_closed(self):
        settings = replace(self.settings, data_dir=self.root / "quota", max_package_bytes=len(self.payload), quota_bytes=len(self.payload))
        other, _ = self.start(settings)
        self.assertEqual(self.upload(server=other)[0], 201)
        self.assertEqual(self.upload(server=other)[0], 503)
        settings = replace(self.settings, data_dir=self.root / "count", max_submissions=1)
        other, _ = self.start(settings)
        self.assertEqual(self.upload(server=other)[0], 201)
        self.assertEqual(self.upload(server=other)[0], 503)

    def test_public_rate_limits(self):
        other, _ = self.start(replace(self.settings, data_dir=self.root / "limited", uploads_per_hour=1, status_per_minute=1))
        _, receipt = self.upload(server=other)
        self.assertEqual(self.upload(server=other)[0], 429)
        path = "/v1/submissions/" + receipt["submission_id"]
        headers = {"Authorization": "Bearer " + receipt["status_token"]}
        self.assertEqual(self.call("GET", path, headers=headers, server=other)[0], 200)
        self.assertEqual(self.call("GET", path, headers=headers, server=other)[0], 429)

    def test_bounded_http_workers(self):
        other, _ = self.start(replace(self.settings, data_dir=self.root / "workers", max_workers=1, read_timeout=2))
        stream = socket.create_connection(("127.0.0.1", other.server_port), timeout=3)
        stream.sendall(b"POST /v1/submissions HTTP/1.0\r\n")
        deadline = time.monotonic() + 1
        while other.worker_slots._value != 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            self.assertEqual(self.call("GET", "/v1/health", server=other)[0], 503)
        except (ConnectionAbortedError, ConnectionResetError, http.client.RemoteDisconnected):
            # An immediate close with unread request bytes can be reported as
            # reset by Windows. The rejected socket must not spawn a worker.
            self.assertEqual(other.worker_slots._value, 0)
        stream.close()

    def test_maintainer_auth_protects_every_admin_route(self):
        _, receipt = self.upload()
        base = "/v1/admin/submissions/" + receipt["submission_id"]
        for method, path in [("GET", "/v1/admin/submissions"), ("GET", base), ("GET", base + "/package"),
                             ("POST", base + "/scan"), ("POST", base + "/review")]:
            with self.subTest(path=path):
                self.assertEqual(self.call(method, path)[0], 401)
        self.assertEqual(self.call("GET", "/v1/admin/submissions", headers=self.admin_headers())[0], 200)
        status, content = self.call("GET", base + "/package", headers=self.admin_headers())
        self.assertEqual(status, 200)
        self.assertEqual(content, self.payload)

    def test_rejected_package_not_downloadable_or_approvable(self):
        _, receipt = self.upload()
        status, result = self.review(receipt, "reject")
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "rejected")
        path = "/v1/admin/submissions/" + receipt["submission_id"]
        self.assertEqual(self.call("GET", path + "/package", headers=self.admin_headers())[0], 403)
        self.assertEqual(self.review(receipt)[0], 409)
        self.assertEqual(list(self.server.get_app().inbox.records.iterdir()), [])

    def test_cannot_approve_without_independent_scan(self):
        _, receipt = self.upload()
        self.assertEqual(self.review(receipt)[1]["error"]["code"], "scan_required")
        path = "/v1/admin/submissions/" + receipt["submission_id"] + "/scan"
        self.assertEqual(self.call("POST", path, headers=self.admin_headers())[0], 503)

    def test_scan_game_evidence_gate_and_nonpublication(self):
        # This child process is a deterministic scanner substitute for protocol
        # tests. Production uses an independently installed antivirus engine.
        self.server.get_app().inbox.settings = replace(self.settings, scanner_command=(sys.executable, "-c", "import sys;sys.exit(0)", "{path}"))
        _, receipt = self.upload()
        path = "/v1/admin/submissions/" + receipt["submission_id"] + "/scan"
        status, scan = self.call("POST", path, headers=self.admin_headers())
        self.assertEqual(status, 200)
        self.assertTrue(scan["clean"])
        self.assertEqual(scan["sha256"], receipt["sha256"])
        self.assertEqual(self.review(receipt, scan_id=scan["scan_id"])[1]["error"]["code"], "game_test_required")
        evidence = {"passed": True, "version": "QA-fixture-only", "evidence": "qa-evidence/fixture-test",
                    "notes": "仅供 HTTP 服务测试使用，不表示真实游戏播放或独立杀毒检查成功。"}
        status, result = self.review(receipt, scan_id=scan["scan_id"], game_test=evidence)
        self.assertEqual(status, 200)
        self.assertEqual(result["status"], "approved")
        record = json.loads((self.settings.data_dir / "reviewed" / (receipt["submission_id"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(record["publication_status"], "not_published")
        self.assertEqual(record["sha256"], receipt["sha256"])
        self.assertFalse(any("github" in str(value) for value in record.values()))

    def test_failed_scan_cannot_approve(self):
        self.server.get_app().inbox.settings = replace(self.settings, scanner_command=(sys.executable, "-c", "import sys;sys.exit(1)", "{path}"))
        _, receipt = self.upload()
        _, scan = self.call("POST", "/v1/admin/submissions/" + receipt["submission_id"] + "/scan", headers=self.admin_headers())
        self.assertFalse(scan["clean"])
        self.assertEqual(self.review(receipt, scan_id=scan["scan_id"])[0], 409)

    def test_changed_package_breaks_prior_scan_and_download(self):
        self.server.get_app().inbox.settings = replace(self.settings, scanner_command=(sys.executable, "-c", "import sys;sys.exit(0)", "{path}"))
        _, receipt = self.upload()
        base = "/v1/admin/submissions/" + receipt["submission_id"]
        _, scan = self.call("POST", base + "/scan", headers=self.admin_headers())
        path = self.settings.data_dir / "quarantine" / (receipt["submission_id"] + ".zip")
        path.write_bytes(pack_bytes([("texture.bundle", resource_bundle(b"modified"))]))
        self.assertEqual(self.call("GET", base + "/package", headers=self.admin_headers())[0], 409)
        self.assertEqual(self.call("POST", base + "/scan", headers=self.admin_headers())[0], 409)
        evidence = {"passed": True, "version": "fixture", "evidence": "fixture", "notes": "这是测试证据，仅检查摘要绑定，并非真实的游戏测试证据。"}
        self.assertEqual(self.review(receipt, scan_id=scan["scan_id"], game_test=evidence)[0], 409)

    def test_private_storage_lock_and_reparse_rejection(self):
        with self.assertRaisesRegex(ValueError, "另一服务"):
            Inbox(self.settings)
        if os.name != "nt":
            link = self.root / "linked"
            link.symlink_to(self.settings.data_dir, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "符号链接"):
                Inbox(replace(self.settings, data_dir=link))

    def test_health_does_not_leak_server_configuration(self):
        status, body = self.call("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        self.assertNotIn(self.settings.admin_secret, json.dumps(body))
        self.assertNotIn(str(self.settings.data_dir), json.dumps(body))

    def test_github_issue_binding_is_admin_only_and_idempotent(self):
        _, receipt = self.upload()
        path = "/v1/admin/submissions/" + receipt["submission_id"] + "/github-issue"
        payload = json.dumps({"issue_number": 123}).encode()
        self.assertEqual(self.call("POST", path, payload, {"Content-Type": "application/json"})[0], 401)
        for _ in range(2):
            status, result = self.call("POST", path, payload, self.admin_headers(json_body=True))
            self.assertEqual(status, 200)
            self.assertEqual(result["github_issue_number"], 123)
        self.assertEqual(self.call("POST", path, b'{"issue_number":124}', self.admin_headers(json_body=True))[0], 409)
        for value in [True, -1, "123", 2**32]:
            self.assertEqual(self.call("POST", path, json.dumps({"issue_number": value}).encode(), self.admin_headers(json_body=True))[0], 400)
        self.assertEqual(self.call("POST", path, b'{"issue_number":123,"url":"https://evil.invalid"}', self.admin_headers(json_body=True))[0], 400)
        public = "/v1/submissions/" + receipt["submission_id"]
        _, result = self.call("GET", public, headers={"Authorization": "Bearer " + receipt["status_token"]})
        self.assertNotIn("github_issue_number", result)

    def test_no_admin_webpage_or_automatic_publication_endpoints(self):
        for path in ["/admin", "/v1/catalog", "/v1/packages/" + "a" * 32]:
            self.assertEqual(self.call("GET", path)[0], 404)

    def test_trusted_proxy_requires_real_ip_and_rejects_spoofing_elsewhere(self):
        other, _ = self.start(replace(self.settings, data_dir=self.root / "proxy", uploads_per_hour=1, trusted_proxy_ips=("127.0.0.1",)))
        self.assertEqual(self.upload(server=other)[0], 400)
        first = self.upload_headers()
        first["X-Real-IP"] = "203.0.113.1"
        self.assertEqual(self.upload(headers=first, server=other)[0], 201)
        second = self.upload_headers()
        second["X-Real-IP"] = "203.0.113.2"
        self.assertEqual(self.upload(headers=second, server=other)[0], 201)
        third = self.upload_headers()
        third["X-Real-IP"] = "203.0.113.1"
        self.assertEqual(self.upload(headers=third, server=other)[0], 429)
        inbox = self.server.get_app().inbox
        self.assertEqual(inbox.peer({"REMOTE_ADDR": "198.51.100.9", "HTTP_X_REAL_IP": "203.0.113.1"}), "198.51.100.9")

    def test_restart_keeps_status_key_and_clears_interrupted_upload(self):
        settings = replace(self.settings, data_dir=self.root / "restart")
        inbox = Inbox(settings)
        token = inbox.token("a" * 32)
        row, duplicate = inbox.reserve(secrets.token_urlsafe(32), metadata(), 100)
        self.assertFalse(duplicate)
        inbox.package_path(row["id"], partial=True).write_bytes(b"partial")
        inbox.close()
        recovered = Inbox(settings)
        try:
            self.assertEqual(recovered.token("a" * 32), token)
            self.assertEqual(list(recovered.packages.iterdir()), [])
            with recovered.connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 0)
        finally:
            recovered.close()

    def test_scanner_timeout_does_not_create_clean_evidence(self):
        self.server.get_app().inbox.settings = replace(self.settings, scanner_command=(sys.executable, "-c", "import time;time.sleep(2)", "{path}"), scan_timeout=0.1)
        _, receipt = self.upload()
        path = "/v1/admin/submissions/" + receipt["submission_id"] + "/scan"
        self.assertEqual(self.call("POST", path, headers=self.admin_headers())[0], 503)
        self.assertEqual(self.review(receipt)[1]["error"]["code"], "scan_required")

    def test_parallel_same_key_cannot_overwrite_pending_stream(self):
        headers = self.upload_headers()
        stream = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3)
        wire_headers = {**headers, "Content-Length": str(len(self.payload))}
        head = "POST /v1/submissions HTTP/1.0\r\n" + "".join(f"{key}: {value}\r\n" for key, value in wire_headers.items()) + "\r\n"
        stream.sendall(head.encode("ascii") + self.payload[:20])
        inbox = self.server.get_app().inbox
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline:
            with inbox.connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM submissions WHERE status='uploading'").fetchone()[0]
            if count:
                break
            time.sleep(0.01)
        self.assertEqual(count, 1)
        self.assertEqual(self.upload(headers=headers)[0], 409)
        stream.sendall(self.payload[20:])
        response = b""
        while block := stream.recv(4096):
            response += block
        stream.close()
        self.assertIn(b"201", response.split(b"\r\n", 1)[0])
        self.assertEqual(self.upload(headers=headers)[0], 200)

    def test_pending_list_excludes_reviewed_and_reject_is_idempotent(self):
        _, receipt = self.upload()
        for _ in range(2):
            self.assertEqual(self.review(receipt, "reject")[0], 200)
        _, pending = self.call("GET", "/v1/admin/submissions/pending", headers=self.admin_headers())
        self.assertEqual(pending, {"submissions": [], "total": 0, "next_page": None})
        self.assertEqual(self.call("GET", "/v1/admin/submissions/pending")[0], 401)
        self.assertEqual(self.call("GET", "/v1/admin/submissions/pending/page/0", headers=self.admin_headers())[0], 200)

    def test_approved_submission_can_be_revoked_idempotently(self):
        inbox = self.server.get_app().inbox
        inbox.settings = replace(self.settings, scanner_command=(sys.executable, "-c", "import sys;sys.exit(0)", "{path}"))
        _, receipt = self.upload()
        base = "/v1/admin/submissions/" + receipt["submission_id"]
        _, scan = self.call("POST", base + "/scan", headers=self.admin_headers())
        evidence = {"passed": True, "version": "QA-fixture", "evidence": "fixture", "notes": "这是隔离服务的测试证据，仅检查审查流程，不表示实机验收。"}
        for _ in range(2):
            self.assertEqual(self.review(receipt, scan_id=scan["scan_id"], game_test=evidence)[0], 200)
        for _ in range(2):
            status, result = self.review(receipt, "revoke")
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.call("GET", base + "/package", headers=self.admin_headers())[0], 403)
        self.assertEqual(result["review"]["previous_review"]["decision"], "approve")
        self.assertTrue((inbox.records / (receipt["submission_id"] + ".revoked.json")).is_file())

    def test_one_github_issue_cannot_bind_multiple_submissions(self):
        _, first = self.upload()
        _, second = self.upload()
        for receipt, expected in [(first, 200), (second, 409)]:
            path = "/v1/admin/submissions/" + receipt["submission_id"] + "/github-issue"
            self.assertEqual(self.call("POST", path, b'{"issue_number":100}', self.admin_headers(json_body=True))[0], expected)

    def test_expired_scan_evidence_cannot_approve(self):
        inbox = self.server.get_app().inbox
        inbox.settings = replace(self.settings, scanner_command=(sys.executable, "-c", "import sys;sys.exit(0)", "{path}"))
        _, receipt = self.upload()
        _, scan = self.call("POST", "/v1/admin/submissions/" + receipt["submission_id"] + "/scan", headers=self.admin_headers())
        scan["scanned_at"] = int(time.time()) - 49 * 3600
        with inbox.connect() as connection:
            connection.execute("UPDATE submissions SET scan=? WHERE id=?", (json.dumps(scan), receipt["submission_id"]))
        evidence = {"passed": True, "version": "fixture", "evidence": "fixture", "notes": "这是测试证据，仅验证扫描期限约束，不是真实游戏测试证据。"}
        self.assertEqual(self.review(receipt, scan_id=scan["scan_id"], game_test=evidence)[1]["error"]["code"], "scan_required")

    def test_admin_queue_summaries_omit_large_inspection_but_details_preserve_it(self):
        _, receipt = self.upload()
        inbox = self.server.get_app().inbox
        inspection = {"bundle_count": 512, "file_count": 512, "expanded_size": 123456,
                      "inner_expanded_size": 234567, "inner_object_count": 1234,
                      "files": [{"path": f"bundle-{number:03d}.bundle", "sha256": "a" * 64,
                                 "description": "x" * 1024} for number in range(512)],
                      "bundles": [{"path": "x" * 200} for _ in range(512)],
                      "bundle_checks": [{"objects": list(range(100))} for _ in range(512)],
                      "mod_info": {"large": "x" * 256000}}
        with inbox.connect() as connection:
            connection.execute("UPDATE submissions SET inspection=? WHERE id=?", (json.dumps(inspection), receipt["submission_id"]))
        for path in ["/v1/admin/submissions", "/v1/admin/submissions/pending"]:
            status, value = self.call("GET", path, headers=self.admin_headers())
            self.assertEqual(status, 200)
            summary = value["submissions"][0]
            self.assertEqual(summary["inspection"], {"bundle_count": 512, "file_count": 512,
                                                   "expanded_size": 123456, "inner_expanded_size": 234567,
                                                   "inner_object_count": 1234})
            self.assertIn("scan", summary)
            self.assertIn("review", summary)
            self.assertLess(len(json.dumps(value).encode()), 4096)
        path = "/v1/admin/submissions/" + receipt["submission_id"]
        status, detailed = self.call("GET", path, headers=self.admin_headers())
        self.assertEqual(status, 200)
        self.assertEqual(detailed["inspection"], inspection)


if __name__ == "__main__":
    unittest.main()
