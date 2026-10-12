"""表情播放地址和视频组成一项草稿，保存失败时一起回退。"""
from __future__ import annotations

import copy
import gc
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import UnityPy

from .bundles import bundle_source_key
from .cri_emote import export_cri_emote_bundle, replace_cri_emote_bundle, validate_usm_file, _write_atomic
from .emote_catalog import discover_emote_locations, emote_id_from_texture
from .emote_config import inspect_emote_config_bundle, replace_emote_config_bundle
from .emote_labels import emote_label, video_label
from .maker import export_mod_pack


def item_bundle_names(item: dict) -> set[str]:
    names = [item.get("bundle"), *(item.get("extra_bundles") or [])]
    return {name for name in names if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]+\.bundle", name)}


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".emote-save-", suffix=".tmp", dir=target.parent)
    os.close(handle)
    pending = Path(name)
    try:
        shutil.copyfile(source, pending)
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


class EmoteWorkspace:
    def __init__(self, controller, data_dir: Path, draft_meta: Path):
        self.controller = controller
        self.data_dir = data_dir
        self.draft_meta = draft_meta
        self._locations = None
        self._source_key = None
        self._context_key = None
        self._context = None
        self._video_bundles = {}

    def _discard_temporary(self, folder: Path) -> None:
        try:
            shutil.rmtree(folder)
        except OSError:
            self.controller.log(f"临时目录未能清理，请关闭占用程序后处理：{folder}")

    @contextmanager
    def _prepare_folder(self, prefix: str):
        folder = Path(tempfile.mkdtemp(prefix=prefix, dir=self.data_dir))
        try:
            yield folder
        finally:
            self._discard_temporary(folder)

    def context(self) -> dict:
        self.controller._require_game()
        source_key = bundle_source_key(self.controller.aa_dirs)
        if source_key != self._source_key:
            self._locations = discover_emote_locations(self.controller.aa_dirs)
            self._source_key = source_key
            self._video_bundles = {}
        config_name = self._locations["config_bundle"]
        config_path = self.controller.original_bundle_path(config_name)
        if config_path is None:
            raise ValueError("找不到原始表情配置，请检查备份和游戏资源。")
        stat = config_path.stat()
        key = (source_key, str(config_path), stat.st_size, stat.st_mtime_ns)
        if key != self._context_key:
            records = inspect_emote_config_bundle(config_path)["records"]
            names = {row["video_name"] for row in records if row["video_name"]}
            if not names or not names <= self._locations["videos"].keys():
                raise ValueError("表情配置和视频目录不匹配，请更新游戏后重新制作。")
            self._context = {"config_bundle": config_name, "config_path": config_path,
                             "records": records, "videos": self._locations["videos"]}
            self._context_key = key
        return self._context

    def selection(self, texture_name: str) -> dict:
        identifier = emote_id_from_texture(texture_name)
        if identifier is None:
            return {"editable": False, "reason": "此贴图没有可验证的角色表情编号。"}
        try:
            context = self.context()
            record = next((row for row in context["records"] if row["emote_id"] == identifier), None)
            if record is None:
                raise ValueError("该表情不在已验证的角色表情配置中，当前只能替换静态图片。")
            choices = []
            native_names = {row["video_name"] for row in context["records"] if row["video_name"]}
            for name in sorted(native_names):
                affected = {row["emote_id"] for row in context["records"] if row["video_name"] == name}
                affected.update(item["emote_id"] for item in self.controller.draft_items
                                if item.get("kind") == "emote" and item.get("video_name") == name)
                affected_ids = sorted(affected)
                choices.append({"name": name, "label": video_label(name), "affected_ids": affected_ids,
                                "affected_labels": [emote_label(identifier) for identifier in affected_ids]})
            character = (identifier - 120_000_000) // 1000
            same_character = [name for name in sorted(native_names) if name.startswith(f"VEmoji_{character}_")]
            default_name = record["video_name"] or next(iter(same_character or sorted(native_names)))
            draft = next((item for item in self.controller.draft_items
                          if item.get("kind") == "emote" and item.get("emote_id") == identifier), {})
            return {"editable": True, "emote_id": identifier, "video_name": record["video_name"],
                    "default_video_name": default_name, "independent_supported": True,
                    "draft_video_name": draft.get("video_name", ""),
                    "draft_independent": draft.get("independent_video", False),
                    "choices": choices, "reason": "使用游戏原生视频播放；只改变本机资源，不改变表情拥有状态。"}
        except (OSError, ValueError, KeyError) as exc:
            return {"editable": False, "emote_id": identifier, "reason": str(exc)}

    def video_bundle(self, video_name: str) -> tuple[str, Path]:
        context = self.context()
        if video_name not in context["videos"]:
            raise ValueError("请选择当前游戏目录中已有的动态视频。")
        if video_name not in self._video_bundles:
            matches = []
            for bundle_name in context["videos"][video_name]:
                path = self.controller.original_bundle_path(bundle_name)
                if path is None or path.stat().st_size > 128 * 1024 * 1024:
                    raise ValueError("动态视频依赖缺失或资源包过大。")
                env = UnityPy.load(path.read_bytes(), path=str(path.parent))
                try:
                    count = sum(obj.type.name == "MonoBehaviour" and obj.peek_name() == video_name for obj in env.objects)
                    if count:
                        if count != 1:
                            raise ValueError("动态视频资源名称不唯一。")
                        matches.append(bundle_name)
                finally:
                    env = None
                    gc.collect()
            if len(matches) != 1:
                raise ValueError("无法唯一确定动态视频资源包。")
            self._video_bundles[video_name] = matches[0]
        name = self._video_bundles[video_name]
        path = self.controller.original_bundle_path(name)
        if path is None:
            raise ValueError("找不到原始动态视频。")
        return name, path

    def _selected_id(self) -> int:
        selection = self.controller.selection or {}
        if selection.get("asset_type") != "texture" or selection.get("category") != "emoji":
            raise ValueError("请先在「贴图 → 表情」中选择一项。")
        current = self.selection(selection.get("name", ""))
        if not current.get("editable"):
            raise ValueError(current.get("reason", "此表情暂不支持动态替换。"))
        return current["emote_id"]

    def _restore_video(self, item: dict, staged: dict, folder: Path) -> None:
        if item.get("independent_video"):
            for name in item_bundle_names(item) - {item.get("bundle")}:
                staged[name] = None
            return
        if not item.get("custom_video"):
            return
        bundle_name, original = self.video_bundle(item["video_name"])
        current = staged.get(bundle_name) or (self.controller._draft_dir() / bundle_name)
        if not current.exists():
            raise ValueError("动态表情草稿视频缺失，请先恢复草稿文件。")
        exported = folder / "restore.usm"
        export_cri_emote_bundle(original, item["video_name"], exported)
        output = folder / bundle_name
        replace_cri_emote_bundle(current, item["video_name"], exported, output)
        staged[bundle_name] = output

    def _commit(self, staged: dict[str, Path | None], items: list[dict]) -> None:
        pack = self.controller._draft_dir()
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+\.bundle", name) for name in staged):
            raise ValueError("表情草稿资源包名称不合法。")
        targets = [pack / name for name in staged] + [self.draft_meta, pack / "mod_info.json", pack / "emote_addresses.json"]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        recovery = Path(tempfile.mkdtemp(prefix="emote-change-", dir=self.data_dir))
        snapshots = []
        old_items = copy.deepcopy(self.controller.draft_items)
        keep_recovery = False
        try:
            for index, target in enumerate(targets):
                snapshot = recovery / str(index) if target.exists() else None
                if snapshot is not None:
                    shutil.copyfile(target, snapshot)
                snapshots.append((target, snapshot))
            (recovery / "recovery.json").write_text(json.dumps(
                [{"target": str(target), "copy": snapshot.name if snapshot else None} for target, snapshot in snapshots],
                ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                for name, source in staged.items():
                    if source is None:
                        (pack / name).unlink(missing_ok=True)
                    else:
                        _copy_atomic(source, pack / name)
                self.controller.draft_items = items
                self.controller._save_draft()
                if items:
                    export_mod_pack(pack, None, pack_name=self.controller.draft_name, items=items)
                else:
                    (pack / "mod_info.json").unlink(missing_ok=True)
                self.write_address_manifest(items)
            except Exception as exc:
                self.controller.draft_items = old_items
                failed = []
                for target, snapshot in snapshots:
                    try:
                        if snapshot is None:
                            target.unlink(missing_ok=True)
                        else:
                            _copy_atomic(snapshot, target)
                    except OSError:
                        failed.append(str(target))
                if failed:
                    keep_recovery = True
                    raise RuntimeError(f"保存失败，部分草稿未能回退。恢复副本保留在：{recovery}") from exc
                raise
        finally:
            if not keep_recovery:
                self._discard_temporary(recovery)

    def write_address_manifest(self, items: list[dict] | None = None) -> None:
        """从草稿派生独立视频地址；没有新增资源时不产生目录补丁。"""
        items = self.controller.draft_items if items is None else items
        pack = self.controller._draft_dir()
        patches = []
        for item in items:
            if not item.get("independent_video"):
                continue
            names = item.get("extra_bundles") or []
            if len(names) != 1 or names[0] not in item_bundle_names(item):
                raise ValueError("独立表情的视频资源记录不完整。")
            path = pack / names[0]
            if not path.is_file():
                raise ValueError("独立表情的草稿视频缺失。")
            patches.append({"emote_id": item["emote_id"], "video_name": item["video_name"],
                            "bundle_name": names[0], "template_video_key": item["reference_video"],
                            "bundle_size": path.stat().st_size})
        target = pack / "emote_addresses.json"
        if not patches:
            target.unlink(missing_ok=True)
            return
        payload = {"format": "jixing-emote-addresses", "version": 1, "patches": patches}
        _write_atomic(target, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    def save_independent(self, template_video: str, replacement_path: Path) -> dict:
        """给当前表情创建自己的视频资源，不替换模板或任何已有动图。"""
        from .emote_addressables import independent_emote_identity, create_independent_emote_bundle
        identifier = self._selected_id()
        context = self.context()
        _, reference = self.video_bundle(template_video)
        validate_usm_file(replacement_path, reference, template_video)
        identity = independent_emote_identity(identifier)
        if identity["video_name"] in context["videos"]:
            raise ValueError("当前游戏已经存在这个独立表情地址，请先禁用旧作品再制作。")
        others = [item for item in self.controller.draft_items
                  if not (item.get("kind") == "emote" and item.get("emote_id") == identifier)]
        existing = next((item for item in self.controller.draft_items
                         if item.get("kind") == "emote" and item.get("emote_id") == identifier), None)
        selection = self.controller.selection
        item = {"kind": "emote", "bundle": context["config_bundle"], "name": selection["name"],
                "source_bundle": selection["bundle"], "emote_id": identifier,
                "video_name": identity["video_name"], "reference_video": template_video,
                "custom_video": True, "independent_video": True, "extra_bundles": [identity["bundle_name"]],
                "note": Path(replacement_path).name, "at": datetime.now().isoformat(timespec="seconds")}
        items = others + [item]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._prepare_folder("emote-independent-") as folder:
            staged = {}
            if existing and existing.get("custom_video"):
                self._restore_video(existing, staged, folder)
            video_output = folder / identity["bundle_name"]
            create_independent_emote_bundle(reference, template_video, replacement_path, video_output, emote_id=identifier)
            staged[identity["bundle_name"]] = video_output
            config_name = context["config_bundle"]
            current = self.controller._draft_dir() / config_name
            source = current if current.exists() else context["config_path"]
            output = folder / config_name
            replace_emote_config_bundle(source, output, identifier, identity["video_name"])
            staged[config_name] = output
            active = set().union(*(item_bundle_names(entry) for entry in items))
            self._commit({name: source if name in active else None for name, source in staged.items()}, items)
        self.controller.log(f"已保存独立动态表情：{emote_label(identifier)}")
        return item

    def save(self, video_name: str, replacement_path: Path | None = None) -> dict:
        identifier = self._selected_id()
        context = self.context()
        video_bundle, original_video = self.video_bundle(video_name)
        others = [item for item in self.controller.draft_items
                  if not (item.get("kind") == "emote" and item.get("emote_id") == identifier)]
        existing = next((item for item in self.controller.draft_items
                         if item.get("kind") == "emote" and item.get("emote_id") == identifier), None)
        if replacement_path is not None and any(item.get("kind") == "emote" and item.get("custom_video")
                                                and item.get("video_name") == video_name for item in others):
            raise ValueError(f"{video_label(video_name)}已被另一项自定义表情占用，请选其它原生动画或先移除那一项。")
        if replacement_path is not None:
            validate_usm_file(replacement_path, original_video, video_name)
        selection = self.controller.selection
        item = {"kind": "emote", "bundle": context["config_bundle"], "name": selection["name"],
                "source_bundle": selection["bundle"], "emote_id": identifier, "video_name": video_name,
                "custom_video": replacement_path is not None,
                "extra_bundles": [video_bundle] if replacement_path is not None else [],
                "note": Path(replacement_path).name if replacement_path else f"原生动画：{video_label(video_name)}",
                "at": datetime.now().isoformat(timespec="seconds")}
        items = others + [item]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._prepare_folder("emote-prepare-") as folder:
            staged = {}
            if existing and existing.get("custom_video"):
                self._restore_video(existing, staged, folder)
            config_name = context["config_bundle"]
            current = self.controller._draft_dir() / config_name
            source = current if current.exists() else context["config_path"]
            output = folder / config_name
            replace_emote_config_bundle(source, output, identifier, video_name)
            staged[config_name] = output
            if replacement_path is not None:
                current_video = staged.get(video_bundle) or self.controller._draft_dir() / video_bundle
                source_video = current_video if current_video.exists() else original_video
                output_video = folder / video_bundle
                replace_cri_emote_bundle(source_video, video_name, replacement_path, output_video)
                staged[video_bundle] = output_video
            active = set().union(*(item_bundle_names(entry) for entry in items))
            self._commit({name: source if name in active else None for name, source in staged.items()}, items)
        self.controller.log(f"已保存动态表情：{emote_label(identifier)} → {video_label(video_name)}")
        return item

    def remove(self, index: int) -> None:
        item = self.controller.draft_items[index]
        if item.get("kind") != "emote":
            raise ValueError("这不是动态表情作品。")
        context = self.context()
        original = next((row for row in context["records"] if row["emote_id"] == item.get("emote_id")), None)
        if original is None or item.get("bundle") != context["config_bundle"]:
            raise ValueError("表情草稿与当前游戏版本不匹配，请保留旧备份并重新制作。")
        items = [entry for position, entry in enumerate(self.controller.draft_items) if position != index]
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._prepare_folder("emote-remove-") as folder:
            config_name = context["config_bundle"]
            current = self.controller._draft_dir() / config_name
            if not current.exists():
                raise ValueError("表情配置草稿缺失。")
            output = folder / config_name
            replace_emote_config_bundle(current, output, item["emote_id"], original["video_name"])
            staged = {config_name: output}
            self._restore_video(item, staged, folder)
            active = set().union(*(item_bundle_names(entry) for entry in items)) if items else set()
            self._commit({name: source if name in active else None for name, source in staged.items()}, items)
        self.controller.log(f"已移除动态表情：{emote_label(item['emote_id'])}")
