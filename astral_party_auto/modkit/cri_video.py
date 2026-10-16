"""只读浏览 CRI 技能与过场影片，复用表情的视频完整性检查和双流解码。"""
from __future__ import annotations

from pathlib import Path

from . import cri_emote
from .animation import _release_readers
from .dynamic import is_cri_usm_object

READONLY_NOTICE = "CRI 技能 / 过场视频：支持播放预览和导出原始 USM，暂不支持替换。游戏内演出仍由原组件控制。"


def _video_bytes(bundle_path: str | Path, asset_name: str) -> tuple[object, bytes]:
    env = cri_emote._load_bundle(bundle_path)
    matches = [obj for obj in env.objects
               if obj.type.name == "MonoBehaviour" and obj.peek_name() == asset_name]
    if len(matches) != 1 or not is_cri_usm_object(matches[0]):
        raise ValueError(f"找不到唯一的 CRI 影片对象：{asset_name}，请重建资源索引。")
    obj, _, _, raw = cri_emote._embedded_object(env, asset_name)
    return obj, raw


@_release_readers
def inspect_cri_video_bundle(bundle_path: str | Path, asset_name: str) -> dict:
    """读取真实影片元信息；编码或外部实现不受支持时明确拒绝。"""
    obj, raw = _video_bytes(bundle_path, asset_name)
    info = cri_emote.inspect_usm_bytes(raw, allow_display_crop=True)
    return {**info, "animation_kind": "cri_usm", "asset_name": asset_name,
            "path_id": obj.path_id, "embedded": True, "editable": False,
            "playable": True, "exportable": True,
            "encoded_width": info["width"], "encoded_height": info["height"],
            "width": info["display_width"], "height": info["display_height"],
            "reason": READONLY_NOTICE, "notice": READONLY_NOTICE}


@_release_readers
def read_cri_video_preview(bundle_path: str | Path, asset_name: str, **options) -> dict:
    """实际解码原始颜色和透明度流；临时文件由现有预览函数自动清理。"""
    from .emote_media import read_usm_preview

    _, raw = _video_bytes(bundle_path, asset_name)
    options["allow_display_crop"] = True
    result = read_usm_preview(raw, **options)
    return {**result, "animation_kind": "cri_usm", "editable": False,
            "notice": result["notice"] + " " + READONLY_NOTICE}


@_release_readers
def export_cri_video_bundle(bundle_path: str | Path, asset_name: str, output_path: str | Path) -> Path:
    """校验后原样导出影片，不转码、不改写游戏资源包。"""
    _, raw = _video_bytes(bundle_path, asset_name)
    cri_emote.inspect_usm_bytes(raw, allow_display_crop=True)
    destination = Path(output_path)
    if destination.suffix.lower() != ".usm":
        destination = destination.with_suffix(".usm")
    if destination.resolve() == Path(bundle_path).resolve():
        raise ValueError("导出位置不能覆盖原始资源包。")
    cri_emote._write_atomic(destination, raw)
    if destination.read_bytes() != raw:
        raise ValueError("导出影片回读校验失败。")
    return destination
