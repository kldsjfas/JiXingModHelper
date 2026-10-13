"""维护者私有取包：SSH、上传服务和扫描均替身，不读取真实凭证。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_workshop import metadata, pack_bytes
from tests.workshop_fixtures import pe_payload, resource_bundle
from tools import workshop_fetch_review as fetch

IDENTIFIER = "a" * 32
TEST_SECRET = "private-test-secret-" + "x" * 32


class FakeService:
    def __init__(self):
        self.payload = pack_bytes()
        self.row = {"submission_id": IDENTIFIER, "status": "pending", "metadata": metadata(),
                    "sha256": hashlib.sha256(self.payload).hexdigest(), "size": len(self.payload),
                    "scan": {}, "review": {}}
        self.events = []
        self.clean = True
        self.download_payload = None
        self.after_download = None
        self.scan_error = None

    def detail(self, identifier):
        if identifier != IDENTIFIER:
            raise AssertionError("Unexpected identifier")
        self.events.append("detail")
        return deepcopy(self.row)

    def scan(self, identifier):
        self.events.append("scan")
        if self.scan_error:
            raise self.scan_error
        self.row["scan"] = {"scan_id": "b" * 32, "clean": self.clean,
                            "sha256": self.row["sha256"], "scanned_at": int(time.time())}
        return deepcopy(self.row["scan"])

    def download(self, row, path):
        self.events.append("download")
        Path(path).write_bytes(self.payload if self.download_payload is None else self.download_payload)
        if self.after_download:
            self.after_download(Path(path))

    def approved(self):
        self.scan(IDENTIFIER)
        self.row["status"] = "approved"
        self.row["review"] = {"decision": "approve", "scan": deepcopy(self.row["scan"]),
            "game_test": {"passed": True, "version": "2026-10-13", "evidence": "private audit evidence 001",
                          "notes": "已在隔离游戏环境确认贴图显示和卸载恢复，全部验证记录已保存。"}}
        self.events.clear()


class WorkshopFetchReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="workshop-fetch-review-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "reviews" / "review.zip"
        self.service = FakeService()
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0, TEST_SECRET.encode(), b"ignored stderr"))
        self.factory = Mock(return_value=self.service)

    def fetch(self, **changes):
        return fetch.fetch_review(IDENTIFIER, changes.pop("output", self.output),
                                  runner=self.runner, service_factory=self.factory, **changes)

    def assert_no_download(self):
        self.assertNotIn("download", self.service.events)
        self.assertFalse(self.output.exists())

    def test_pending_scan_and_structure_check_then_atomic_zip_only(self):
        result = self.fetch()
        self.assertEqual(self.service.events, ["detail", "scan", "detail", "download", "detail"])
        self.assertEqual(self.output.read_bytes(), self.service.payload)
        self.assertEqual(result["sha256"], self.service.row["sha256"])
        self.assertEqual(result["status"], "pending")
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])
        self.assertNotIn(TEST_SECRET, json.dumps(result))
        self.factory.assert_called_once_with(TEST_SECRET)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(self.service.row["status"], "pending", "Fetch changed review state")

    def test_nonempty_review_directory_is_allowed_without_changing_other_files(self):
        self.output.parent.mkdir()
        original = self.output.parent / "previous-review.txt"
        original.write_text("keep original")
        self.fetch()
        self.assertEqual(original.read_text(), "keep original")
        self.assertEqual({path.name for path in self.output.parent.iterdir()}, {original.name, self.output.name})

    def test_approved_record_requires_fresh_scan_and_proof_without_new_approval(self):
        self.service.approved()
        self.fetch()
        self.assertEqual(self.service.events, ["detail", "download", "detail"])
        self.assertEqual(self.service.row["status"], "approved")

    def test_rejected_record_never_downloads(self):
        self.service.row["status"] = "rejected"
        with self.assertRaisesRegex(fetch.FetchError, "拒绝或撤回"):
            self.fetch()
        self.assert_no_download()

    def test_scanner_unavailable_or_threat_never_downloads(self):
        self.service.scan_error = RuntimeError("scanner unavailable")
        with self.assertRaises(RuntimeError):
            self.fetch()
        self.assert_no_download()
        self.service.scan_error = None
        self.service.clean = False
        with self.assertRaisesRegex(fetch.FetchError, "扫描未通过"):
            self.fetch()
        self.assert_no_download()

    def test_scan_must_match_hash_and_freshness(self):
        for changes in ({"sha256": "c" * 64}, {"scan_id": "wrong"},
                        {"scanned_at": int(time.time()) - 49 * 3600}, {"clean": 1}):
            self.service.events.clear()
            scan = {"scan_id": "b" * 32, "clean": True, "sha256": self.service.row["sha256"],
                    "scanned_at": int(time.time()), **changes}
            with self.subTest(changes=changes), patch.object(self.service, "scan", return_value=scan), self.assertRaises(fetch.FetchError):
                self.fetch()
            self.assert_no_download()

    def test_approved_without_game_proof_or_expired_scan_never_downloads(self):
        self.service.approved()
        self.service.row["review"]["game_test"]["evidence"] = ""
        with self.assertRaises(ValueError):
            self.fetch()
        self.assert_no_download()
        self.service.approved()
        self.service.row["scan"]["scanned_at"] = int(time.time()) - 49 * 3600
        with self.assertRaises(ValueError):
            self.fetch()
        self.assert_no_download()

    def test_existing_output_is_preserved_before_ssh(self):
        self.output.parent.mkdir()
        self.output.write_bytes(b"original")
        with self.assertRaisesRegex(fetch.FetchError, "输出已经存在"):
            self.fetch()
        self.assertEqual(self.output.read_bytes(), b"original")
        self.runner.assert_not_called()

    def test_invalid_identifier_alias_and_output_stop_before_service(self):
        for identifier in ("../escape", "A" * 32, "a" * 31):
            with self.subTest(identifier=identifier), self.assertRaises(fetch.FetchError):
                fetch.fetch_review(identifier, self.output, runner=self.runner, service_factory=self.factory)
        for alias in ("-oProxyCommand=bad", "host;bad", "host@evil", "../alias"):
            with self.subTest(alias=alias), self.assertRaises(fetch.FetchError):
                self.fetch(ssh_alias=alias)
        for output in (self.root / "bad.exe", self.root / ".." / "bad.zip"):
            with self.subTest(output=output), self.assertRaises(fetch.FetchError):
                self.fetch(output=output)
        self.runner.assert_not_called()
        self.factory.assert_not_called()

    def test_known_tool_data_and_game_directories_are_rejected(self):
        for directory in (self.root / "modkit_data", self.root / "made_mods", self.root / "_internal", self.root / "Astral Party"):
            with self.subTest(directory=directory), self.assertRaisesRegex(fetch.FetchError, "游戏或助手"):
                self.fetch(output=directory / "new.zip")
        game = self.root / "renamed-game"
        game.mkdir()
        (game / "AstralParty_CN.exe").write_bytes(b"fixture only")
        with self.assertRaisesRegex(fetch.FetchError, "游戏目录"):
            self.fetch(output=game / "new-reviews" / "new.zip")
        self.runner.assert_not_called()

    def test_symlink_parent_and_dangling_output_are_rejected(self):
        target = self.root / "ordinary"
        target.mkdir()
        alias = self.root / "alias"
        try:
            alias.symlink_to(target, target_is_directory=True)
            dangling = self.root / "dangling.zip"
            dangling.symlink_to(self.root / "missing.zip")
        except OSError:
            self.skipTest("This account cannot create symlinks")
        with self.assertRaises(fetch.FetchError):
            self.fetch(output=alias / "new.zip")
        with self.assertRaises(fetch.FetchError):
            self.fetch(output=dangling)
        self.runner.assert_not_called()

    def test_windows_reparse_parent_is_rejected_without_link_creation(self):
        ordinary_lstat = Path.lstat
        parent = self.output.parent

        def lstat(path):
            if path == parent:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
            return ordinary_lstat(path)

        with patch.object(Path, "lstat", lstat), self.assertRaisesRegex(fetch.FetchError, "重解析点"):
            self.fetch()
        self.runner.assert_not_called()

    def test_ssh_command_is_fixed_and_never_puts_secret_in_arguments(self):
        self.fetch(ssh_alias="trusted-alias")
        command, = self.runner.call_args.args
        self.assertEqual(command[:3], ["ssh", "-o", "BatchMode=yes"])
        self.assertIn("trusted-alias", command)
        self.assertEqual(command[-1], fetch.REMOTE_COMMAND)
        self.assertTrue(command[-1].startswith("sudo -n python3.11 -c "))
        self.assertNotIn(TEST_SECRET, " ".join(command))
        self.assertNotIn(IDENTIFIER, command[-1])
        options = self.runner.call_args.kwargs
        self.assertFalse(options["shell"])
        self.assertEqual(options["timeout"], 30)
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.PIPE)

    def test_bad_ssh_result_and_timeout_do_not_expose_output(self):
        results = [subprocess.CompletedProcess([], 1, TEST_SECRET.encode(), TEST_SECRET.encode()),
                   subprocess.CompletedProcess([], 0, (TEST_SECRET + "\n").encode(), TEST_SECRET.encode()),
                   subprocess.CompletedProcess([], 0, b"short", TEST_SECRET.encode())]
        for result in results:
            self.runner.return_value = result
            with self.subTest(result=result.returncode), self.assertRaises(fetch.FetchError) as raised:
                self.fetch()
            self.assertNotIn(TEST_SECRET, str(raised.exception))
            self.assert_no_download()
        self.runner.side_effect = subprocess.TimeoutExpired("ssh", 30, output=TEST_SECRET.encode(), stderr=TEST_SECRET.encode())
        with self.assertRaises(fetch.FetchError) as raised:
            self.fetch()
        self.assertNotIn(TEST_SECRET, str(raised.exception))
        self.factory.assert_not_called()

    def test_download_hash_mismatch_leaves_no_final_or_temporary_files(self):
        self.service.download_payload = pack_bytes(info={"name": "changed", "items": [], "bundle_count": 1})
        with self.assertRaisesRegex(fetch.FetchError, "摘要或大小"):
            self.fetch()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.output.parent.iterdir()))

    def test_valid_hash_but_hidden_program_is_not_saved(self):
        self.service.payload = pack_bytes([("evil.bundle", resource_bundle(pe_payload()))])
        self.service.row.update(size=len(self.service.payload), sha256=hashlib.sha256(self.service.payload).hexdigest())
        with self.assertRaises(ValueError):
            self.fetch()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.output.parent.iterdir()))

    def test_rejection_during_download_prevents_final_output(self):
        self.service.after_download = lambda _path: self.service.row.update(status="rejected")
        with self.assertRaisesRegex(fetch.FetchError, "拒绝或撤回"):
            self.fetch()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.output.parent.iterdir()))

    def test_scan_changed_to_failed_during_download_prevents_output(self):
        self.service.after_download = lambda _path: self.service.row["scan"].update(clean=False)
        with self.assertRaisesRegex(fetch.FetchError, "扫描未通过"):
            self.fetch()
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.output.parent.iterdir()))

    def test_output_created_during_download_is_never_overwritten(self):
        self.service.after_download = lambda _path: self.output.write_bytes(b"other writer original")
        with self.assertRaisesRegex(fetch.FetchError, "输出已经存在"):
            self.fetch()
        self.assertEqual(self.output.read_bytes(), b"other writer original")
        self.assertEqual(list(self.output.parent.iterdir()), [self.output])

    def test_cli_hides_arbitrary_remote_exception(self):
        stderr = io.StringIO()
        with patch.object(fetch, "fetch_review", side_effect=RuntimeError(TEST_SECRET)), patch("sys.stderr", stderr):
            result = fetch.main([IDENTIFIER, "--output", str(self.output)])
        self.assertEqual(result, 1)
        self.assertNotIn(TEST_SECRET, stderr.getvalue())
        self.assertIn("未输出凭证或远端内容", stderr.getvalue())

    def test_cli_reports_only_output_hash_and_review_notice(self):
        stdout = io.StringIO()
        result = {"path": str(self.output), "sha256": "d" * 64, "submission_id": IDENTIFIER, "size": 42, "status": "pending"}
        with patch.object(fetch, "fetch_review", return_value=result), patch("sys.stdout", stdout):
            status = fetch.main([IDENTIFIER, "--output", str(self.output), "--ssh-alias", "trusted-alias"])
        self.assertEqual(status, 0)
        self.assertIn(str(self.output), stdout.getvalue())
        self.assertIn(result["sha256"], stdout.getvalue())
        self.assertIn(fetch.REVIEW_NOTICE, stdout.getvalue())
        self.assertNotIn(TEST_SECRET, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
