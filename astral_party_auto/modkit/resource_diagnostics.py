"""只读收集当前安装的资源位置，方便把完整原因交给维护者。"""
from __future__ import annotations

import os
import sys
from collections import Counter
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path

from .unity_paths import local_low_dir, read_unity_identity, unity_data_dir_for_exe

MAX_DIRECTORY_ENTRIES = 128
MAX_CATALOG_FILES = 24
MAX_REPORT_CHARS = 24_000
MAX_MISSING_BUNDLES = 12


def _assistant_entry() -> str:
    if getattr(sys, "frozen", False):
        return f"助手程序：{Path(sys.executable).resolve()}"
    entry = sys.argv[0] if sys.argv else ""
    script = str(Path(entry).resolve()) if entry not in ("", "-", "-c") else "交互或测试入口"
    return f"助手程序：源码运行（Python：{Path(sys.executable).resolve()}；入口：{script}）"


def _fresh_bundle_files(directories, required: set[str]) -> dict[str, Path]:
    # 已经限量读取当前 catalog；这里只核对它明确引用的文件，避免再读其它目录的 catalog。
    from .bundles import _entries_from_dir

    files = {}
    for directory in directories[:8]:
        for name, path in _entries_from_dir(Path(directory), required).items():
            if name not in files and path.is_file():
                files[name] = path
    return files


def _catalog_check(controller, lines: list[str]) -> dict:
    """从同一份当前目录核对配置和依赖，不打开图集、视频或实际配置包。"""
    from .catalog_mods import _official_hash
    from .emote_catalog import read_emote_locations
    from .visual_catalog import find_catalog_source, visual_bundle_locations

    result = {"catalog": "not_checked", "emote": "not_checked", "visual": "not_checked",
              "independent": "unavailable", "missing_bundles": []}
    lines.extend(("", "当前目录与依赖核对（本次重新读取）："))
    executable = getattr(controller, "game_executable", None)
    if not executable:
        result["catalog"] = "no_game"
        lines.append("  未连接游戏，未读取其它安装的 catalog。")
        return result
    directories = tuple(getattr(controller, "aa_dirs", ()) or ())
    try:
        source = find_catalog_source(directories, executable, purpose="资源诊断")
    except (OSError, ValueError, RecursionError) as exc:
        result["catalog"] = "invalid_catalog" if isinstance(exc, RecursionError) else "unavailable"
        detail = "catalog 或 settings 的 JSON 嵌套过深，未继续读取。" if isinstance(exc, RecursionError) else str(exc)
        lines.append("  catalog 定位失败：" + detail)
        return result
    result.update(catalog_path=str(source.path), catalog_kind=source.kind)
    lines.append(f"  实际 catalog：{source.path}（{source.kind}）")
    if source.settings_problem:
        result["catalog"] = "unverified_source"
        lines.append("  来源未通过校验：" + source.settings_problem)
        return result
    if source.kind == "base_bundle":
        result["catalog"] = "bundled_source"
        lines.append("  已发现内置 catalog.bundle；诊断不解包二进制目录，未据此判断资源完整。")
        lines.append("  独立自定义导入：需要可核对官方版本的热更新 JSON catalog。")
        return result
    try:
        document = source.read_document(purpose="资源诊断")
    except (OSError, ValueError, KeyError, RecursionError) as exc:
        result["catalog"] = "invalid_catalog"
        detail = "JSON 嵌套过深，未继续读取。" if isinstance(exc, RecursionError) else str(exc)
        lines.append("  catalog 无法读取：" + detail)
        return result
    result["catalog"] = "readable"
    emote = visual = None
    try:
        emote = read_emote_locations(document)
    except (OSError, ValueError, KeyError) as exc:
        result["emote"] = "unknown_type" if "类型" in str(exc) else "invalid_catalog"
        lines.append("  表情地址解析失败：" + str(exc))
    try:
        visual = visual_bundle_locations(document)
    except (OSError, ValueError, KeyError) as exc:
        result["visual"] = "invalid_catalog"
        lines.append("  卡面地址解析失败：" + str(exc))
    required = set(visual.values()) if visual else set()
    if emote:
        required.update(emote["config_bundles"])
        for dependencies in emote["videos"].values():
            required.update(dependencies)
    try:
        files = _fresh_bundle_files(directories, required)
    except OSError as exc:
        result.update(emote="inventory_unavailable", visual="inventory_unavailable")
        lines.append("  无法检查资源文件：" + str(exc))
        return result
    missing = sorted(required - files.keys())
    result["missing_bundles"] = missing[:MAX_MISSING_BUNDLES]
    result["missing_bundle_count"] = len(missing)
    if emote:
        configs = emote["config_bundles"]
        complete = sum(bool(dependencies) and dependencies <= files.keys()
                       for dependencies in emote["videos"].values())
        total = len(emote["videos"])
        result.update(emote_total=total, emote_downloaded=complete)
        if len(configs) != 1:
            result["emote"] = "ambiguous_configuration"
            lines.append(f"  表情配置：需要唯一配置包，当前目录解析到 {len(configs)} 个。")
        else:
            config = next(iter(configs))
            found = config in files
            result["emote_config_found"] = found
            lines.append(f"  表情配置包：{config}（{'本机已找到' if found else '本机缺失'}）")
            result["emote"] = ("missing_configuration" if not found else "no_animation_addresses" if not total
                               else "missing_dependencies" if not complete else "available")
        lines.append(f"  动态表情：已完整下载 {complete}/{total} 项（按 catalog 依赖核对）")
        if emote["video_types"]:
            types = Counter(emote["video_types"].values())
            lines.append("  资源类型：" + "、".join(f"{name} {count} 项" for name, count in sorted(types.items())))
        if not total:
            lines.append("  当前 catalog 没有可识别的动态表情地址。")
    if visual is not None:
        config_names = {visual[key] for key in ("GameData_CN", "GameData_INT") if key in visual}
        visual_required = config_names | {visual[key] for key in ("Common_fui", "Common_atlas0") if key in visual}
        if len(config_names) != 1 or any(key not in visual for key in ("Common_fui", "Common_atlas0")):
            result["visual"] = "incomplete_addresses"
            lines.append("  卡面目录：缺少唯一配置、Common 界面或图集地址。")
        elif not visual_required <= files.keys():
            result["visual"] = "missing_dependencies"
            lines.append("  卡面目录：当前 catalog 所需的配置、界面或图集包尚未在本机找齐。")
        else:
            result["visual"] = "available"
            lines.append("  卡面目录：配置、Common 界面与图集包均已找到。")
    if missing:
        lines.append(f"  缺失资源包：共 {len(missing)} 个，最多列出 {MAX_MISSING_BUNDLES} 个：")
        lines.extend("    " + name for name in missing[:MAX_MISSING_BUNDLES])
    if source.kind != "hot_json":
        result["independent"] = "no_hot_catalog"
        lines.append("  独立自定义导入：当前不是热更新 JSON catalog，不能把内置目录当作安装目标。")
    else:
        try:
            _official_hash(source.path)
            result["independent"] = "version_verified"
            lines.append("  官方版本 hash：可读取；独立导入的版本检查条件已具备。")
        except (OSError, ValueError) as exc:
            result["independent"] = "missing_hash"
            lines.append("  官方版本 hash：无法核对，独立自定义导入不可用。" + str(exc))
    lines.append("  以上只核对目录与依赖；具体材质、视频、配置内容和游戏内效果仍由制作流程继续验证。")
    return result


