"""用 C# WebView2 原生窗口承载 HTML 前端，Python 仅提供本地接口。"""
from __future__ import annotations

import os
import secrets
import subprocess
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from . import web_app
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


def main() -> None:
    """启动本地 API，并将 HTML 页面交给 C# WebView2 原生窗口显示。"""
    import tkinter as tk

    web_app._set_windows_app_id()
    os.chdir(APP_ROOT)
    web_app.WEB_DIR = web_app._web_dir()
    if not (web_app.WEB_DIR / "index.html").exists():
        raise FileNotFoundError(f"找不到界面文件：{web_app.WEB_DIR / 'index.html'}")

    api = web_app.DesktopApi()
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
        str(_host_executable()),
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
        api.controller.close()
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        if host.poll() is None:
            host.terminate()
