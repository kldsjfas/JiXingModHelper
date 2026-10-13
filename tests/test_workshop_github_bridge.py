"""维护者桥接状态机：全部 API 在内存中，不联系 GitHub 或公网。"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import ssl
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_workshop import metadata, pack_bytes
from tools.workshop_github_bridge import (ApiFailure, REPOSITORY, SERVICE_URL, ServiceApi, TrustedHttp, WorkshopBridge,
                                          GithubApi, approved_submission, authorize_environment, issue_body, main)

SUBMISSION = "a" * 32
PROOF = {"passed": True, "version": "2026-10-13", "evidence": "private-test-record-001",
         "notes": "已在隔离游戏副本安装并确认资源显示、撤销恢复与退出后启停均正常。"}


class FakeService:
    def __init__(self, events):
        self.events = events
        self.payload = pack_bytes()
        self.row = {"submission_id": SUBMISSION, "status": "pending", "metadata": metadata(),
                    "sha256": hashlib.sha256(self.payload).hexdigest(), "size": len(self.payload),
                    "inspection": {"bundle_count": 1}, "scan": {}, "review": {}, "github_issue_number": None}
        self.clean = True

    def pending(self):
        return [deepcopy(self.row)] if self.row["status"] == "pending" else []

    def detail(self, identifier):
        if identifier != SUBMISSION:
            raise ValueError("未知测试编号")
        return deepcopy(self.row)

    def bind_issue(self, identifier, number):
        self.row["github_issue_number"] = number
        self.events.append("bind")

    def scan(self, identifier):
        self.events.append("scan")
        self.row["scan"] = {"scan_id": "b" * 32, "clean": self.clean,
                            "sha256": self.row["sha256"], "scanned_at": int(time.time())}
        return deepcopy(self.row["scan"])

    def review(self, identifier, value):
        self.events.append(value["decision"])
        if value["decision"] == "approve":
            if value["game_test"] != PROOF:
                raise ValueError("游戏实测未通过")
            self.row["status"] = "approved"
            self.row["review"] = {**value, "scan": deepcopy(self.row["scan"])}
        else:
            self.row["status"] = "rejected"
            self.row["review"] = dict(value)
        return deepcopy(self.row)

    def download(self, row, path):
        self.events.append("download-private-approved")
        Path(path).write_bytes(self.payload)


class FakeGithub:
    def __init__(self, events):
        self.events = events
        self.issues = []
        self.release_value = None
        self.assets = []
        self.directory = {"format": "jixing-workshop", "version": 1, "mods": []}
        self.generation = 1
        self.conflicts = 0
        self.permission = "write"
        self.bad_asset_digest = False

    def request(self, method, path, value=None):
        if method == "GET" and path == "":
            return {"default_branch": "main", "owner": {"login": "kldsjfas"}}
        if method == "GET" and path.startswith("/collaborators/"):
            return {"permission": self.permission}
        if method == "POST" and path == "/issues":
            self.events.append("create-issue")
            issue = {"number": len(self.issues) + 1, **deepcopy(value), "user": {"login": "github-actions[bot]"}}
            self.issues.append(issue)
            return deepcopy(issue)
        if method == "POST" and path == "/releases":
            self.events.append("create-draft")
            self.release_value = {**deepcopy(value), "id": 101, "author": {"login": "github-actions[bot]"}}
            return deepcopy(self.release_value)
        if method == "GET" and path == "/releases/101/assets?per_page=100":
            return deepcopy(self.assets)
        if method == "PATCH" and path == "/releases/101":
            self.events.append("hide-release" if value.get("draft") else "publish-release")
            self.release_value.update(value)
            return deepcopy(self.release_value)
        raise AssertionError((method, path))

    def find_issue(self, identifier):
        return deepcopy(self.issues[0]) if self.issues else None

    def catalog(self):
        return deepcopy(self.directory), f"{self.generation:040x}"

    def write_catalog(self, catalog, sha, message):
        self.events.append("catalog-revoke" if "Revoke" in message else "catalog-publish")
        if self.conflicts:
            self.conflicts -= 1
            self.generation += 1
            raise ApiFailure(409)
        if sha != f"{self.generation:040x}":
            raise ApiFailure(409)
        self.directory = deepcopy(catalog)
        self.generation += 1

    def release(self, tag):
        return deepcopy(self.release_value)

    def upload_asset(self, release_id, name, path, row):
        self.events.append("upload-approved-asset")
        actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if actual != row["sha256"]:
            raise AssertionError("桥接没有上传实际批准的资源")
        asset = {"name": name, "size": row["size"], "digest": "sha256:" + ("0" * 64 if self.bad_asset_digest else actual),
                 "state": "uploaded"}
        self.assets.append(asset)
        return deepcopy(asset)


class WorkshopGithubBridgeTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.service = FakeService(self.events)
        self.github = FakeGithub(self.events)
        self.bridge = WorkshopBridge(self.service, self.github, actor="maintainer", default_branch="main")

    def approve(self):
        return self.bridge.approve(SUBMISSION, PROOF, "经实际游戏测试批准")

    def test_sync_only_creates_safe_text_issue_and_never_downloads_pending_zip(self):
        self.service.row["metadata"]["description"] = "<script>bad()</script> @everyone\n![click](https://attacker.example)"
        self.service.row["query_secret"] = "must-not-publish-query-secret"
        self.service.row["local_path"] = "C:/private/source.zip"
        result = self.bridge.sync()
        self.assertEqual(result, {"synced": 1})
        self.assertEqual(self.events, ["create-issue", "bind"])
        body = self.github.issues[0]["body"]
        self.assertNotIn("<script>", body)
        self.assertNotIn("@everyone", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("must-not-publish-query-secret", body)
        self.assertNotIn("C:/private", body)
        self.assertIsNone(self.github.release_value)

    def test_sync_after_lost_bind_reuses_bot_issue_and_rejects_human_forgery(self):
        self.bridge.sync()
        self.service.row["github_issue_number"] = None
        self.assertEqual(self.bridge.sync(), {"synced": 1})
        self.assertEqual(len(self.github.issues), 1)
        self.service.row["github_issue_number"] = None
        self.github.issues[0]["user"]["login"] = "attacker"
        with self.assertRaisesRegex(ValueError, "可信"):
            self.bridge.sync()

    def test_approval_orders_scan_test_hash_release_and_catalog_last(self):
        result = self.approve()
        self.assertTrue(result["published"])
        self.assertEqual(self.events, ["scan", "approve", "download-private-approved", "create-draft",
                                      "upload-approved-asset", "publish-release", "catalog-publish"])
        entry = self.github.directory["mods"][0]
        self.assertEqual(entry["asset"]["sha256"], self.service.row["sha256"])
        self.assertEqual(entry["asset"]["tag"], "workshop-" + SUBMISSION)
        self.assertEqual(entry["game_version"], PROOF["version"])
        self.assertFalse(self.github.release_value["draft"])
        self.assertFalse(entry["revoked"])

    def test_scan_or_game_failure_never_creates_release_or_catalog(self):
        self.service.clean = False
        with self.assertRaisesRegex(ValueError, "扫描"):
            self.approve()
        self.assertIsNone(self.github.release_value)
        self.service.clean = True
        with self.assertRaisesRegex(ValueError, "游戏实测"):
            self.bridge.approve(SUBMISSION, {**PROOF, "passed": False}, "不能证明通过")
        self.assertIsNone(self.github.release_value)
        self.assertEqual(self.github.directory["mods"], [])

    def test_rejected_submission_cannot_download_or_publish(self):
        self.service.row["status"] = "rejected"
        with self.assertRaisesRegex(ValueError, "拒绝"):
            self.approve()
        self.assertEqual(self.events, [])

    def test_unsafe_bundle_is_rechecked_even_after_service_claims_approval(self):
        self.service.payload = pack_bytes([("plugin.dll", b"MZ"), ("mod_info.json", b"{}")], include_info=False)
        self.service.row.update(sha256=hashlib.sha256(self.service.payload).hexdigest(), size=len(self.service.payload))
        with self.assertRaises(ValueError):
            self.approve()
        self.assertIsNone(self.github.release_value)
        self.assertNotIn("catalog-publish", self.events)

    def test_asset_digest_mismatch_keeps_draft_and_catalog_unchanged(self):
        self.github.bad_asset_digest = True
        with self.assertRaisesRegex(ValueError, "摘要"):
            self.approve()
        self.assertTrue(self.github.release_value["draft"])
        self.assertEqual(self.github.directory["mods"], [])

    def test_approved_retry_finishes_existing_draft_without_rescanning_or_duplicate_assets(self):
        self.github.bad_asset_digest = True
        with self.assertRaises(ValueError):
            self.approve()
        self.github.assets[0]["digest"] = "sha256:" + self.service.row["sha256"]
        self.github.bad_asset_digest = False
        self.events.clear()
        self.assertTrue(self.approve()["published"])
        self.assertNotIn("scan", self.events)
        self.assertNotIn("create-draft", self.events)
        self.assertNotIn("upload-approved-asset", self.events)
        self.assertEqual(len(self.github.assets), 1)

    def test_existing_catalog_id_with_different_bytes_cannot_be_overwritten(self):
        self.approve()
        self.github.directory["mods"][0]["asset"]["sha256"] = "b" * 64
        self.events.clear()
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.approve()
        self.assertEqual(self.github.directory["mods"][0]["asset"]["sha256"], "b" * 64)
        self.assertEqual(self.events, [])

    def test_catalog_concurrent_update_is_retried_with_new_sha(self):
        self.github.conflicts = 1
        self.approve()
        self.assertEqual(self.events.count("catalog-publish"), 2)
        self.assertEqual(len(self.github.directory["mods"]), 1)

    def test_revoke_blocks_catalog_before_server_decision_and_release_hiding(self):
        self.approve()
        self.events.clear()
        result = self.bridge.revoke(SUBMISSION, "发现资源问题，撤回")
        self.assertTrue(result["revoked"])
        self.assertEqual(self.events, ["catalog-revoke", "revoke", "hide-release"])
        self.assertTrue(self.github.directory["mods"][0]["revoked"])
        self.assertEqual(self.service.row["status"], "rejected")
        with self.assertRaises(ValueError):
            self.approve()

    def test_revoke_catalog_failure_leaves_active_service_and_release_untouched(self):
        self.approve()
        self.events.clear()
        self.github.conflicts = 9
        with self.assertRaises(RuntimeError):
            self.bridge.revoke(SUBMISSION, "撤回")
        self.assertNotIn("revoke", self.events)
        self.assertNotIn("hide-release", self.events)

    def test_revoke_never_withdraws_another_asset_with_a_colliding_metadata_id(self):
        self.approve()
        self.github.directory["mods"][0]["asset"]["tag"] = "workshop-" + "c" * 32
        self.events.clear()
        with self.assertRaisesRegex(ValueError, "其它作品"):
            self.bridge.revoke(SUBMISSION, "撤回这份投稿")
        self.assertFalse(self.github.directory["mods"][0]["revoked"])
        self.assertEqual(self.events, [])

    def test_pending_or_ordinary_rejected_submission_cannot_revoke_catalog(self):
        for state in ("pending", "rejected"):
            with self.subTest(state=state):
                self.service.row["status"] = state
                with self.assertRaisesRegex(ValueError, "已批准"):
                    self.bridge.revoke(SUBMISSION, "错误点击")
                self.assertEqual(self.events, [])

    def test_second_live_review_check_prevents_publish_after_server_revocation(self):
        original_detail = self.service.detail
        calls = 0

        def revoked_midway(identifier):
            nonlocal calls
            calls += 1
            if calls == 4:
                self.service.row["status"] = "rejected"
            return original_detail(identifier)

        with patch.object(self.service, "detail", side_effect=revoked_midway):
            with self.assertRaisesRegex(ValueError, "发布需要"):
                self.approve()
        self.assertNotIn("publish-release", self.events)
        self.assertEqual(self.github.directory["mods"], [])

    def test_revocation_after_release_publish_hides_release_and_stops_catalog(self):
        original_detail = self.service.detail
        calls = 0

        def revoked_after_publish(identifier):
            nonlocal calls
            calls += 1
            if calls == 5:
                self.service.row["status"] = "rejected"
            return original_detail(identifier)

        with patch.object(self.service, "detail", side_effect=revoked_after_publish):
            with self.assertRaises(ValueError):
                self.approve()
        self.assertEqual(self.events[-2:], ["publish-release", "hide-release"])
        self.assertTrue(self.github.release_value["draft"])
        self.assertEqual(self.github.directory["mods"], [])

    def test_failed_compensation_reports_required_manual_hiding_without_success(self):
        original_detail, original_request = self.service.detail, self.github.request
        calls = 0

        def revoked_after_publish(identifier):
            nonlocal calls
            calls += 1
            if calls == 5:
                self.service.row["status"] = "rejected"
            return original_detail(identifier)

        def malformed_hide(method, path, value=None):
            if method == "PATCH" and value.get("draft") is True:
                raise ValueError("不可信失败响应不得输出")
            return original_request(method, path, value)

        with patch.object(self.service, "detail", side_effect=revoked_after_publish), patch.object(self.github, "request", side_effect=malformed_hide):
            with self.assertRaisesRegex(RuntimeError, "需要维护者立即处理") as caught:
                self.approve()
        self.assertNotIn("不可信失败响应", str(caught.exception))
        self.assertEqual(self.github.directory["mods"], [])

    def test_stale_scan_and_missing_manual_proof_cannot_publish(self):
        self.service.scan(SUBMISSION)
        self.service.review(SUBMISSION, {"decision": "approve", "reviewer": "maintainer", "message": "批准",
                                        "scan_id": self.service.row["scan"]["scan_id"], "game_test": PROOF})
        self.service.row["scan"]["scanned_at"] = int(time.time()) - 49 * 3600
        with self.assertRaises(ValueError):
            approved_submission(self.service.row, SUBMISSION)
        self.service.row["scan"]["scanned_at"] = int(time.time())
        self.service.row["review"]["game_test"] = {}
        with self.assertRaises(ValueError):
            approved_submission(self.service.row, SUBMISSION)

    def test_forged_release_marker_or_extra_asset_cannot_be_published(self):
        self.approve()
        self.github.release_value["author"]["login"] = "attacker"
        with self.assertRaisesRegex(ValueError, "可信"):
            self.approve()
        self.github.release_value["author"]["login"] = "github-actions[bot]"
        self.github.assets.append({"name": "script.exe"})
        with self.assertRaisesRegex(ValueError, "意外"):
            self.approve()

    def test_context_guards_reject_pr_fork_branch_and_nonmaintainer(self):
        safe = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main", "GITHUB_ACTOR": "maintainer"}
        self.assertEqual(authorize_environment(safe, self.github), ("maintainer", "main"))
        for replacement in ({"GITHUB_REPOSITORY": "attacker/fork"}, {"GITHUB_EVENT_NAME": "pull_request"},
                            {"GITHUB_REF": "refs/heads/untrusted"}, {"GITHUB_ACTOR": "bad/name"}):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                authorize_environment({**safe, **replacement}, self.github)
        self.github.permission = "read"
        with self.assertRaisesRegex(ValueError, "维护者"):
            authorize_environment(safe, self.github)

    def test_transport_never_follows_redirect_or_logs_remote_secret_body(self):
        transport = TrustedHttp("https://api.github.com", "private-token")
        response = Mock(status=302)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch("tools.workshop_github_bridge.http.client.HTTPSConnection", return_value=connection):
            with self.assertRaises(ApiFailure) as caught:
                transport.request("GET", "/repos/test")
        response.read.assert_not_called()
        self.assertNotIn("private-token", str(caught.exception))
        connection.close.assert_called_once()

    def test_transport_rejects_http_and_credentials_in_url(self):
        for url in ("http://api.github.com", "https://@api.github.com", "https://name:key@api.github.com", "https://api.github.com?secret=key"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                TrustedHttp(url, "protected-token")

    def test_owner_can_run_without_unavailable_collaborator_permission_endpoint(self):
        safe = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_REF": "refs/heads/main", "GITHUB_ACTOR": "kldsjfas"}
        original = self.github.request

        def permission_forbidden(method, path, value=None):
            if path.startswith("/collaborators/"):
                raise ApiFailure(403)
            return original(method, path, value)

        with patch.object(self.github, "request", side_effect=permission_forbidden) as calls:
            self.assertEqual(authorize_environment(safe, self.github), ("kldsjfas", "main"))
            self.assertEqual(calls.call_count, 1)
            with self.assertRaises(ApiFailure):
                authorize_environment({**safe, "GITHUB_ACTOR": "maintainer"}, self.github)

    def test_published_tag_endpoint_404_recovers_existing_draft_from_bounded_list(self):
        github = GithubApi("test-protected-token")
        draft = {"id": 101, "draft": True, "tag_name": "workshop-" + SUBMISSION,
                 "author": {"login": "github-actions[bot]"}, "body": "<!-- jixing-workshop:" + SUBMISSION + " -->"}

        def published_only(method, path, value=None):
            if path.startswith("/releases/tags/"):
                raise ApiFailure(404)
            if path == "/releases?per_page=100&page=1":
                return [draft]
            raise AssertionError((method, path))

        with patch.object(github, "request", side_effect=published_only):
            self.assertEqual(github.release("workshop-" + SUBMISSION), draft)

    def test_draft_lookup_refuses_duplicate_tag_instead_of_creating_another_release(self):
        github = GithubApi("test-protected-token")
        tag = "workshop-" + SUBMISSION

        def duplicate_drafts(method, path, value=None):
            if path.startswith("/releases/tags/"):
                raise ApiFailure(404)
            return [{"id": 1, "tag_name": tag}, {"id": 2, "tag_name": tag}]

        with patch.object(github, "request", side_effect=duplicate_drafts):
            with self.assertRaisesRegex(ValueError, "多个"):
                github.release(tag)

    def test_http_failure_reports_only_fixed_source_host(self):
        response = Mock(status=403)
        connection = Mock()
        connection.getresponse.return_value = response
        transport = TrustedHttp(SERVICE_URL, "private-test-admin-token", origin_ip="8.8.8.8")
        with patch.object(transport, "_connection", return_value=connection):
            with self.assertRaises(ApiFailure) as caught:
                transport.request("GET", "/v1/admin/submissions/pending")
        self.assertEqual(caught.exception.source_host, "xn--killer-gx2m.top")
        self.assertIn("xn--killer-gx2m.top", str(caught.exception))
        self.assertNotIn("8.8.8.8", str(caught.exception))
        self.assertNotIn("private-test-admin-token", str(caught.exception))
        response.read.assert_not_called()
        self.assertNotIn("8.8.8.8", str(ApiFailure(403, "8.8.8.8")))

    def test_origin_pin_keeps_domain_host_sni_and_certificate_verification(self):
        hostname = "xn--killer-gx2m.top"
        transport = TrustedHttp(SERVICE_URL, "private-test-admin-token", origin_ip="8.8.8.8")
        context = Mock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)
        encrypted_socket = Mock()
        context.wrap_socket.return_value = encrypted_socket
        raw_socket = Mock()
        with patch("tools.workshop_github_bridge.ssl.create_default_context", return_value=context), patch("tools.workshop_github_bridge.socket.create_connection", return_value=raw_socket) as dialer, patch("tools.workshop_github_bridge.trusted_https_connection") as proxy_factory:
            connection = transport._connection()
            try:
                self.assertEqual(connection.host, hostname)
                self.assertEqual(connection.port, 443)
                connection.connect()
                dialer.assert_called_once_with(("8.8.8.8", 443), 30, None)
                context.wrap_socket.assert_called_once_with(raw_socket, server_hostname=hostname)
                self.assertIs(connection.sock, encrypted_socket)
                proxy_factory.assert_not_called()
                connection.putrequest("GET", "/jixing-workshop/v1/admin/submissions/pending")
                self.assertIn(b"Host: " + hostname.encode(), connection._buffer)
                self.assertNotIn(b"Host: 8.8.8.8", connection._buffer)
            finally:
                connection.close()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_pinned_connection_certificate_failure_sends_no_admin_bearer(self):
        transport = TrustedHttp(SERVICE_URL, "private-test-admin-token", origin_ip="8.8.8.8")
        context = Mock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("test-domain-mismatch")
        raw_socket = Mock()
        with patch("tools.workshop_github_bridge.ssl.create_default_context", return_value=context), patch("tools.workshop_github_bridge.socket.create_connection", return_value=raw_socket):
            with self.assertRaisesRegex(RuntimeError, "网络连接失败") as caught:
                transport.request("GET", "/v1/admin/submissions/pending")
        raw_socket.sendall.assert_not_called()
        self.assertNotIn("private-test-admin-token", str(caught.exception))
        self.assertNotIn("8.8.8.8", str(caught.exception))

    def test_origin_pin_rejects_nonpublic_addresses_and_cannot_pin_github(self):
        for address in ("127.0.0.1", "10.1.2.3", "192.168.2.3", "172.16.2.3", "169.254.169.254", "0.0.0.0",
                        "224.0.0.1", "255.255.255.255", "192.0.2.1", "::1", "fc00::1", "fe80::1", "ff02::1",
                        "::ffff:8.8.8.8", "2001:4860:4860::8888%eth0", "8.8.8.8\n", "not-an-address"):
            with self.subTest(address=address), self.assertRaises(ValueError) as caught:
                TrustedHttp(SERVICE_URL, "test-protected-token", origin_ip=address)
            self.assertNotIn(address, str(caught.exception))
        for origin in ("https://api.github.com", "https://uploads.github.com", SERVICE_URL + "/extra"):
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, "固定工坊"):
                TrustedHttp(origin, "test-protected-token", origin_ip="8.8.8.8")

    def test_unpinned_service_still_uses_normal_trusted_system_proxy_connection(self):
        service = ServiceApi("test-protected-token")
        factory = Mock()
        with patch("tools.workshop_github_bridge.trusted_https_connection", factory):
            service.http._connection()
        factory.assert_called_once_with("xn--killer-gx2m.top", timeout=30)
        github = GithubApi("test-protected-github-token")
        self.assertEqual(github.http._origin_ip, "")
        self.assertEqual(github.uploads._origin_ip, "")
        self.assertFalse(github.http._review_tunnel)
        self.assertFalse(github.uploads._review_tunnel)

    def test_review_tunnel_keeps_domain_host_sni_and_certificate_verification(self):
        hostname = "xn--killer-gx2m.top"
        service = ServiceApi("private-test-admin-token", review_tunnel_port="18443")
        context = Mock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)
        encrypted_socket, raw_socket = Mock(), Mock()
        context.wrap_socket.return_value = encrypted_socket
        with patch("tools.workshop_github_bridge.ssl.create_default_context", return_value=context), patch("tools.workshop_github_bridge.socket.create_connection", return_value=raw_socket) as dialer, patch("tools.workshop_github_bridge.trusted_https_connection") as proxy_factory:
            connection = service.http._connection()
            try:
                self.assertEqual((connection.host, connection.port), (hostname, 443))
                connection.connect()
                dialer.assert_called_once_with(("127.0.0.1", 18443), 30, None)
                context.wrap_socket.assert_called_once_with(raw_socket, server_hostname=hostname)
                self.assertIs(connection.sock, encrypted_socket)
                proxy_factory.assert_not_called()
                connection.putrequest("GET", "/jixing-workshop/v1/admin/submissions/pending")
                self.assertIn(b"Host: " + hostname.encode(), connection._buffer)
                self.assertNotIn(b"Host: 127.0.0.1", connection._buffer)
                self.assertNotIn(b"Host: " + hostname.encode() + b":18443", connection._buffer)
            finally:
                connection.close()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_review_tunnel_certificate_failure_sends_no_admin_bearer(self):
        service = ServiceApi("private-test-admin-token", review_tunnel_port="18443")
        context = Mock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("test-domain-mismatch")
        raw_socket = Mock()
        with patch("tools.workshop_github_bridge.ssl.create_default_context", return_value=context), patch("tools.workshop_github_bridge.socket.create_connection", return_value=raw_socket):
            with self.assertRaisesRegex(RuntimeError, "网络连接失败") as caught:
                service.http.request("GET", "/v1/admin/submissions/pending")
        raw_socket.sendall.assert_not_called()
        self.assertNotIn("private-test-admin-token", str(caught.exception))
        self.assertNotIn("test-domain-mismatch", str(caught.exception))

    def test_unavailable_review_tunnel_never_falls_back_to_proxy_or_public_origin(self):
        service = ServiceApi("private-test-admin-token", review_tunnel_port="18443")
        with patch("tools.workshop_github_bridge.socket.create_connection", side_effect=ConnectionRefusedError("test-tunnel-unavailable")) as dialer, patch("tools.workshop_github_bridge.trusted_https_connection") as proxy_factory:
            with self.assertRaisesRegex(RuntimeError, "网络连接失败") as caught:
                service.http.request("GET", "/v1/admin/submissions/pending")
        dialer.assert_called_once_with(("127.0.0.1", 18443), 30, None)
        proxy_factory.assert_not_called()
        self.assertNotIn("private-test-admin-token", str(caught.exception))
        self.assertNotIn("test-tunnel-unavailable", str(caught.exception))

    def test_review_tunnel_rejects_other_ports_addresses_and_types(self):
        for value in ("443", "19443", "018443", "+18443", "18443.0", " 18443", "18443 ", "18443\n",
                      "127.0.0.1:18443", "localhost:18443", "8.8.8.8:18443", None, False, True, 0, 18443, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "固定的本机端口"):
                ServiceApi("test-protected-token", review_tunnel_port=value)

    def test_review_tunnel_cannot_replace_github_or_combine_with_origin_pin(self):
        for origin in ("https://api.github.com", "https://uploads.github.com", SERVICE_URL + "/extra", "https://example.com"):
            with self.subTest(origin=origin), self.assertRaisesRegex(ValueError, "固定工坊"):
                TrustedHttp(origin, "test-protected-token", review_tunnel_port="18443")
        with self.assertRaisesRegex(ValueError, "同时配置") as caught:
            ServiceApi("test-protected-token", origin_ip="8.8.8.8", review_tunnel_port="18443")
        self.assertNotIn("8.8.8.8", str(caught.exception))

    def test_review_tunnel_socket_callback_refuses_altered_source(self):
        service = ServiceApi("test-protected-token", review_tunnel_port="18443")
        connection = service.http._connection()
        try:
            with patch("tools.workshop_github_bridge.socket.create_connection") as dialer:
                for address in (("xn--killer-gx2m.top", 18443), ("127.0.0.1", 18443), ("api.github.com", 443)):
                    with self.subTest(address=address), self.assertRaisesRegex(ValueError, "受信服务来源"):
                        connection._create_connection(address)
                dialer.assert_not_called()
        finally:
            connection.close()

    def test_review_tunnel_still_refuses_redirects_without_reading_remote_body(self):
        service = ServiceApi("private-test-admin-token", review_tunnel_port="18443")
        connection = Mock()
        response = Mock(status=302)
        connection.getresponse.return_value = response
        with patch.object(service.http, "_connection", return_value=connection):
            with self.assertRaises(ApiFailure) as caught:
                service.http.request("GET", "/v1/admin/submissions/pending")
        self.assertEqual(caught.exception.source_host, "xn--killer-gx2m.top")
        self.assertNotIn("private-test-admin-token", str(caught.exception))
        response.read.assert_not_called()
        connection.close.assert_called_once()

    def test_main_reads_fixed_tunnel_setting_only_for_service(self):
        environment = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
                       "GITHUB_REF": "refs/heads/main", "GITHUB_ACTOR": "kldsjfas",
                       "GITHUB_TOKEN": "test-github-token", "WORKSHOP_ADMIN_SECRET": "test-admin-token",
                       "WORKSHOP_REVIEW_TUNNEL_PORT": "18443", "WORKSHOP_REVIEW_TUNNEL_HOST": "ignored.example"}
        with patch("tools.workshop_github_bridge.GithubApi", return_value=self.github) as github_factory, patch("tools.workshop_github_bridge.ServiceApi", return_value=self.service) as service_factory, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(environment), 0)
        github_factory.assert_called_once_with("test-github-token")
        service_factory.assert_called_once_with("test-admin-token", origin_ip="", review_tunnel_port="18443")
        self.assertEqual(json.loads(output.getvalue()), {"synced": 1})

    def test_main_rejects_invalid_tunnel_environment_before_service_request(self):
        environment = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_EVENT_NAME": "workflow_dispatch",
                       "GITHUB_REF": "refs/heads/main", "GITHUB_ACTOR": "kldsjfas",
                       "GITHUB_TOKEN": "test-github-token", "WORKSHOP_ADMIN_SECRET": "test-admin-token",
                       "WORKSHOP_REVIEW_TUNNEL_PORT": "19443"}
        with patch("tools.workshop_github_bridge.GithubApi", return_value=self.github), patch("tools.workshop_github_bridge.trusted_https_connection") as proxy_factory:
            with self.assertRaisesRegex(ValueError, "固定的本机端口"):
                main(environment)
        proxy_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
