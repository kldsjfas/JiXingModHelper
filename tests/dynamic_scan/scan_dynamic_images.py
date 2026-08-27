"""扫描本机游戏资源中的动态 2D 图像资源，输出资源种类统计与候选清单。

用法（在仓库根目录运行）：
    python tests/dynamic_scan/scan_dynamic_images.py

会扫描 Unity Addressable bundle 里：
- 所有 Unity 对象类型（Texture2D/Sprite/AnimationClip/AnimatorController/VideoClip/...）
- 序列帧贴图（同包内按“动作名_帧号”成组的 Texture2D/Sprite）
- 视频类资源（VideoClip / VideoPlayer / MovieTexture）
- 动画相关资源（AnimationClip / AnimatorController）
- 可能的 Live2D / Spine / DragonBones / GIF / WebP / APNG 资源
- FairyGUI（*_fui）资源包（游戏内动态 UI 图通常打包在这里）
"""
from __future__ import annotations

import collections
import sys
from pathlib import Path

import UnityPy

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astral_party_auto.core.detector import find_game_install
from astral_party_auto.modkit.bundles import (
    bundle_dirs_for_exe,
    iter_bundle_entries,
)
from astral_party_auto.modkit.dynamic import (
    SEQUENCE_MIN_FRAMES,
    classify_text_asset,
    extract_fairygui_dynamic_names,
    sequence_groups_from_names,
    text_asset_bytes,
)

# 读取对象名时可能遇到损坏对象，统一吞掉
def _read_name(obj) -> str:
    try:
        data = obj.read()
    except Exception:
        return ""
    return str(getattr(data, "m_Name", "") or getattr(data, "name", "") or "")



