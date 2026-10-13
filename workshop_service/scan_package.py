"""Fixed ClamAV invocation for private quarantine ZIPs.

Exit 0 means this invocation completed without a reported threat or limit hit.
It does not prove game compatibility or establish that every resource is safe.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

CLAMSCAN_EXECUTABLE = Path("/usr/bin/clamscan")
DATABASE_DIRECTORY = Path("/var/lib/clamav")
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_DATABASE_AGE = 7 * 24 * 3600
MAX_FUTURE_SKEW = 15 * 60
SCAN_TIMEOUT = 90


class ScanError(ValueError):
    """A missing precondition is an error, never a clean scan."""


def _regular_path(path: Path):
    try:
        information = path.lstat()
    except OSError as error:
        raise ScanError("文件不存在或无法读取。") from error
    if (not stat.S_ISREG(information.st_mode) or stat.S_ISLNK(information.st_mode)
            or getattr(information, "st_file_attributes", 0) & 0x400):
        raise ScanError("只能检查普通文件。")
    return information


def validate_package(value: str) -> Path:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ScanError("资源包路径不正确。")
    package = Path(value).absolute()
    if package.suffix.casefold() != ".zip":
        raise ScanError("只能扫描 ZIP 投稿包。")
    information = _regular_path(package)
    if not 0 < information.st_size <= MAX_PACKAGE_BYTES:
        raise ScanError("资源包大小不正确。")
    for parent in package.parents:
        try:
            details = parent.lstat()
        except OSError as error:
            raise ScanError("资源包目录无法读取。") from error
        if stat.S_ISLNK(details.st_mode) or getattr(details, "st_file_attributes", 0) & 0x400:
            raise ScanError("资源包不能位于链接目录内。")
    if os.name != "nt":
        directory = package.parent.stat()
        if information.st_mode & 0o077 or directory.st_mode & 0o077 or information.st_mode & 0o111:
            raise ScanError("资源包和隔离目录必须私有，资源包不能有执行权限。")
    if not zipfile.is_zipfile(package):
        raise ScanError("资源包不是有效的 ZIP。")
    return package


def database_build_time(path: Path) -> int:
    _regular_path(path)
    with path.open("rb") as content:
        header = content.read(512)
    if len(header) != 512:
        raise ScanError("签名数据库头不完整。")
    try:
        fields = header.rstrip(b"\0 \r\n").decode("ascii").split(":")
    except UnicodeError as error:
        raise ScanError("签名数据库头不正确。") from error
    if (len(fields) != 9 or fields[0] != "ClamAV-VDB" or not fields[2].isdigit()
            or not fields[3].isdigit() or int(fields[3]) < 1 or not fields[4].isdigit()
            or not fields[-1].isdigit() or len(fields[-1]) > 12):
        raise ScanError("签名数据库构建资料不正确。")
    built_at = int(fields[-1])
    if built_at < 946684800:
        raise ScanError("签名数据库构建日期不正确。")
    return built_at


def validate_database(*, now=None) -> int:
    checked_at = time.time() if now is None else now
    builds = []
    for name in ("daily.cvd", "daily.cld"):
        candidate = DATABASE_DIRECTORY / name
        if candidate.exists():
            builds.append(database_build_time(candidate))
    if not builds:
        raise ScanError("缺少 daily 签名数据库。")
    latest = max(builds)
    if latest > checked_at + MAX_FUTURE_SKEW or checked_at - latest > MAX_DATABASE_AGE:
        raise ScanError("签名数据库过期或构建时间不正确。")
    return latest


def scan(package: Path) -> int:
    validate_database()
    _regular_path(CLAMSCAN_EXECUTABLE)
    command = [str(CLAMSCAN_EXECUTABLE), "--database=" + str(DATABASE_DIRECTORY),
               "--no-summary", "--max-scansize=512M", "--max-filesize=128M",
               "--max-recursion=8", "--max-files=1024", "--alert-exceeds-max=yes",
               "--", str(package)]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=SCAN_TIMEOUT, shell=False, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ScanError("扫描器无法完成扫描。") from error
    return result.returncode if result.returncode in (0, 1, 2) else 2


def main(arguments=None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 1:
        print("扫描器需要且只需要一个私有 ZIP 路径。", file=sys.stderr)
        return 2
    try:
        package = validate_package(arguments[0])
        return scan(package)
    except (ScanError, OSError, zipfile.BadZipFile):
        # Do not expose the private path, scanner output or database header.
        print("扫描未通过：资源包、签名数据库或扫描器不可用。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
