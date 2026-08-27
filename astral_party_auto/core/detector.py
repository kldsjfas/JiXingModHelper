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
