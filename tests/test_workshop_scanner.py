"""Scanner command and fail-closed QA; these mocks are not real antivirus."""
import contextlib
import io
import os
import subprocess
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from workshop_service import scan_package as scanner


def database_header(timestamp, *, signatures=100):
    value = f"ClamAV-VDB:13 Oct 2026 00-00 +0000:100:{signatures}:90:md5:signature:builder:{timestamp}"
    return value.encode("ascii").ljust(512, b" ")


class WorkshopScannerTests(unittest.TestCase):
    def setUp(self):
        cache = os.environ.get("WORKSHOP_TEST_CACHE")
        self.temporary = tempfile.TemporaryDirectory(dir=cache)
        self.root = Path(self.temporary.name)
        self.quarantine = self.root / "quarantine"
        self.quarantine.mkdir(mode=0o700)
        self.package = self.quarantine / "fixture.zip"
        with zipfile.ZipFile(self.package, "w") as archive:
            archive.writestr("fixture.txt", b"scanner-protocol-fixture")
        self.package.chmod(0o600)
        self.database = self.root / "database"
        self.database.mkdir()
        self.now = int(time.time())
        self.daily = self.database / "daily.cvd"
        self.daily.write_bytes(database_header(self.now))
        self.executable = self.root / "clamscan-stub"
        self.executable.write_bytes(b"fixture, never executed")
        self.patchers = [patch.object(scanner, "DATABASE_DIRECTORY", self.database),
                         patch.object(scanner, "CLAMSCAN_EXECUTABLE", self.executable)]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        self.temporary.cleanup()

    def call(self, arguments):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            result = scanner.main(arguments)
        return result, output.getvalue()

    def test_fixed_command_limits_and_no_shell(self):
        with patch.object(scanner.subprocess, "run", return_value=Mock(returncode=0)) as invocation:
            self.assertEqual(self.call([str(self.package)])[0], 0)
        command = invocation.call_args.args[0]
        self.assertEqual(command[0], str(self.executable))
        self.assertEqual(command[-2:], ["--", str(self.package)])
        for option in ["--max-scansize=512M", "--max-filesize=128M", "--max-recursion=8", "--max-files=1024", "--alert-exceeds-max=yes"]:
            self.assertIn(option, command)
        options = invocation.call_args.kwargs
        self.assertFalse(options["shell"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertLess(options["timeout"], 120)

    def test_only_one_zip_path_is_accepted(self):
        for arguments in [[], [str(self.package), "--database=evil"], [""], [str(self.package) + "\nsecret"]]:
            with self.subTest(arguments=arguments):
                self.assertEqual(self.call(arguments)[0], 2)

    def test_nonzip_missing_directory_and_bad_zip_rejected(self):
        bad = self.quarantine / "not-a-package.zip"
        bad.write_bytes(b"not ZIP")
        bad.chmod(0o600)
        for path in [bad, self.quarantine, self.quarantine / "absent.zip", self.executable]:
            self.assertEqual(self.call([str(path)])[0], 2)

    def test_missing_empty_and_oversized_package_rejected(self):
        with patch.object(scanner, "MAX_PACKAGE_BYTES", 1):
            self.assertEqual(self.call([str(self.package)])[0], 2)
        empty = self.quarantine / "empty.zip"
        empty.touch(mode=0o600)
        self.assertEqual(self.call([str(empty)])[0], 2)

    @unittest.skipIf(os.name == "nt", "NTFS ACL is configured by the operator")
    def test_world_readable_or_executable_package_rejected(self):
        for permission in (0o644, 0o700):
            self.package.chmod(permission)
            self.assertEqual(self.call([str(self.package)])[0], 2)
        self.package.chmod(0o600)
        self.quarantine.chmod(0o755)
        self.assertEqual(self.call([str(self.package)])[0], 2)

    @unittest.skipIf(os.name == "nt", "Symlink creation requires additional Windows permissions")
    def test_link_package_and_link_parent_rejected(self):
        linked = self.quarantine / "linked.zip"
        linked.symlink_to(self.package)
        self.assertEqual(self.call([str(linked)])[0], 2)
        directory = self.root / "linked-directory"
        directory.symlink_to(self.quarantine, target_is_directory=True)
        self.assertEqual(self.call([str(directory / self.package.name)])[0], 2)

    def test_daily_is_required_and_main_alone_is_not_enough(self):
        self.daily.unlink()
        (self.database / "main.cvd").write_bytes(database_header(self.now))
        with self.assertRaises(scanner.ScanError):
            scanner.validate_database(now=self.now)
        self.assertEqual(self.call([str(self.package)])[0], 2)

    def test_header_build_date_does_not_depend_on_file_mtime(self):
        os.utime(self.daily, (1, 1))
        self.assertEqual(scanner.validate_database(now=self.now), self.now)

    def test_stale_or_future_database_rejected(self):
        for timestamp in [self.now - 8 * 86400, self.now + 3600]:
            self.daily.write_bytes(database_header(timestamp))
            with self.assertRaises(scanner.ScanError):
                scanner.validate_database(now=self.now)
        self.daily.write_bytes(database_header(self.now - scanner.MAX_DATABASE_AGE))
        self.assertEqual(scanner.validate_database(now=self.now), self.now - scanner.MAX_DATABASE_AGE)

    def test_cld_is_supported_and_freshest_daily_is_used(self):
        self.daily.write_bytes(database_header(self.now - 8 * 86400))
        (self.database / "daily.cld").write_bytes(database_header(self.now))
        self.assertEqual(scanner.validate_database(now=self.now), self.now)
        self.daily.unlink()
        self.assertEqual(scanner.validate_database(now=self.now), self.now)

    def test_corrupt_short_and_empty_signature_headers_rejected(self):
        values = [b"too short", b"bad header".ljust(512, b" "), database_header(self.now, signatures=0),
                  database_header(1), database_header(self.now).replace(b"ClamAV-VDB", b"NotOur-VDB")]
        for value in values:
            self.daily.write_bytes(value)
            with self.assertRaises(scanner.ScanError):
                scanner.validate_database(now=self.now)

    def test_detection_errors_and_unknown_exit_codes_are_not_clean(self):
        for code in (1, 2, 3, -9, 127):
            with patch.object(scanner.subprocess, "run", return_value=Mock(returncode=code)):
                self.assertEqual(self.call([str(self.package)])[0], code if code in (1, 2) else 2)

    def test_timeout_missing_scanner_and_errors_fail_closed(self):
        for failure in [OSError("private secret"), subprocess.TimeoutExpired("private secret", 90)]:
            with patch.object(scanner.subprocess, "run", side_effect=failure):
                code, output = self.call([str(self.package)])
                self.assertEqual(code, 2)
                self.assertNotIn(str(self.package), output)
                self.assertNotIn("private secret", output)
        self.executable.unlink()
        self.assertEqual(self.call([str(self.package)])[0], 2)


if __name__ == "__main__":
    unittest.main()