def _selection_report(controller, lines: list[str]) -> None:
    selection = getattr(controller, "selection", None)
    if not isinstance(selection, dict):
        return
    lines.extend(("", "当前选择（最近一次读取）：",
                  f"  资源：{str(selection.get('name', ''))[:512]}",
                  f"  资源包：{str(selection.get('bundle', ''))[:512]}"))
    for key, label in (("emote", "动态表情"), ("visual_animation", "动态卡面 / 立绘")):
        details = selection.get(key)
        if not isinstance(details, dict):
            continue
        status = "可制作" if details.get("editable") is True else "暂不可制作"
        lines.append(f"  {label}：{status}")
        lines.append(str(details.get("reason") or "未记录原因。")[:4096])
        if key == "emote" and details.get("independent_supported") is False:
            lines.append("  独立自定义导入：暂不可用")
            lines.append(str(details.get("independent_reason") or "未记录独立导入不可用的原因。")[:4096])


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
    lines = ["吉星派对 Mod 助手 · 资源诊断", f"助手版本：{version}", _assistant_entry(), f"检查时间：{checked_at}",
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

    checks = _catalog_check(controller, lines)
    _selection_report(controller, lines)

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

    lines.extend(("", "请保留现有缓存；若需反馈，复制本报告并附上操作时的完整报错。",
                  "报告只在本地显示，包含本机路径，请检查内容后再发送。"))
    report = "\n".join(lines)
    if len(report) > MAX_REPORT_CHARS:
        report = report[:MAX_REPORT_CHARS] + "\n报告过长，后续内容已省略。"
    return {"text": report, "checked_at": checked_at, "checks": checks}
