"""工坊 API 的安装关卡，所有游戏资源与杀毒结果均隔离。"""
from __future__ import annotations

import io
import json
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from wsgiref.util import setup_testing_defaults

from astral_party_auto.web_app import DesktopApi, _build_server
from astral_party_auto.modkit.workshop_security import scan_workshop_package
from astral_party_auto.modkit.workshop import inspect_package
from tests.test_workshop import bundle, metadata, pack_bytes


class WorkshopApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="workshop-api-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = {"id": "safe-mod", "name": "测试作品", "version": "1", "asset": {"sha256": "a" * 64}}
        self.prepared = {"mod_dir": str(self.root), "path": str(self.root / "pack.zip"), "entry": self.entry,
                         "inspection": {"sha256": "a" * 64}, "game_key": "game-a"}
        self.api = object.__new__(DesktopApi)
        self.api._controller_lock = threading.RLock()
        self.api._append_log = Mock()
        self.api._workshop_client = Mock()
        self.api._workshop_pending = self.prepared
        self.api._workshop_submission = None
        self.api._workshop_issue_url = None
        self.api._workshop_client.prepare.return_value = self.prepared
        self.api.controller = Mock()
        self.api.controller.aa_dirs = ()
        self.api.controller._index_source_key = "game-a"
        self.api.controller.install.return_value = Mock(matched=["one.bundle"])
        self.api._dashboard_state = Mock(return_value={"has_game": True})
        self.api._workshop_analysis = Mock(return_value={"can_install": True, "message": "ok"})
        self.prepared["analysis"] = {"can_install": True, "message": "ok"}
        self.scanner = self.enterContext(patch("astral_party_auto.modkit.workshop_security.scan_workshop_package",
                                              return_value={"status": "passed"}))

    def test_success_rechecks_catalog_and_scans_before_install(self):
        order = []
        self.scanner.side_effect = lambda *_: order.append("scan") or {"status": "passed"}
        self.api._workshop_client.verify_prepared.side_effect = lambda *_args, **_kwargs: order.append("verify")
        self.api.controller.install.side_effect = lambda *_args, **_kwargs: order.append("install") or Mock(matched=["one.bundle"])
        result = self.api.workshop_install()
        self.assertTrue(result["ok"], result)
        self.assertEqual(order, ["scan", "verify", "install"])
        self.api._workshop_client.prepare.assert_called_once_with("safe-mod", expected_sha256="a" * 64, forbidden_bundle_names=set())
        self.assertIsNone(self.api._workshop_pending)
        self.assertEqual(self.api._workshop_client.discard.call_count, 2)

    def test_each_failed_gate_leaves_game_untouched(self):
        for gate in ("withdrawn", "incompatible", "running", "scan", "tampered"):
            with self.subTest(gate=gate):
                self.api._workshop_pending = self.prepared
                self.api.controller.install.reset_mock()
                self.api._workshop_client.prepare.side_effect = RuntimeError("已下架") if gate == "withdrawn" else None
                self.api._workshop_analysis.return_value = {"can_install": gate != "incompatible", "message": "版本不符"}
                self.api.controller._require_catalog_game_stopped.side_effect = RuntimeError("游戏运行中") if gate == "running" else None
                self.scanner.side_effect = RuntimeError("扫描失败") if gate == "scan" else None
                self.api._workshop_client.verify_prepared.side_effect = RuntimeError("包已改变") if gate == "tampered" else None
                result = self.api.workshop_install()
                self.assertFalse(result["ok"], result)
                self.assertIsNone(self.api._workshop_pending)
                self.api.controller.install.assert_not_called()

    def test_game_changed_or_missing_candidate_never_installs(self):
        self.api.controller._index_source_key = "game-b"
        self.assertFalse(self.api.workshop_install()["ok"])
        self.api._workshop_client.prepare.assert_not_called()
        self.assertFalse(self.api.workshop_install()["ok"])
        self.api.controller.install.assert_not_called()

    def test_new_conflict_requires_a_fresh_confirmation(self):
        self.api._workshop_analysis.return_value = {"can_install": True, "message": "ok", "conflicts": 1,
                                                   "conflict_names": ["刚启用的 Mod"]}
        result = self.api.workshop_install()
        self.assertFalse(result["ok"])
        self.assertIn("冲突已改变", result["error"])
        self.scanner.assert_not_called()
        self.api.controller.install.assert_not_called()

    def test_http_origin_token_and_json_guards_apply_to_workshop(self):
        app = _build_server(self.api, "test-only-token")
        for token, origin, content_type, expected in (
            ("wrong", "http://127.0.0.1:1234", "application/json", 403),
            ("test-only-token", "https://attacker.example", "application/json", 403),
            ("test-only-token", "http://127.0.0.1:1234", "text/plain", 415),
        ):
            environment = {}
            setup_testing_defaults(environment)
            payload = b"[]"
            environment.update(REQUEST_METHOD="POST", PATH_INFO="/api/workshop_install", HTTP_HOST="127.0.0.1:1234",
                               HTTP_X_JIXING_TOKEN=token, HTTP_ORIGIN=origin, CONTENT_TYPE=content_type,
                               CONTENT_LENGTH=str(len(payload)), **{"wsgi.input": io.BytesIO(payload)})
            response = {}
            chunks = app(environment, lambda status, headers, *_: response.update(status=int(status.split()[0])))
            try:
                list(chunks)
            finally:
                if hasattr(chunks, "close"):
                    chunks.close()
            self.assertEqual(response["status"], expected)
        self.api.controller.install.assert_not_called()

    def test_cancelled_selection_clears_old_submission_and_link(self):
        self.api._workshop_submission = {"path": "stale.zip"}
        self.api._workshop_issue_url = "https://github.com/stale"
        self.api._pick_path = Mock(return_value=None)
        self.assertTrue(self.api.workshop_choose_submission()["data"]["cancelled"])
        self.assertIsNone(self.api._workshop_submission)
        self.assertIsNone(self.api._workshop_issue_url)

    def test_external_zip_selection_and_submission_use_real_package_checks(self):
        source = self.root / "outside-editor.zip"
        payload = pack_bytes([("OtherEditor/texture.bundle", bundle())], include_info=False)
        source.write_bytes(payload)
        destination = self.root / "outside-submission.zip"
        self.api._pick_path = Mock(side_effect=[source, destination])
        chosen = self.api.workshop_choose_submission()
        self.assertTrue(chosen["ok"], chosen)
        self.assertIsNone(chosen["data"]["inspection"]["mod_info"])
        self.assertEqual(chosen["data"]["inspection"]["bundle_count"], 1)
        self.assertEqual(self.api._workshop_submission["sha256"], inspect_package(source)["sha256"])
        details = metadata()
        details.pop("id")
        exported = self.api.workshop_export_submission(details)
        self.assertTrue(exported["ok"], exported)
        result = exported["data"]
        self.assertTrue(Path(result["path"]).samefile(destination))
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(set(archive.namelist()), {"texture.bundle", "mod_info.json"})
            self.assertEqual(archive.read("texture.bundle"), bundle())
            self.assertEqual(json.loads(archive.read("mod_info.json"))["name"], details["name"])
        inspection = inspect_package(destination)
        report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(inspection["sha256"], report["asset"]["sha256"])
        self.assertEqual(report["review_status"], "pending")
        self.assertTrue(result["issue_url"].startswith("https://github.com/kldsjfas/JiXingModHelper/issues/new?"))
        self.assertEqual(source.read_bytes(), payload)
        self.api.controller.install.assert_not_called()
        self.scanner.assert_not_called()

    def test_external_unsafe_selection_clears_candidate_without_export_or_install(self):
        source = self.root / "unsafe-outside.zip"
        for name, data in (("plugin.dll", b"MZ"), ("run.ps1", b"script"), ("../bad.bundle", bundle()),
                           ("bad.bundle", b"not-unity"), ("mod_info.json", b'{"name":"bad","items":"invalid"}')):
            with self.subTest(name=name):
                source.write_bytes(pack_bytes([("texture.bundle", bundle()), (name, data)], include_info=False))
                self.api._workshop_submission = {"path": "stale.zip", "sha256": "a" * 64}
                self.api._workshop_issue_url = "https://github.com/stale"
                self.api._pick_path = Mock(return_value=source)
                chosen = self.api.workshop_choose_submission()
                self.assertFalse(chosen["ok"], chosen)
                self.assertIsNone(self.api._workshop_submission)
                self.assertIsNone(self.api._workshop_issue_url)
                self.assertEqual(self.api._pick_path.call_count, 1)
        self.api.controller.install.assert_not_called()

    def test_external_link_is_never_accepted(self):
        with patch("astral_party_auto.web_app.os.startfile", create=True) as opener:
            self.assertFalse(self.api.workshop_open_link("https://attacker.example")["ok"])
            opener.assert_not_called()


class DefenderScanTests(unittest.TestCase):
    def test_scan_error_and_detection_fail_closed(self):
        with TemporaryDirectory(prefix="workshop-scan-") as root:
            for returncode in (2, 1, -1):
                with self.subTest(returncode=returncode), patch("astral_party_auto.modkit.workshop_security._defender_executable", return_value=Path("trusted/MpCmdRun.exe")), patch("astral_party_auto.modkit.workshop_security.subprocess.run", return_value=Mock(returncode=returncode)) as run:
                    with self.assertRaises(RuntimeError):
                        scan_workshop_package(Path(root))
                    self.assertIn("-DisableRemediation", run.call_args.args[0])
                    self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_scanner_unavailable_never_runs_mod_program(self):
        with TemporaryDirectory(prefix="workshop-scan-") as root, patch("astral_party_auto.modkit.workshop_security._defender_executable", side_effect=RuntimeError("不可用")), patch("astral_party_auto.modkit.workshop_security.subprocess.run") as run:
            with self.assertRaises(RuntimeError):
                scan_workshop_package(Path(root))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
