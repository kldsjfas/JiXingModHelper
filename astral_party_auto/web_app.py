"""HTML 界面的本地接口层：窗口由 C# WebView2 承载，资源处理复用 ModController。"""
from __future__ import annotations

import base64
import hmac
import hashlib
import json
import mimetypes
import os
import tempfile
import threading
from collections import deque
from copy import deepcopy
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from .core.config import APP_ROOT, RESOURCE_ROOT
from . import __version__
from .core.updates import REPOSITORY_URL, RELEASES_URL, UpdateChecker
from .mod_controller import DATA_DIR, MADE_DIR, ModController
from .modkit.categories import ASSET_TYPES, annotate_texture_name_duplicates


def _web_dir() -> Path:
    candidates = [
        RESOURCE_ROOT / "astral_party_auto" / "webui",
        RESOURCE_ROOT / "webui",
        Path(__file__).resolve().parent / "webui",
        APP_ROOT / "astral_party_auto" / "webui",
    ]
    for path in candidates:
        if (path / "index.html").exists():
            return path
    return candidates[0]


WEB_DIR = _web_dir()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def exposed(method: Callable) -> Callable:
    """所有 JS API 都返回统一结构，前端不用处理 Python 异常对象。"""

    @wraps(method)
    def wrapped(self: "DesktopApi", *args, **kwargs):
        try:
            with self._controller_lock:
                data = method(self, *args, **kwargs)
            return {"ok": True, "data": _json_safe(data)}
        except Exception as exc:
            message = str(exc) or exc.__class__.__name__
            self._append_log(f"操作失败：{message}")
            return {"ok": False, "error": message}

    wrapped._exposed = True  # type: ignore[attr-defined]
    return wrapped


def _to_tk_filetypes(file_types) -> list:
    """把 pywebview 风格的 file_types（'图片 (*.png;*.jpg)'）转成 tkinter 的 [(label, pattern)]。"""
    result = []
    for entry in file_types or ():
        text = str(entry)
        if "(" in text and ")" in text:
            label = text.split("(", 1)[0].strip() or "文件"
            patterns = text[text.find("(") + 1 : text.rfind(")")].replace(";", " ").replace(",", " ")
            result.append((label, patterns.strip() or "*.*"))
        else:
            result.append((text, "*.*"))
    return result or [("所有文件", "*.*")]


