from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ASTRAL_PARTY_APP_ID = "2622000"
TAP_TAP_APP_ID = "taptap"

# TapTap 版 / Steam 版都可能出现的可执行文件名（大小写不敏感）
GAME_EXE_NAMES = (
    "AstralParty_CN.exe",
    "AstralParty_INT.exe",
    "AstralParty.exe",
    "吉星派对.exe",
    "星引擎派对.exe",
)


@dataclass(frozen=True)
class GameInstall:
    app_id: str
    name: str
    install_dir: Path
    cn_exe: Path | None
    int_exe: Path | None
    launcher: str = "steam"

    def executable_for_region(self, region: str) -> Path | None:
        normalized = region.strip().upper()
        if normalized == "INT":
            return self.int_exe or self.cn_exe
        return self.cn_exe or self.int_exe


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _acf_value(text: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s+"([^"]*)"', text)
    if not match:
        return None
    return match.group(1)


def _steam_paths_from_registry() -> list[Path]:
    paths: list[Path] = []
    try:
        import winreg
    except ImportError:
        return paths

    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
    ]

    for hive, key_name in keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for value_name in ("SteamPath", "InstallPath"):
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    if value:
                        paths.append(Path(str(value)))
        except OSError:
            continue

    return paths


def _candidate_steam_roots() -> list[Path]:
    roots: list[Path] = []
    roots.extend(_steam_paths_from_registry())
    roots.extend(
        [
            Path(r"D:\Steam"),
            Path(r"C:\Program Files (x86)\Steam"),
            Path(r"C:\Program Files\Steam"),
        ]
    )

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _parse_library_paths(libraryfolders_path: Path) -> list[Path]:
    try:
        text = _read_text(libraryfolders_path)
    except OSError:
        return []

    paths = []
    for raw_path in re.findall(r'"path"\s+"([^"]+)"', text):
        paths.append(Path(raw_path.replace(r"\\", "\\")))
    return paths


def _taptap_paths_from_registry() -> list[Path]:
    paths: list[Path] = []
    try:
        import winreg
    except ImportError:
        return paths

    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\TapTap"),
        (winreg.HKEY_CURRENT_USER, r"Software\TapTap\PC"),
        (winreg.HKEY_CURRENT_USER, r"Software\TAP\TapTap"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\TapTap"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\TapTap"),
    ]
    value_names = ("InstallPath", "InstallDir", "GamePath", "RootPath", "Path", "Location")

    for hive, key_name in keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for value_name in value_names:
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    if value:
                        paths.append(Path(str(value)))
        except OSError:
            continue

    return paths


