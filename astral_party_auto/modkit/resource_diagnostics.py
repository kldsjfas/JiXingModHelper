"""只读收集当前安装的资源位置，方便把完整原因交给维护者。"""
from __future__ import annotations

import os
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path

from .unity_paths import local_low_dir, read_unity_identity, unity_data_dir_for_exe

MAX_DIRECTORY_ENTRIES = 128
MAX_CATALOG_FILES = 24
MAX_REPORT_CHARS = 24_000


def _directory_status(path: Path) -> str:
    try:
        return "存在" if path.is_dir() else "不存在或不是目录"
    except OSError as exc:
        return f"无法检查：{exc}"


def _catalog_files(directory: Path) -> list[str]:
    """只枚举元数据目录的第一层，不遍历资源缓存或读取 catalog 内容。"""
    files = []
    try:
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_DIRECTORY_ENTRIES:
                    files.append(f"目录条目超过 {MAX_DIRECTORY_ENTRIES} 项，已停止枚举。")
                    break
                name = entry.name
                if not (name in ("catalog.json", "catalog.bundle", "settings.json")
                        or fnmatchcase(name, "catalog_*.json") or fnmatchcase(name, "catalog_*.hash")):
                    continue
                if len(files) >= MAX_CATALOG_FILES:
                    files.append(f"元数据文件超过 {MAX_CATALOG_FILES} 项，已停止枚举。")
                    break
                if entry.is_symlink():
                    files.append(f"{name}（链接，未跟随）")
                elif entry.is_file(follow_symlinks=False):
                    files.append(f"{name}（{entry.stat(follow_symlinks=False).st_size} 字节）")
    except FileNotFoundError:
        return ["目录不存在。"]
    except OSError as exc:
        files.append(f"无法列出元数据：{exc}")
    return sorted(files) or ["未发现 catalog 或 settings 文件。"]


def resource_diagnostics(controller, version: str) -> dict:
    checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = ["吉星派对 Mod 助手 · 资源诊断", f"助手版本：{version}", f"检查时间：{checked_at}",
             "这是当前本机的只读检查结果；存在资源目录不代表动态制作已通过校验。", ""]
    executable = getattr(controller, "game_executable", None)
    lines.append(f"已连接的游戏 exe：{executable or '未连接'}")
    try:
        local_low = local_low_dir()
        lines.append(f"Windows LocalLow：{local_low}（{_directory_status(local_low)}）")
    except (OSError, ValueError) as exc:
        local_low = None
        lines.append(f"Windows LocalLow：无法定位，{exc}")

    metadata_roots = []
    if executable:
        data_root = unity_data_dir_for_exe(executable)
        lines.append(f"Unity 安装信息：{data_root / 'app.info'}")
        company = product = None
        try:
            company, product = read_unity_identity(executable)
            lines.extend((f"Unity 公司：{company}", f"Unity 产品：{product}"))
        except (OSError, ValueError) as exc:
            lines.append(f"Unity 身份读取失败：{exc}")
        metadata_roots.append(("客户端元数据", data_root / "com.unity.addressables"))
        if local_low is not None and company and product:
            metadata_roots.append(("用户元数据", local_low / company / product / "com.unity.addressables"))
        metadata_roots.append(("内置元数据", data_root / "StreamingAssets" / "aa"))
    else:
        lines.append("未连接实际游戏 exe，未猜测公司名、产品名或其他安装的缓存位置。")

    for label, directory in metadata_roots:
        lines.extend(("", f"{label}：{directory}（{_directory_status(directory)}）"))
        lines.extend(f"  {entry}" for entry in _catalog_files(directory))
        if label != "内置元数据":
            bundles = directory / "AssetBundles"
            lines.append(f"  资源目录：{bundles}（{_directory_status(bundles)}）")

    lines.extend(("", "当前资源搜索目录（按优先顺序）："))
    directories = tuple(getattr(controller, "aa_dirs", ()) or ())
    for directory in directories[:8]:
        path = Path(directory)
        lines.append(f"  {path}（{_directory_status(path)}）")
    if not directories:
        lines.append("  尚无资源搜索目录。")
    if len(directories) > 8:
        lines.append("  其余目录已省略。")

    selection = getattr(controller, "selection", None)
    if isinstance(selection, dict):
        lines.extend(("", "当前选择（最近一次读取）：",
                      f"  资源：{str(selection.get('name', ''))[:512]}",
                      f"  资源包：{str(selection.get('bundle', ''))[:512]}"))
        for key, label in (("emote", "动态表情"), ("visual_animation", "动态卡面 / 立绘")):
            details = selection.get(key)
            if isinstance(details, dict):
                status = "可制作" if details.get("editable") is True else "暂不可制作"
                lines.append(f"  {label}：{status}")
                lines.append(str(details.get("reason") or "未记录原因。")[:8192])
                if key == "emote" and details.get("independent_supported") is False:
                    lines.append("  独立自定义导入：暂不可用")
                    lines.append(str(details.get("independent_reason") or "未记录独立导入不可用的原因。")[:8192])
    lines.extend(("", "请保留现有缓存；若需反馈，复制本报告并附上操作时的完整报错。",
                  "报告只在本地显示，包含本机路径，请检查内容后再发送。"))
    report = "\n".join(lines)
    if len(report) > MAX_REPORT_CHARS:
        report = report[:MAX_REPORT_CHARS] + "\n报告过长，后续内容已省略。"
    return {"text": report, "checked_at": checked_at}
