"""从私有投稿箱取审核 ZIP；不会解包、安装、运行或发布作品。"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astral_party_auto.modkit.workshop import inspect_package
from tools.workshop_github_bridge import ServiceApi, approved_submission, checked_submission

IDENTIFIER = re.compile(r"[0-9a-f]{32}\Z")
SSH_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SSH_TIMEOUT = 30
PROTECTED_NAMES = {"modkit_data", "made_mods", "_internal", "astral party", "astralparty", "astralparty_cn", "astralparty_int"}
GAME_EXECUTABLES = ("AstralParty_CN.exe", "AstralParty_INT.exe", "AstralParty.exe", "吉星派对.exe", "星引擎派对.exe")
REVIEW_NOTICE = "仅供审核，尚未批准安装或分发；仍需 Windows Defender 扫描及实际游戏验证。"

# 命令固定，只读这一项；不 source 环境文件，也不接受用户提供远端代码。
REMOTE_SECRET_CODE = """from pathlib import Path
import sys
path = Path('/etc/jixing-workshop.env')
if path.stat().st_size > 65536:
    sys.exit(2)
values = [line.split('=', 1)[1].strip() for line in path.read_text(encoding='utf-8').splitlines()
          if line.startswith('WORKSHOP_ADMIN_SECRET=')]
if len(values) != 1:
    sys.exit(2)
value = values[0]
if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
    value = value[1:-1]
if not 32 <= len(value) <= 256 or not value.isascii() or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
    sys.exit(2)