def main() -> int:
    install = find_game_install()
    if install is None:
        print("未检测到游戏，请先确认 Steam / TapTap 版已安装。")
        return 1

    exe = install.cn_exe or install.int_exe
    if exe is None:
        print("检测到游戏目录但没有找到 exe。")
        return 1

    print("游戏目录:", install.install_dir)
    print("启动 exe :", exe)
    aa_dirs = bundle_dirs_for_exe(exe)
    print("资源目录 :")
    for d in aa_dirs:
        print("  -", d)

    entries = list(iter_bundle_entries(aa_dirs))
    print("资源包数量:", len(entries))

    type_counts: collections.Counter[str] = collections.Counter()
    # bundle -> 贴图名集合（Texture2D 与 Sprite 可能同名，先去重），用于后面按包分组找序列帧
    texture_names: dict[str, set[str]] = {}
    # 动态候选
    video_rows: list[tuple[str, str]] = []
    anim_clip_rows: list[tuple[str, str]] = []
    animator_rows: list[tuple[str, str]] = []
    text_assets: list[tuple[str, str, bytes]] = []  # bundle, name, raw
    fairygui_items: list[tuple[str, str]] = []  # bundle, "包名/组件名"

    total = len(entries)
    for done, bundle in enumerate(entries, start=1):
        try:
            env = UnityPy.load(str(bundle.path))
        except Exception:
            continue

        for obj in env.objects:
            tn = obj.type.name
            type_counts[tn] += 1

            if tn in ("Texture2D", "Sprite"):
                name = _read_name(obj)
                if name:
                    texture_names.setdefault(bundle.name, set()).add(name)
            elif tn in ("VideoClip", "VideoPlayer", "MovieTexture"):
                name = _read_name(obj)
                video_rows.append((bundle.name, name))
            elif tn == "AnimationClip":
                name = _read_name(obj)
                anim_clip_rows.append((bundle.name, name))
            elif tn == "AnimatorController":
                name = _read_name(obj)
                animator_rows.append((bundle.name, name))
            elif tn == "TextAsset":
                try:
                    data = obj.read()
                except Exception:
                    continue
                name = _read_name(obj)
                raw = text_asset_bytes(data)
                text_assets.append((bundle.name, name, raw))
                if classify_text_asset(name, raw) == "fairygui":
                    for item in extract_fairygui_dynamic_names(raw):
                        fairygui_items.append((bundle.name, f"{name}/{item}"))

        if done % 1000 == 0 or done == total:
            print(f"  扫描进度: {done}/{total}，已见资源名 {sum(len(v) for v in texture_names.values())}", flush=True)

    print("\n===== 资源种类统计（Unity 对象类型） =====")
    for type_name, count in type_counts.most_common():
        print(f"  {type_name}: {count}")

    print("\n===== 动态 2D 资源候选 =====")

    # 1) 视频
    print(f"\n[视频] VideoClip / VideoPlayer / MovieTexture 共 {len(video_rows)} 个")
    for bundle_name, name in video_rows[:30]:
        print(f"  {bundle_name}  [{name or '(未命名)'}]")

    # 2) 动画片段与控制器
    print(f"\n[动画片段] AnimationClip 共 {len(anim_clip_rows)} 个")
    for bundle_name, name in anim_clip_rows[:20]:
        print(f"  {bundle_name}  [{name or '(未命名)'}]")
    print(f"\n[动画控制器] AnimatorController 共 {len(animator_rows)} 个")
    for bundle_name, name in animator_rows[:20]:
        print(f"  {bundle_name}  [{name or '(未命名)'}]")

    # 3) 序列帧贴图
    sequence_groups: list[tuple[str, str, int, int, int, list[str]]] = []
    # (bundle, base, frame_count, min_index, max_index, examples)
    for bundle_name, names in texture_names.items():
        for group in sequence_groups_from_names(names, bundle=bundle_name):
            sequence_groups.append((
                bundle_name,
                group.base,
                group.frame_count,
                group.min_index,
                group.max_index,
                list(group.examples),
            ))

    sequence_groups.sort(key=lambda row: -row[2])
    total_frames = sum(row[2] for row in sequence_groups)
    print(f"\n[序列帧贴图] 疑似动态序列 {len(sequence_groups)} 组，共 {total_frames} 帧")
    print(f"  （按同包同名前缀 + 数字帧号分组，最少 {SEQUENCE_MIN_FRAMES} 帧；实际游戏中表现常为序列帧动画）")
    for bundle_name, base, count, min_idx, max_idx, examples in sequence_groups[:30]:
        print(f"  {bundle_name}  [{base}] {count}帧 ({min_idx}~{max_idx}) 例: {', '.join(examples)}")

    # 4) TextAsset 里可能藏着的动态格式
    dynamic_text: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for bundle_name, name, raw in text_assets:
        kind = classify_text_asset(name, raw)
        if kind:
            dynamic_text[kind].append((bundle_name, name))

    for kind in ("gif", "webp", "apng", "live2d", "spine", "dragonbones", "fairygui"):
        rows = dynamic_text.get(kind, [])
        label = {
            "gif": "GIF",
            "webp": "WebP",
            "apng": "APNG",
            "live2d": "Live2D",
            "spine": "Spine",
            "dragonbones": "DragonBones",
            "fairygui": "FairyGUI 资源包",
        }[kind]
        if kind == "fairygui":
            print(f"\n[{label}] 共 {len(rows)} 个（游戏内动态 UI 图通常由这些包引用序列帧贴图实现）")
        else:
            print(f"\n[{label}] 共 {len(rows)} 个")
        for bundle_name, name in rows[:20]:
            print(f"  {bundle_name}  [{name}]")
        if not rows:
            print("  （未发现）")

    print(f"\n[FairyGUI 内部动效/组件] 共 {len(fairygui_items)} 个")
    for bundle_name, name in fairygui_items[:30]:
        print(f"  {bundle_name}  [{name}]")

    # 汇总是否找到动态资源
    found_any = bool(
        video_rows
        or anim_clip_rows
        or animator_rows
        or sequence_groups
        or any(dynamic_text.values())
        or fairygui_items
    )
    print("\n===== 汇总 =====")
    print("  视频:", len(video_rows))
    print("  动画片段:", len(anim_clip_rows))
    print("  动画控制器:", len(animator_rows))
    print("  序列帧贴图组:", len(sequence_groups))
    for kind, rows in dynamic_text.items():
        print(f"  {kind}: {len(rows)}")
    print(f"  fairygui_items: {len(fairygui_items)}")

    return 0 if found_any else 2


if __name__ == "__main__":
    raise SystemExit(main())

