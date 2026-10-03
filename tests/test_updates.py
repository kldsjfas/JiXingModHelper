"""正式版比较、离线状态、后台请求与固定仓库入口。"""
import io
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from astral_party_auto.core.updates import (
    REPOSITORY_URL, RELEASES_URL, UpdateChecker, describe_release, version_key,
)
from astral_party_auto.web_app import DesktopApi


def release(tag):
    return {"tag_name": tag, "prerelease": False, "draft": False,
            "html_url": "https://unrelated.example/download.exe"}


class UpdateTests(unittest.TestCase):
    def test_numeric_and_prerelease_comparison(self):
        self.assertGreater(version_key("v1.10.0"), version_key("1.9.99"))
        self.assertGreater(version_key("1.3.0"), version_key("1.3.0-preview.10"))
        self.assertGreater(version_key("1.3.0-preview.10"), version_key("1.3.0-preview.2"))
        self.assertEqual(version_key("v1.2.0+build.2"), version_key("1.2.0"))

    def test_local_preview_does_not_offer_downgrade(self):
        result = describe_release("1.3.0-preview.2", release("v1.2.0"))
        self.assertEqual(result["status"], "ahead")

    def test_available_and_equal_release(self):
        self.assertEqual(describe_release("1.3.0-preview.2", release("v1.3.0"))["status"], "available")
        self.assertEqual(describe_release("1.2.0", release("v1.2.0"))["status"], "up_to_date")

    def test_only_valid_stable_releases(self):
        for payload in (None, {}, release("invalid"), release("v1.3.0-preview.1"),
                        {**release("v1.3.0"), "draft": True},
                        {**release("v1.3.0"), "prerelease": True}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                describe_release("1.2.0", payload)
        for value in ("1.3.0-preview..1", "1.3.0-preview.01", "1.3.0/elsewhere", None):
            with self.assertRaises(ValueError):
                version_key(value)

    def test_network_link_cannot_redirect_outside_repository(self):
        result = describe_release("1.2.0", release("v1.3.0"))
        self.assertEqual(result["release_url"], REPOSITORY_URL + "/releases/tag/v1.3.0")

    def test_offline_and_rate_limit_never_report_up_to_date(self):
        errors = [URLError("offline"), TimeoutError(), HTTPError("", 403, "limited", {}, None),
                  HTTPError("", 429, "limited", {}, None)]
        for error in errors:
            checker = UpdateChecker("1.2.0")
            with self.subTest(error=error), patch("astral_party_auto.core.updates.urlopen", side_effect=error):
                checker._check()
                self.assertEqual(checker.snapshot()["status"], "error")
                self.assertFalse(checker.snapshot()["checking"])

    def test_missing_release_has_separate_state(self):
        checker = UpdateChecker("1.2.0")
        with patch("astral_party_auto.core.updates.urlopen", side_effect=HTTPError("", 404, "missing", {}, None)):
            checker._check()
        self.assertEqual(checker.snapshot()["status"], "no_release")

    def test_bad_and_oversized_responses(self):
        for content in (b"<html>gateway</html>", b"x" * (512 * 1024 + 1)):
            with patch("astral_party_auto.core.updates.urlopen", return_value=io.BytesIO(content)):
                checker = UpdateChecker("1.2.0")
                checker._check()
                self.assertEqual(checker.snapshot()["status"], "error")

    def test_background_check_coalesces_requests_and_caches_result(self):
        started, resume = threading.Event(), threading.Event()
        def fetch(*args, **kwargs):
            started.set()
            resume.wait(2)
            return io.BytesIO(json.dumps(release("v1.3.0")).encode())
        checker = UpdateChecker("1.2.0")
        with patch("astral_party_auto.core.updates.urlopen", side_effect=fetch) as fetch_mock:
            checker.start()
            self.assertTrue(started.wait(1))
            self.assertTrue(checker.snapshot()["checking"])
            checker.start(force=True)
            self.assertEqual(fetch_mock.call_count, 1)
            resume.set()
            deadline = time.monotonic() + 2
            while checker.snapshot()["checking"] and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(checker.snapshot()["status"], "available")
            checker.start()
            self.assertEqual(fetch_mock.call_count, 1)
            self.assertEqual(fetch_mock.call_args.kwargs["timeout"], 8)

    def test_failed_refresh_clears_stale_update_link(self):
        checker = UpdateChecker("1.2.0")
        with patch("astral_party_auto.core.updates.urlopen", return_value=io.BytesIO(json.dumps(release("v1.3.0")).encode())):
            checker._check()
        with patch("astral_party_auto.core.updates.urlopen", side_effect=TimeoutError()):
            checker._check()
        self.assertIsNone(checker.snapshot()["latest_version"])
        self.assertEqual(checker.snapshot()["release_url"], RELEASES_URL)

    def test_project_link_only_opens_fixed_destinations(self):
        api = object.__new__(DesktopApi)
        api._controller_lock = threading.RLock()
        api._append_log = Mock()
        api._update_checker = UpdateChecker("1.2.0")
        with patch("astral_party_auto.web_app.os.startfile") as open_url:
            self.assertFalse(api.open_project_link("file:///C:/bad.exe")["ok"])
            open_url.assert_not_called()
            self.assertTrue(api.open_project_link("repository")["ok"])
            open_url.assert_called_once_with(REPOSITORY_URL)
            self.assertTrue(api.open_project_link("release")["ok"])
            self.assertEqual(open_url.call_args.args[0], RELEASES_URL)
            self.assertTrue(api.open_project_link("community")["ok"])
            self.assertEqual(open_url.call_args.args[0], "https://qm.qq.com/q/QC1pQPUpyM")


if __name__ == "__main__":
    unittest.main()
