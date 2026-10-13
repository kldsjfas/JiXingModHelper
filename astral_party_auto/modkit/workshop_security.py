"""工坊安装前的本机扫描；无法确认扫描完成时停止安装。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _defender_executable() -> Path:
    # 只寻找 Windows 自带扫描器，不能从 Mod 或 PATH 加载同名程序。
    if os.name != "nt":
        raise RuntimeError("工坊自动安装需要 Windows Defender 完成扫描。")
    platform = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows Defender/Platform"
    candidates = sorted(platform.glob("*/MpCmdRun.exe"), reverse=True) if platform.is_dir() else []
    candidates.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender/MpCmdRun.exe")
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise RuntimeError("未找到 Windows Defender 扫描器，工坊已停止自动安装。请先检查系统防病毒保护。")


def scan_workshop_package(directory: Path) -> dict:
    directory = Path(directory).resolve(strict=True)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError("工坊待扫描目录无效。")
    scanner = _defender_executable()
    try:
        result = subprocess.run(
            [str(scanner), "-Scan", "-ScanType", "3", "-File", str(directory), "-DisableRemediation"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("防病毒扫描超时，未安装。请稍后重试。") from exc
    except OSError as exc:
        raise RuntimeError("无法启动防病毒扫描，未安装。请检查 Windows Defender。") from exc
    if result.returncode != 0:
        raise RuntimeError("Windows Defender 发现威胁或未能完成扫描，工坊已停止安装。请检查防病毒保护状态后重试；本次只扫描，不处置文件。")
    return {"scanner": "Windows Defender", "status": "passed", "message": "扫描完成；这不代表资源包绝对安全。"}
