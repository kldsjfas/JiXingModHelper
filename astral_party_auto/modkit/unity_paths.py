"""按 Unity 安装信息查找资源元数据，不猜测游戏的产品名称。"""
from __future__ import annotations

import ctypes
import os
import uuid
from pathlib import Path

MAX_APP_INFO_BYTES = 8192
_LOCAL_APP_DATA_LOW = "A520A1A4-1780-4FF6-BD18-167343C5AF16"
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{number}" for number in range(1, 10)),
                   *(f"LPT{number}" for number in range(1, 10))}


def unity_data_dir_for_exe(exe_path: str | Path) -> Path:
    executable = Path(exe_path)
    return executable.parent / f"{executable.stem}_Data"


def _identity_component(value: str, label: str, source: Path) -> str:
    if (not value or len(value) > 255 or value != value.strip() or value in (".", "..")
            or value.endswith(".") or any(character in '<>:"/\\|?*' or ord(character) < 32 for character in value)
            or value.split(".", 1)[0].upper() in _RESERVED_NAMES):
        raise ValueError(f"Unity {label}名称无效，无法确定用户缓存位置：{source}")
    return value


def read_unity_identity(exe_path: str | Path) -> tuple[str, str]:
    """app.info 的前两行是公司名和产品名；exe 可以被启动器改名。"""
    source = unity_data_dir_for_exe(exe_path) / "app.info"
    try:
        if not source.is_file() or not 0 < source.stat().st_size <= MAX_APP_INFO_BYTES:
            raise ValueError(f"缺少或无法识别 Unity 安装信息，无法确定用户缓存位置：{source}")
        lines = source.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法读取 Unity 安装信息，无法确定用户缓存位置：{source}") from exc
    if len(lines) < 2 or any(line.strip() for line in lines[2:]):
        raise ValueError(f"Unity 安装信息应包含公司名和产品名两行：{source}")
    return (_identity_component(lines[0], "公司", source),
            _identity_component(lines[1], "产品", source))


def _local_low_dir_from_windows() -> Path:
    if os.name != "nt":
        raise ValueError("当前系统无法通过 Windows 已知文件夹接口定位 LocalLow。")

    class Guid(ctypes.Structure):
        _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                    ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    folder_id = Guid.from_buffer_copy(uuid.UUID(_LOCAL_APP_DATA_LOW).bytes_le)
    result_path = ctypes.c_void_p()
    try:
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        allocator = ctypes.WinDLL("ole32", use_last_error=True)
        get_folder = shell.SHGetKnownFolderPath
        get_folder.argtypes = [ctypes.POINTER(Guid), ctypes.c_uint32, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_void_p)]
        get_folder.restype = ctypes.c_long
        allocator.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        allocator.CoTaskMemFree.restype = None
        result = get_folder(ctypes.byref(folder_id), 0, None, ctypes.byref(result_path))
        try:
            if result != 0 or not result_path.value:
                raise ValueError(f"Windows 无法定位 LocalLow 已知文件夹（HRESULT 0x{result & 0xffffffff:08X}）。")
            return Path(ctypes.wstring_at(result_path.value))
        finally:
            if result_path.value:
                allocator.CoTaskMemFree(result_path)
    except OSError as exc:
        raise ValueError("无法调用 Windows 已知文件夹接口定位 LocalLow。") from exc


def local_low_dir(*, user_profile: str | Path | None = None) -> Path:
    """生产环境使用系统接口；显式 profile 参数只用于隔离测试布局。"""
    directory = (Path(user_profile) / "AppData" / "LocalLow" if user_profile is not None
                 else _local_low_dir_from_windows())
    if not directory.is_absolute():
        raise ValueError("Windows LocalLow 位置不是绝对路径，已停止猜测用户缓存位置。")
    return directory


def unity_catalog_roots_for_exe(exe_path: str | Path, *,
                                user_profile: str | Path | None = None) -> tuple[Path, ...]:
    """保留元数据目录，即使对应的 AssetBundles 子目录尚未创建。"""
    company, product = read_unity_identity(exe_path)
    candidates = (unity_data_dir_for_exe(exe_path) / "com.unity.addressables",
                  local_low_dir(user_profile=user_profile) / company / product / "com.unity.addressables")
    roots, seen = [], set()
    for directory in candidates:
        key = os.path.normcase(str(directory.resolve()))
        if key not in seen:
            roots.append(directory)
            seen.add(key)
    return tuple(roots)
