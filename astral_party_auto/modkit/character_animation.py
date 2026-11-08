"""制作可由 Toys / CharacterSkinMod 读取的角色精灵动画包。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import threading
import zipfile
from copy import deepcopy
from pathlib import Path, PurePosixPath

from PIL import Image, UnidentifiedImageError

from .animation import (
    MAX_FILE_BYTES, MAX_FRAME_PIXELS, MAX_SOURCE_FRAMES, MAX_TOTAL_PIXELS,
    _preview, _preview_indices, _read_replacement,
)
from .emote_media import adapt_video_source, detect_emote_media


FORMAT_SOURCE = "https://github.com/higashitaniyume/CesiumLoader/blob/e8a598389b9f510fda66d830ede4dd5137f06989/shared/CharacterSkins/CharacterSkinManifest.cs"
RUNTIME_SOURCE = "https://github.com/higashitaniyume/CesiumLoader/blob/e8a598389b9f510fda66d830ede4dd5137f06989/mods/CharacterSkinMod/ActorPlayback.cs"
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_PROJECT_PIXELS = 192_000_000
ACTION_CHOICES = [
    ("Idle", "待机"), ("Walk", "正向行走"), ("Walk-back", "背向行走"),
    ("Fight", "战斗"), ("Show", "展示"), ("Talent", "天赋"),
    ("Hit", "受击"), ("Hit-01", "受击变体 1"), ("Hit-02", "受击变体 2"),
    ("Cheer", "欢呼"), ("Cry", "哭泣"), ("Die", "倒地"), ("Eat", "进食"),
    ("Hospitalized", "住院"), ("Vomit", "呕吐"), ("Lose", "失败"), ("Electricshock", "触电"),
]
NOTICE = (
    "试用。导出的角色动画包需要 CesiumLoader 与 CharacterSkinMod，导入 Toys 后加入角色方案并应用。"
    "目标为该角色的全部皮肤。动作名必须与原角色实际动作一致，未填写动作可能播放待机或保留上一张自定义帧。"
    "建议补齐待机、行走及角色实际使用的战斗动作，具体动作名以原版为准。游戏内效果仍需实测。"
)
DEFAULT_LAYOUT = {"PivotX": 0.5, "PivotY": 0.1, "PixelsPerUnit": 100.0,
                  "Scale": 1.0, "HudScale": 1.0, "HudOffsetX": 0.0, "HudOffsetY": 0.0}
_ROOT_LOCKS: dict[str, threading.RLock] = {}
_ROOT_LOCK_GUARD = threading.Lock()


def _role_id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[1-9]\d{0,9}", str(value)):
        raise ValueError("角色 ID 必须是正整数。")
    result = int(value)
    if result > 2_147_483_647:
        raise ValueError("角色 ID 超过支持范围。")
    return result


def _action_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("动作名需为 1～80 位英文字母、数字、下划线或短横线，并以字母开头。")
    return next((action for action, _label in ACTION_CHOICES if action.lower() == value.lower()), value)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    handle, temporary = tempfile.mkstemp(prefix=".project-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        if json.loads(Path(temporary).read_text(encoding="utf-8")) != payload:
            raise ValueError("角色工程清单回读失败，原工程已保留。")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class CharacterAnimationWorkspace:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with _ROOT_LOCK_GUARD:
            self._lock = _ROOT_LOCKS.setdefault(str(self.root).casefold(), threading.RLock())

    def _folder(self, role_id: int) -> Path:
        folder = self.root / f"hero-{_role_id(role_id)}"
        if folder.exists() and (folder.is_symlink() or folder.resolve().parent != self.root):
            raise ValueError("角色工程目录不属于当前工作台。")
        return folder

    def _new_project(self, role_id: int) -> dict:
        return {
            "schema_version": 1, "role_id": role_id, "sources": {},
            "manifest": {"SchemaVersion": 1, "Id": f"jixing-hero-{role_id}",
                         "Name": f"角色 {role_id} 自定义动画", "Author": "", "Description": NOTICE,
                         "HeroId": role_id, "SkinItemId": 0, "Images": {}, "Texts": {},
                         "Animations": {}, "SpriteMappings": {}, "UseOriginalSpriteLayout": False,
                         "Layout": dict(DEFAULT_LAYOUT), "Media": {}},
        }

    def _load(self, role_id: int) -> dict:
        path = self._folder(role_id) / "project.json"
        if not path.exists():
            return self._new_project(role_id)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("角色工程清单过大，请保留工程后检查文件。")
        project = json.loads(path.read_text(encoding="utf-8"))
        if (project.get("schema_version") != 1 or project.get("role_id") != role_id
                or not isinstance(project.get("sources"), dict)):
            raise ValueError("角色工程版本或角色 ID 不匹配。")
        self._validate(project, images=False)
        return project

    def _frame_path(self, role_id: int, relative: str) -> Path:
        if not isinstance(relative, str):
            raise ValueError("动画帧路径需为文本。")
        parts = PurePosixPath(relative)
        if (relative != parts.as_posix() or "\\" in relative or ":" in relative
                or parts.is_absolute() or ".." in parts.parts or len(parts.parts) != 3
                or parts.parts[0] != "animations" or not re.fullmatch(r"[A-Za-z0-9_-]+-[0-9a-f]{32}", parts.parts[1])
                or not re.fullmatch(r"\d{4}\.png", parts.parts[2])):
            raise ValueError("动画帧路径不属于当前角色工程。")
        folder = self._folder(role_id)
        path = folder.joinpath(*parts.parts)
        if not path.resolve().is_relative_to(folder.resolve()) or any(parent.is_symlink() for parent in (path, path.parent, path.parent.parent)):
            raise ValueError("动画帧路径越界。")
        return path

    def _validate(self, project: dict, *, images: bool) -> dict[str, Path]:
        role_id = _role_id(project["role_id"])
        manifest = project["manifest"]
        if manifest.get("SchemaVersion") != 1 or manifest.get("HeroId") != role_id or manifest.get("SkinItemId") != 0:
            raise ValueError("角色动画清单不符合兼容格式。")
        if any(not isinstance(manifest.get(key), dict) for key in ("Images", "Texts", "SpriteMappings", "Media", "Layout")):
            raise ValueError("角色动画清单的资源或布局字段无效。")
        layout = manifest["Layout"]
        for key, default in DEFAULT_LAYOUT.items():
            value = layout.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("角色布局包含无效数值。")
            if key.startswith("Pivot") and not 0 <= value <= 1:
                raise ValueError("角色锚点需位于 0～1。")
            if key == "PixelsPerUnit" and not 1 <= value <= 10000:
                raise ValueError("角色每单位像素需位于 1～10000。")
            if key in {"Scale", "HudScale"} and not 0.01 <= value <= 20:
                raise ValueError("角色缩放需位于 0.01～20。")
        actions = manifest.get("Animations")
        if not isinstance(actions, dict) or len(actions) > 80:
            raise ValueError("动作列表无效或超过 80 项。")
        files, total_frames, total_bytes, total_pixels = {}, 0, 0, 0
        seen = set()
        for action, animation in actions.items():
            _action_name(action)
            if action.casefold() in seen:
                raise ValueError("动作名称大小写重复。")
            seen.add(action.casefold())
            frames, durations = animation.get("Frames"), animation.get("DurationsMs")
            fps = animation.get("Fps")
            if (not isinstance(frames, list) or not 1 <= len(frames) <= MAX_SOURCE_FRAMES
                    or not isinstance(durations, list) or len(durations) != len(frames)
                    or isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 1 <= fps <= 120
                    or not isinstance(animation.get("Loop"), bool)
                    or any(type(duration) is not int or not 1 <= duration <= 60000 for duration in durations)):
                raise ValueError(f"动作 {action} 的帧数、时长或帧率无效。")
            total_frames += len(frames)
            for relative in frames:
                path = self._frame_path(role_id, relative)
                if relative in files:
                    raise ValueError("工程中存在重复的帧文件引用。")
                if not path.is_file():
                    raise ValueError(f"动画帧缺失：{path.name}")
                files[relative] = path
                total_bytes += path.stat().st_size
                if images:
                    with Image.open(path) as image:
                        if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                            raise ValueError("工程帧需为静态 PNG。")
                        if min(image.size) < 1 or max(image.size) > 16384 or image.width * image.height > MAX_FRAME_PIXELS:
                            raise ValueError("工程帧尺寸超过支持范围。")
                        total_pixels += image.width * image.height
                        image.load()
        if total_frames > 10000 or total_bytes > MAX_PACKAGE_BYTES or total_pixels > MAX_PROJECT_PIXELS:
            raise ValueError("角色工程超过 10000 帧、256 MiB 图片或 1.92 亿总像素，请缩小素材或减少动作。")
        return files

    def status(self, role_id: int = 0) -> dict:
        with self._lock:
            roles = sorted(int(path.name[5:]) for path in self.root.glob("hero-*")
                           if re.fullmatch(r"hero-[1-9]\d{0,9}", path.name) and (path / "project.json").is_file())
            selected = _role_id(role_id) if role_id else (roles[0] if roles else 0)
            actions = []
            if selected:
                project = self._load(selected)
                for action, animation in project["manifest"]["Animations"].items():
                    previews = [str(self._frame_path(selected, relative)) for relative in animation["Frames"][:3]]
                    actions.append({"action": action, "label": dict(ACTION_CHOICES).get(action, action),
                                    "frame_count": len(animation["Frames"]), "duration_ms": sum(animation["DurationsMs"]),
                                    "fps": animation["Fps"], "loop": animation["Loop"],
                                    "preview_path": previews[0], "preview_frames": previews,
                                    "source_file": project["sources"].get(action, "")})
            return {"role_id": selected, "roles": roles, "actions": actions,
                    "action_choices": [{"action": action, "label": label} for action, label in ACTION_CHOICES],
                    "root": str(self.root), "experimental": True, "notice": NOTICE,
                    "dependency": "CesiumLoader + CharacterSkinMod（建议 0.2.7 或更新版本）", "format_source": FORMAT_SOURCE}

    @staticmethod
    def _source_files(source: Path) -> list[Path]:
        if source.is_dir():
            files = [path for path in source.iterdir() if path.is_file() and path.suffix.lower() == ".png"]
            if not 1 <= len(files) <= MAX_SOURCE_FRAMES:
                raise ValueError("帧目录需包含 1～512 张静态 PNG。")
            sizes = [path.stat().st_size for path in files]
            if any(not 0 < size <= MAX_FILE_BYTES for size in sizes) or sum(sizes) > MAX_PACKAGE_BYTES:
                raise ValueError("单帧文件超过 64 MiB，或帧目录总大小超过 256 MiB。")
            metadata = source / "clip-frames.json"
            if metadata.is_file():
                if metadata.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("动作帧包清单超过支持范围。")
                files.append(metadata)
            return files
        if not source.is_file() or not 0 < source.stat().st_size <= MAX_FILE_BYTES:
            raise ValueError("请选择不超过 64 MiB 的图片、动画或视频。")
        return [source]

    @staticmethod
    def _decode(source: Path, fps: float, scratch: Path):
        if source.is_dir():
            if (source / "clip-frames.json").is_file():
                from .clip_frame_export import read_clip_frame_directory
                frames, durations, _metadata = read_clip_frame_directory(source)
                return frames, durations
            files = CharacterAnimationWorkspace._source_files(source)
            if len(files) == 1:
                source = files[0]
            else:
                return _read_replacement(source, fps)[:2]
        try:
            with Image.open(source) as image:
                if getattr(image, "n_frames", 1) == 1:
                    if image.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                        raise ValueError("静态动作帧支持 PNG、JPG、WebP 或 GIF。")
                    if max(image.size) > 16384 or image.width * image.height > MAX_FRAME_PIXELS:
                        raise ValueError("静态动作帧尺寸超过支持范围。")
                    return [image.convert("RGBA")], [1000 / fps]
            frames, durations, _kind = _read_replacement(source, fps)
            # 角色包支持毫秒时长，不沿用 bundle 预览的最低 10 ms 播放限制。
            with Image.open(source) as image:
                first = int(bool(image.info.get("default_image")))
                for index in range(first, getattr(image, "n_frames", 1)):
                    image.seek(index)
                    image.load()
                    duration = image.info.get("duration", 100)
                    if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0:
                        durations[index - first] = duration
            return frames, durations
        except UnidentifiedImageError:
            if detect_emote_media(source) != "video":
                raise ValueError("此素材不是可导入的角色动画。")
            # 视频统一放入透明方形画布，解码器会保留比例与可用的透明通道。
            frames, info = adapt_video_source(source, {"width": 512, "height": 512, "fps": fps}, work_dir=scratch)
            return frames, [info["duration_ms"] / len(frames)] * len(frames)

    def _discard_frames(self, role_id: int, frames: list[str]) -> list[str]:
        pending, folders = [], set()
        for relative in frames:
            try:
                path = self._frame_path(role_id, relative)
                folders.add(path.parent)
                path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pending.append(relative)
        for folder in folders:
            try:
                folder.rmdir()
            except OSError:
                pass
        return pending

    def import_action(self, role_id: int, action: str, source: str | Path, *, fps: float = 15, loop: bool = True) -> dict:
        role_id, action = _role_id(role_id), _action_name(action)
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 1 <= fps <= 120:
            raise ValueError("动作帧率需为 1～120。视频导入最高支持 60 帧/秒。")
        if not isinstance(loop, bool):
            raise ValueError("循环选项需为布尔值。")
        source = Path(source).resolve()
        with self._lock:
            project = self._load(role_id)
            folder = self._folder(role_id)
            folder.mkdir(parents=True, exist_ok=True)
            fingerprints = {str(path): _digest(path) for path in self._source_files(source)}
            old_key = next((key for key in project["manifest"]["Animations"] if key.casefold() == action.casefold()), action)
            previous = project["manifest"]["Animations"].get(old_key, {}).get("Frames", [])
            generated = []
            committed = False
            try:
                with tempfile.TemporaryDirectory(prefix=".action-", dir=folder) as temporary:
                    scratch = Path(temporary)
                    frames, durations = self._decode(source, float(fps), scratch)
                    try:
                        if not 1 <= len(frames) <= MAX_SOURCE_FRAMES or len(frames) != len(durations):
                            raise ValueError("导入动作的帧数或时长不完整。")
                        pixel_limit = MAX_TOTAL_PIXELS
                        if source.is_dir() and (source / "clip-frames.json").is_file():
                            from .clip_frame_export import MAX_CLIP_PACKAGE_PIXELS
                            pixel_limit = MAX_CLIP_PACKAGE_PIXELS
                        if sum(frame.width * frame.height for frame in frames) > pixel_limit:
                            raise ValueError("导入动作超过支持的总像素范围。")
                        rounded_durations, elapsed, emitted = [], 0.0, 0
                        for duration in durations:
                            if not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 < duration <= 60000:
                                raise ValueError("动作单帧时长无效或超过 60 秒。")
                            elapsed += duration
                            length = max(1, round(elapsed) - emitted)
                            rounded_durations.append(length)
                            emitted += length
                        durations = rounded_durations
                        token = os.urandom(16).hex()
                        staged = scratch / "frames"
                        staged.mkdir()
                        relative_folder = f"animations/{action}-{token}"
                        for index, frame in enumerate(frames):
                            if max(frame.size) > 16384 or frame.width * frame.height > MAX_FRAME_PIXELS:
                                raise ValueError("导入动作帧尺寸超过支持范围。")
                            filename = f"{index:04d}.png"
                            frame.convert("RGBA").save(staged / filename, format="PNG")
                            generated.append(f"{relative_folder}/{filename}")
                        current = {str(path): _digest(path) for path in self._source_files(source)}
                        if current != fingerprints:
                            raise ValueError("素材在导入时发生变化，请重新导入。")
                        destination = self._frame_path(role_id, generated[0]).parent
                        destination.parent.mkdir(exist_ok=True)
                        os.replace(staged, destination)
                        project["manifest"]["Animations"].pop(old_key, None)
                        project["sources"].pop(old_key, None)
                        project["manifest"]["Animations"][action] = {"Frames": generated, "DurationsMs": durations,
                                                                       "Fps": float(fps), "Loop": loop}
                        project["sources"][action] = source.name
                        self._validate(project, images=True)
                        _atomic_json(folder / "project.json", project)
                        committed = True
                    finally:
                        for frame in frames:
                            frame.close()
            finally:
                if not committed:
                    self._discard_frames(role_id, generated)
            pending = self._discard_frames(role_id, previous)
            result = self.status(role_id)
            result["action"] = action
            result["preview"] = next(row for row in result["actions"] if row["action"] == action)
            result["cleanup_pending"] = pending
            return result

    def remove_action(self, role_id: int, action: str) -> dict:
        role_id, action = _role_id(role_id), _action_name(action)
        with self._lock:
            project = self._load(role_id)
            key = next((key for key in project["manifest"]["Animations"] if key.casefold() == action.casefold()), None)
            if key is None:
                raise ValueError("该动作尚未加入当前工程。")
            frames = project["manifest"]["Animations"].pop(key)["Frames"]
            project["sources"].pop(key, None)
            _atomic_json(self._folder(role_id) / "project.json", project)
            pending = self._discard_frames(role_id, frames)
            result = self.status(role_id)
            result["cleanup_pending"] = pending
            return result

    def preview(self, role_id: int, action: str) -> dict:
        role_id, action = _role_id(role_id), _action_name(action)
        with self._lock:
            metadata = self._folder(role_id) / "project.json"
            if not metadata.is_file():
                raise ValueError("请先导入角色动作。")
            metadata_digest = _digest(metadata)
            project = self._load(role_id)
            key = next((key for key in project["manifest"]["Animations"] if key.casefold() == action.casefold()), None)
            if key is None:
                raise ValueError("当前工程没有这个动作。")
            self._validate(project, images=True)
            animation = project["manifest"]["Animations"][key]
            paths = [self._frame_path(role_id, relative) for relative in animation["Frames"]]
            fingerprints = {path: _digest(path) for path in paths}
            indices, durations = _preview_indices(len(paths), animation["DurationsMs"])

            def images():
                for index in indices:
                    with Image.open(paths[index]) as image:
                        yield image.convert("RGBA")

            result = _preview(images(), [f"第 {index + 1} 帧" for index in indices], durations, len(paths),
                              info={"action": key, "label": dict(ACTION_CHOICES).get(key, key),
                                    "loop": animation["Loop"], "original_frame_count": len(paths),
                                    "experimental": True})
            if _digest(metadata) != metadata_digest or any(_digest(path) != digest for path, digest in fingerprints.items()):
                raise ValueError("动作帧或工程清单在预览期间发生变化，请重新加载。")
            result["notice"] = "按工程中的逐帧时长播放。" + ("预览均匀抽样，导出包保留全部动作帧。" if result["truncated"] else "")
            return result

    def export(self, role_id: int, name: str = "", author: str = "") -> dict:
        role_id = _role_id(role_id)
        if not isinstance(name, str) or not isinstance(author, str) or len(name) > 120 or len(author) > 120:
            raise ValueError("作品名称和作者需为 120 字以内的文本。")
        with self._lock:
            project = self._load(role_id)
            if not project["manifest"]["Animations"]:
                raise ValueError("请先加入至少一个包含真实图片帧的动作。")
            files = self._validate(project, images=True)
            fingerprints = {relative: _digest(path) for relative, path in files.items()}
            manifest = deepcopy(project["manifest"])
            manifest.update(Name=name.strip() or manifest["Name"], Author=author.strip())
            identity = hashlib.sha256(json.dumps([manifest, fingerprints], ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
            manifest["Id"] = f"jixing-hero-{role_id}-{identity}"
            exports = self.root / "exports"
            exports.mkdir(exist_ok=True)
            if exports.is_symlink() or exports.resolve().parent != self.root:
                raise ValueError("导出目录不属于当前角色动画工作台。")
            destination = exports / f"{manifest['Id']}.zip"
            handle, temporary = tempfile.mkstemp(prefix=".export-", suffix=".tmp", dir=exports)
            os.close(handle)
            try:
                with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("character-skin.json", json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False))
                    archive.writestr("README.txt", f"{manifest['Name']}\n\n{NOTICE}\n\n角色 ID {role_id}，适用于全部皮肤。\n"
                                     "在 Toys 中打开角色皮肤 → 皮肤包素材库 → 导入 ZIP → 添加到多人物方案 → 应用方案，再重启游戏。\n"
                                     "动图保留逐帧时长，PNG 帧目录按所选帧率播放；单张静态图作为单帧动作。视频会等比适配 512×512 透明留边画布并忽略声音。\n"
                                     f"依赖 CharacterSkinMod，建议使用 0.2.7 或更新版本。\n格式说明 {FORMAT_SOURCE}\n播放行为 {RUNTIME_SOURCE}\n")
                    for relative, path in files.items():
                        archive.write(path, relative)
                if Path(temporary).stat().st_size > MAX_PACKAGE_BYTES:
                    raise ValueError("导出 ZIP 超过 256 MiB，请减少动作或缩小素材。")
                with zipfile.ZipFile(temporary) as archive:
                    if archive.testzip() or json.loads(archive.read("character-skin.json")) != manifest:
                        raise ValueError("导出的动画清单或 ZIP 完整性校验失败。")
                    for relative, digest in fingerprints.items():
                        if hashlib.sha256(archive.read(relative)).hexdigest() != digest:
                            raise ValueError("动画帧在打包时发生变化，请重新导出。")
                os.replace(temporary, destination)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return {"path": str(destination), "package": str(destination), "id": manifest["Id"],
                    "role_id": role_id, "action_count": len(manifest["Animations"]),
                    "frame_count": sum(len(action["Frames"]) for action in manifest["Animations"].values()),
                    "size": destination.stat().st_size, "sha256": _digest(destination),
                    "experimental": True, "dependency": "CesiumLoader + CharacterSkinMod", "notice": NOTICE}
