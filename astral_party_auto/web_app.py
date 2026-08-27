"""HTML 界面的本地接口层：窗口由 C# WebView2 承载，资源处理复用 ModController。"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
from collections import deque
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from .core.config import APP_ROOT, RESOURCE_ROOT
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
        self._pending_mod_path: Path | None = None
        self._replacement_path: Path | None = None
        self._draft_crop_path: Path | None = None
        self._draft_crop_index: int | None = None
        self._migration_source: Path | None = None
        self._migration_plans = []
        self._migration_warnings: list[str] = []
        # Mod 管理预览：{bundle文件名: Path}
        self._mod_preview_files: dict[str, Path] = {}
        self._mod_preview_title: str = ""
        self.controller = ModController(self._append_log)

    def set_tk_root(self, root) -> None:
        self._tk_root = root

    def _run_on_ui(self, func):
        """把需要在主线程执行的操作（tkinter 文件框）从 HTTP 线程调回主线程。"""
        if self._tk_root is None:
            return func()
        box: dict = {}
        done = threading.Event()

        def wrapper():
            try:
                box["value"] = func()
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        self._tk_root.after(0, wrapper)
        done.wait(180)
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
        if full_text and selection.get("asset_type") == "text":
            source = selection.get("original_path") or selection.get("bundle_path")
            payload["full_text"] = self.controller.read_text(source, selection["name"])
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
            "installed": self.controller.installed_mods(),
            "draft": self._draft_state(),
            "logs": list(self._logs),
            "asset_types": [{"id": type_id, "label": label} for type_id, label in ASSET_TYPES],
        }

    @exposed
    def refresh_detection(self) -> dict:
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
        # 装完后从 mod_store 继续预览
        store = DATA_DIR / "mod_store" / name
        bundles: list[str] = []
        if store.exists():
            files = {p.name: p for p in sorted(store.glob("*.bundle"))}
            bundles = self._set_mod_preview_files(files, f"已装 · {name}")
        return {
            "name": name,
            "matched": len(analysis.matched),
            "unmatched": len(analysis.unmatched),
            "installed": self.controller.installed_mods(),
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
        if store.exists():
            files = {p.name: p for p in store.glob("*.bundle")}
        if not files and self.controller.has_game and file_names:
            store.mkdir(parents=True, exist_ok=True)
            import shutil

            for fname in file_names:
                src = self.controller.bundle_path(fname)
                if src is None:
                    continue
                dst = store / fname
                try:
                    shutil.copy2(src, dst)
                    files[fname] = dst
                except OSError:
                    files[fname] = src
            info["store"] = str(store)
            state["mods"][name] = info
            manager._save_state(state)
        if not files and self.controller.has_game and file_names:
            for fname in file_names:
                p = self.controller.bundle_path(fname)
                if p is not None:
                    files[fname] = p
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

    @exposed
    def get_sequence_frames(self, bundle: str, name: str) -> dict:
        from astral_party_auto.modkit.bundles import read_bundle_asset_names
        from astral_party_auto.modkit.dynamic import (
            sequence_groups_from_names,
            sorted_sequence_names,
        )

        path = self.controller.original_bundle_path(bundle)
        if not path:
            raise RuntimeError(f"找不到资源包：{bundle}")
        names_by_type = read_bundle_asset_names(path)
        texture_names = names_by_type.get("sequence_frames") or names_by_type.get("texture") or []
        groups = [
            group for group in sequence_groups_from_names(texture_names)
            if group.base == name
        ]
        if not groups:
            raise RuntimeError("该资源不是序列帧动画组。")
        group = groups[0]
        frame_names = sorted_sequence_names(group.names)[:120]
        frames = []
        width = height = 0
        for frame_name in frame_names:
            png, info = self.controller.preview_bundle(
                path,
                frame_name,
                tag=f"seqframe_{bundle[:16]}",
            )
            if png:
                frames.append(self._image_data(png))
                if info:
                    width, height = info.width, info.height
        return {
            "frames": frames,
            "names": frame_names,
            "fps": 30,
            "width": width,
            "height": height,
            "total": group.frame_count,
            "truncated": group.frame_count > len(frame_names),
        }

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
        selection = self.controller.set_selection(
            bundle,
            name,
            asset_type=asset_type,
            force=bool(force),
        )
        return self._selection_payload(selection) or {}

    @exposed
    def refresh_selection(self) -> dict | None:
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
        return self._selection_payload(full_text=True)

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
    def choose_replacement(self) -> dict | None:
        selection = self.controller.selection
        if not selection:
            raise RuntimeError("请先选择资源。")
        asset_type = selection.get("asset_type") or "texture"
        if asset_type in ("texture", "anim"):
            file_types = (
                "图片或动画字节 (*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.animbin;*.bin)",
                "所有文件 (*.*)",
            )
        else:
            raise RuntimeError("当前类型不需要选择替换文件。")
        path = self._pick_path("open", file_types=file_types)
        if path is None:
            return None
        self._replacement_path = path
        preview_data = ""
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            preview_data = self._image_data(path)
        return {"path": str(path), "name": path.name, "size": path.stat().st_size, "preview_data": preview_data}

    @exposed
    def crop_replacement(self, crop_box: list) -> dict:
        """裁剪已选的替换图（浏览器端框选后调用）；裁剪结果覆盖为待提交文件。"""
        if self._replacement_path is None:
            raise RuntimeError("请先选择替换文件。")
        if self._replacement_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}:
            raise RuntimeError("当前文件不是图片，不能裁剪。")
        from PIL import Image

        box = tuple(int(v) for v in crop_box)
        cropped = Image.open(self._replacement_path).convert("RGBA").crop(box)
        out = DATA_DIR / "crop_preview.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(out)
        self._replacement_path = out
        return {"path": str(out), "name": out.name, "size": out.stat().st_size, "preview_data": self._image_data(out)}

    @exposed
    def commit_replacement(self, text_content: str = "") -> dict:
        selection = self.controller.selection
        if not selection:
            raise RuntimeError("请先选择资源。")
        asset_type = selection.get("asset_type") or "texture"
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
            elif asset_type == "anim":
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
        original, modified = self.controller.draft_preview_paths(index)
        return {
            "index": index,
            "item": dict(self.controller.draft_items[index]),
            "original_data": self._image_data(original),
            "modified_data": self._image_data(modified),
        }

    @exposed
    def replace_draft_image(self, index: int) -> dict | None:
        path = self._pick_path(
            "open",
            file_types=("图片 (*.png;*.jpg;*.jpeg;*.webp;*.bmp)", "所有文件 (*.*)"),
        )
        if path is None:
            return None
        item = self.controller.update_draft_texture(int(index), path)
        # “我的作品集”里的换图是面向已安装效果的操作：选完新图后
        # 立即重新安装当前作品集，避免用户还要再猜一次“安装到游戏”。
        self.controller.install_draft()
        return {
            "item": item,
            "draft": self._draft_state(),
            "installed": self.controller.installed_mods(),
            "dashboard": self._dashboard_state(),
        }

    @exposed
    def pick_draft_crop_source(self, index: int) -> dict | None:
        """裁剪换图第一步：选图并返回预览 + 游戏原尺寸，供浏览器端框选。"""
        index = int(index)
        if not (0 <= index < len(self.controller.draft_items)):
            raise RuntimeError("作品集项不存在。")
        path = self._pick_path(
            "open",
            file_types=("图片 (*.png;*.jpg;*.jpeg;*.webp;*.bmp)", "所有文件 (*.*)"),
        )
        if path is None:
            return None
        self._draft_crop_path = path
        self._draft_crop_index = index
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
        return {
            "path": str(path),
            "name": path.name,
            "preview_data": self._image_data(path),
            "target_width": target_w,
            "target_height": target_h,
        }

    @exposed
    def commit_draft_crop(self, crop_box: list | None = None) -> dict:
        """裁剪换图第二步：用框选区域写入作品集项。"""
        if self._draft_crop_path is None or self._draft_crop_index is None:
            raise RuntimeError("请先选择要裁剪的图片。")
        box = tuple(int(v) for v in crop_box) if crop_box else None
        item = self.controller.update_draft_texture(self._draft_crop_index, self._draft_crop_path, crop_box=box)
        self._draft_crop_path = None
        self._draft_crop_index = None
        self.controller.install_draft()
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


def _build_server(api: "DesktopApi"):
    """把 DesktopApi 的 @exposed 方法包成一个本地 HTTP 服务（前端用 fetch 调）。"""
    import bottle

    web_root = str(WEB_DIR)
    app = bottle.Bottle()

    @app.get("/")
    def _index():
        return bottle.static_file("index.html", root=web_root)

    @app.get("/poll")
    def _poll():
        bottle.response.content_type = "application/json"
        return json.dumps(api.poll_events(bottle.request.query.get("since", "0")), ensure_ascii=False)

    @app.post("/api/<name>")
    def _call(name):
        bottle.response.content_type = "application/json"
        method = getattr(api, name, None)
        if method is None or not getattr(method, "_exposed", False):
            bottle.response.status = 404
            return json.dumps({"ok": False, "error": f"未知接口 {name}"}, ensure_ascii=False)
        try:
            args = bottle.request.json
        except Exception:
            args = None
        if not isinstance(args, list):
            args = []
        return json.dumps(method(*args), ensure_ascii=False)

    @app.get("/<path:path>")
    def _static(path):
        return bottle.static_file(path, root=web_root)

    return app
