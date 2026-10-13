"""Use native FairyGUI MovieClips for verified card and standing-painting URLs."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
import copy
from pathlib import Path

import UnityPy
from PIL import Image

from .animation import _load_bundle, _preview, _read_replacement, _release_readers
from .bundles import bundle_source_key
from .dynamic import text_asset_bytes
from .emote_addressables import _catalog_parts
from .emote_catalog import discover_emote_locations
from .emote_config import _objects
from .catalog_mods import active_catalog_path, atomic_bytes
from .emote_workspace import item_bundle_names

MAX_ATLAS_EDGE = 4096
MAX_MOVIE_FRAMES = 120


def animation_identity(texture_name: str) -> str:
    return "jm" + hashlib.sha256(texture_name.encode("utf-8")).hexdigest()[:16]


def _split_free(free: list[tuple], occupied: tuple) -> list[tuple]:
    ox, oy, ow, oh = occupied
    result = []
    for x, y, width, height in free:
        right, bottom = min(x + width, ox + ow), min(y + height, oy + oh)
        left, top = max(x, ox), max(y, oy)
        if right <= left or bottom <= top:
            result.append((x, y, width, height))
            continue
        if left > x:
            result.append((x, y, left - x, height))
        if right < x + width:
            result.append((right, y, x + width - right, height))
        if top > y:
            result.append((x, y, width, top - y))
        if bottom < y + height:
            result.append((x, bottom, width, y + height - bottom))
    return [rect for index, rect in enumerate(result) if not any(
        index != other and rect != candidate and rect[0] >= candidate[0] and rect[1] >= candidate[1]
        and rect[0] + rect[2] <= candidate[0] + candidate[2]
        and rect[1] + rect[3] <= candidate[1] + candidate[3]
        for other, candidate in enumerate(result))]


def pack_frames(base_size: tuple[int, int], occupied: list[list[int]], frame_size: tuple[int, int], count: int) -> list[list[int]]:
    """Reserve the entire original atlas; leave a pixel gutter around new frames."""
    width, height = frame_size
    if min(width, height, count) <= 0 or max(*base_size, width, height) > MAX_ATLAS_EDGE:
        return []
    free = _split_free([(0, 0, MAX_ATLAS_EDGE, MAX_ATLAS_EDGE)], (0, 0, *base_size))
    for x, y, w, h in occupied:
        free = _split_free(free, (max(0, x - 1), max(0, y - 1), w + 2, h + 2))
    placed = []
    for _ in range(count):
        fits = [rect for rect in free if rect[2] >= width + 2 and rect[3] >= height + 2]
        if not fits:
            break
        x, y, _, _ = min(fits, key=lambda rect: (min(rect[2] - width - 2, rect[3] - height - 2), rect[2] * rect[3], rect[1], rect[0]))
        placed.append([x + 1, y + 1, width, height])
        free = _split_free(free, (x, y, width + 2, height + 2))
    return placed


def sample_frames(frames: list, durations: list, count: int) -> tuple[list, list[int]]:
    count = min(count, len(frames))
    if count < 2 or len(durations) != len(frames):
        raise ValueError("至少需要两帧动画。")
    indices = [index * len(frames) // count for index in range(count)]
    times = [max(1, round(sum(durations[start:end]))) for start, end in zip(indices, indices[1:] + [len(frames)])]
    if max(times) > 60000 or sum(times) > 30000:
        raise ValueError("动态素材最长支持 30 秒。")
    return [frames[index] for index in indices], times


def _clear_clip_pixels(atlas: Image.Image, package: dict, clip: dict, base_size: tuple[int, int]) -> None:
    """Check every owned rectangle before clearing its pixels and transparent border."""
    sprites = {row["id"]: row for row in package["sprites"]}
    owned = {frame["sprite_id"] for frame in clip["frames"]}
    protected = [[0, 0, *base_size]] + [row["rect"] for row in package["sprites"] if row["id"] not in owned]
    areas = []
    for sprite_id in owned:
        sprite = sprites[sprite_id]
        x, y, width, height = sprite["rect"]
        if (sprite["atlas_id"] != "atlas0" or sprite["rotated"] or sprite["trimmed"]
                or min(x, y, width, height) < 1 or x + width >= atlas.width or y + height >= atlas.height):
            raise ValueError("动画帧或留边已被外部修改，已停止操作。")
        area = [x - 1, y - 1, width + 2, height + 2]
        left, top, w, h = area
        if any(left < px + pw and left + w > px and top < py + ph and top + h > py
               for px, py, pw, ph in protected):
            raise ValueError("动画帧或留边与其它资源重叠，已停止操作。")
        areas.append(area)
    for x, y, width, height in areas:
        atlas.paste((0, 0, 0, 0), (x, y, x + width, y + height))


def _object(env, kind: str, name: str):
    matches = [obj for obj in env.objects if obj.type.name == kind and obj.peek_name() == name]
    if len(matches) != 1:
        raise ValueError(f"资源名称不唯一或缺失：{name}")
    return matches[0]


@_release_readers
def read_descriptor(path: Path) -> bytes:
    return text_asset_bytes(_object(_load_bundle(path), "TextAsset", "Common_fui").read())


@_release_readers
def read_atlas(path: Path) -> Image.Image:
    data = _object(_load_bundle(path), "Texture2D", "Common_atlas0").read()
    if data.m_MipCount != 1 or data.m_ImageCount != 1 or data.m_TextureDimension != 2:
        raise ValueError("公共图集的格式已变化，暂不进行动态制作。")
    if max(data.m_Width, data.m_Height) > MAX_ATLAS_EDGE:
        raise ValueError("公共图集超过支持尺寸。")
    return data.image.convert("RGBA")


@_release_readers
def write_descriptor(source: Path, output: Path, raw: bytes) -> None:
    env = _load_bundle(source)
    target = _object(env, "TextAsset", "Common_fui")
    before = _objects(env)
    key = (target.assets_file.name, target.path_id)
    data = target.read()
    data.m_Script = raw.decode("utf-8", errors="surrogateescape") if isinstance(data.m_Script, str) else raw
    data.save()
    payload = env.file.save(packer="lz4")
    restored = UnityPy.load(payload)
    after = _objects(restored)
    if before.keys() != after.keys() or any(before[k] != after[k] for k in before if k != key):
        raise ValueError("保存改变了公共界面的其它对象。")
    if text_asset_bytes(_object(restored, "TextAsset", "Common_fui").read()) != raw:
        raise ValueError("公共界面资源回读失败。")
    atomic_bytes(output, payload)


@_release_readers
def write_atlas(source: Path, output: Path, image: Image.Image) -> None:
    env = _load_bundle(source)
    target = _object(env, "Texture2D", "Common_atlas0")
    before = _objects(env)
    key = (target.assets_file.name, target.path_id)
    data = target.read()
    # RGBA32 keeps the decoded official pixels exact; never recompress them as BC7.
    data.set_image(image, target_format=4, mipmap_count=1)
    data.save()
    payload = env.file.save(packer="lz4")
    if len(payload) > 128 * 1024 * 1024:
        raise ValueError("动态公共图集包超过 128 MB。")
    restored = UnityPy.load(payload)
    after = _objects(restored)
    if before.keys() != after.keys() or any(before[k] != after[k] for k in before if k != key):
        raise ValueError("保存改变了图集中的其它对象。")
    saved = _object(restored, "Texture2D", "Common_atlas0").read().image.convert("RGBA")
    if saved.size != image.size or saved.tobytes() != image.tobytes():
        raise ValueError("图集像素回读失败。")
    atomic_bytes(output, payload)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class VisualAnimationWorkspace:
    def __init__(self, controller):
        self.controller = controller
        self._context = None
        self._source_key = None

    def _bundle_for(self, key: str, kind: str) -> str:
        catalog = json.loads(active_catalog_path(self.controller.aa_dirs).read_text(encoding="utf-8-sig"))
        _, _, buckets, entries = _catalog_parts(catalog)
        matches = [ids for _, name, ids in buckets if name == key]
        if len(matches) != 1 or len(matches[0]) != 1:
            raise ValueError(f"当前目录没有唯一的资源地址：{key}")
        entry = entries[matches[0][0]]
        if catalog["m_resourceTypes"][entry[6]].get("m_ClassName") != kind or entry[2] < 0:
            raise ValueError("公共资源的加载方式已变化。")
        names = []
        for dependency in buckets[entry[2]][2]:
            name = catalog["m_InternalIds"][entries[dependency][0]].replace("\\", "/").rsplit("/", 1)[-1]
            if self.controller.original_bundle_path(name) is not None:
                names.append(name)
        if len(names) != 1:
            raise ValueError("公共资源包含未验证的额外依赖。")
        return names[0]

    def context(self) -> dict:
        from .fairygui_movieclip import inspect_movieclip_package
        from .visual_config import inspect_visual_config
        self.controller._require_game()
        key = bundle_source_key(self.controller.aa_dirs)
        if self._source_key == key and self._context is not None:
            return self._context
        config_name = discover_emote_locations(self.controller.aa_dirs)["config_bundle"]
        ui_name = self._bundle_for("Common_fui", "UnityEngine.TextAsset")
        atlas_name = self._bundle_for("Common_atlas0", "UnityEngine.Texture2D")
        originals = {name: self.controller.original_bundle_path(name) for name in (config_name, ui_name, atlas_name)}
        if any(path is None for path in originals.values()):
            raise ValueError("缺少原始配置或公共界面资源。")
        raw = read_descriptor(originals[ui_name])
        package = inspect_movieclip_package(raw)
        if package["package_name"] != "Common" or len(package["atlases"]) != 1 or package["atlases"][0]["id"] != "atlas0":
            raise ValueError("当前公共界面布局尚未验证，暂不制作动态资源。")
        atlas = read_atlas(originals[atlas_name])
        if atlas.size != (package["atlases"][0]["width"], package["atlases"][0]["height"]):
            raise ValueError("公共图集与界面尺寸不一致。")
        env = _load_bundle(originals[config_name])
        tables = {name: inspect_visual_config(text_asset_bytes(_object(env, "TextAsset", name).read()), name) for name in ("Card", "Skin")}
        self._context = {"config_bundle": config_name, "ui_bundle": ui_name, "atlas_bundle": atlas_name,
                         "originals": originals, "base_size": atlas.size, "package_id": package["package_id"], "tables": tables}
        self._source_key = key
        return self._context

    def selection(self, name: str) -> dict:
        try:
            context = self.context()
            targets = [(table, target) for table, info in context["tables"].items()
                       for target in info["targets"] if target["name"] == name]
            if not targets:
                return {"editable": False, "reason": "此资源尚无经过核对的原生动态播放入口。"}
            draft = next((item for item in self.controller.draft_items if item.get("kind") == "visual_animation" and item.get("name") == name), None)
            return {"editable": True, "experimental": True, "tables": [table for table, _ in targets],
                    "label": "动态卡面" if targets[0][0] == "Card" else "动态立绘", "draft": bool(draft),
                    "reason": "使用游戏原生逐帧播放，保留玩法与拥有状态。共享同一图片的界面会一起改变；游戏内效果需实测。"}
        except (OSError, ValueError, KeyError) as exc:
            return {"editable": False, "reason": str(exc)}

    def _selection(self) -> tuple[dict, dict]:
        selection = self.controller.selection or {}
        if selection.get("asset_type") != "texture":
            raise ValueError("请先选择一张卡面或立绘图片。")
        support = self.selection(selection.get("name", ""))
        if not support.get("editable"):
            raise ValueError(support["reason"])
        return selection, support

    def _source(self, name: str, context: dict) -> Path:
        draft = self.controller._draft_dir() / name
        return draft if draft.is_file() else context["originals"][name]

    def preview(self, name: str, descriptor: Path, atlas_path: Path) -> dict:
        from .fairygui_movieclip import inspect_movieclip_package
        package = inspect_movieclip_package(read_descriptor(descriptor))
        clip = next((row for row in package["movieclips"] if row["id"] == animation_identity(name)), None)
        if clip is None:
            raise ValueError("作品集里没有此动态资源。")
        sprites = {row["id"]: row for row in package["sprites"]}
        atlas = read_atlas(atlas_path)
        frames = []
        for frame in clip["frames"]:
            sprite = sprites[frame["sprite_id"]]
            x, y, width, height = sprite["rect"]
            pixels = atlas.crop((x, y, x + width, y + height))
            canvas = Image.new("RGBA", (clip["width"], clip["height"]))
            canvas.paste(pixels, tuple(frame["rect"][:2]))
            frames.append(canvas)
        return _preview(frames, [f"第 {index + 1} 帧" for index in range(len(frames))],
                        [frame["duration_ms"] for frame in clip["frames"]], len(frames),
                        info={"animation_kind": "native_movieclip", "notice": "从生成的界面包和图集回读，游戏内效果仍需实测。"})

    def draft_preview(self) -> dict:
        selection, _ = self._selection()
        context = self.context()
        return self.preview(selection["name"], self._source(context["ui_bundle"], context), self._source(context["atlas_bundle"], context))

    def prepare(self, source: Path, folder: Path) -> dict:
        from .emote_media import detect_emote_media, adapt_video_source, _fit_frame
        from .fairygui_movieclip import inspect_movieclip_package, append_movieclip, remove_movieclip
        from .visual_config import replace_visual_config_bundle
        selection, support = self._selection()
        context = self.context()
        shared_bundles = {context["ui_bundle"], context["atlas_bundle"]}
        if any(item.get("kind") != "visual_animation" and item_bundle_names(item) & shared_bundles for item in self.controller.draft_items):
            raise ValueError("此作品集已经编辑了公共界面或图集，请另建一套动态作品，避免两类编辑互相覆盖。")
        item_id = animation_identity(selection["name"])
        sources = {name: self._source(name, context) for name in context["originals"]}
        selected_source = self.controller.original_bundle_path(selection["bundle"])
        if selected_source is None:
            raise ValueError("原图片资源缺失。")
        fingerprints = {str(path): _digest(path) for path in [source, selected_source, *sources.values(), *context["originals"].values()]}
        draft_items = copy.deepcopy(self.controller.draft_items)
        raw = read_descriptor(sources[context["ui_bundle"]])
        package = inspect_movieclip_package(raw)
        existing_package = package
        existing = next((row for row in package["movieclips"] if row["id"] == item_id), None)
        if existing:
            raw = remove_movieclip(raw, item_id=item_id)
            package = inspect_movieclip_package(raw)
        size = (selection["width"], selection["height"])
        kind = detect_emote_media(source)
        if kind == "video":
            frames, details = adapt_video_source(source, {"width": size[0], "height": size[1], "fps": 12}, work_dir=folder)
            durations = [1000 / 12] * len(frames)
        elif kind == "usm":
            raise ValueError("卡面和立绘目前使用逐帧图片，请导入 GIF、APNG、WebP 或普通视频。")
        else:
            frames, durations, _ = _read_replacement(source)
        original_count = len(frames)
        occupied = [row["rect"] for row in package["sprites"] if row["id"].startswith("jm")]
        placements = pack_frames(context["base_size"], occupied, size, min(len(frames), MAX_MOVIE_FRAMES))
        if len(placements) < 2:
            raise ValueError("当前图集放不下两张完整尺寸的动画帧，请选择尺寸较小的资源或移除其它动态作品。")
        frames, durations = sample_frames(frames, durations, len(placements))
        placements = placements[:len(frames)]
        atlas = Image.new("RGBA", (MAX_ATLAS_EDGE, MAX_ATLAS_EDGE))
        current_atlas = read_atlas(sources[context["atlas_bundle"]])
        atlas.paste(current_atlas, (0, 0))
        before_original = current_atlas.crop((0, 0, *context["base_size"])).tobytes()
        if existing:
            _clear_clip_pixels(atlas, existing_package, existing, context["base_size"])
        records = []
        for frame, duration, rect in zip(frames, durations, placements):
            fitted = _fit_frame(frame, *size)
            atlas.paste(fitted, tuple(rect[:2]))
            records.append({"rect": [0, 0, *size], "atlas_rect": rect, "duration_ms": duration})
        if atlas.crop((0, 0, *context["base_size"])).tobytes() != before_original:
            raise ValueError("动态帧覆盖了原版界面像素，未保存。")
        raw = append_movieclip(raw, item_id=item_id, atlas_id="atlas0", width=size[0], height=size[1],
                               frames=records, atlas_width=MAX_ATLAS_EDGE, atlas_height=MAX_ATLAS_EDGE)
        ui_output = folder / context["ui_bundle"]
        atlas_output = folder / context["atlas_bundle"]
        write_descriptor(sources[context["ui_bundle"]], ui_output, raw)
        write_atlas(sources[context["atlas_bundle"]], atlas_output, atlas)
        url = "ui://" + context["package_id"] + item_id
        config_output = folder / context["config_bundle"]
        config_source = sources[context["config_bundle"]]
        old_item = next((item for item in self.controller.draft_items if item.get("kind") == "visual_animation" and item.get("name") == selection["name"]), None)
        for index, table in enumerate(support["tables"]):
            old_address = old_item["movieclip_url"] if old_item else selection["name"]
            destination = config_output if index == len(support["tables"]) - 1 else folder / f"config-{index}.bundle"
            replace_visual_config_bundle(config_source, destination, table, old_address, url)
            config_source = destination
        item = {"kind": "visual_animation", "bundle": context["config_bundle"], "name": selection["name"],
                "source_bundle": selection["bundle"], "extra_bundles": [context["ui_bundle"], context["atlas_bundle"]],
                "tables": support["tables"], "movieclip_url": url, "frame_count": len(frames), "duration_ms": sum(durations),
                "width": size[0], "height": size[1], "note": source.name, "at": datetime.now().isoformat(timespec="seconds")}
        if any(_digest(Path(path)) != digest for path, digest in fingerprints.items()):
            raise ValueError("素材或草稿在制作过程中改变，请重新导入。")
        preview = self.preview(selection["name"], ui_output, atlas_output)
        notice = f"输出 {len(frames)} 帧 · {sum(durations) / 1000:.2f} 秒 · {size[0]} × {size[1]}。"
        if len(frames) < original_count:
            notice += "已按图集剩余空间抽帧。"
        return {"item": item, "staged": {context["config_bundle"]: config_output, context["ui_bundle"]: ui_output, context["atlas_bundle"]: atlas_output},
                "fingerprints": fingerprints, "output_hashes": {str(path): _digest(path) for path in (config_output, ui_output, atlas_output)},
                "draft_items": draft_items, "source_paths": {name: str(path) for name, path in sources.items()},
                "animation": preview, "notice": notice}

    def save(self, candidate: dict) -> dict:
        selection, _ = self._selection()
        if candidate["item"]["name"] != selection["name"] or candidate["item"]["source_bundle"] != selection["bundle"]:
            raise ValueError("当前选择已改变，请重新导入动图。")
        context = self.context()
        if self.controller.draft_items != candidate["draft_items"] or any(
                str(self._source(name, context)) != path for name, path in candidate["source_paths"].items()):
            raise ValueError("作品集已改变，请重新导入动图。")
        for path, digest in {**candidate["fingerprints"], **candidate["output_hashes"]}.items():
            if not Path(path).is_file() or _digest(Path(path)) != digest:
                raise ValueError("素材、草稿或候选文件已改变，请重新导入。")
        items = [item for item in self.controller.draft_items if not (item.get("kind") == "visual_animation" and item.get("name") == selection["name"])] + [candidate["item"]]
        self.controller.emote_workspace._commit(candidate["staged"], items)
        self.controller.log(f"已保存动态资源：{selection['name']}")
        return candidate["item"]

    def remove(self, index: int) -> None:
        from .fairygui_movieclip import inspect_movieclip_package, remove_movieclip
        from .visual_config import replace_visual_config_bundle
        context = self.context()
        item = self.controller.draft_items[index]
        expected_tables = [table for table, info in context["tables"].items()
                           if any(target["name"] == item.get("name") for target in info["targets"])]
        if (item.get("kind") != "visual_animation" or item.get("bundle") != context["config_bundle"]
                or set(item.get("extra_bundles") or []) != {context["ui_bundle"], context["atlas_bundle"]}
                or not expected_tables or item.get("tables") != expected_tables
                or item.get("movieclip_url") != "ui://" + context["package_id"] + animation_identity(item["name"])):
            raise ValueError("动态草稿与当前游戏版本不一致，已停止移除；请保留草稿后检查版本。")
        items = [row for position, row in enumerate(self.controller.draft_items) if position != index]
        with self.controller.emote_workspace._prepare_folder("visual-remove-") as folder:
            ui_source = self._source(context["ui_bundle"], context)
            config_source = self._source(context["config_bundle"], context)
            ui_output = folder / context["ui_bundle"]
            config_output = folder / context["config_bundle"]
            atlas_output = folder / context["atlas_bundle"]
            raw = read_descriptor(ui_source)
            package = inspect_movieclip_package(raw)
            removed_raw = remove_movieclip(raw, item_id=animation_identity(item["name"]))
            clip = next(row for row in package["movieclips"] if row["id"] == animation_identity(item["name"]))
            atlas = read_atlas(self._source(context["atlas_bundle"], context))
            official_pixels = atlas.crop((0, 0, *context["base_size"])).tobytes()
            _clear_clip_pixels(atlas, package, clip, context["base_size"])
            if atlas.crop((0, 0, *context["base_size"])).tobytes() != official_pixels:
                raise ValueError("待移除的动画范围与官方界面重叠，已停止操作。")
            write_descriptor(ui_source, ui_output, removed_raw)
            write_atlas(self._source(context["atlas_bundle"], context), atlas_output, atlas)
            for position, table in enumerate(item["tables"]):
                destination = config_output if position == len(item["tables"]) - 1 else folder / f"config-{position}.bundle"
                replace_visual_config_bundle(config_source, destination, table, item["movieclip_url"], item["name"])
                config_source = destination
            active = set().union(*(item_bundle_names(row) for row in items)) if items else set()
            staged = {context["config_bundle"]: config_output, context["ui_bundle"]: ui_output, context["atlas_bundle"]: atlas_output}
            for name in item_bundle_names(item):
                if name not in active:
                    staged[name] = None
            self.controller.emote_workspace._commit(staged, items)
