"""检查仓库的正式 Release；网络请求在后台运行，不改动本地安装。"""
from __future__ import annotations

import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


REPOSITORY_URL = "https://github.com/kldsjfas/JiXingModHelper"
RELEASES_URL = f"{REPOSITORY_URL}/releases"
LATEST_RELEASE_API = "https://api.github.com/repos/kldsjfas/JiXingModHelper/releases/latest"
_VERSION = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?\Z")


def version_key(value: str) -> tuple:
    """按语义版本比较：1.10 高于 1.9，正式版高于同版本的 preview。"""
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("版本号格式无法识别。")
    matched = _VERSION.fullmatch(value.strip())
    if not matched:
        raise ValueError("版本号格式无法识别。")
    major, minor, patch, preview, metadata = matched.groups()
    for group in (preview, metadata):
        if group is not None and any(not part for part in group.split(".")):
            raise ValueError("版本号格式无法识别。")
    identifiers = []
    for part in preview.split(".") if preview else ():
        if part.isdigit():
            if len(part) > 1 and part.startswith("0"):
                raise ValueError("版本号格式无法识别。")
            identifiers.append((0, int(part)))
        else:
            identifiers.append((1, part))
    return ((int(major), int(minor), int(patch)), (0, tuple(identifiers)) if preview else (1,))


def describe_release(current_version: str, release: dict) -> dict:
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("prerelease") is not False:
        raise ValueError("GitHub 没有返回有效的正式版本信息，请稍后重试。")
    tag = release.get("tag_name")
    latest, current = version_key(tag), version_key(current_version)
    if latest[1] != (1,):
        raise ValueError("最新正式版接口返回了试用版本，请打开发布页核对。")
    if latest > current:
        status, message = "available", f"发现新版本 {tag}，可打开发布页下载。"
    elif latest == current:
        status, message = "up_to_date", f"已是最新正式版 {tag}。"
    else:
        status, message = "ahead", f"当前版本 {current_version} 高于最新正式版 {tag}，无需降级。"
    # 不接受网络返回的任意跳转地址，发布链接始终属于本项目。
    return {"status": status, "message": message, "latest_version": tag,
            "release_url": f"{REPOSITORY_URL}/releases/tag/{quote(tag, safe='')}"}


class UpdateChecker:
    def __init__(self, current_version: str):
        self.current_version = current_version
        self._lock = threading.Lock()
        self._last_check = None
        self._state = {"checking": False, "status": "idle", "current_version": current_version,
                       "latest_version": None, "message": "尚未检查更新。",
                       "repository_url": REPOSITORY_URL, "release_url": RELEASES_URL}

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, force=False) -> dict:
        with self._lock:
            if self._state["checking"] or (not force and self._last_check is not None and time.monotonic() - self._last_check < 900):
                return dict(self._state)
            self._state.update(checking=True, status="checking", message="正在检查 GitHub 正式版本…")
        threading.Thread(target=self._check, daemon=True, name="release-update-check").start()
        return self.snapshot()

    def _check(self) -> None:
        result = {"status": "error", "message": "检查更新失败，请稍后重试。"}
        try:
            request = Request(LATEST_RELEASE_API, headers={"Accept": "application/vnd.github+json",
                "User-Agent": f"JiXingModHelper/{self.current_version}", "X-GitHub-Api-Version": "2022-11-28"})
            with urlopen(request, timeout=8) as response:
                payload = response.read(512 * 1024 + 1)
            if len(payload) > 512 * 1024:
                raise ValueError("GitHub 返回的信息过大，请打开发布页检查。")
            result = describe_release(self.current_version, json.loads(payload))
        except HTTPError as exc:
            if exc.code == 404:
                result = {"status": "no_release", "message": "仓库暂时没有可用的正式版本信息，可打开发布页查看。"}
            elif exc.code in (403, 429):
                result = {"status": "error", "message": "GitHub 暂时限制了检查频率，请稍后重试或直接打开发布页。"}
            else:
                result = {"status": "error", "message": f"检查更新失败（HTTP {exc.code}），请稍后重试。"}
        except (URLError, TimeoutError, OSError):
            result = {"status": "error", "message": "暂时连不上 GitHub，请检查网络后重试；仍可继续使用助手。"}
        except (ValueError, TypeError):
            result = {"status": "error", "message": "无法识别 GitHub 的版本信息，请重试或打开发布页查看。"}
        finally:
            # 即使检查失败也保留清晰状态，避免界面一直显示“检查中”。
            with self._lock:
                self._last_check = time.monotonic()
                self._state.update(latest_version=None, release_url=RELEASES_URL)
                self._state.update(result)
                self._state["checking"] = False
