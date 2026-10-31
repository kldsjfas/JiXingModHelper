"""先核对 GIF 与静态表情映射，再一次提交整批独立动画。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from PIL import Image

from .animation import _decode_animation
from .catalog_mods import MAX_PATCHES, _official_hash, active_catalog_path
from .emote_addressables import independent_emote_identity
from .emote_catalog import MATERIAL_VIDEO_TYPE, emote_id_from_texture
from .emote_labels import emote_label
from .emote_workspace import EmoteWorkspace
from .material_emote import prepare_material_emote

MAX_BATCH_FILES = 64
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_BATCH_SOURCE_BYTES = 256 * 1024 * 1024
MAX_BATCH_PIXELS = 1_024_000_000
MAX_DRAFT_BYTES = 512 * 1024 * 1024
MAX_DRAFT_FILES = 512
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_DURATION_MS = 30_000
MAX_PENDING_SECONDS = 3600
_BUNDLE_NAME = re.compile(r"[A-Za-z0-9_.-]+\.bundle\Z")
_NUMERIC_NAME = re.compile(r"12[0-9]{7}\Z")


def _regular(path: Path, limit: int) -> None:
    if path is None:
        raise ValueError("所需资源文件已经丢失，请重新预检。")
    attributes = path.stat(follow_symlinks=False)
    if (not stat.S_ISREG(attributes.st_mode)
            or getattr(attributes, "st_file_attributes", 0) & 0x400
            or not 0 < attributes.st_size <= limit):
        raise ValueError(f"文件缺失、是链接或超过大小限制：{path.name}")


def _file_hash(path: Path, limit: int = MAX_BUNDLE_BYTES) -> str:
    _regular(path, limit)
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _suggested_id(name: str) -> int | None:
    stem = Path(name).stem
    if _NUMERIC_NAME.fullmatch(stem):
        return int(stem)
    return emote_id_from_texture(stem)


def _inspect_gif(path: Path) -> dict:
    if path.suffix.casefold() != ".gif":
        raise ValueError(f"批量表情只接受 GIF 动图：{path.name}")
    _regular(path, MAX_SOURCE_BYTES)
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES or not raw.startswith((b"GIF87a", b"GIF89a")):
        raise ValueError(f"文件不是受支持的 GIF：{path.name}")
    try:
        frames, durations, kind = _decode_animation(raw)
        if kind != "gif":
            raise ValueError("不是 GIF 动图")
        duration = sum(durations)
        if not 1000 / 30 <= duration <= MAX_DURATION_MS:
            raise ValueError("GIF 时长需要在 1/30 秒到 30 秒之间")
        signatures = set()
        visible = False
        for frame in frames:
            # 透明像素的隐藏 RGB 变化不算可见动画。
            normalized = Image.alpha_composite(Image.new("RGBA", frame.size), frame)
            visible |= normalized.getchannel("A").getbbox() is not None
            signatures.add(hashlib.sha256(normalized.tobytes()).digest())
        if not visible or len(signatures) < 2:
            raise ValueError("没有可见的逐帧变化，不能把静态图当作动态表情")
        result = {"name": path.name, "path": str(path), "size": len(raw),
                  "sha256": hashlib.sha256(raw).hexdigest(), "frames": len(frames),
                  "width": frames[0].width, "height": frames[0].height,
                  "duration_ms": duration, "pixels": sum(frame.width * frame.height for frame in frames),
                  "suggested_emote_id": _suggested_id(path.name)}
    except (OSError, EOFError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"GIF 无法作为动态表情导入：{path.name}（{exc}）") from exc
    if _file_hash(path, MAX_SOURCE_BYTES) != result["sha256"]:
        raise ValueError(f"GIF 在检查期间改变，请重新选择：{path.name}")
    return result


class _StagedController:
    """沿用已验证的制作器，但所有草稿写入都留在本次临时目录。"""

    def __init__(self, source, directory: Path):
        self.pack = directory / "pack"
        self.pack.mkdir()
        self.data_dir = directory / "data"
        self.data_dir.mkdir()
        self.meta = self.data_dir / "draft_pack.json"
        self.draft_items = copy.deepcopy(source.draft_items)
        self.draft_name = source.draft_name
        self.selection = None
        self.log = lambda message: None

    def _draft_dir(self) -> Path:
        return self.pack

    def _save_draft(self) -> None:
        self.meta.write_text(json.dumps({"name": self.draft_name, "items": self.draft_items},
                                        ensure_ascii=False), encoding="utf-8")


class _StagedWorkspace(EmoteWorkspace):
    def __init__(self, controller: _StagedController, original: EmoteWorkspace):
        super().__init__(controller, controller.data_dir, controller.meta)
        self.original = original

    def context(self) -> dict:
        return self.original.context()

    def video_bundle(self, video_name: str) -> tuple[str, Path]:
        return self.original.video_bundle(video_name)

    def require_independent(self, video_name: str | None = None) -> None:
        self.original.require_independent(video_name)

    def independent_support(self) -> dict:
        return self.original.independent_support()


class EmoteBatchWorkspace:
    def __init__(self, controller):
        self.controller = controller
        self._pending: dict | None = None
        self._running = False
        self._lock = threading.RLock()

    def _log(self, message: str) -> None:
        try:
            self.controller.log(message)
        except Exception:
            # 保存结果不依赖日志窗口；窗口关闭不能把成功提交误报成失败。
            pass

    @contextmanager
    def _staging_directory(self, parent: Path):
        temporary = tempfile.TemporaryDirectory(prefix="emote-batch-", dir=parent)
        try:
            yield Path(temporary.name)
        finally:
            try:
                temporary.cleanup()
            except OSError as exc:
                self._log(f"批量表情暂存未能清理，原素材仍保留：{temporary.name}（{exc}）")

    def _catalog_stamp(self) -> dict:
        workspace = self.controller.emote_workspace
        context = workspace.context()
        target = active_catalog_path(self.controller.aa_dirs, self.controller.game_executable)
        return {"game": str(Path(self.controller.game_executable).resolve()),
                "index": repr(getattr(self.controller, "_index_source_key", "")),
                "source": repr(context["source_key"]), "catalog": str(target.resolve()),
                "catalog_sha256": _file_hash(target, 32 * 1024 * 1024),
                "official_hash": _official_hash(target),
                "config_path": str(Path(context["config_path"]).resolve()),
                "config_sha256": _file_hash(Path(context["config_path"]))}

    def _draft_stamp(self) -> dict:
        directory = self.controller._draft_dir()
        attributes = directory.stat(follow_symlinks=False)
        if (not stat.S_ISDIR(attributes.st_mode)
                or getattr(attributes, "st_file_attributes", 0) & 0x400):
            raise ValueError("作品集目录不可用或是链接，请先恢复作品集目录。")
        files = {}
        total = 0
        for path in sorted(directory.iterdir(), key=lambda entry: entry.name):
            if path.name not in {"mod_info.json", "emote_addresses.json"} and not path.name.endswith(".bundle"):
                continue
            if path.name.endswith(".bundle") and not _BUNDLE_NAME.fullmatch(path.name):
                raise ValueError("作品集存在无法安全暂存的资源包名称。")
            total += path.stat(follow_symlinks=False).st_size
            if len(files) >= MAX_DRAFT_FILES or total > MAX_DRAFT_BYTES:
                raise ValueError("当前作品集过大，批量导入暂不能安全暂存；请先导出并另建作品集。")
            files[path.name] = _file_hash(path)
        metadata = self.controller.emote_workspace.draft_meta
        return {"directory": str(directory.resolve()), "files": files,
                "metadata": _file_hash(metadata, 16 * 1024 * 1024) if metadata.exists() else None,
                "name": self.controller.draft_name,
                "items": copy.deepcopy(self.controller.draft_items)}

    def _targets(self) -> tuple[list[dict], list[dict]]:
        context = self.controller.emote_workspace.context()
        texture_index = self.controller.typed_index.get("texture") or {}
        if not texture_index:
            raise ValueError("请先在「浏览资源」刷新索引，再批量导入 GIF。")
        images: dict[int, set[tuple[str, str]]] = {}
        for bundle, names in texture_index.items():
            for name in names:
                identifier = emote_id_from_texture(name)
                if identifier is not None:
                    images.setdefault(identifier, set()).add((bundle, name))
        # catalog 可能叠加了其它已装独立表情；分享包只能借用原始配置
        # 实际引用的原生模板，不能继续依赖别人的 *_999 自定义资源。
        native_names = {record["video_name"] for record in context["records"]
                        if record["video_name"] and not record["video_name"].endswith("_999")}
        materials = sorted(name for name in native_names if name in context["videos"]
                           and context.get("video_types", {}).get(name) == MATERIAL_VIDEO_TYPE)
        if not materials:
            raise ValueError("当前游戏没有已完整下载、可核对的原生材质序列图模板，暂不支持批量 GIF 导入。")
        targets, unavailable = [], []
        for record in context["records"]:
            if record["video_name"]:
                continue
            identifier, character = record["emote_id"], record["character_id"]
            if (identifier - 120_000_000) // 1000 != character:
                raise ValueError("角色表情编号与配置归属不一致，已停止批量导入。")
            matches = images.get(identifier, set())
            reason = ""
            if len(matches) != 1:
                reason = "原静态贴图缺失或目标不唯一，请刷新资源索引"
            elif independent_emote_identity(identifier)["video_name"] in context["videos"]:
                reason = "此独立地址已在游戏中存在，请先禁用对应旧作品再制作"
            if reason:
                unavailable.append({"emote_id": identifier, "label": emote_label(identifier), "reason": reason})
                continue
            bundle, name = next(iter(matches))
            original = self.controller.original_bundle_path(bundle)
            if original is None or not original.is_file():
                unavailable.append({"emote_id": identifier, "label": emote_label(identifier), "reason": "原静态资源包缺失"})
                continue
            same_character = [video for video in materials if video.startswith(f"VEmoji_{character}_")]
            targets.append({"emote_id": identifier, "character_id": character,
                            "label": emote_label(identifier), "character": emote_label(identifier).split(" · ", 1)[0],
                            "name": name, "bundle": bundle,
                            "template_video": (same_character or materials)[0]})
        return sorted(targets, key=lambda row: row["emote_id"]), unavailable

    def _reference_stamps(self, targets: list[dict]) -> dict:
        context = self.controller.emote_workspace.context()
        references, total = {}, 0
        for video in sorted({target["template_video"] for target in targets}):
            self.controller.emote_workspace.require_independent(video)
            dependencies = context["videos"][video]
            if len(dependencies) != 1:
                raise ValueError("批量导入只接受已验证的单包材质模板，请单独选择支持的表情。")
            path = self.controller.original_bundle_path(dependencies[0])
            digest = _file_hash(path)
            total += path.stat().st_size
            if total > MAX_DRAFT_BYTES:
                raise ValueError("本次表情模板校验总量过大，请先核对游戏资源目录。")
            references[video] = {"path": path, "sha256": digest}
        return references

    def inspect(self, paths) -> dict:
        with self._lock:
            if self._running:
                raise ValueError("已有一批 GIF 正在处理，请等待完成。")
            self._pending = None
            if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= MAX_BATCH_FILES:
                raise ValueError("一次请选择 1 到 64 个 GIF 动图。")
            self.controller._require_game()
            self.controller.emote_workspace.require_independent()
            catalog = self._catalog_stamp()
            draft = self._draft_stamp()
            sources, seen, total_bytes, total_pixels = [], set(), 0, 0
            for value in paths:
                path = Path(value).expanduser()
                # 保留原入口检查链接；不能先 resolve 后把链接伪装成普通文件。
                _regular(path, MAX_SOURCE_BYTES)
                path = path.absolute()
                key = str(path.resolve()).casefold()
                if key in seen:
                    raise ValueError("同一 GIF 被重复选择，请移除重复文件。")
                seen.add(key)
                source = _inspect_gif(path)
                total_bytes += source["size"]
                total_pixels += source["pixels"]
                if total_bytes > MAX_BATCH_SOURCE_BYTES or total_pixels > MAX_BATCH_PIXELS:
                    raise ValueError("本批 GIF 的总大小或解码总像素过大，请分批导入。")
                source["source_id"] = uuid.uuid4().hex
                sources.append(source)
            targets, unavailable = self._targets()
            if not targets:
                raise ValueError("当前资源索引中没有可核对的静态角色表情，请刷新索引并检查游戏资源。")
            references = self._reference_stamps(targets)
            known = {target["emote_id"] for target in targets}
            for source in sources:
                if source["suggested_emote_id"] not in known:
                    source["suggested_emote_id"] = None
            if (catalog != self._catalog_stamp() or draft != self._draft_stamp()
                    or any(_file_hash(reference["path"]) != reference["sha256"] for reference in references.values())):
                raise ValueError("检查期间游戏资源或作品集发生变化，请重新预检。")
            token = uuid.uuid4().hex
            self._pending = {"token": token, "created": time.monotonic(), "catalog": catalog, "draft": draft,
                             "sources": {source["source_id"]: source for source in sources},
                             "targets": {target["emote_id"]: target for target in targets},
                             "references": references,
                             "previews": {}}
            counts: dict[int, dict] = {}
            for target in targets:
                row = counts.setdefault(target["character_id"], {"character_id": target["character_id"],
                                        "label": target["character"], "count": 0})
                row["count"] += 1
            return {"token": token, "sources": copy.deepcopy(sources), "targets": copy.deepcopy(targets),
                    "characters": list(counts.values()), "unavailable_targets": unavailable,
                    "total_bytes": total_bytes, "draft_count": len(self.controller.draft_items),
                    "notice": "规范文件名只提示目标编号，不判断表情含义。请逐项对照原图与 GIF，确认角色和表达一致后再提交。"}

    def _checked_pending(self, token: str) -> dict:
        pending = self._pending
        if (not isinstance(token, str) or not pending or pending["token"] != token
                or time.monotonic() - pending["created"] > MAX_PENDING_SECONDS):
            raise ValueError("批量导入预检已取消、过期或被替换，请重新选择 GIF。")
        if pending["catalog"] != self._catalog_stamp() or pending["draft"] != self._draft_stamp():
            raise ValueError("游戏资源或作品集已经变化，请重新预检再确认映射。")
        for source in pending["sources"].values():
            if _file_hash(Path(source["path"]), MAX_SOURCE_BYTES) != source["sha256"]:
                raise ValueError(f"GIF 内容已经变化，请重新预检：{source['name']}")
        for reference in pending["references"].values():
            if _file_hash(reference["path"]) != reference["sha256"]:
                raise ValueError("动画模板内容已经变化，请重新预检。")
        for preview in pending["previews"].values():
            if _file_hash(Path(preview["path"])) != preview["sha256"]:
                raise ValueError("已经预览的原静态表情发生变化，请重新预检。")
        return pending

    def preview(self, token: str, source_id: str, emote_id: int) -> dict:
        with self._lock:
            if self._running:
                raise ValueError("已有一批 GIF 正在处理，请等待完成。")
            pending = self._checked_pending(token)
            if (not isinstance(source_id, str) or source_id not in pending["sources"]
                    or type(emote_id) is not int or emote_id not in pending["targets"]):
                raise ValueError("请选择本次预检中的 GIF 和静态表情目标。")
            source = pending["sources"][source_id]
            target = pending["targets"][emote_id]
            original = self.controller.original_bundle_path(target["bundle"])
            digest = _file_hash(original)
            image, _ = self.controller.preview_bundle(original, target["name"], tag="emote-batch-original", force=True)
            if image is None or not Path(image).is_file() or _file_hash(original) != digest:
                raise ValueError("原静态表情无法完整预览，请重新检查资源。")
            pending["previews"][emote_id] = {"path": str(original), "sha256": digest}
            return {"source_path": source["path"], "original_path": str(image),
                    "source": copy.deepcopy(source), "target": copy.deepcopy(target)}

    def commit(self, token: str, mappings: list[dict], *, progress: Callable[[dict], None] | None = None) -> dict:
        with self._lock:
            if self._running:
                raise ValueError("已有一批 GIF 正在处理，请等待完成。")
            pending = self._checked_pending(token)
            if not isinstance(mappings, list) or len(mappings) != len(pending["sources"]):
                raise ValueError("请为本批每个 GIF 明确选择一个静态表情目标。")
            chosen, source_ids, target_ids = [], set(), set()
            for mapping in mappings:
                if not isinstance(mapping, dict) or set(mapping) != {"source_id", "emote_id"}:
                    raise ValueError("批量表情映射格式不正确。")
                source_id, identifier = mapping["source_id"], mapping["emote_id"]
                if (not isinstance(source_id, str) or source_id not in pending["sources"]
                        or type(identifier) is not int or identifier not in pending["targets"]):
                    raise ValueError("映射包含未知 GIF 或非静态表情目标。")
                if source_id in source_ids or identifier in target_ids:
                    raise ValueError("同一 GIF 或静态表情目标不能重复映射。")
                source, target = pending["sources"][source_id], pending["targets"][identifier]
                declared = _suggested_id(source["name"])
                if declared is not None and declared != identifier:
                    raise ValueError(f"规范文件名与所选目标不一致：{source['name']}；请更名后重新预检。")
                chosen.append((source, target))
                source_ids.add(source_id)
                target_ids.add(identifier)
            if source_ids != set(pending["sources"]):
                raise ValueError("本批 GIF 映射不完整。")
            existing_ids = {item.get("emote_id") for item in self.controller.draft_items
                            if item.get("independent_video")}
            if len(existing_ids | target_ids) > MAX_PATCHES:
                raise ValueError("同一作品集最多包含 128 个独立动态表情，请先导出并另建作品集。")
            self._running = True
            # 一次确认只能消费一次，失败后需要重新预检，不能复用旧目标。
            self._pending = None
        try:
            original_workspace = self.controller.emote_workspace
            references = {}
            for _, target in chosen:
                video = target["template_video"]
                original_workspace.require_independent(video)
                _, path = original_workspace.video_bundle(video)
                expected = pending["references"][video]
                if Path(path).resolve() != expected["path"].resolve() or _file_hash(path) != expected["sha256"]:
                    raise ValueError("动画模板内容已经变化，请重新预检。")
                references[video] = expected
            directory = original_workspace.data_dir
            directory.mkdir(parents=True, exist_ok=True)
            with self._staging_directory(directory) as temporary:
                stage_controller = _StagedController(self.controller, temporary)
                stage_workspace = _StagedWorkspace(stage_controller, original_workspace)
                live_pack = self.controller._draft_dir()
                for name in pending["draft"]["files"]:
                    shutil.copyfile(live_pack / name, stage_controller.pack / name)
                prepared_items = []
                for index, (source, target) in enumerate(chosen):
                    if progress:
                        progress({"done": index, "total": len(chosen), "stage": "convert", "label": target["label"]})
                    if _file_hash(Path(source["path"]), MAX_SOURCE_BYTES) != source["sha256"]:
                        raise ValueError(f"GIF 在转换期间改变：{source['name']}")
                    identity = independent_emote_identity(target["emote_id"])
                    output = temporary / identity["bundle_name"]
                    reference = references[target["template_video"]]
                    if _file_hash(reference["path"]) != reference["sha256"]:
                        raise ValueError("原生动画模板在转换期间改变，请重新预检。")
                    prepare_material_emote(source["path"], reference["path"], target["template_video"], output,
                                           emote_id=target["emote_id"], work_dir=temporary)
                    stage_controller.selection = {"asset_type": "texture", "category": "emoji",
                                                  "name": target["name"], "bundle": target["bundle"]}
                    item = stage_workspace.save_independent(target["template_video"], output)
                    item["note"] = source["name"]
                    prepared_items.append(item)
                    total = sum(path.stat().st_size for path in stage_controller.pack.glob("*.bundle"))
                    if len(list(stage_controller.pack.iterdir())) > MAX_DRAFT_FILES:
                        raise ValueError("转换后作品集超过 512 个文件，已保留原作品集；请另建作品集后导入。")
                    if total > MAX_DRAFT_BYTES:
                        raise ValueError("转换成品总量超过 512 MB，已保留原作品集；请减少帧数或分批制作。")
                if progress:
                    progress({"done": len(chosen), "total": len(chosen), "stage": "verify", "label": "核对全部成品与原作品集"})
                self._verify_before_commit(pending, references)
                staged = {}
                for path in stage_controller.pack.glob("*.bundle"):
                    if _file_hash(path) != pending["draft"]["files"].get(path.name):
                        staged[path.name] = path
                for name in pending["draft"]["files"]:
                    if name.endswith(".bundle") and not (stage_controller.pack / name).exists():
                        staged[name] = None
                original_workspace._commit(staged, stage_controller.draft_items)
            self._log(f"批量保存完成：{len(prepared_items)} 个 GIF 已转为独立动态表情；本次未修改游戏文件。")
            return {"items": copy.deepcopy(prepared_items), "count": len(prepared_items),
                    "characters": sorted({target["character_id"] for _, target in chosen}),
                    "notice": "整批已保存到作品集，可逐项查看成品，再统一导出或安装。"}
        finally:
            with self._lock:
                self._running = False

    def _verify_before_commit(self, pending: dict, references: dict) -> None:
        if pending["catalog"] != self._catalog_stamp() or pending["draft"] != self._draft_stamp():
            raise ValueError("转换期间游戏资源或作品集发生变化，已停止提交并保留原作品集。")
        for source in pending["sources"].values():
            if _file_hash(Path(source["path"]), MAX_SOURCE_BYTES) != source["sha256"]:
                raise ValueError("转换期间 GIF 内容变化，已停止提交并保留原作品集。")
        for reference in references.values():
            if _file_hash(reference["path"]) != reference["sha256"]:
                raise ValueError("转换期间动画模板变化，已停止提交并保留原作品集。")
        for preview in pending["previews"].values():
            if _file_hash(Path(preview["path"])) != preview["sha256"]:
                raise ValueError("转换期间原静态表情变化，已停止提交并保留原作品集。")
