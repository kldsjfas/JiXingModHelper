# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir, windowed (no console)
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

ROOT = Path(SPECPATH)

def gather(pkg: str):
    try:
        return collect_all(pkg)
    except Exception:
        return [], [], []

datas = [
    (str(ROOT / "astral_party_auto" / "webui"), "astral_party_auto/webui"),
    (str(ROOT / "assets"), "assets"),
    (str(ROOT / "icon.ico"), "."),
    (str(ROOT / "icon.png"), "."),
    (str(ROOT / "角色表.csv"), "."),
    (str(ROOT / "native_host" / "publish"), "native_host"),
]
binaries = []
hiddenimports = [
    "astral_party_auto",
    "astral_party_auto.app",
    "astral_party_auto.native_host_app",
    "astral_party_auto.web_app",
    "astral_party_auto.mod_controller",
    "bottle",
]

for pkg in (
    "UnityPy",
    "texture2ddecoder",
    "etcpak",
    "astc_encoder",
    "archspec",  # astc_encoder 读图时需要 json/cpu 数据，漏了会预览报 No such file
    "fmod_toolkit",
    "PIL",
):
    d, b, h = gather(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# 保险：整包拷贝（含 tpk / archspec json）
import UnityPy as _upy
import archspec as _arch

datas.append((str(Path(_upy.__file__).resolve().parent), "UnityPy"))
datas.append((str(Path(_arch.__file__).resolve().parent), "archspec"))

try:
    import astc_encoder as _astc

    datas.append((str(Path(_astc.__file__).resolve().parent), "astc_encoder"))
except Exception:
    pass

hiddenimports += collect_submodules("UnityPy")
hiddenimports += collect_submodules("archspec")
hiddenimports = sorted(set(hiddenimports))

a = Analysis(
    [str(ROOT / "launch.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pandas", "matplotlib", "scipy", "IPython", "notebook",
        "webview", "pythonnet", "clr", "clr_loader", "proxy_tools",
        "customtkinter", "windnd", "darkdetect",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JiXingModHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "app_icon.ico"),
    uac_admin=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="JiXingModHelper",
)
