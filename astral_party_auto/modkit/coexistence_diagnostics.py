"""只检查当前游戏旁的已知 Mod 路径，不加载 DLL 或追踪清单里的外部文件。"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

MAX_CONFIG_BYTES = 64 * 1024
MAX_RESOURCE_MODS = 128


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _inspect(path: Path, root: Path, *, directory: bool = False) -> tuple[str, os.stat_result | None]:
    """逐级 lstat，遇到链接、重解析点或不可读目录就停止。"""
    try:
        path.relative_to(root)
        # 安装目录本身也可能经过 junction；不解析它指向的另一处安装。
        for ancestor in (*reversed(root.parents), root):
            info = ancestor.lstat()
            if _is_link(info):
                return "linked", None
            if not stat.S_ISDIR(info.st_mode):
                return "invalid", None
        current = root
        relative = path.relative_to(root).parts
        info = root.lstat()
        for index, part in enumerate(relative):
            current /= part
            info = current.lstat()
            if _is_link(info):
                return "linked", None
            if index < len(relative) - 1 and not stat.S_ISDIR(info.st_mode):
                return "invalid", None
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        return ("present", info) if expected(info.st_mode) else ("invalid", None)
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError):
        return "unreadable", None


def _without_comments(source: str) -> str:
    """读取常见 JSONC 配置；字符串中的网址、引号和斜线保持原样。"""
    result = []
    index = 0
    quoted = escaped = False
    while index < len(source):
        character = source[index]
        if quoted:
            result.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            index += 1
        elif character == '"':
            quoted = True
            result.append(character)
            index += 1
        elif source.startswith("//", index):
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end
            result.append(" ")
        elif source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise ValueError("配置注释未结束")
            result.append(" ")
            index = end + 2
        else:
            result.append(character)
            index += 1
    return "".join(result)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        key = name.casefold()
        if key in result:
            raise ValueError("配置包含重复字段")
        result[key] = value
    return result


def _read_config(path: Path, root: Path) -> tuple[str, dict | None]:
    status, expected = _inspect(path, root)
    if status != "present":
        return status, None
    if not expected or expected.st_size > MAX_CONFIG_BYTES:
        return "oversized", None
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        actual = os.fstat(descriptor)
        if (not stat.S_ISREG(actual.st_mode) or _is_link(actual)
                or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)):
            return "changed", None
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            raw = source.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return "oversized", None
        document = json.loads(_without_comments(raw.decode("utf-8-sig")), object_pairs_hook=_unique_object)
        if not isinstance(document, dict):
            return "invalid", None
        return "readable", document
    except (UnicodeError, ValueError, RecursionError):
        return "invalid", None
    except OSError:
        return "unreadable", None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _boolean(document: dict | None, key: str) -> bool | None:
    value = document.get(key) if document else None
    return value if isinstance(value, bool) else None


def coexistence_diagnostics(executable: str | Path | None) -> dict:
    result = {"status": "no_game", "game_directory": "", "entries": [],
              "checked_paths": [], "possible_overrides": False, "runtime_confirmed": False}
    if not executable:
        return result
    root = Path(os.path.abspath(executable)).parent
    result["game_directory"] = str(root)
    result["status"] = "not_found"
    entries = result["entries"]

    def inspect(relative: str, *, directory: bool = False) -> str:
        path = root / relative
        result["checked_paths"].append(str(path))
        return _inspect(path, root, directory=directory)[0]

    def config(relative: str) -> tuple[str, dict | None]:
        path = root / relative
        result["checked_paths"].append(str(path))
        return _read_config(path, root)

    def add(kind: str, label: str, relative: str, status: str, detail: str, **facts) -> None:
        if status != "missing":
            entries.append({"kind": kind, "label": label, "path": str(root / relative),
                            "status": status, "detail": detail, **facts})

    proxy_status = inspect("version.dll")
    add("proxy_dll", "version.dll", "version.dll", proxy_status,
        "发现代理 DLL，来源与用途未核实；仅凭文件名不能确认是 CesiumLoader。")
    loader_root = "AstralParty_ModLoader"
    loader_status = inspect(loader_root, directory=True)
    add("runtime_loader", "运行时模组目录", loader_root, loader_status,
        "发现 AstralParty_ModLoader 安装痕迹；目录存在不代表加载器正在运行。")
    loader_config_status, loader_config = config("doorstop_config.json")
    loader_enabled = _boolean(loader_config, "enabled")
    add("loader_config", "加载器配置", "doorstop_config.json", loader_config_status,
        "只读取 enabled 字段，不据此确认 DLL 来源或游戏内加载状态。", enabled=loader_enabled)

    for mod, label in (("CharacterSkinMod", "角色换肤配置"), ("CardSkinMod", "卡牌换肤配置")):
        relative = f"{loader_root}/mods/{mod}/config.json"
        status, document = config(relative)
        enabled = _boolean(document, "enabled")
        selection = None
        if document is not None:
            if mod == "CharacterSkinMod":
                scheme = document.get("activeschemeid")
                packages = document.get("activepackages")
                if isinstance(scheme, str) and isinstance(packages, list):
                    selection = bool(scheme.strip() or packages)
            else:
                skin = document.get("activeskin")
                if isinstance(skin, str):
                    selection = bool(skin.strip()) and skin != "__game_default__"
        possible = enabled is True and selection is not False and loader_enabled is not False
        result["possible_overrides"] |= possible
        add("skin_config", label, relative, status,
            "若相应加载器和 Mod 已在游戏中加载，运行时换肤可能覆盖资源包修改；未读取方案素材或执行模组。",
            enabled=enabled, has_selection=selection, possible_override=possible)

    bepinex_status = inspect("BepInEx", directory=True)
    add("bepinex", "BepInEx 目录", "BepInEx", bepinex_status,
        "发现 BepInEx 安装痕迹；未扫描插件，无法确认是否包含外观覆盖。")
    resource_root = "AstralParty_ResourceMods"
    resource_status = inspect(resource_root, directory=True)
    add("resource_mods", "资源 Mod 目录", resource_root, resource_status,
        "发现另一套资源 Mod 的存储目录；未读取或修改其中的资源包。")
    manifest = f"{resource_root}/installed.json"
    status, document = config(manifest)
    facts = {"enabled_count": None, "recorded_count": None, "applied_redirects": None}
    detail = "仅检查安装记录；启用标记或历史应用记录不代表当前 catalog 仍在使用这些修改。"
    if document is not None:
        mods = document.get("mods")
        if not isinstance(mods, list) or any(not isinstance(mod, dict) for mod in mods[:MAX_RESOURCE_MODS]):
            status = "invalid"
        else:
            inspected = mods[:MAX_RESOURCE_MODS]
            facts["recorded_count"] = len(mods)
            facts["enabled_count"] = sum(_boolean(mod, "enabled") is True for mod in inspected)
            facts["count_limited"] = len(mods) > MAX_RESOURCE_MODS
            applied = document.get("applied")
            redirects = applied.get("redirects") if isinstance(applied, dict) else None
            if isinstance(redirects, int) and not isinstance(redirects, bool) and redirects >= 0:
                facts["applied_redirects"] = redirects
            result["possible_overrides"] |= bool(facts["enabled_count"] or facts["applied_redirects"])
    add("resource_manifest", "资源 Mod 安装记录", manifest, status, detail, **facts)
    if entries:
        result["status"] = "found" if any(entry["status"] in ("present", "readable") for entry in entries) else "incomplete"
    return result


def coexistence_report_lines(result: dict) -> list[str]:
    lines = ["", "外部加载器与资源 Mod（只读共存检查）："]
    if result["status"] == "no_game":
        return lines + ["  未连接游戏，未检查其他安装或用户目录。"]
    lines.append("  只检查当前游戏 exe 同目录：" + result["game_directory"])
    labels = {"present": "发现安装痕迹", "readable": "配置已读取", "linked": "链接或重解析点，未跟随",
              "invalid": "类型或配置格式不明确", "unreadable": "无法读取", "oversized": "配置超过读取上限，已跳过",
              "changed": "检查期间文件已变化，未读取"}
    if not result["entries"]:
        lines.append("  未发现所列固定路径的安装痕迹；未扫描其他位置的第三方 Mod。")
    for entry in result["entries"]:
        status = entry["status"]
        lines.append(f"  {entry['label']}：{entry['path']}（{labels.get(status, status)}）")
        if status not in ("present", "readable"):
            continue
        if "enabled" in entry:
            enabled = entry["enabled"]
            state = "显式启用" if enabled is True else "显式停用" if enabled is False else "未明确或无法识别"
            lines.append("    配置 enabled：" + state + "（只表示文件中的设置）")
        if entry.get("has_selection") is False:
            lines.append("    配置未选择外观方案，或选择了游戏默认外观。")
        if entry.get("recorded_count") is not None:
            qualifier = f"（仅检查前 {MAX_RESOURCE_MODS} 条）" if entry.get("count_limited") else ""
            lines.append(f"    记录 {entry['recorded_count']} 个包，显式启用 {entry['enabled_count']} 个{qualifier}。")
        if entry.get("applied_redirects") is not None:
            lines.append(f"    最近应用记录：{entry['applied_redirects']} 条重定向；未比对当前 catalog。")
        lines.append("    " + entry["detail"])
    if result["possible_overrides"]:
        lines.append("  存在配置启用或历史应用记录的外观修改；若助手安装后没有效果，请先核对另一层覆盖来源。")
    lines.append("  本检查不能确认游戏内最终生效来源，不会安装、停用或修改其他工具。")
    lines.append("  实际检查路径：")
    lines.extend("    " + path for path in result["checked_paths"])
    return lines
