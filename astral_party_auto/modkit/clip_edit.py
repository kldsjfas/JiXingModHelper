"""AnimationClip 原始数据导入：验证目标结构后才替换草稿文件。"""
from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

import UnityPy


def _clip(env, name):
    matches = [obj for obj in env.objects if obj.type.name == "AnimationClip" and obj.peek_name() == name]
    if len(matches) != 1:
        raise ValueError(f"动画不存在或名称不唯一：{name}")
    return matches[0]


def _pointers(value):
    found = set()
    if isinstance(value, dict):
        if "m_FileID" in value and value.get("m_PathID"):
            found.add((value["m_FileID"], value["m_PathID"]))
        for child in value.values():
            found.update(_pointers(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_pointers(child))
    return found


def replace_clip_checked(src_bundle, asset_name, raw_bytes, out_bundle, *, reference_bundle=None):
    if not raw_bytes or len(raw_bytes) > 32 * 1024 * 1024:
        raise ValueError("动画数据为空或超过 32 MB。")
    source = Path(src_bundle)
    env = UnityPy.load(source.read_bytes(), path=str(source.parent))
    target = _clip(env, asset_name)
    if reference_bundle is not None:
        baseline = Path(reference_bundle)
        reference_env = UnityPy.load(baseline.read_bytes(), path=str(baseline.parent))
        original = _clip(reference_env, asset_name).read_typetree()
    else:
        original = target.read_typetree()
    target.set_raw_data(raw_bytes)
    payload = env.file.save()
    try:
        restored = UnityPy.load(payload, path=str(source.parent))
        imported = _clip(restored, asset_name).read_typetree()
        rate = float(imported.get("m_SampleRate", 0))
        if not math.isfinite(rate) or not 0 < rate <= 1000:
            raise ValueError("动画帧率无效。")
        if _pointers(imported) - _pointers(original):
            raise ValueError("导入动画引用了目标中没有的对象。")
    except Exception as exc:
        raise ValueError(f"动画数据不兼容，请使用该资源同源导出的 .animbin：{exc}") from exc
    out = Path(out_bundle)
    out.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".clip-", suffix=".tmp", dir=out.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
        os.replace(temporary, out)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return asset_name