class DesktopApi:
    def __init__(self) -> None:
        self._controller_lock = threading.RLock()
        self._logs: deque[str] = deque(maxlen=800)
        self._events: list[dict] = []
        self._event_seq = 0
        self._events_lock = threading.Lock()
        self._tk_root = None
        self._image_cache: dict[tuple, str] = {}
        self._animation_cache: dict[tuple, dict] = {}
        self._pending_mod_path: Path | None = None
        self._replacement_path: Path | None = None
        self._emote_replacement: dict | None = None
        self._emote_candidate_dir = None
        self._emote_preview_cache: dict[tuple, dict] = {}
        self._visual_candidate = None
        self._visual_candidate_dir = None
        self._draft_crop_path: Path | None = None
        self._draft_crop_index: int | None = None
        self._draft_crop_item: dict | None = None
        self._migration_source: Path | None = None
        self._migration_plans = []
        self._migration_warnings: list[str] = []
        # Mod 管理预览：{bundle文件名: Path}
        self._mod_preview_files: dict[str, Path] = {}
        self._mod_preview_title: str = ""
        self.controller = ModController(self._append_log)
        self._audio_workspace = None
        self._update_checker = UpdateChecker(__version__)
        self._workshop_client = None
        self._workshop_pending = None
        self._workshop_submission = None
        self._workshop_issue_url = None
        self._workshop_upload_client = None

    def _workshop(self):
        if self._workshop_client is None:
            from .modkit.workshop import WorkshopClient
            self._workshop_client = WorkshopClient(DATA_DIR)
        return self._workshop_client

    def _workshop_upload(self):
        if getattr(self, "_workshop_upload_client", None) is None:
            from .modkit.workshop_upload import WorkshopUploadClient
            self._workshop_upload_client = WorkshopUploadClient(DATA_DIR)
        return self._workshop_upload_client

    def _clear_workshop_pending(self) -> None:
        pending = getattr(self, "_workshop_pending", None)
        self._workshop_pending = None
        if pending is not None:
            cleanup = self._workshop().discard(pending)
            if isinstance(cleanup, dict) and not cleanup.get("removed"):
                self._append_log("工坊暂存保留：" + cleanup.get("message", "暂存无法安全清理。"))

    def _workshop_analysis(self, directory: Path, entry: dict) -> dict:
        manager = self.controller.manager
        if manager is None:
            return {"matched": 0, "unmatched": 0, "conflicts": 0, "conflict_names": [],
                    "can_install": False, "message": "未检测到游戏；可以浏览和投稿，安装需要先连接游戏。"}
        analysis = manager.analyze_mod(directory)
        name = "工坊 " + entry["id"]
        conflict_files = set()
        conflicts = []
        for mod_name, mod in manager._load_state().get("mods", {}).items():
            overlap = set(analysis.matched).intersection(mod.get("files", []))
            if mod_name != name and not mod.get("disabled", False) and overlap:
                conflicts.append(mod_name)
                conflict_files.update(overlap)
        compatible = bool(analysis.matched) and not analysis.unmatched
        return {"matched": len(analysis.matched), "unmatched": len(analysis.unmatched),
                "conflicts": len(conflict_files), "conflict_names": conflicts, "can_install": compatible,
                "matched_names": sorted(analysis.matched), "unmatched_names": sorted(analysis.unmatched),
                "message": "资源名称匹配，仍请核对作者标注的游戏版本。" if compatible else "资源与当前游戏不完全匹配，工坊停止安装。"}

    def _workshop_forbidden_bundles(self) -> set[str]:
        if not getattr(self.controller, "aa_dirs", ()):
            return set()
        from .modkit.workshop_security import forbidden_workshop_bundle_names
        return forbidden_workshop_bundle_names(self.controller.aa_dirs)

    @exposed
    def workshop_catalog(self, force: bool = False) -> dict:
        return self._workshop().catalog(force=bool(force))

    @exposed
    def workshop_prepare(self, mod_id: str, sha256: str) -> dict:
        self._clear_workshop_pending()
        prepared = self._workshop().prepare(mod_id, expected_sha256=sha256, forbidden_bundle_names=self._workshop_forbidden_bundles())
        try:
            analysis = self._workshop_analysis(Path(prepared["mod_dir"]), prepared["entry"])
            prepared["game_key"] = self.controller._index_source_key
            prepared["analysis"] = analysis
            self._workshop_pending = prepared
            return {"entry": prepared["entry"], "inspection": prepared["inspection"], "analysis": analysis}
        except Exception:
            self._workshop().discard(prepared)
            raise

    @exposed
    def workshop_install(self) -> dict:
        from .modkit.workshop_security import scan_workshop_package
        previous = self._workshop_pending
        if previous is None:
            raise ValueError("请先下载并检查一个工坊作品。")
        entry = previous["entry"]
        game_key = previous["game_key"]
        previous_analysis = previous.get("analysis")
        self._clear_workshop_pending()
        if game_key != self.controller._index_source_key or self.controller.manager is None:
            raise ValueError("游戏目录已改变，请重新下载并检查作品。")
        # 即使刚才下载通过，也要重新检查撤回状态和目录摘要。
        prepared = self._workshop().prepare(entry["id"], expected_sha256=entry["asset"]["sha256"], forbidden_bundle_names=self._workshop_forbidden_bundles())
        try:
            analysis = self._workshop_analysis(Path(prepared["mod_dir"]), prepared["entry"])
            if not analysis["can_install"]:
                raise ValueError(analysis["message"])
            keys = ("matched", "unmatched", "conflicts", "matched_names", "unmatched_names", "conflict_names")
            if previous_analysis is None or any(analysis.get(key) != previous_analysis.get(key) for key in keys):
                raise ValueError("资源匹配或 Mod 冲突已改变，请重新下载并检查后确认安装。")
            self.controller._require_catalog_game_stopped()
            scan = scan_workshop_package(Path(prepared["mod_dir"]))
            self._workshop().verify_prepared(prepared, forbidden_bundle_names=self._workshop_forbidden_bundles())
            name = "工坊 " + entry["id"]
            result = self.controller.install(Path(prepared["mod_dir"]), name=name)
            self._append_log(f"工坊安装完成：{entry['name']} {entry['version']}；已通过格式、摘要和本机扫描。")
            return {"name": name, "matched": len(result.matched), "entry": prepared["entry"], "scan": scan,
                    "installed": self.controller.installed_mods(), "dashboard": self._dashboard_state()}
        finally:
            self._workshop().discard(prepared)

    @exposed
    def workshop_choose_submission(self) -> dict:
        from .modkit.workshop import inspect_package
        self._workshop_submission = None
        self._workshop_issue_url = None
        path = self._pick_path("open", file_types=("资源替换 Mod ZIP (*.zip)",))
        if path is None:
            return {"cancelled": True}
        inspection = inspect_package(path, forbidden_bundle_names=self._workshop_forbidden_bundles())
        self._workshop_submission = {"path": path, "sha256": inspection["sha256"]}
        return {"cancelled": False, "path_label": path.name, "inspection": inspection}

    @exposed
    def get_workshop_upload_status(self, refresh: bool = False) -> dict:
        return {**self._workshop_upload().snapshot(refresh=bool(refresh)), "game_version": ""}

    @exposed
    def submit_workshop_upload(self, metadata: dict) -> dict:
        """复制已检查作品再上传；不发送源路径、编辑记录或审核日志。"""
        from .modkit.workshop import inspect_package, prepare_submission, validate_metadata
        candidate = self._workshop_submission
        if candidate is None:
            raise ValueError("请先选择投稿 ZIP。")
        if not isinstance(metadata, dict):
            raise ValueError("投稿信息无效。")
        client = self._workshop_upload()
        state = client.snapshot()
        if not state["configured"]:
            raise RuntimeError(state["message"])
        forbidden = self._workshop_forbidden_bundles()
        if inspect_package(candidate["path"], forbidden_bundle_names=forbidden)["sha256"] != candidate["sha256"]:
            self._workshop_submission = None
            raise ValueError("投稿 ZIP 已改变，请重新选择。")
        details = dict(metadata)
        if not details.get("version"):
            details["version"] = "1.0.0"
        if not details.get("game_version"):
            details["game_version"] = "未标注"
        details["id"] = "pending"
        details = validate_metadata(details)
        # 同原件和资料在重选、重启后仍得到同一编号，丢失回执不会重复投稿。
        identity = json.dumps({key: value for key, value in details.items() if key != "id"},
                              ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        details["id"] = "mod-" + hashlib.sha256(candidate["sha256"].encode("ascii") + b"\0" + identity).hexdigest()[:32]
        stage_root = DATA_DIR / "workshop-upload-staging"
        stage_root.mkdir(parents=True, exist_ok=True)
        if stage_root.is_symlink() or getattr(stage_root.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("投稿暂存目录不安全，已停止上传。")
        with tempfile.TemporaryDirectory(prefix="submission-", dir=stage_root) as temporary:
            prepared = prepare_submission(candidate["path"], details, Path(temporary) / "submission.zip",
                                          forbidden_bundle_names=forbidden)
            if (self._workshop_submission is not candidate
                    or inspect_package(candidate["path"], forbidden_bundle_names=forbidden)["sha256"] != candidate["sha256"]):
                self._workshop_submission = None
                raise ValueError("投稿作品在准备期间发生改变，请重新选择。")
            inspection = inspect_package(prepared["path"], forbidden_bundle_names=forbidden)
            if (inspection["sha256"] != prepared["inspection"]["sha256"]
                    or inspection["size"] != prepared["inspection"]["size"]):
                raise ValueError("投稿暂存发生改变，请重新选择。")
            result = client.upload(prepared["path"], details, expected_sha256=inspection["sha256"],
                                   expected_size=inspection["size"])
        self._append_log("工坊投稿已确认：" + details["name"] + "；" + result["message"])
        return result

    @exposed
    def workshop_export_submission(self, metadata: dict) -> dict:
        from uuid import uuid4
        from .modkit.workshop import inspect_package, prepare_submission, validate_metadata
        candidate = self._workshop_submission
        self._workshop_issue_url = None
        if candidate is None:
            raise ValueError("请先选择投稿 ZIP。")
        if not isinstance(metadata, dict):
            raise ValueError("投稿信息无效。")
        if inspect_package(candidate["path"], forbidden_bundle_names=self._workshop_forbidden_bundles())["sha256"] != candidate["sha256"]:
            self._workshop_submission = None
            raise ValueError("投稿 ZIP 已改变，请重新选择。")
        details = dict(metadata)
        if not details.get("version"):
            details["version"] = "1.0.0"
        if not details.get("game_version"):
            details["game_version"] = "未标注"
        details["id"] = "mod-" + uuid4().hex[:20]
        details = validate_metadata(details)
        destination = self._pick_path("save", file_types=("投稿 ZIP (*.zip)",), save_filename="workshop-submission.zip")
        if destination is None:
            return {"cancelled": True}
        if destination.suffix.lower() != ".zip":
            destination = destination.with_suffix(".zip")
        if destination.resolve() == candidate["path"].resolve():
            raise ValueError("请另存投稿包，不能覆盖原始作品。")
        if inspect_package(candidate["path"], forbidden_bundle_names=self._workshop_forbidden_bundles())["sha256"] != candidate["sha256"]:
            self._workshop_submission = None
            raise ValueError("投稿 ZIP 在选择保存位置时发生改变，请重新选择。")
        result = prepare_submission(candidate["path"], details, destination, forbidden_bundle_names=self._workshop_forbidden_bundles())
        return {"cancelled": False, **result}

    @exposed
    def workshop_open_link(self, kind: str, mod_id: str = "") -> bool:
        from urllib.parse import urlencode
        if kind == "submission":
            raise ValueError("请在工坊内填写资料并点击上传，无需打开 GitHub。")
        elif kind == "report":
            # 固定仓库入口，远端条目不能指定任意导航地址。
            entry = next((item for item in self._workshop().catalog()["mods"] if item["id"] == mod_id), None)
            if entry is None:
                raise ValueError("作品已不在当前目录，请刷新工坊。")
            url = REPOSITORY_URL + "/issues/new?" + urlencode({"title": "[工坊举报] " + entry["id"], "body": "作品：" + entry["id"] + "\n请描述问题及复现情况，不要公开个人信息。"})
        elif kind == "repository":
            url = REPOSITORY_URL + "/tree/workshop"
        else:
            raise ValueError("未知的工坊链接。")
        os.startfile(url)
        return True

    @exposed
    def check_for_updates(self, force: bool = False) -> dict:
        return self._update_checker.start(bool(force))

    @exposed
    def get_update_status(self) -> dict:
        return self._update_checker.snapshot()

    @exposed
    def open_project_link(self, kind: str = "repository") -> bool:
        if kind == "repository":
            url = REPOSITORY_URL
        elif kind == "release":
            url = self._update_checker.snapshot().get("release_url") or RELEASES_URL
        elif kind == "community":
            url = "https://qm.qq.com/q/QC1pQPUpyM"
        else:
            raise ValueError("未知的项目链接。")
        os.startfile(url)
        return True

    def _audio(self):
        if self._audio_workspace is None:
            from .modkit.audio_workspace import AudioWorkspace
            self._audio_workspace = AudioWorkspace(self.controller, DATA_DIR, MADE_DIR)
        return self._audio_workspace

    @exposed
    def audio_catalog(self, query="", category="all", offset=0, limit=40, refresh=False) -> dict:
        return self._audio().catalog(query, category, offset, limit, refresh)

    @exposed
    def audio_preview(self, audio_id: str, source="original") -> dict:
        return self._audio().preview(audio_id, source)

    @exposed
    def audio_draft_state(self) -> dict:
        return self._audio().draft_state()

    @exposed
    def audio_choose_replacement(self, audio_id: str) -> dict | None:
        # 显式绑定媒体编号，选文件期间切换条目也不会把音频写到另一项。
        self._audio().row(audio_id)
        candidate = self._pick_path("open", file_types=("Wwise 音频 (*.wem)", "所有文件 (*.*)"))
        if candidate is None:
            return None
        return self._audio().prepare(audio_id, candidate)

    @exposed
    def audio_commit(self, audio_id: str, ticket: str) -> dict:
        return self._audio().commit(audio_id, ticket)

    @exposed
    def audio_remove(self, audio_id: str) -> dict:
        return self._audio().remove(audio_id)

    @exposed
    def audio_clear_draft(self) -> dict:
        return self._audio().clear_draft()

    @exposed
    def audio_export_preview(self, audio_id: str, source="original", format="wav") -> dict | None:
        if format not in ("wav", "wem"):
            raise ValueError("只支持 WAV 和 WEM。")
        row = self._audio().row(audio_id)
        import re
        name = re.sub(r'[<>:"/\\|?*]', "_", row["name"])
        output = self._pick_path("save", save_filename=f"{name}.{format}", file_types=(f"音频 (*.{format})",))
        if output is None:
            return None
        self._audio().export_audio(audio_id, source, format, output)
        return {"path": str(output)}

    @exposed
    def audio_export_pack(self) -> dict:
        return {"path": str(self._audio().export_pack())}

    @exposed
    def audio_install(self) -> dict:
        self._audio().install()
        return {"dashboard": self._dashboard_state(), "installed": self.controller.installed_mods()}

    def set_tk_root(self, root) -> None:
        self._tk_root = root

    def _run_on_ui(self, func):
        """把需要在主线程执行的操作（tkinter 文件框）从 HTTP 线程调回主线程。"""
        if self._tk_root is None:
            return func()
        from time import monotonic

        box: dict = {}
        done = threading.Event()
        result_lock = threading.Lock()
        expired = False
        deadline = monotonic() + 180
        timeout_message = "文件选择等待超时，请关闭仍打开的选择窗口后重试。"

        def wrapper():
            with result_lock:
                if expired:
                    done.set()
                    return
                if monotonic() >= deadline:
                    box["error"] = TimeoutError(timeout_message)
                    done.set()
                    return
            try:
                value = func()
            except Exception as exc:  # noqa: BLE001
                with result_lock:
                    if not expired:
                        box["error"] = exc
            else:
                with result_lock:
                    if not expired:
                        box["value"] = value
            finally:
                done.set()

        self._tk_root.after(0, wrapper)
        if not done.wait(180):
            with result_lock:
                expired = True
                box.clear()
            raise TimeoutError(timeout_message)
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def _append_log(self, message: str) -> None:
        from datetime import datetime

        line = f"[{datetime.now():%H:%M:%S}] {message}"
        self._logs.append(line)
        self._emit("log", {"line": line})

    def _emit(self, event: str, payload: dict) -> None:
        # 事件先缓存，前端通过 /poll 轮询取走（不再依赖 pywebview 的 run_js）。
        with self._events_lock:
            self._event_seq += 1
            self._events.append({"id": self._event_seq, "event": event, "payload": payload})
            if len(self._events) > 500:
                self._events = self._events[-500:]

    def poll_events(self, since: int) -> dict:
        with self._events_lock:
            items = [e for e in self._events if e["id"] > int(since or 0)]
            cursor = self._event_seq
        return {"events": items, "cursor": cursor}

    def _image_data(self, path: str | Path | None) -> str:
        if not path:
            return ""
        image_path = Path(path)
        if not image_path.exists() or not image_path.is_file():
            return ""
        stat = image_path.stat()
        cache_key = (str(image_path), stat.st_mtime_ns, stat.st_size)
        cached = self._image_cache.get(cache_key)
        if cached:
            return cached
        mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        data_url = f"data:{mime};base64,{encoded}"
        if len(self._image_cache) > 40:
            self._image_cache.clear()
        self._image_cache[cache_key] = data_url
        return data_url

    def _selection_payload(self, selection: dict | None = None, *, full_text: bool = False) -> dict | None:
        selection = selection or self.controller.selection
        if not selection:
            return None
        payload = dict(selection)
        payload["preview_data"] = self._image_data(selection.get("preview"))
        if full_text and selection.get("asset_type") == "text" and selection.get("editable", True):
            source = selection.get("original_path") or selection.get("bundle_path")
            payload["original_text"] = self.controller.read_text(source, selection["name"])
            draft_path = self.controller._draft_dir() / selection["bundle"]
            payload["full_text"] = self.controller.read_text(draft_path, selection["name"]) if draft_path.exists() else payload["original_text"]
        payload.setdefault("editable", payload.get("asset_type") in ("text", "texture", "anim"))
        return payload

    def _dashboard_state(self) -> dict:
        controller = self.controller
        installed = controller.installed_mods()
        backup_count = 0
        if controller.manager and controller.manager.backup_dir.exists():
            backup_count = sum(1 for _ in controller.manager.backup_dir.glob("*.bundle"))
        game_name = "未检测到"
        if controller.game_install:
            game_name = controller.game_install.name
        return {
            "has_game": controller.has_game,
            "game_name": game_name,
            "game_exe": controller.game_exe_display(),
            "bundle_count": controller.bundle_count,
            "texture_bundle_count": len(controller.index),
            "installed_count": len(installed),
            "backup_count": backup_count,
            "index_ready": controller.index_ready,
            "asset_type_counts": controller.asset_type_counts(),
        }

    def _draft_state(self) -> dict:
        return {
            "name": self.controller.draft_name,
            "items": [dict(item) for item in self.controller.draft_items],
        }

    def _pick_path(self, kind: str, *, file_types=(), save_filename: str = "") -> Path | None:
        """用 tkinter 原生文件框，kind = 'open' | 'folder' | 'save'。"""

        def _open_dialog():
            from tkinter import filedialog

            filetypes = _to_tk_filetypes(file_types)
            parent = self._tk_root
            options = {"parent": parent} if parent is not None else {}
            previous_topmost = False
            try:
                if parent is not None:
                    previous_topmost = bool(parent.attributes("-topmost"))
                    parent.attributes("-topmost", True)
                    parent.update_idletasks()
                if kind == "folder":
                    return filedialog.askdirectory(title="选择文件夹", **options)
                if kind == "save":
                    return filedialog.asksaveasfilename(
                        title="保存为",
                        initialfile=save_filename or "",
                        filetypes=filetypes,
                        **options,
                    )
                return filedialog.askopenfilename(title="选择文件", filetypes=filetypes, **options)
            finally:
                if parent is not None:
                    parent.attributes("-topmost", previous_topmost)

        result = self._run_on_ui(_open_dialog)
        return Path(result) if result else None

    @exposed
    def bootstrap(self) -> dict:
        # 启动时强制再检一次：WebView 冷启动 / 杀软拦截注册表时，构造期检测可能为空
        self.controller.refresh_detection()
        if not self.controller.has_game:
            # 再试一轮（Steam 库盘刚就绪等情况）
            self.controller.refresh_detection()
        dash = self._dashboard_state()
        if dash.get("has_game"):
            self._append_log(f"已连接游戏：{dash.get('game_exe') or dash.get('game_name')}")
        else:
            self._append_log("未检测到游戏。可点「刷新检测」，或确认已安装 Steam 或 TapTap 版吉星派对。")
        return {
            "dashboard": dash,
            "app_info": {"version": __version__, "repository_url": REPOSITORY_URL},
            "installed": self.controller.installed_mods(),
            "draft": self._draft_state(),
            "logs": list(self._logs),
            "asset_types": [{"id": type_id, "label": label} for type_id, label in ASSET_TYPES],
        }

    @exposed
    def refresh_detection(self) -> dict:
        self._clear_workshop_pending()
        self._clear_emote_candidate()
        self._clear_visual_candidate()
        self._replacement_path = None
        self.controller.refresh_detection()
        if self.controller.has_game:
            self._append_log(f"已刷新游戏检测：{self.controller.game_exe_display()}")
        else:
            self._append_log("已刷新游戏检测：仍未找到。")
        return self._dashboard_state()

    @exposed
    def launch_game(self) -> dict:
        self.controller.launch_game("CN")
        return self._dashboard_state()

    @exposed
    def restore_all(self) -> dict:
        restored = self.controller.restore_all()
        return {"restored": restored, "dashboard": self._dashboard_state(), "installed": self.controller.installed_mods()}

    @exposed
    def open_asset_dir(self) -> bool:
        if not self.controller.has_game:
            raise RuntimeError("没有检测到游戏资源目录。")
        os.startfile(str(self.controller.aa_dir))
        return True

    @exposed
    def open_made_dir(self) -> bool:
        MADE_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(MADE_DIR))
        return True

    def _set_mod_preview_files(self, files: dict[str, Path], title: str) -> list[str]:
        self._mod_preview_files = dict(files)
        self._mod_preview_title = title
        return sorted(self._mod_preview_files.keys())

    @exposed
    def choose_mod(self, mode: str) -> dict | None:
        if mode == "folder":
            path = self._pick_path("folder")
        else:
            path = self._pick_path(
                "open",
                file_types=("Mod 压缩包 (*.zip;*.rar)", "所有文件 (*.*)"),
            )
        if path is None:
            return None
        analysis = self.controller.analyze(path)
        self._pending_mod_path = path
        name = path.stem if path.is_file() else path.name
        # 可装的包优先列入预览列表
        if analysis.matched:
            files = {n: analysis.files_map[n] for n in analysis.matched if n in analysis.files_map}
        else:
            files = dict(analysis.files_map)
        bundles = self._set_mod_preview_files(files, f"待装 · {name}")
        return {
            "path": str(path),
            "name": name,
            "total": analysis.total,
            "matched": len(analysis.matched),
            "unmatched": len(analysis.unmatched),
            "preview_title": self._mod_preview_title,
            "bundles": bundles,
        }

    @exposed
    def choose_migration_source(self, mode: str = "file") -> dict | None:
        if mode == "folder":
            path = self._pick_path("folder")
        else:
            path = self._pick_path(
                "open",
                file_types=("旧资源包 (*.bundle;__data)", "所有文件 (*.*)"),
            )
        if path is None:
            return None

        plans, warnings = self.controller.plan_old_mod_migration(path)
        self._migration_source = path
        self._migration_plans = plans
        self._migration_warnings = warnings
        summary = self.controller.migration_plan_summary(plans, warnings)
        summary.update({"path": str(path), "name": path.name})
        return summary

    @exposed
    def run_migration(self) -> dict:
        if self._migration_source is None or not self._migration_plans:
            raise RuntimeError("请先选择旧 Mod 文件或文件夹。")
        report = self.controller.migrate_old_mod(self._migration_plans)
        report["warnings"] = list(self._migration_warnings)
        return {"report": report, "draft": self._draft_state()}

    @exposed
    def install_pending_mod(self) -> dict:
        if self._pending_mod_path is None:
            raise RuntimeError("请先选择 Mod。")
        source = self._pending_mod_path
        name = source.stem if source.is_file() else source.name
        analysis = self.controller.install(source, name=name)
        self._pending_mod_path = None
        installed = self.controller.installed_mods()
        info = next((item for item in installed if item.get("name") == name), {})
        store = Path(info.get("store") or (DATA_DIR / "mod_store" / name))
        bundles: list[str] = []
        if store.exists():
            files = {p.name: p for p in sorted(store.glob("*.bundle"))}
            bundles = self._set_mod_preview_files(files, f"已装 · {name}")
        return {
            "name": name,
            "matched": len(analysis.matched),
            "unmatched": len(analysis.unmatched),
            "installed": installed,
            "dashboard": self._dashboard_state(),
            "preview_title": self._mod_preview_title,
            "bundles": bundles,
        }

    @exposed
    def change_mod(self, action: str, name: str) -> dict:
        if action == "enable":
            count = self.controller.enable_mod(name)
        elif action == "disable":
            count = self.controller.disable_mod(name)
        elif action == "uninstall":
            count = self.controller.uninstall(name)
            if self._mod_preview_title.endswith(name) or name in self._mod_preview_title:
                self._mod_preview_files = {}
                self._mod_preview_title = ""
        else:
            raise RuntimeError(f"不支持的操作：{action}")
        return {"changed": count, "installed": self.controller.installed_mods(), "dashboard": self._dashboard_state()}

    def _load_mod_preview_impl(self, name: str) -> dict:
        """点已装 Mod「预览」：列出该 mod 全部资源包，供左侧点选看图。"""
        manager = self.controller.manager
        if manager is None:
            raise RuntimeError("没有检测到游戏。")
        state = manager._load_state()
        info = state.get("mods", {}).get(name)
        if not info:
            raise RuntimeError("找不到该 Mod 的安装记录。")
        file_names = list(info.get("files") or [])
        store = Path(info.get("store") or (DATA_DIR / "mod_store" / name))
        files: dict[str, Path] = {}
        missing: list[str] = []
        for fname in file_names:
            path = manager._mod_file_source(info, fname)
            legacy_path = store / fname
            if path is None and legacy_path.is_file():
                path = legacy_path
            if path is None:
                missing.append(fname)
            else:
                files[fname] = path
        if missing:
            raise RuntimeError("该 Mod 的资源文件缺失，请重新安装：" + "、".join(missing))
        if not files:
            raise RuntimeError("没有可预览的文件。请重新安装该 mod。")
        bundles = self._set_mod_preview_files(files, f"已装 · {name}")
        first = bundles[0] if bundles else ""
        first_preview = self._preview_mod_bundle_impl(first) if first else None
        return {
            "title": self._mod_preview_title,
            "name": name,
            "bundles": bundles,
            "first": first_preview,
        }

    def _preview_mod_bundle_impl(self, bundle_name: str) -> dict:
        """预览当前预览会话中某个资源包的第一张贴图。"""
        path = self._mod_preview_files.get(bundle_name)
        if path is None or not Path(path).exists():
            raise RuntimeError(f"找不到资源包：{bundle_name}")
        names = self.controller.list_bundle_texture_names(path)
        if not names:
            return {
                "bundle": bundle_name,
                "texture": "",
                "size": "",
                "preview_data": "",
                "message": "包内无可预览贴图",
            }
        preview, texture_info = self.controller.preview_mod_bundle(path, names[0])
        return {
            "bundle": bundle_name,
            "texture": texture_info.name if texture_info else names[0],
            "size": f"{texture_info.width}×{texture_info.height}" if texture_info else "",
            "preview_data": self._image_data(preview),
            "message": "",
        }

    @exposed
    def load_mod_preview(self, name: str) -> dict:
        return self._load_mod_preview_impl(name)

    @exposed
    def preview_mod_bundle(self, bundle_name: str) -> dict:
        return self._preview_mod_bundle_impl(bundle_name)

    @exposed
    def get_categories(self, asset_type: str) -> list[dict]:
        return [
            {"id": category_id, "label": label, "count": count}
            for category_id, label, count in self.controller.categories(
                include_advanced=False,
                asset_type=asset_type,
            )
        ]

    @exposed
    def browse_assets(
        self,
        asset_type: str,
        category_id: str,
        query: str = "",
        character: str = "",
        page: int = 0,
        page_size: int = 50,
    ) -> dict:
        page = max(0, int(page or 0))
        page_size = max(1, int(page_size or 50))
        offset = page * page_size
        total = self.controller.count_labelled(
            category_id,
            query,
            asset_type=asset_type,
            character=character,
        )
        rows = self.controller.browse_labelled(
            category_id,
            query,
            limit=page_size,
            offset=offset,
            asset_type=asset_type,
            character=character,
        )
        names_only = [(bundle, name) for bundle, name, _char in rows]
        annotated = annotate_texture_name_duplicates(names_only)
        return {
            "items": [
                {
                    "bundle": bundle,
                    "name": name,
                    "character": char,
                    "duplicates": duplicates,
                }
                for (bundle, name, char), (_b, _n, duplicates) in zip(rows, annotated)
            ],
            "total": total,
        }

    def _animation_preview(self, path: Path, name: str, kind: str, fps: float = 30) -> dict:
        from .modkit.animation import read_animation_preview
        import math

        fps = float(fps)
        if not math.isfinite(fps) or not 1 <= fps <= 120:
            raise RuntimeError("预览帧率应在 1–120 之间。")
        stat = path.stat()
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, name, kind, fps)
        if key not in self._animation_cache:
            if kind == "sprite_clip":
                from .modkit.clip_preview import read_clip_preview
                result = read_clip_preview(path, name)
            else:
                result = read_animation_preview(path, name, kind=kind, fps=fps)
            if len(self._animation_cache) >= 4:
                self._animation_cache.pop(next(iter(self._animation_cache)))
            self._animation_cache[key] = result
        return self._animation_cache[key]

    @exposed
    def get_animation_preview(self, bundle: str, name: str, source: str = "original", fps: float = 30) -> dict:
        if source not in ("original", "draft"):
            raise RuntimeError("预览来源无效。")
        path = self.controller.original_bundle_path(bundle)
        if path is None:
            raise RuntimeError(f"找不到资源包：{bundle}")
        if source == "draft":
            draft = self.controller._draft_dir() / Path(bundle).name
            if not draft.exists():
                return {"frames": [], "durations": [], "total": 0, "notice": "还没有保存此动画的替换。"}
            path = draft
        selection = self.controller.selection or {}
        if selection.get("bundle") == bundle and selection.get("name") == name:
            kind = selection.get("animation_kind") or "sequence"
        else:
            item = next((x for x in self.controller.draft_items if x.get("bundle") == bundle and x.get("name") == name), {})
            kind = "sprite_clip" if item.get("kind") == "anim" else item.get("animation_kind", "sequence")
        return self._animation_preview(path, name, kind, fps)

    def _clip_candidate_preview(self, selection: dict, replacement: Path) -> dict:
        """预览临时副本中的替换结果；选择文件和裁剪都不写入作品集。"""
        from .modkit.clip_preview import read_clip_preview
        from .modkit.clip_edit import replace_clip_checked
        from .modkit.clip_atlas import replace_clip_atlas_checked

        original = Path(selection.get("original_path") or selection["bundle_path"])
        draft = self.controller._draft_dir() / selection["bundle"]
        source = draft if draft.exists() else original
        with tempfile.TemporaryDirectory(prefix="jixing-clip-preview-") as folder:
            temporary = Path(folder) / "candidate.bundle"
            if replacement.suffix.lower() in {".animbin", ".bin"}:
                if replacement.stat().st_size > 32 * 1024 * 1024:
                    raise RuntimeError("动画数据超过 32 MB。")
                replace_clip_checked(source, selection["name"], replacement.read_bytes(), temporary, reference_bundle=original)
            else:
                replace_clip_atlas_checked(source, selection["name"], replacement, temporary)
            try:
                return read_clip_preview(temporary, selection["name"])
            except ValueError as exc:
                if replacement.suffix.lower() not in {".animbin", ".bin"}:
                    raise
                return {"frames": [], "durations": [], "total": 0, "notice": f"替换文件可以读取，但无法播放：{exc}"}

    @exposed
    def get_sequence_frames(self, bundle: str, name: str) -> dict:
        path = self.controller.original_bundle_path(bundle)
        if path is None:
            raise RuntimeError(f"找不到资源包：{bundle}")
        return self._animation_preview(path, name, "sequence")

    @exposed
    def get_character_list(self) -> list[str]:
        return self.controller.character_list()

    @exposed
    def get_character_labels(self) -> dict:
        return self.controller.character_labels()

    @exposed
    def set_resource_character(self, bundle: str, name: str, character: str) -> dict:
        return self.controller.set_resource_character(bundle, name, character)

    @exposed
    def set_bundle_character(self, bundle: str, character: str) -> dict:
        return self.controller.set_bundle_character(bundle, character)

    @exposed
    def set_resources_character(self, items: list[dict], character: str) -> dict:
        return self.controller.set_resources_character(items, character)

    @exposed
    def select_asset(self, asset_type: str, bundle: str, name: str, force: bool = False) -> dict:
        self._replacement_path = None
        self._clear_emote_candidate()
        self._clear_visual_candidate()
        selection = self.controller.set_selection(
            bundle,
            name,
            asset_type=asset_type,
            force=bool(force),
        )
        return self._selection_payload(selection) or {}

    @exposed
    def refresh_selection(self) -> dict | None:
        self._clear_emote_candidate()
        self._clear_visual_candidate()
        if not self.controller.selection:
            return None
        sel = self.controller.selection
        selection = self.controller.set_selection(
            sel.get("bundle", ""),
            sel.get("name", ""),
            asset_type=sel.get("asset_type") or sel.get("kind") or "texture",
            force=True,
        )
        return self._selection_payload(selection) or {}

    @exposed
    def get_studio_state(self) -> dict | None:
        selection = self.controller.selection
        if selection and selection.get("visual_animation"):
            selection["visual_animation"] = self.controller.visual_animation_workspace.selection(selection["name"])
        return self._selection_payload(full_text=True)

    def _clear_visual_candidate(self) -> None:
        self._visual_candidate = None
        folder = getattr(self, "_visual_candidate_dir", None)
        self._visual_candidate_dir = None
        if folder is not None:
            try:
                folder.cleanup()
            except OSError as exc:
                self._append_log(f"动态资源制作缓存暂未清理：{folder.name}（{exc}）")

    @exposed
    def choose_visual_animation(self) -> dict | None:
        self._clear_visual_candidate()
        workspace = self.controller.visual_animation_workspace
        workspace._selection()
        key = self._emote_selection_key()
        path = self._pick_path("open", file_types=("动图或视频 (*.gif;*.apng;*.webp;*.png;*.mp4;*.webm;*.mov;*.mkv;*.avi)", "所有文件 (*.*)"))
        if path is None:
            return None
        if key != self._emote_selection_key():
            raise ValueError("当前选择已改变，请重新导入。")
        parent = DATA_DIR / "visual-animation-candidates"
        parent.mkdir(parents=True, exist_ok=True)
        self._visual_candidate_dir = tempfile.TemporaryDirectory(prefix="import-", dir=parent)
        try:
            prepared = workspace.prepare(path, Path(self._visual_candidate_dir.name))
            if key != self._emote_selection_key():
                raise ValueError("当前选择已改变，请重新导入。")
            prepared["selection"] = key
            self._visual_candidate = prepared
            return {"name": path.name, "animation": prepared["animation"], "notice": prepared["notice"]}
        except Exception:
            self._clear_visual_candidate()
            raise

    @exposed
    def get_visual_animation_preview(self) -> dict:
        return self.controller.visual_animation_workspace.draft_preview()

    @exposed
    def save_visual_animation(self) -> dict:
        candidate = self._visual_candidate
        self._visual_candidate = None
        try:
            if candidate is None or candidate.get("selection") != self._emote_selection_key():
                raise ValueError("没有当前资源的动态候选，请重新导入。")
            item = self.controller.visual_animation_workspace.save(candidate)
            self.controller.selection["visual_animation"] = self.controller.visual_animation_workspace.selection(item["name"])
            try:
                animation = self.controller.visual_animation_workspace.draft_preview()
            except (OSError, ValueError, KeyError) as exc:
                animation = {"frames": [], "notice": f"动态作品已经保存，重新加载预览失败：{exc}"}
            return {"item": item, "animation": animation, "draft": self._draft_state()}
        finally:
            self._clear_visual_candidate()

    def _emote_selection_key(self) -> tuple:
        selection = self.controller.selection or {}
        return (getattr(self.controller, "_index_source_key", ""), selection.get("original_path"),
                selection.get("asset_type"), selection.get("bundle"), selection.get("name"))

    def _clear_emote_candidate(self) -> None:
        """只清理由本会话创建的转码目录，用户选入的原文件始终保留。"""
        self._emote_replacement = None
        folder = getattr(self, "_emote_candidate_dir", None)
        self._emote_candidate_dir = None
        if folder is not None:
            try:
                folder.cleanup()
            except OSError as exc:
                self._append_log(f"转码缓存暂时无法清理：{folder.name}（{exc}）")

    @staticmethod
    def _emote_file_hash(path: Path, max_bytes: int = 64 * 1024 * 1024) -> str:
        if not path.is_file() or path.stat().st_size > max_bytes:
            raise ValueError(f"表情素材不存在或已超过 {max_bytes // (1024 * 1024)} MB，请重新选择。")
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _emote_video_preview(self, video_name: str, variant: str = "original") -> dict:
        if variant not in ("original", "draft"):
            raise ValueError("请选择原表情或已保存的表情预览。")
        independent = next((item for item in self.controller.draft_items
                            if item.get("kind") == "emote" and item.get("independent_video")
                            and item.get("video_name") == video_name), None)
        if independent is not None:
            if variant != "draft":
                raise ValueError("独立表情只在作品集中存在，请查看已保存效果。")
            from .modkit.emote_addressables import independent_emote_identity
            identity = independent_emote_identity(independent["emote_id"])
            if identity["video_name"] != video_name or independent.get("extra_bundles") != [identity["bundle_name"]]:
                raise ValueError("独立表情的视频记录不完整，请重新制作。")
            path = self.controller._draft_dir() / identity["bundle_name"]
            if not path.is_file():
                raise ValueError("已保存的独立动态表情视频缺失，请恢复草稿文件后重试。")
        else:
            bundle_name, original = self.controller.emote_workspace.video_bundle(video_name)
            path = original
        if variant == "draft" and independent is None:
            draft = self.controller._draft_dir() / bundle_name
            custom = any(item.get("kind") == "emote" and item.get("custom_video")
                         and item.get("video_name") == video_name for item in self.controller.draft_items)
            if custom and not draft.is_file():
                raise ValueError("已保存的动态表情视频缺失，请恢复草稿文件后重试。")
            if draft.is_file():
                path = draft
        stat = path.stat()
        key = (getattr(self.controller, "_index_source_key", ""), str(path.resolve()),
               stat.st_size, stat.st_mtime_ns, video_name, self._emote_file_hash(path, 128 * 1024 * 1024))
        cache = getattr(self, "_emote_preview_cache", None)
        if cache is None:
            self._emote_preview_cache = cache = {}
        if key not in cache:
            from .modkit.emote_media import preview_emote_media
            animation = preview_emote_media(path, video_name, work_dir=DATA_DIR / "emote-preview-work")
            if not animation.get("frames"):
                raise ValueError("动态表情没有解码出画面，请重新预览。")
            if len(cache) >= 4:
                cache.pop(next(iter(cache)))
            cache[key] = animation
        return deepcopy(cache[key])

    @exposed
    def get_emote_preview(self, video_name: str, variant: str = "original") -> dict:
        self.controller.emote_workspace._selected_id()
        return self._emote_video_preview(video_name, variant)

    @exposed
    def choose_emote_file(self, video_name: str) -> dict | None:
        self._clear_emote_candidate()
        self.controller.emote_workspace._selected_id()
        selection_key = self._emote_selection_key()
        _, original = self.controller.emote_workspace.video_bundle(video_name)
        path = self._pick_path("open", file_types=(
            "动图或视频 (*.gif;*.apng;*.png;*.webp;*.mp4;*.webm;*.mov;*.mkv;*.avi;*.usm)",
            "所有文件 (*.*)"))
        if path is None:
            return None
        if selection_key != self._emote_selection_key():
            raise ValueError("表情已经变化，请重新选择素材。")
        path = Path(path)
        source_hash = self._emote_file_hash(path)
        reference_hash = self._emote_file_hash(original, 128 * 1024 * 1024)
        parent = DATA_DIR / "emote-candidates"
        parent.mkdir(parents=True, exist_ok=True)
        self._emote_candidate_dir = tempfile.TemporaryDirectory(prefix="import-", dir=parent)
        folder = Path(self._emote_candidate_dir.name)
        try:
            from .modkit.emote_media import prepare_emote_media
            output = folder / path.with_suffix(".usm").name
            prepared = prepare_emote_media(path, original, video_name, output, work_dir=folder)
            if selection_key != self._emote_selection_key():
                raise ValueError("表情已经变化，请重新选择素材。")
            if source_hash != self._emote_file_hash(path) or reference_hash != self._emote_file_hash(original, 128 * 1024 * 1024):
                raise ValueError("转换期间素材或原表情发生变化，请重新选择。")
            converted = Path(prepared["path"])
            self._emote_replacement = {"path": converted, "video_name": video_name,
                                       "independent": True,
                                       "selection": selection_key, "sha256": self._emote_file_hash(converted),
                                       "source_path": path, "source_sha256": source_hash,
                                       "reference_path": original, "reference_sha256": reference_hash}
            return {**prepared, "name": path.name, "source_name": path.name, "independent": True,
                    "video_name": video_name}
        except Exception:
            self._clear_emote_candidate()
            raise

    @exposed
    def save_emote(self, video_name: str, use_custom: bool = False) -> dict:
        candidate = self._emote_replacement
        self._emote_replacement = None
        try:
            if not isinstance(use_custom, bool):
                raise ValueError("表情导入模式不合法。")
            replacement = None
            if use_custom:
                if not candidate or candidate["selection"] != self._emote_selection_key() or candidate["video_name"] != video_name:
                    raise ValueError("表情或所选动图已经变化，请重新选择素材。")
                replacement = candidate["path"]
                if self._emote_file_hash(replacement) != candidate["sha256"]:
                    raise ValueError("转换成品内容已变化，请重新选择素材。")
                for label in ("source", "reference"):
                    source_path = candidate.get(f"{label}_path")
                    max_bytes = (128 if label == "reference" else 64) * 1024 * 1024
                    if source_path is not None and self._emote_file_hash(source_path, max_bytes) != candidate[f"{label}_sha256"]:
                        raise ValueError("素材或原表情内容已变化，请重新选择素材。")
            if use_custom:
                item = self.controller.add_emote_to_draft(video_name, replacement, independent=True)
            else:
                item = self.controller.add_emote_to_draft(video_name, replacement)
            self._emote_preview_cache = {}
            return {"item": item, "draft": self._draft_state()}
        finally:
            self._clear_emote_candidate()

    @exposed
    def export_emote_video(self, video_name: str) -> dict | None:
        self.controller.emote_workspace._selected_id()
        _, original = self.controller.emote_workspace.video_bundle(video_name)
        path = self._pick_path("save", file_types=("表情视频 (*.usm)",), save_filename=f"{video_name}.usm")
        if path is None:
            return None
        from .modkit.cri_emote import export_cri_emote_bundle
        output = export_cri_emote_bundle(original, video_name, path)
        return {"path": str(output), "name": output.name, "size": output.stat().st_size}

    @exposed
    def export_selection(self, variant: str = "primary") -> dict | None:
        selection = self.controller.selection
        if not selection:
            raise RuntimeError("请先选择资源。")
        asset_type = selection.get("asset_type") or "texture"
        default_name = self.controller.default_export_filename()
        fmt = None
        if asset_type == "texture":
            fmt = "jpg" if variant == "secondary" else "png"
            default_name = str(Path(default_name).with_suffix(f".{fmt}"))
        elif asset_type == "anim" and variant == "secondary":
            default_name = str(Path(default_name).with_suffix(".animbin"))
        elif asset_type == "dynamic" and selection.get("frame_names"):
            if variant == "secondary":
                fmt = "png"
                default_name = str(Path(default_name).with_suffix(".png"))
            else:
                fmt = "apng"
                default_name = str(Path(default_name).with_suffix(".apng"))
        file_path = self._pick_path(
            "save",
            save_filename=default_name,
            file_types=("所有文件 (*.*)",),
        )
        if file_path is None:
            return None
        output = self.controller.export_selection(file_path, fmt=fmt)
        return {"path": str(output)}

    @exposed
    def choose_replacement(self, sequence_folder: bool = False) -> dict | None:
        selection = self.controller.selection
        if not selection:
            raise RuntimeError("请先选择资源。")
        asset_type = selection.get("asset_type") or "texture"
        if asset_type == "anim":
            self._replacement_path = None
        if not selection.get("editable", True):
            raise RuntimeError(selection.get("reason") or "此资源暂不支持替换。")
        if asset_type in ("texture", "anim"):
            file_types = (
                "图片或动画字节 (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.animbin;*.bin)",
                "所有文件 (*.*)",
            )
        elif asset_type == "dynamic":
            file_types = ("动画图片 (*.gif;*.apng;*.png;*.webp)", "所有文件 (*.*)")
        elif asset_type == "text":
            file_types = ("文本文件 (*.txt;*.json;*.xml;*.csv;*.bytes)", "所有文件 (*.*)")
        else:
            raise RuntimeError("当前类型不需要选择替换文件。")
        if sequence_folder and not (asset_type == "dynamic" and selection.get("animation_kind") == "sequence"):
            raise RuntimeError("只有序列帧动画可以选择帧图文件夹。")
        path = self._pick_path("folder" if sequence_folder else "open", file_types=file_types)
        if path is None:
            return None
        if asset_type == "text":
            from .modkit.maker import decode_text_asset_raw
            if path.stat().st_size > 8 * 1024 * 1024:
                raise RuntimeError("文本超过 8 MB，请先拆分或精简。")
            text, kind = decode_text_asset_raw(path.read_bytes())
            if kind != "text" or text is None:
                raise RuntimeError("选中的文件不是可编辑的纯文本。")
            return {"path": str(path), "name": path.name, "text_content": text}
        animation = None
        if asset_type == "dynamic":
            from .modkit.animation import read_adapted_replacement
            source = selection.get("original_path") or selection.get("bundle_path")
            animation = read_adapted_replacement(source, selection["name"], path, kind=selection["animation_kind"])
        elif asset_type == "anim":
            animation = self._clip_candidate_preview(selection, path)
        elif asset_type == "texture":
            from .modkit.maker import read_static_image
            with read_static_image(path):
                pass
        preview_data = ""
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            preview_data = self._image_data(path)
        self._replacement_path = path
        return {"path": str(path), "name": path.name, "size": path.stat().st_size, "preview_data": preview_data, "animation": animation}

    @exposed
    def crop_replacement(self, crop_box: list) -> dict:
        """裁剪已选的替换图（浏览器端框选后调用）；裁剪结果覆盖为待提交文件。"""
        if self._replacement_path is None:
            raise RuntimeError("请先选择替换文件。")
        if (self.controller.selection or {}).get("asset_type") == "dynamic":
            raise RuntimeError("动画按原帧尺寸适配，不能用静态图片裁剪。")
        if (self.controller.selection or {}).get("asset_type") == "anim":
            raise RuntimeError("动画完整图集不能裁剪，请保留原图集尺寸和布局。")
        if self._replacement_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            raise RuntimeError("当前文件不是图片，不能裁剪。")
        from PIL import Image

        box = tuple(int(v) for v in crop_box)
        from .modkit.maker import read_static_image
        with read_static_image(self._replacement_path) as source:
            cropped = source.crop(box)
        out = DATA_DIR / "crop_preview.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(out)
        selection = self.controller.selection or {}
        animation = self._clip_candidate_preview(selection, out) if selection.get("asset_type") == "anim" and selection.get("playable") else None
        self._replacement_path = out
        return {"path": str(out), "name": out.name, "size": out.stat().st_size, "preview_data": self._image_data(out), "animation": animation}

    @exposed
    def commit_replacement(self, text_content: str = "", fps: float = 30) -> dict:
        selection = self.controller.selection
        if not selection:
            raise RuntimeError("请先选择资源。")
        asset_type = selection.get("asset_type") or "texture"
        if not selection.get("editable", True):
            raise RuntimeError(selection.get("reason") or "此资源暂不支持替换。")
        source = selection.get("original_path") or selection.get("bundle_path")
        if asset_type == "text":
            item = self.controller.add_text_to_draft(
                source,
                selection["name"],
                text_content,
                bundle_name=selection["bundle"],
            )
        else:
            if self._replacement_path is None:
                raise RuntimeError("请先选择替换文件。")
            replacement = self._replacement_path
            if asset_type == "texture":
                item = self.controller.add_texture_to_draft(
                    source,
                    replacement,
                    texture_name=selection["name"],
                    bundle_name=selection["bundle"],
                )
            elif asset_type == "dynamic":
                item = self.controller.add_dynamic_to_draft(
                    source, selection["name"], replacement, bundle_name=selection["bundle"],
                    animation_kind=selection.get("animation_kind"), fps=fps,
                )
            elif asset_type == "anim":
                try:
                    if replacement.suffix.lower() in {".animbin", ".bin"}:
                        item = self.controller.add_anim_to_draft(
                            source,
                            selection["name"],
                            raw_path=replacement,
                            bundle_name=selection["bundle"],
                        )
                    else:
                        item = self.controller.add_anim_to_draft(
                            source,
                            selection["name"],
                            image_path=replacement,
                            preview_texture=selection.get("preview_texture"),
                            bundle_name=selection["bundle"],
                        )
                except Exception:
                    self._replacement_path = None
                    raise
            else:
                raise RuntimeError("3D 模型目前只支持导出。")
        self._replacement_path = None
        return {"item": item, "draft": self._draft_state()}

    @exposed
    def get_draft(self) -> dict:
        return self._draft_state()

    @exposed
    def set_draft_name(self, name: str) -> dict:
        self.controller.set_draft_name(name)
        return self._draft_state()

    @exposed
    def get_draft_detail(self, index: int) -> dict:
        index = int(index)
        if not (0 <= index < len(self.controller.draft_items)):
            raise RuntimeError("作品集项不存在。")
        item = dict(self.controller.draft_items[index])
        result = {"index": index, "item": item, "original_data": "", "modified_data": ""}
        original = self.controller.original_bundle_path(item["bundle"])
        modified = self.controller._draft_dir() / item["bundle"]
        if original is None or not modified.exists():
            raise RuntimeError("找不到原资源或作品集文件。")
        if item.get("kind") == "emote":
            source_bundle = item.get("source_bundle")
            source = self.controller.original_bundle_path(source_bundle) if source_bundle else None
            if source is not None:
                image, _ = self.controller.preview_bundle(source, item["name"], tag="emote-cover")
                result["original_data"] = result["modified_data"] = self._image_data(image)
            from .modkit.emote_labels import video_label, emote_label
            try:
                result["emote_animation"] = self._emote_video_preview(item["video_name"], "draft")
            except (ValueError, OSError) as exc:
                result["emote_animation"] = {"frames": [], "notice": f"动图预览失败：{exc}；可返回制作页重新预览。"}
            records = self.controller.emote_workspace.context()["records"]
            original_name = next((row["video_name"] for row in records if row["emote_id"] == item.get("emote_id")), "")
            if original_name:
                try:
                    result["emote_original_animation"] = self._emote_video_preview(original_name, "original")
                except (ValueError, OSError) as exc:
                    result["emote_original_animation"] = {"frames": [], "notice": f"原动图预览失败：{exc}"}
            label = f"{emote_label(item['emote_id'])} · 自定义动图" if item.get("independent_video") else video_label(item["video_name"])
            result["emote_info"] = {"video_name": item["video_name"], "video_label": label,
                                    "independent_video": item.get("independent_video", False),
                                    "custom_video": item.get("custom_video", False),
                                    "preview_notice": result["emote_animation"].get("notice", "")}
        elif item.get("kind") == "visual_animation":
            source = self.controller.original_bundle_path(item["source_bundle"])
            if source is None:
                raise ValueError("动态资源的原图片缺失。")
            image, _ = self.controller.preview_bundle(source, item["name"], tag="visual-original")
            result["original_data"] = self._image_data(image)
            workspace = self.controller.visual_animation_workspace
            try:
                context = workspace.context()
                result["modified_animation"] = workspace.preview(item["name"], self.controller._draft_dir() / context["ui_bundle"], self.controller._draft_dir() / context["atlas_bundle"])
            except (OSError, ValueError, KeyError) as exc:
                result["modified_animation"] = {"frames": [], "notice": f"作品已保存，但动态预览未加载：{exc}。可以重新打开检查或移除这一项。"}
            result["visual_info"] = {"label": "动态卡面 / 立绘（试用）", "frame_count": item["frame_count"],
                                     "duration_ms": item["duration_ms"], "notice": result["modified_animation"]["notice"]}
        elif item.get("kind") == "text":
            result["original_text"] = self.controller.read_text(original, item["name"])
            result["modified_text"] = self.controller.read_text(modified, item["name"])
        elif item.get("kind") in ("dynamic", "anim") or item.get("from_anim"):
            kind = "sprite_clip" if item.get("kind") == "anim" or item.get("from_anim") else item.get("animation_kind", "sequence")
            fps = item.get("fps", 30)
            animation_name = item.get("from_anim") or item["name"]
            for label, path in (("original_animation", original), ("modified_animation", modified)):
                try:
                    result[label] = self._animation_preview(path, animation_name, kind, fps)
                except ValueError as exc:
                    result[label] = {"frames": [], "notice": str(exc)}
            if item.get("kind") == "texture" and item.get("from_anim") and any(
                not result[label].get("frames") for label in ("original_animation", "modified_animation")
            ):
                # 旧版动画图集草稿仍可对照像素，不能播放时明确保留为静态参考。
                original_image, modified_image = self.controller.draft_preview_paths(index)
                for side, image in (("original", original_image), ("modified", modified_image)):
                    if not result[f"{side}_animation"].get("frames"):
                        result[f"{side}_data"] = self._image_data(image)
        else:
            original, modified = self.controller.draft_preview_paths(index)
            result["original_data"] = self._image_data(original)
            result["modified_data"] = self._image_data(modified)
        return result

    @exposed
    def replace_draft_image(self, index: int) -> dict | None:
        index = int(index)
        if not (0 <= index < len(self.controller.draft_items)):
            raise RuntimeError("作品集项不存在。")
        if self.controller.draft_items[index].get("kind") in ("emote", "visual_animation"):
            raise RuntimeError("动态资源请在制作页更换素材，不能通过普通换图修改。")
        path = self._pick_path(
            "open",
            file_types=("图片 (*.png;*.jpg;*.jpeg;*.webp;*.bmp)", "所有文件 (*.*)"),
        )
        if path is None:
            return None
        item = self.controller.update_draft_texture(int(index), path)
        return {
            "item": item,
            "draft": self._draft_state(),
            "installed": self.controller.installed_mods(),
            "dashboard": self._dashboard_state(),
        }

    @exposed
    def pick_draft_crop_source(self, index: int) -> dict | None:
        """裁剪换图第一步：选图并返回预览 + 游戏原尺寸，供浏览器端框选。"""
        self._clear_draft_crop()
        index = int(index)
        if not (0 <= index < len(self.controller.draft_items)):
            raise RuntimeError("作品集项不存在。")
        if self.controller.draft_items[index].get("kind") in ("emote", "visual_animation"):
            raise RuntimeError("动态资源请在制作页更换素材，不能通过普通裁剪修改。")
        if self.controller.draft_items[index].get("from_anim"):
            raise RuntimeError("动画完整图集不能裁剪，请保留原图集尺寸和布局。")
        path = self._pick_path(
            "open",
            file_types=("图片 (*.png;*.jpg;*.jpeg;*.webp;*.bmp)", "所有文件 (*.*)"),
        )
        if path is None:
            return None
        item = self.controller.draft_items[index]
        target_w = target_h = 0
        orig_bundle = self.controller.original_bundle_path(item.get("bundle", ""))
        if orig_bundle:
            try:
                _png, info = self.controller.preview_bundle(orig_bundle, item.get("name"), tag="orig")
                if info:
                    target_w, target_h = info.width, info.height
            except Exception:
                pass
        from .modkit.maker import read_static_image
        with read_static_image(path):
            pass
        preview = self._image_data(path)
        self._draft_crop_path = path
        self._draft_crop_index = index
        self._draft_crop_item = deepcopy(item)
        return {
            "path": str(path),
            "name": path.name,
            "preview_data": preview,
            "target_width": target_w,
            "target_height": target_h,
        }

    def _clear_draft_crop(self) -> None:
        self._draft_crop_path = None
        self._draft_crop_index = None
        self._draft_crop_item = None

    @exposed
    def commit_draft_crop(self, crop_box: list | None = None) -> dict:
        """裁剪换图第二步：用框选区域写入作品集项。"""
        if self._draft_crop_path is None or self._draft_crop_index is None:
            raise RuntimeError("请先选择要裁剪的图片。")
        index = self._draft_crop_index
        path = self._draft_crop_path
        expected_item = self._draft_crop_item
        self._clear_draft_crop()
        if not (0 <= index < len(self.controller.draft_items)):
            raise RuntimeError("作品集项不存在。")
        if self.controller.draft_items[index] != expected_item:
            raise RuntimeError("作品集已变化，请重新选择要裁剪的项目和图片。")
        if self.controller.draft_items[index].get("kind") in ("emote", "visual_animation"):
            raise RuntimeError("动态资源请在制作页更换素材，不能通过普通裁剪修改。")
        if self.controller.draft_items[index].get("from_anim"):
            raise RuntimeError("动画完整图集不能裁剪，请保留原图集尺寸和布局。")
        box = tuple(int(v) for v in crop_box) if crop_box else None
        item = self.controller.update_draft_texture(index, path, crop_box=box)
        return {
            "item": item,
            "draft": self._draft_state(),
            "installed": self.controller.installed_mods(),
            "dashboard": self._dashboard_state(),
        }

    @exposed
    def remove_draft_item(self, index: int) -> dict:
        self.controller.remove_draft_item(int(index))
        return {
            "draft": self._draft_state(),
            "installed": self.controller.installed_mods(),
            "dashboard": self._dashboard_state(),
        }

    @exposed
    def clear_draft(self) -> dict:
        self.controller.clear_draft()
        return {
            "draft": self._draft_state(),
            "installed": self.controller.installed_mods(),
            "dashboard": self._dashboard_state(),
        }

    @exposed
    def export_draft(self) -> dict:
        output = self.controller.export_draft(as_zip=True)
        return {"path": str(output), "draft": self._draft_state()}

    @exposed
    def install_draft(self) -> dict:
        self.controller.install_draft()
        return {"installed": self.controller.installed_mods(), "dashboard": self._dashboard_state()}

    @exposed
    def quick_create_sfw_texture(self) -> dict:
        result = self.controller.quick_create_sfw_texture_mod()
        result["dashboard"] = self._dashboard_state()
        return result

    @exposed
    def build_index(self) -> dict:
        if not self.controller.has_game:
            raise RuntimeError("没有检测到游戏。")

        def progress(done: int, total: int) -> None:
            self._emit("index_progress", {"done": done, "total": total})

        def finished(count: int) -> None:
            self._emit(
                "index_done",
                {
                    "bundles": count,
                    "counts": self.controller.asset_type_counts(),
                },
            )

        self.controller.build_index_async(progress, finished)
        return {"started": True}

    @exposed
    def validate_dynamic(self) -> dict:
        if not self.controller.has_game:
            raise RuntimeError("没有检测到游戏。")

        def progress(done: int, total: int, current: str) -> None:
            self._emit(
                "dynamic_validate_progress",
                {"done": done, "total": total, "current": current},
            )

        def worker() -> None:
            try:
                result = self.controller.validate_dynamic_resources(progress)
                self._emit("dynamic_validate_done", result)
            except Exception as exc:
                self._emit("dynamic_validate_done", {"error": str(exc)})

        threading.Thread(target=worker, daemon=True).start()
        return {"started": True}

    @exposed
    def get_logs(self) -> list[str]:
        return list(self._logs)

    @exposed
    def clear_logs(self) -> list[str]:
        self._logs.clear()
        return []