sys.stdout.write(value)
"""
REMOTE_COMMAND = "sudo -n python3.11 -c " + shlex.quote(REMOTE_SECRET_CODE)


class FetchError(ValueError):
    """只携带本工具生成的提示，不能夹带远端输出或凭证。"""


def _is_link(information):
    return stat.S_ISLNK(information.st_mode) or bool(getattr(information, "st_file_attributes", 0) & 0x400)


def _check_parents(directory: Path):
    for current in (directory, *directory.parents):
        if current.name.casefold() in PROTECTED_NAMES:
            raise FetchError("审核包不能保存到游戏或助手运行数据目录，请使用单独的审核目录。")
        try:
            information = current.lstat()
        except FileNotFoundError:
            continue
        if _is_link(information) or not stat.S_ISDIR(information.st_mode):
            raise FetchError("保存目录及其父目录必须是普通目录，不能包含链接或重解析点。")
        if any((current / name).exists() for name in GAME_EXECUTABLES):
            raise FetchError("审核包不能保存到游戏目录，请使用单独的审核目录。")


def _output_path(value: str | Path) -> Path:
    candidate = Path(value)
    if candidate.suffix.casefold() != ".zip" or ".." in candidate.parts:
        raise FetchError("请指定全新的 .zip 文件路径，不使用上级路径跳转。")
    destination = candidate.absolute()
    _check_parents(destination.parent)
    try:
        destination.lstat()
    except FileNotFoundError:
        return destination
    raise FetchError("输出已经存在，请另选 ZIP 文件名；不会覆盖原文件。")


def read_admin_secret(alias: str, *, runner=None) -> str:
    if not isinstance(alias, str) or not SSH_ALIAS.fullmatch(alias):
        raise FetchError("SSH 别名只能包含英文字母、数字、下划线或短横线。")
    runner = subprocess.run if runner is None else runner
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-T", alias, REMOTE_COMMAND]
    try:
        result = runner(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=SSH_TIMEOUT, shell=False, check=False)
    except (OSError, subprocess.SubprocessError):
        raise FetchError("SSH 无法读取维护凭证，请检查可信别名和连接；远端输出已隐藏。") from None
    if result.returncode != 0 or not isinstance(result.stdout, bytes) or not 32 <= len(result.stdout) <= 256:
        raise FetchError("SSH 未返回有效维护凭证；远端输出已隐藏。")
    try:
        secret = result.stdout.decode("ascii")
    except UnicodeError:
        raise FetchError("SSH 未返回有效维护凭证；远端输出已隐藏。") from None
    if any(ord(char) <= 32 or ord(char) >= 127 for char in secret):
        raise FetchError("SSH 未返回有效维护凭证；远端输出已隐藏。")
    return secret


def _reviewable_row(row, identifier):
    checked_submission(row, identifier)
    if row["status"] == "rejected":
        raise FetchError("投稿已经拒绝或撤回，不能取得审核包。")
    if row["status"] == "approved":
        approved_submission(row, identifier)
    return row


def _same_package(latest, original):
    if any(latest[key] != original[key] for key in ("submission_id", "metadata", "sha256", "size")):
        raise FetchError("审核记录在下载期间变化，未保存 ZIP，请重新获取。")


def _checked_scan(scan, row):
    if (not isinstance(scan, dict) or scan.get("clean") is not True or scan.get("sha256") != row["sha256"]
            or not isinstance(scan.get("scan_id"), str) or not IDENTIFIER.fullmatch(scan["scan_id"])
            or type(scan.get("scanned_at")) is not int
            or not time.time() - 48 * 3600 <= scan["scanned_at"] <= time.time() + 300):
        raise FetchError("服务器扫描未通过或记录无效，未保存审核包。")


def fetch_review(identifier: str, output: str | Path, *, ssh_alias="killer-star", runner=None,
                 service_factory=None) -> dict:
    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        raise FetchError("投稿编号必须是 32 位小写十六进制值。")
    destination = _output_path(output)
    secret = read_admin_secret(ssh_alias, runner=runner)
    service = (ServiceApi if service_factory is None else service_factory)(secret)
    row = _reviewable_row(service.detail(identifier), identifier)
    if row["status"] == "pending":
        scan = service.scan(identifier)
        _checked_scan(scan, row)
        latest = _reviewable_row(service.detail(identifier), identifier)
        _same_package(latest, row)
        _checked_scan(latest.get("scan"), latest)
        row = latest
    _check_parents(destination.parent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _check_parents(destination.parent)
    with tempfile.TemporaryDirectory(prefix=".workshop-review-", dir=destination.parent) as temporary:
        temporary_dir = Path(temporary)
        os.chmod(temporary_dir, 0o700)
        package = temporary_dir / "review.zip"
        service.download(row, package)
        information = package.lstat()
        if _is_link(information) or not stat.S_ISREG(information.st_mode):
            raise FetchError("下载产物不是普通 ZIP 文件，未保存审核包。")
        os.chmod(package, 0o600)
        inspection = inspect_package(package)
        if inspection["sha256"] != row["sha256"] or inspection["size"] != row["size"]:
            raise FetchError("审核包的摘要或大小与服务器记录不一致，未保存 ZIP。")
        latest = _reviewable_row(service.detail(identifier), identifier)
        _same_package(latest, row)
        _checked_scan(latest.get("scan"), latest)
        _check_parents(destination.parent)
        # 暂存与目标同盘；硬链接只创建新文件，不覆盖途中出现的同名文件。
        try:
            os.link(package, destination)
        except FileExistsError:
            raise FetchError("输出已经存在，请另选 ZIP 文件名；不会覆盖原文件。") from None
    return {"path": str(destination), "submission_id": identifier,
            "sha256": inspection["sha256"], "size": inspection["size"], "status": latest["status"]}


def main(arguments=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("submission_id", help="32 位小写十六进制投稿编号")
    parser.add_argument("--output", required=True, type=Path, help="全新的审核 ZIP 路径，不能位于游戏或助手数据目录")
    parser.add_argument("--ssh-alias", default="killer-star", help="已配置的可信 SSH 别名，默认 killer-star")
    arguments = parser.parse_args(arguments)
    try:
        result = fetch_review(arguments.submission_id, arguments.output, ssh_alias=arguments.ssh_alias)
    except FetchError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        print("获取审核包失败，未输出凭证或远端内容；请检查服务、扫描器和保存目录后重试。", file=sys.stderr)
        return 1
    print("审核 ZIP：" + result["path"])
    print("SHA-256：" + result["sha256"])
    print(REVIEW_NOTICE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
