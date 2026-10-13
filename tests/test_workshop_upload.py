"""投稿传输只连接本机模拟服务，覆盖真实流式请求和不可信响应。"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from astral_party_auto.modkit.workshop import MAX_PACKAGE_BYTES
from astral_party_auto.modkit.workshop_upload import BLOCK_BYTES, WorkshopUploadClient
from tests.test_workshop import metadata


class UploadService(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _reply(self, status, payload, content_type="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        service = self.server
        service.paths.append(self.path)
        route = self.path.removeprefix(service.base_path)
        if route == "/v1/health":
            payload = {"service": "jixing-workshop-upload", "version": 1,
                       "max_package_bytes": service.upload_limit, "accepting_uploads": service.accepting}
            if service.mode == "bad_health":
                payload["version"] = True
            self._reply(200, payload)
            return
        service.queries.append((self.path, self.headers.get("Authorization")))
        if service.mode == "invalid_status":
            self._reply(200, {"submission_id": "b" * 32, "status": "approved"})
            return
        submission = service.posts[-1]
        self._reply(200, {"submission_id": "a" * 32, "status": service.review_status,
                          "metadata": submission["metadata"], "sha256": submission["sha256"],
                          "size": submission["size"], "created_at": "2026-10-13T00:00:00Z",
                          "updated_at": "2026-10-13T01:00:00Z", "message": service.private_token})

    def do_POST(self):
        service = self.server
        service.paths.append(self.path)
        count = int(self.headers["Content-Length"])
        digest = hashlib.sha256()
        blocks = []
        remaining = count
        while remaining:
            block = self.rfile.read(min(32 * 1024, remaining))
            digest.update(block)
            blocks.append(len(block))
            remaining -= len(block)
        metadata_header = self.headers["X-Submission-Metadata"]
        details = json.loads(base64.urlsafe_b64decode(metadata_header + "=" * (-len(metadata_header) % 4)))
        service.posts.append({"metadata": details, "headers": dict(self.headers), "size": count,
                              "sha256": digest.hexdigest(), "blocks": blocks})
        result = {"submission_id": "a" * 32, "status": "pending", "status_token": service.private_token,
                  "sha256": digest.hexdigest(), "size": count}
        if service.mode == "redirect":
            self.send_response(307)
            self.send_header("Location", "https://untrusted.example/receive")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if service.mode == "http_error":
            self._reply(503, {"error": {"code": "busy", "message": service.private_token}})
            return
        if service.mode == "mismatch":
            result["sha256"] = "b" * 64
        elif service.mode == "not_pending":
            result["status"] = "uploading"
        elif service.mode in ("approved_receipt", "rejected_receipt"):
            result["status"] = service.mode.split("_", 1)[0]
        elif service.mode == "token_type":
            result["status_token"] = {"unexpected": service.private_token}
        elif service.mode == "extra_fields":
            result["debug"] = service.private_token
        elif service.mode == "invalid_json":
            self._reply(201, b'{"status":"pending","status":"pending"}')
            return
        elif service.mode == "huge_response":
            self._reply(201, b" " * (64 * 1024 + 1))
            return
        elif service.mode == "wrong_type":
            self._reply(201, result, "text/plain")
            return
        self._reply(201, result)


class WorkshopUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="workshop-upload-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "private-source-name.zip"
        self.payload = os.urandom(BLOCK_BYTES * 3 + 123)
        self.package.write_bytes(self.payload)
        self.sha256 = hashlib.sha256(self.payload).hexdigest()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), UploadService)
        self.server.daemon_threads = True
        self.server.posts = []
        self.server.queries = []
        self.server.mode = "normal"
        self.server.accepting = True
        self.server.review_status = "pending"
        self.server.paths = []
        self.server.base_path = ""
        self.server.upload_limit = MAX_PACKAGE_BYTES
        self.server.private_token = "s" * 43
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = WorkshopUploadClient(self.root / "data", service_url=self.url, allow_local_http=True)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def upload(self):
        return self.client.upload(self.package, metadata(), expected_sha256=self.sha256,
                                  expected_size=len(self.payload))

    def test_unconfigured_is_honest_and_never_networks(self):
        client = WorkshopUploadClient(self.root, service_url="")
        with patch.object(client, "_connection") as connection:
            state = client.snapshot(refresh=True)
            self.assertFalse(state["configured"])
            self.assertFalse(state["accepting_uploads"])
            with self.assertRaisesRegex(RuntimeError, "尚未部署"):
                client.upload(self.package, metadata(), expected_sha256=self.sha256,
                              expected_size=len(self.payload))
            connection.assert_not_called()

    def test_untrusted_service_addresses_are_rejected(self):
        for url, allow_local in ((self.url, False), ("http://attacker.example", True),
                                 ("https://name:password@example.com", False),
                                 ("https://example.com/../extra", False), ("https://example.com?token=secret", False),
                                 ("https://example.com#fragment", False), ("https://example.com\n", False),
                                 ("file:///private", False), ("https://example.com:99999", False)):
            with self.subTest(url=url), self.assertRaises(ValueError):
                WorkshopUploadClient(self.root, service_url=url, allow_local_http=allow_local)

    def test_real_request_streams_zip_without_local_paths_or_secret_in_result(self):
        reads = []
        actual_open = Path.open

        class LimitedReader:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()

            def read(self, count=-1):
                reads.append(count)
                if count < 0 or count > BLOCK_BYTES:
                    raise AssertionError("包不应一次加载到内存")
                return self.stream.read(count)

            def seek(self, *args):
                return self.stream.seek(*args)

            def tell(self):
                return self.stream.tell()

        def limited_open(path, *args, **kwargs):
            stream = actual_open(path, *args, **kwargs)
            return LimitedReader(stream) if path == self.package else stream

        with patch.object(Path, "open", limited_open):
            result = self.upload()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["sha256"], self.sha256)
        self.assertEqual(result["size"], len(self.payload))
        post = self.server.posts[0]
        self.assertEqual(post["headers"]["Content-Type"], "application/zip")
        self.assertEqual(post["sha256"], self.sha256)
        self.assertEqual(post["metadata"], metadata())
        self.assertGreater(len(post["blocks"]), 1)
        self.assertTrue(reads)
        public = json.dumps(result) + json.dumps(self.client.snapshot())
        self.assertNotIn(self.server.private_token, public)
        self.assertNotIn("Idempotency-Key", public)
        self.assertNotIn(str(self.root), json.dumps(post["metadata"]))

    def test_private_receipt_survives_restart_and_queries_without_js_secret(self):
        result = self.upload()
        if os.name == "nt":
            self.assertNotIn(self.server.private_token.encode(), self.client._receipt_path.read_bytes())
        else:
            self.assertEqual(stat.S_IMODE(self.client._receipt_path.stat().st_mode), 0o600)
        restarted = WorkshopUploadClient(self.root / "data", service_url=self.url, allow_local_http=True)
        self.server.review_status = "approved"
        state = restarted.snapshot(refresh=True)
        self.assertEqual(state["submissions"][0]["status"], "approved")
        self.assertEqual(self.server.queries[-1], ("/v1/submissions/" + result["submission_id"],
                                                "Bearer " + self.server.private_token))
        self.assertNotIn(self.server.private_token, json.dumps(state))

    def test_failed_confirmation_retry_reuses_idempotency_key(self):
        self.server.mode = "mismatch"
        with self.assertRaisesRegex(ValueError, "未确认"):
            self.upload()
        state = self.client.snapshot()
        self.assertEqual(state["submissions"][0]["status"], "awaiting_confirmation")
        self.server.mode = "normal"
        result = self.upload()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(self.server.posts), 2)
        self.assertEqual(self.server.posts[0]["headers"]["Idempotency-Key"],
                         self.server.posts[1]["headers"]["Idempotency-Key"])
        self.assertEqual(self.upload(), result)
        self.assertEqual(len(self.server.posts), 2)

    def test_all_invalid_responses_leave_awaiting_confirmation_and_hide_secrets(self):
        for mode in ("redirect", "http_error", "mismatch", "not_pending", "token_type", "extra_fields",
                     "invalid_json", "huge_response", "wrong_type"):
            with self.subTest(mode=mode):
                self.server.mode = mode
                client = WorkshopUploadClient(self.root / mode, service_url=self.url, allow_local_http=True)
                with self.assertRaises((RuntimeError, ValueError)) as caught:
                    client.upload(self.package, metadata(), expected_sha256=self.sha256,
                                  expected_size=len(self.payload))
                self.assertNotIn(self.server.private_token, str(caught.exception))
                self.assertEqual(client.snapshot()["submissions"][0]["status"], "awaiting_confirmation")

    def test_wrong_health_and_closed_service_never_send_package(self):
        for mode, accepting in (("bad_health", True), ("normal", False)):
            with self.subTest(mode=mode):
                self.server.mode = mode
                self.server.accepting = accepting
                with self.assertRaises(RuntimeError):
                    self.upload()
                self.assertFalse(self.client.snapshot()["accepting_uploads"])
                self.assertFalse(self.server.posts)

    def test_changed_package_never_creates_submission(self):
        self.package.write_bytes(b"x" * len(self.payload))
        with self.assertRaisesRegex(ValueError, "改变"):
            self.upload()
        self.assertFalse(self.server.posts)

    def test_invalid_remote_status_does_not_change_confirmed_receipt(self):
        self.upload()
        self.server.mode = "invalid_status"
        state = self.client.snapshot(refresh=True)
        self.assertEqual(state["submissions"][0]["status"], "pending")
        self.assertIn("不一致", state["message"])

    def test_corrupted_private_store_never_silently_discards_receipts(self):
        self.upload()
        self.client._receipt_path.write_bytes(b"corrupted")
        restarted = WorkshopUploadClient(self.root / "data", service_url=self.url, allow_local_http=True)
        for _ in range(2):
            with self.assertRaisesRegex(ValueError, "凭证无法读取"):
                restarted.snapshot()

    def test_socket_failure_has_no_exception_details_or_secret(self):
        with patch.object(self.client, "_connection", side_effect=OSError(self.server.private_token)):
            with self.assertRaises(RuntimeError) as caught:
                self.client._request("GET", "/v1/health")
        # 创建连接的异常也应被归一化，不泄露底层错误。
        self.assertNotIn(self.server.private_token, str(caught.exception))

    def test_fixed_service_path_prefix_is_preserved_for_health_and_upload(self):
        self.server.base_path = "/jixing-workshop"
        self.client = WorkshopUploadClient(self.root / "prefixed-data", service_url=self.url + self.server.base_path,
                                          allow_local_http=True)
        self.assertEqual(self.upload()["status"], "pending")
        self.assertEqual(self.server.paths, ["/jixing-workshop/v1/health", "/jixing-workshop/v1/submissions"])

    def test_service_specific_upload_limit_is_enforced_before_sending(self):
        self.server.upload_limit = len(self.payload) - 1
        with self.assertRaisesRegex(ValueError, "大小限制"):
            self.upload()
        self.assertEqual(self.client.snapshot()["max_package_bytes"], self.server.upload_limit)
        self.assertFalse(self.server.posts)

    def test_completed_idempotent_receipt_is_reported_as_its_actual_state(self):
        for state in ("approved", "rejected"):
            with self.subTest(state=state):
                self.client = WorkshopUploadClient(self.root / state, service_url=self.url, allow_local_http=True)
                self.server.mode = state + "_receipt"
                result = self.upload()
                self.assertEqual(result["status"], state)
                self.assertNotIn("等待人工审核", result["message"])
                self.assertNotIn(self.server.private_token, json.dumps(result))

    def test_approved_receipt_is_refreshed_after_maintainer_revokes_it(self):
        self.upload()
        self.server.review_status = "approved"
        self.assertEqual(self.client.snapshot(refresh=True)["submissions"][0]["status"], "approved")
        self.server.review_status = "rejected"
        state = self.client.snapshot(refresh=True)
        self.assertEqual(state["submissions"][0]["status"], "rejected")


class WorkshopServiceIntegrationTests(unittest.TestCase):
    def setUp(self):
        from workshop_service.server import Settings, make_http_server
        from tests.test_workshop import pack_bytes

        self.temp = TemporaryDirectory(prefix="workshop-client-service-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        settings = Settings(self.root / "inbox", "q" * 48, max_package_bytes=80 * 1024 * 1024,
                            quota_bytes=100 * 1024 * 1024, read_timeout=2, upload_timeout=10)
        self.server = make_http_server(settings)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.package = self.root / "resource-mod.zip"
        self.package.write_bytes(pack_bytes())
        self.sha256 = hashlib.sha256(self.package.read_bytes()).hexdigest()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.client = WorkshopUploadClient(self.root / "desktop-data", service_url=self.url, allow_local_http=True)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()

    def upload(self):
        return self.client.upload(self.package, metadata(), expected_sha256=self.sha256,
                                  expected_size=self.package.stat().st_size)

    def test_desktop_client_and_real_service_confirm_pending_and_private_query(self):
        original = self.package.read_bytes()
        result = self.upload()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["sha256"], self.sha256)
        self.assertEqual(result["size"], len(original))
        self.assertEqual(self.package.read_bytes(), original)
        restarted = WorkshopUploadClient(self.root / "desktop-data", service_url=self.url, allow_local_http=True)
        state = restarted.snapshot(refresh=True)
        self.assertEqual(state["max_package_bytes"], 80 * 1024 * 1024)
        self.assertEqual(state["submissions"][0]["submission_id"], result["submission_id"])
        self.assertNotIn("status_token", state["submissions"][0])

    def test_lost_confirmation_retry_creates_only_one_real_server_submission(self):
        original_request = self.client._request
        lose_confirmation = True

        def interrupted(method, *args, **kwargs):
            nonlocal lose_confirmation
            result = original_request(method, *args, **kwargs)
            if method == "POST" and lose_confirmation:
                lose_confirmation = False
                raise RuntimeError("模拟服务接收后连接中断")
            return result

        with patch.object(self.client, "_request", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "连接中断"):
                self.upload()
            result = self.upload()
        self.assertEqual(result["status"], "pending")
        self.assertEqual(len(list((self.root / "inbox" / "quarantine").glob("*.zip"))), 1)


if __name__ == "__main__":
    unittest.main()