def _candidate_taptap_roots() -> list[Path]:
    roots: list[Path] = []
    roots.extend(_taptap_paths_from_registry())

    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "APPDATA"):
        value = os.environ.get(env_name)
        if value:
            root = Path(value) / "TapTap"
            roots.append(root)
            if env_name == "LOCALAPPDATA":
                roots.append(root / "PC Games")

    roots.extend(
        [
            Path(r"C:\TapTap"),
            Path(r"D:\TapTap"),
            Path(r"E:\TapTap"),
            Path(r"F:\TapTap"),
            Path(r"D:\TapTapGames"),
            Path(r"D:\TapTap Games"),
            Path(r"C:\Program Files\TapTap"),
            Path(r"C:\Program Files (x86)\TapTap"),
            Path(r"D:\Program Files\TapTap"),
        ]
    )

    # 如果 TapTap 装在某个盘的根目录（例如 C:\TapTap），直接补进候选。
    for letter in "CDEFGH":
        drive = Path(f"{letter}:\\")
        if not drive.exists():
            continue
        try:
            entries = list(drive.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            name_l = entry.name.lower()
            if name_l in ("taptap", "taptapgames", "taptap games", "taptap游戏"):
                roots.append(entry)

    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _is_game_executable(path: Path) -> bool:
    lower_name = path.name.lower()
    if lower_name in {name.lower() for name in GAME_EXE_NAMES}:
        return True
    stem = path.stem.lower()
    compact_stem = stem.replace("_", "").replace("-", "").replace(" ", "")
    return path.suffix.lower() == ".exe" and (
        "astralparty" in compact_stem or "吉星" in stem or "星引擎" in stem
    )


def manual_game_install(path: str | Path) -> GameInstall:
    """校验用户选中的安装，不把启动器或另一款 Unity 游戏当作目标。"""
    from ..modkit.unity_paths import read_unity_identity, unity_data_dir_for_exe

    selected = Path(path).resolve()
    if selected.is_dir():
        if selected == Path(selected.anchor) or selected.name.lower().endswith("_data"):
            raise ValueError("请选择吉星派对的安装文件夹，或直接选择实际的游戏 exe。")
        queue, candidates, visited = [(selected, 0)], [], 0
        ignored = {"streamingassets", "resources", "plugins", "il2cpp_data", "assetbundles",
                   "modkit_data", "made_mods", "monobleedingedge"}
        while queue:
            directory, depth = queue.pop(0)
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > 256:
                            raise ValueError("所选目录内容太多，请进入游戏程序所在文件夹，或直接选择游戏 exe。")
                        if (entry.is_symlink()
                                or getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400):
                            continue
                        if entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() == ".exe":
                            try:
                                candidate = manual_game_install(Path(entry.path))
                            except ValueError:
                                continue
                            candidates.append(candidate)
                        elif (depth < 2 and entry.is_dir(follow_symlinks=False)
                              and not entry.name.lower().endswith("_data") and entry.name.lower() not in ignored):
                            queue.append((Path(entry.path), depth + 1))
            except OSError as exc:
                raise ValueError(f"无法读取所选游戏目录：{directory}") from exc
        unique = {}
        for candidate in candidates:
            executable = candidate.cn_exe or candidate.int_exe
            unique[os.path.normcase(str(executable))] = candidate
        candidates = list(unique.values())
        if not candidates:
            raise ValueError("这个文件夹里没有找到带有有效 app.info 的吉星派对客户端，请选择实际游戏 exe。")
        if len(candidates) > 1:
            raise ValueError("找到多个客户端，可能同时安装了国服、国际服或多个版本。请用「选择游戏程序」明确选择要修改的 exe。")
        return candidates[0]
    if not selected.is_file() or selected.suffix.lower() != ".exe":
        raise ValueError("请选择吉星派对的游戏程序，不能选择启动器、资源包或其它程序。")
    try:
        company, product = read_unity_identity(selected)
    except (OSError, ValueError) as exc:
        raise ValueError(f"所选程序没有有效的 Unity 安装信息：{exc}") from exc
    products = {"astralparty", "astralparty_cn", "astralparty_int", "吉星派对", "星引擎派对"}
    if company.casefold() != "feimo" or product.casefold() not in products:
        raise ValueError("所选程序的 app.info 不是已识别的吉星派对客户端，请核对游戏位置。")
    data = unity_data_dir_for_exe(selected)
    if not (data / "StreamingAssets" / "aa").is_dir() and not (data / "com.unity.addressables").is_dir():
        raise ValueError("所选程序旁没有吉星派对资源目录，请选择完整安装的游戏客户端。")
    international = product.casefold() == "astralparty_int"
    return GameInstall(app_id="manual", name="吉星派对（国际服）" if international else "吉星派对（国服）",
                       install_dir=selected.parent, cn_exe=None if international else selected,
                       int_exe=selected if international else None, launcher="manual")


def _iter_taptap_game_dirs() -> Iterable[Path]:
    """在 TapTap 安装目录里找出包含游戏 exe 的目录（支持多层版本目录）。"""
    seen: set[str] = set()
    for root in _candidate_taptap_roots():
        if not root.is_dir():
            continue
        try:
            walk_iter = os.walk(root)
            for dirpath, dirnames, filenames in walk_iter:
                current = Path(dirpath)
                try:
                    depth = len(current.relative_to(root).parts)
                except ValueError:
                    depth = 0
                if depth >= 8:
                    dirnames[:] = []
                    continue
                # 避免扫进 Unity 的 *_Data / StreamingAssets 等海量资源目录，
                # TapTap 的启动器目录结构很浅，只需要在版本目录里找 exe。
                dirnames[:] = [
                    d
                    for d in dirnames
                    if not d.endswith("_Data")
                    and d.lower()
                    not in {
                        "support",
                        "streamingassets",
                        "resources",
                        "plugins",
                        "il2cpp_data",
                        "monobleedingedge",
                        "temp",
                        "logs",
                    }
                ]
                for filename in filenames:
                    candidate = current / filename
                    if _is_game_executable(candidate):
                        key = os.path.normcase(str(current))
                        if key not in seen:
                            seen.add(key)
                            yield current
                        break
        except OSError:
            continue


def find_taptap_game_install(app_id: str = TAP_TAP_APP_ID) -> GameInstall | None:
    for install_dir in _iter_taptap_game_dirs():
        game_name = install_dir.name
        for candidate in install_dir.glob("*.exe"):
            if _is_game_executable(candidate):
                game_name = candidate.stem
                break
        install = _build_install(app_id, game_name, install_dir, launcher="taptap")
        if install.cn_exe or install.int_exe:
            return install
    return None


def iter_steamapps_dirs() -> Iterable[Path]:
    steamapps: list[Path] = []
    for steam_root in _candidate_steam_roots():
        primary = steam_root / "steamapps"
        if primary.exists():
            steamapps.append(primary)
            for library_root in _parse_library_paths(primary / "libraryfolders.vdf"):
                library_steamapps = library_root / "steamapps"
                if library_steamapps.exists():
                    steamapps.append(library_steamapps)

    seen: set[str] = set()
    for path in steamapps:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        yield path


def find_game_install(app_id: str = ASTRAL_PARTY_APP_ID) -> GameInstall | None:
    for steamapps_dir in iter_steamapps_dirs():
        manifest = steamapps_dir / f"appmanifest_{app_id}.acf"
        if not manifest.exists():
            continue

        text = _read_text(manifest)
        install_dir_name = _acf_value(text, "installdir") or "Astral Party"
        game_name = (_acf_value(text, "name") or "Astral Party").strip()
        install_dir = steamapps_dir / "common" / install_dir_name
        install = _build_install(app_id, game_name, install_dir)
        if install.cn_exe or install.int_exe:
            return install

    # 常见盘符硬探 + 在各 steamapps/common 下找目录名
    for steamapps_dir in iter_steamapps_dirs():
        common = steamapps_dir / "common"
        if not common.exists():
            continue
        for child in common.iterdir():
            if not child.is_dir():
                continue
            name_l = child.name.lower()
            if "astral" not in name_l and "party" not in name_l:
                continue
            install = _build_install(app_id, child.name, child)
            if install.cn_exe or install.int_exe:
                return install

    for known_dir in (
        Path(r"D:\Steam\steamapps\common\Astral Party"),
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\Astral Party"),
        Path(r"C:\Steam\steamapps\common\Astral Party"),
        Path(r"E:\Steam\steamapps\common\Astral Party"),
        Path(r"F:\Steam\steamapps\common\Astral Party"),
    ):
        if known_dir.exists():
            install = _build_install(app_id, "Astral Party", known_dir)
            if install.cn_exe or install.int_exe:
                return install

    # 额外适配 TapTap 客户端安装的版本
    taptap_install = find_taptap_game_install()
    if taptap_install is not None:
        return taptap_install

    return None


def _find_executable(install_dir: Path, names: Iterable[str]) -> Path | None:
    """在游戏安装目录里定位常见 exe，兼容 Steam/TapTap 的目录差异。"""
    for name in names:
        direct = install_dir / name
        if direct.is_file():
            return direct
        for sub_dir in ("8vJXn6CN", "8vJXnINT"):
            candidate = install_dir / sub_dir / name
            if candidate.is_file():
                return candidate

    # 再兜底一层：有些版本把 exe 放在一层子目录里。
    try:
        for child in install_dir.iterdir():
            if not child.is_dir():
                continue
            for name in names:
                candidate = child / name
                if candidate.is_file():
                    return candidate
    except OSError:
        pass
    return None


def _build_install(
    app_id: str,
    game_name: str,
    install_dir: Path,
    *,
    launcher: str = "steam",
) -> GameInstall:
    cn_exe = _find_executable(
        install_dir,
        ("AstralParty_CN.exe", "AstralParty.exe", "吉星派对.exe", "星引擎派对.exe"),
    )
    int_exe = _find_executable(install_dir, ("AstralParty_INT.exe",))
    # TapTap 国服通常只有一个中文名 exe，把它视作 CN 入口
    if cn_exe is None and int_exe is not None:
        cn_exe = int_exe
        int_exe = None
    return GameInstall(
        app_id=app_id,
        name=game_name,
        install_dir=install_dir,
        cn_exe=cn_exe,
        int_exe=int_exe,
        launcher=launcher,
    )


def launch_with_steam(app_id: str = ASTRAL_PARTY_APP_ID) -> None:
    os.startfile(f"steam://rungameid/{app_id}")


def launch_direct(executable: Path) -> subprocess.Popen:
    return subprocess.Popen([str(executable)], cwd=str(executable.parent))