def _set_windows_app_id() -> None:
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AstralParty.ModHelper.1.0"
        )
    except Exception:
        pass


def _free_port() -> int:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _build_server(api: "DesktopApi", api_token: str):
    """把 DesktopApi 的 @exposed 方法包成一个本地 HTTP 服务（前端用 fetch 调）。"""
    import bottle

    if not api_token:
        raise ValueError("本地 API token 不能为空。")

    web_root = str(WEB_DIR)
    app = bottle.Bottle()

    @app.hook("after_request")
    def _security_headers():
        bottle.response.set_header("Cache-Control", "no-store")
        bottle.response.set_header("Referrer-Policy", "no-referrer")
        bottle.response.set_header("X-Content-Type-Options", "nosniff")
        bottle.response.set_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; media-src 'self' blob:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )

    def _json_error(status: int, message: str) -> str:
        bottle.response.status = status
        bottle.response.content_type = "application/json"
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False)

    def _request_is_authorized() -> bool:
        supplied_token = bottle.request.get_header("X-JiXing-Token", "")
        if not hmac.compare_digest(supplied_token, api_token):
            return False

        origin = bottle.request.get_header("Origin")
        if not origin:
            return True
        expected_origin = f"http://{bottle.request.get_header('Host')}"
        return hmac.compare_digest(origin.rstrip("/"), expected_origin.rstrip("/"))

    @app.get("/")
    def _index():
        return bottle.static_file("index.html", root=web_root)

    @app.get("/audio-media/<handle>")
    def _audio_media(handle):
        if not _request_is_authorized():
            return _json_error(403, "请求未授权。")
        workspace = api._audio_workspace
        path = workspace.media_path(handle) if workspace else None
        if path is None or not path.is_file():
            return _json_error(404, "音频试听已失效，请重新选择资源。")
        return bottle.static_file(path.name, root=str(path.parent), mimetype="audio/wav")

    @app.get("/poll")
    def _poll():
        if not _request_is_authorized():
            return _json_error(403, "请求未授权。")
        bottle.response.content_type = "application/json"
        return json.dumps(api.poll_events(bottle.request.query.get("since", "0")), ensure_ascii=False)

    @app.post("/api/<name>")
    def _call(name):
        if not _request_is_authorized():
            return _json_error(403, "请求未授权。")
        if bottle.request.content_type.lower() != "application/json":
            return _json_error(415, "接口只接受 application/json。")

        bottle.response.content_type = "application/json"
        method = getattr(api, name, None)
        if method is None or not getattr(method, "_exposed", False):
            return _json_error(404, f"未知接口 {name}")
        try:
            args = bottle.request.json
        except Exception:
            return _json_error(400, "JSON 请求体无效。")
        if not isinstance(args, list):
            return _json_error(400, "JSON 请求体必须是参数数组。")
        return json.dumps(method(*args), ensure_ascii=False)

    @app.get("/<path:path>")
    def _static(path):
        return bottle.static_file(path, root=web_root)

    return app
