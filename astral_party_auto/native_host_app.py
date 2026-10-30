"""用 C# WebView2 原生窗口承载 HTML 前端，Python 仅提供本地接口。"""
from __future__ import annotations

import os
import secrets
import subprocess
import tempfile
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from . import web_app
from . import __version__
from .core.config import APP_ROOT, ASSETS_DIR, RESOURCE_ROOT
from .mod_controller import DATA_DIR


def _host_executable() -> Path:
    candidates = (
        RESOURCE_ROOT / "native_host" / "JiXingModHelperHost.exe",
        APP_ROOT / "native_host" / "JiXingModHelperHost.exe",
        Path(__file__).resolve().parent.parent / "native_host" / "publish" / "JiXingModHelperHost.exe",
    )
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("找不到原生 WebView 窗口组件。请重新完整解压发布包。")


def _check_data_directory() -> None:
    for directory in (APP_ROOT, DATA_DIR, APP_ROOT / "made_mods"):
        if directory != APP_ROOT and not directory.exists():
            continue
        try:
            with tempfile.TemporaryFile(prefix=".jixing-write-check-", dir=directory):
                pass
        except OSError as exc:
            raise OSError(
                "助手无法在当前目录保存设置、作品集或备份。\n"
                "请关闭助手，把完整 ZIP 解压到你有写入权限的新文件夹后再打开，"
                "例如桌面或自己的游戏工具目录；保留旧版及其备份。\n\n"
                f"无法写入的目录：{directory}"
            ) from exc


def _show_startup_error(message: str) -> None:
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, message, f"吉星派对 Mod 助手 · {__version__}", 0x10)


def main() -> None:
    """启动本地 API，并将 HTML 页面交给 C# WebView2 原生窗口显示。"""
    import tkinter as tk

    web_app._set_windows_app_id()
    os.chdir(APP_ROOT)
    web_app.WEB_DIR = web_app._web_dir()
    if not (web_app.WEB_DIR / "index.html").exists():
        raise FileNotFoundError(f"找不到界面文件：{web_app.WEB_DIR / 'index.html'}")

    try:
        _check_data_directory()
        host_executable = _host_executable()
        api = web_app.DesktopApi()
    except OSError as exc:
        _show_startup_error(str(exc))
        return
    root = tk.Tk()
    root.withdraw()
    api.set_tk_root(root)

    class ThreadingServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    port = web_app._free_port()
    api_token = secrets.token_urlsafe(32)
    server = make_server(
        "127.0.0.1",
        port,
        web_app._build_server(api, api_token),
        server_class=ThreadingServer,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()

    icon = ASSETS_DIR / "app_icon.ico"
    profile_dir = DATA_DIR / "webview2_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    host_args = [
        str(host_executable),
        "--version",
        __version__,
        "--url",
        f"http://127.0.0.1:{port}/#token={api_token}",
        "--profile",
        str(profile_dir),
    ]
    if icon.exists():
        host_args.extend(("--icon", str(icon.resolve())))
    host = subprocess.Popen(
        host_args,
        cwd=str(APP_ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    def wait_for_host() -> None:
        host.wait()
        try:
            root.after(0, root.destroy)
        except Exception:
            pass

    threading.Thread(target=wait_for_host, daemon=True).start()
    try:
        root.mainloop()
    finally:
        api._clear_workshop_pending()
        api._clear_replacement()
        api.controller.close()
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        if host.poll() is None:
            host.terminate()
