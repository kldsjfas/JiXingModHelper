"""完整解压与可写目录的启动检查先于创建用户数据。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from astral_party_auto import native_host_app as host


class StartupChecksTests(unittest.TestCase):
    def test_fresh_directory_probe_leaves_no_files_or_user_directories(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.multiple(host, APP_ROOT=root, DATA_DIR=root / "modkit_data"):
                host._check_data_directory()
            self.assertEqual(list(root.iterdir()), [])

    def test_permission_failure_explains_recovery_without_moving_user_data(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.multiple(host, APP_ROOT=root, DATA_DIR=root / "modkit_data"), \
                    patch.object(host.tempfile, "TemporaryFile", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(OSError, "完整 ZIP") as result:
                    host._check_data_directory()
            self.assertIn(str(root), str(result.exception))
            self.assertEqual(list(root.iterdir()), [])

    def test_existing_data_folder_is_checked_before_loading_drafts(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "modkit_data"
            data.mkdir()
            probe = host.tempfile.TemporaryFile
            def check(*args, **kwargs):
                if Path(kwargs["dir"]) == data:
                    raise PermissionError("data folder denied")
                return probe(*args, **kwargs)
            with patch.multiple(host, APP_ROOT=root, DATA_DIR=data), \
                    patch.object(host.tempfile, "TemporaryFile", side_effect=check):
                with self.assertRaisesRegex(OSError, "modkit_data"):
                    host._check_data_directory()


if __name__ == "__main__":
    unittest.main()
