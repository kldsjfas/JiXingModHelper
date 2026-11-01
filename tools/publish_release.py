"""Publish one checked build from the trusted main branch or a matching version tag."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

REPOSITORY = "kldsjfas/JiXingModHelper"
ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


class PublicationError(RuntimeError):
    pass


def checked_version(value: str) -> str:
    if not isinstance(value, str) or len(value) > 80 or not VERSION.fullmatch(value):
        raise PublicationError("构建版本必须是明确的三段版本号，可附预发布标识。")
    if "-" in value:
        for part in value.split("-", 1)[1].split("."):
            if part.isdigit() and len(part) > 1 and part.startswith("0"):
                raise PublicationError("预发布版本中的数字不能带前导零。")
    return value


def project_version(root: Path = ROOT) -> str:
    tree = ast.parse((root / "astral_party_auto/__init__.py").read_text(encoding="utf-8-sig"))
    assignments = []
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            assignments.append(node)
    if (len(assignments) != 1 or isinstance(assignments[0], ast.AugAssign)
            or not isinstance(assignments[0].value, ast.Constant)
            or not isinstance(assignments[0].value.value, str)):
        raise PublicationError("无法唯一读取构建版本。")
    return checked_version(assignments[0].value.value)


def run_gh(arguments: list[str], *, input_text: str | None = None):
    try:
        return subprocess.run(["gh", *arguments], input=input_text, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=180, check=False,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        raise PublicationError("GitHub 发布命令无法完成，已停止；请核对远端状态后再处理。") from None


def api(method: str, path: str, *, payload=None, missing_ok=False, runner=run_gh):
    arguments = ["api", "--include", "--method", method, "/repos/" + REPOSITORY + path]
    if payload is not None:
        arguments.extend(["--input", "-"])
    result = runner(arguments, input_text=None if payload is None else json.dumps(payload))
    header, separator, body = result.stdout.replace("\r\n", "\n").partition("\n\n")
    first_line = header.splitlines()[0] if header else ""
    status_match = re.fullmatch(r"HTTP/\S+ ([0-9]{3})(?: .*)?", first_line)
    status = int(status_match[1]) if status_match else None
    if status == 404 and missing_ok:
        return None
    if result.returncode or status is None or not 200 <= status < 300 or not separator:
        suffix = f"（HTTP {status}）" if status is not None else ""
        raise PublicationError("无法确认 GitHub 状态" + suffix + "，已停止发布。")
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        raise PublicationError("GitHub 状态响应不正确，已停止发布。") from None


def tag_commit(tag: str, *, runner=run_gh) -> str | None:
    ref = api("GET", "/git/ref/tags/" + tag, missing_ok=True, runner=runner)
    if ref is None:
        return None
    if not isinstance(ref, dict) or ref.get("ref") != "refs/tags/" + tag:
        raise PublicationError("GitHub 版本标签与请求不符。")
    target = ref.get("object", {})
    for _ in range(5):
        if not isinstance(target, dict) or not SHA.fullmatch(str(target.get("sha", ""))):
            break
        if target.get("type") == "commit":
            return target["sha"]
        if target.get("type") != "tag":
            break
        annotation = api("GET", "/git/tags/" + target["sha"], runner=runner)
        target = annotation.get("object", {}) if isinstance(annotation, dict) else {}
    raise PublicationError("无法核实版本标签指向的提交。")


def checked_artifacts(directory: Path, version: str) -> tuple[Path, Path, Path]:
    package = directory / "JiXingModHelper.zip"
    checksum = directory / "SHA256SUMS.txt"
    notes = directory / "RELEASE_NOTES.md"
    if any(not path.is_file() or path.is_symlink() for path in (package, checksum, notes)):
        raise PublicationError("同一构建的正式包、校验文件或发布说明不完整。")
    fields = checksum.read_text(encoding="ascii").split()
    if len(fields) != 2 or fields[1] != package.name or not re.fullmatch(r"[0-9a-f]{64}", fields[0]):
        raise PublicationError("构建校验文件格式不正确。")
    with package.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != fields[0]:
        raise PublicationError("构建 ZIP 与校验文件不一致，未创建版本标签。")
    lines = notes.read_text(encoding="utf-8-sig").splitlines()
    if not lines or lines[0].strip() != "# 吉星派对 Mod 助手 v" + version:
        raise PublicationError("构建发布说明与版本不一致，未创建版本标签。")
    return package, checksum, notes


def publish(version: str, environment, *, directory=Path("release"), runner=run_gh) -> dict:
    version = checked_version(version)
    tag = "v" + version
    ref, commit = environment.get("GITHUB_REF", ""), environment.get("GITHUB_SHA", "")
    if (environment.get("GITHUB_REPOSITORY") != REPOSITORY or environment.get("GITHUB_EVENT_NAME") != "push"
            or environment.get("BUILD_VERSION") != version or not isinstance(commit, str) or not SHA.fullmatch(commit)
            or ref not in ("refs/heads/main", "refs/tags/" + tag)):
        raise PublicationError("发布只允许可信主分支或与构建版本相同的标签 push。")
    existing_tag = tag_commit(tag, runner=runner)
    if ref == "refs/heads/main" and existing_tag is not None and existing_tag != commit:
        return {"status": "skipped", "tag": tag, "reason": "existing_tag_unchanged"}
    if ref.startswith("refs/tags/") and existing_tag != commit:
        raise PublicationError("版本标签不再指向本次构建提交，已停止发布。")
    existing_release = api("GET", "/releases/tags/" + tag, missing_ok=True, runner=runner)
    if existing_release is not None:
        if not isinstance(existing_release, dict) or existing_release.get("tag_name") != tag:
            raise PublicationError("已有 Release 与版本标签不一致。")
        if existing_release.get("draft") is not False:
            raise PublicationError("此版本已有草稿或未知状态，保留其资产并停止；请人工检查。")
        return {"status": "skipped", "tag": tag, "reason": "existing_release_unchanged"}
    package, checksum, notes = checked_artifacts(Path(directory), version)
    if existing_tag is None:
        created = api("POST", "/git/refs", payload={"ref": "refs/tags/" + tag, "sha": commit}, runner=runner)
        if (not isinstance(created, dict) or created.get("ref") != "refs/tags/" + tag
                or created.get("object", {}).get("type") != "commit" or created["object"].get("sha") != commit):
            raise PublicationError("新标签确认失败，停止发布；请检查远端标签。")
    if tag_commit(tag, runner=runner) != commit:
        raise PublicationError("发布前版本标签发生变化，已停止。")
    command = ["release", "create", tag, str(package), str(checksum), "--repo", REPOSITORY,
               "--verify-tag", "--target", commit, "--title", "吉星派对 Mod 助手 " + tag,
               "--notes-file", str(notes)]
    if "-" in version:
        command.append("--prerelease")
    result = runner(command)
    if result.returncode:
        raise PublicationError("Release 创建未确认；标签和构建均保留，不覆盖任何已有发布。")
    return {"status": "published", "tag": tag, "commit": commit}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="store_true", help="仅校验并输出源码版本")
    arguments = parser.parse_args()
    try:
        version = project_version()
        if arguments.version:
            print(version)
        else:
            print(json.dumps(publish(version, os.environ), ensure_ascii=False))
        return 0
    except (PublicationError, OSError, UnicodeError, SyntaxError, IndexError) as error:
        print(str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
