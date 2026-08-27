"""动态 2D 资源识别与预览辅助。

集中管理“哪些资源在游戏里表现为动态 2D 图片”的判定规则，便于后续扩展：
- 序列帧贴图（同包内同名前缀 + 数字帧号）
- 视频（VideoClip / VideoPlayer / MovieTexture）
- GIF / WebP / APNG 容器
- Live2D / Spine / DragonBones 文本或组件特征
- FairyGUI（*_fui）资源包
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

# 序列帧最少帧数：少于这个数多半是不同变体/编号资源，不是动画序列
SEQUENCE_MIN_FRAMES = 3
# 匹配 “名字_00000 / 名字-001 / 名字12 / 名字_3” 这类帧号结尾
SEQUENCE_RE = re.compile(r"^(.*?)[_\-]?(\d{1,5})$")

# 只要名字/内容出现这些词，就当作对应引擎/格式的动态 2D 候选
LIVE2D_KEYWORDS = (
    "live2d",
    "cubism",
    "model3.json",
    "motion3.json",
    "physics3.json",
    "pose3.json",
    ".moc3",
)
SPINE_KEYWORDS = (
    "spine",
    "skeleton",
    ".skel",
    ".atlas",
)
DRAGONBONES_KEYWORDS = (
    "dragonbones",
    "dragon_bones",
    ".dbbin",
    "armature",
)

# FairyGUI 内部组件/动效名中出现这些词时，视为动态 UI 资源候选
DYNAMIC_FGUI_KEYWORDS = (
    "anim",
    "banner",
    "bg",
    "card",
    "cutin",
    "display",
    "effect",
    "float",
    "glow",
    "guide",
    "hover",
    "light",
    "loop",
    "move",
    "play",
    "quality",
    "replace",
    "reward",
    "roll",
    "shake",
    "show",
    "sparkle",
    "star",
    "switch",
    "transition",
    "up",
    "down",
)

# TextAsset 动态分类的中文名，便于界面展示
DYNAMIC_KIND_LABELS = {
    "video": "视频",
    "gif": "GIF",
    "webp": "WebP",
    "apng": "APNG",
    "live2d": "Live2D",
    "spine": "Spine",
    "dragonbones": "DragonBones",
    "fairygui": "FairyGUI 资源包",
    "sequence": "序列帧动画",
}


@dataclass(frozen=True)
class SequenceGroup:
    """同一个 bundle 里共享前缀、仅帧号不同的一组贴图。"""

    bundle: str
    base: str
    names: tuple[str, ...]

    @property
    def frame_count(self) -> int:
        return len(self.names)

    @property
    def min_index(self) -> int:
        return min(int(_frame_index(name)) for name in self.names)

    @property
    def max_index(self) -> int:
        return max(int(_frame_index(name)) for name in self.names)

    @property
    def examples(self) -> tuple[str, ...]:
        return tuple(sorted(self.names, key=lambda name: int(_frame_index(name)))[:5])


def _frame_index(name: str) -> str:
    match = SEQUENCE_RE.match(name)
    if not match:
        return "0"
    return match.group(2)


def text_asset_bytes(data) -> bytes:
    """从 UnityPy TextAsset 对象里取出原始字节。"""
    raw = getattr(data, "m_Script", None)
    if raw is None:
        raw = getattr(data, "script", b"")
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="surrogatepass")
    return bytes(raw or b"")


def classify_text_asset(name: str, raw: bytes) -> str | None:
    """返回 TextAsset 的动态类型；不是动态返回 None。"""
    low = name.lower()
    if low.endswith("_fui") or raw.startswith(b"FGUI"):
        return "fairygui"

    if any(k in low for k in LIVE2D_KEYWORDS):
        return "live2d"
    if any(k in low for k in SPINE_KEYWORDS):
        return "spine"
    if any(k in low for k in DRAGONBONES_KEYWORDS):
        return "dragonbones"

    # 内容特征：Live2D model3 / motion3 JSON
    head = raw.lstrip(b"\xef\xbb\xbf")[:1]
    if head in (b"{", b"["):
        sample = raw[:4096].lower()
        if b"filereferences" in sample and b'"version"' in sample:
            return "live2d"
        if b'"skeleton"' in sample and b'"bones"' in sample:
            return "spine"
        if b'"armature"' in sample and b'"bone"' in sample:
            return "dragonbones"

    # 动画图片容器
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:8] == b"\x89PNG\r\n\x1a\n" and b"acTL" in raw[:64]:
        return "apng"
    return None


def extract_fairygui_dynamic_names(raw: bytes) -> list[str]:
    """从 FairyGUI 二进制包里提取疑似动态组件/动效名。

    这类名字不是独立 Unity 对象，而是 FGUI 包内的 MovieClip / 动效组件名；
    加上包名前缀后可作为「动态图像」候选，方便定位卡牌、横幅等动态 UI。
    """
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in re.findall(rb"[ -~]{3,}", raw):
        try:
            text = raw_name.decode("ascii")
        except Exception:
            continue
        # 去掉二进制里常见的前缀符号，如 ( + # ! 等
        clean = text.lstrip(" !\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
        if len(clean) < 3 or len(clean) > 80:
            continue
        low = clean.lower()
        if not any(k in low for k in DYNAMIC_FGUI_KEYWORDS):
            continue
        # 过滤纯哈希/短内部 ID 和明显非组件名
        if clean in ("FGUI", "Common", "Font", "atlas0", "atlas0.png"):
            continue
        if clean not in seen:
            seen.add(clean)
            names.append(clean)
    return names


def find_fairygui_atlas_texture(
    texture_index: dict[str, list[str]],
    fui_name: str,
) -> tuple[str, str] | None:
    """在贴图索引里找 FairyGUI 包对应的 atlas 贴图。

    FairyGUI 的 UI 图集通常命名为 ``包名_atlas0``，可能和 fui 包不在同一 bundle。
    返回 ``(bundle, texture_name)``；找不到返回 None。
    """
    stem = fui_name[:-4] if fui_name.lower().endswith("_fui") else fui_name
    patterns = {
        f"{stem}_atlas0",
        f"{fui_name}_atlas0",
        f"{stem}_atlas",
        f"{fui_name}_atlas",
    }
    stem_l = stem.lower()
    fui_l = fui_name.lower()
    for bundle, names in (texture_index or {}).items():
        for tex in names:
            low = tex.lower()
            if "atlas" not in low:
                continue
            if tex in patterns:
                return bundle, tex
            if low.startswith(stem_l + "_atlas") or low.startswith(fui_l + "_atlas"):
                return bundle, tex
    return None


def sequence_groups_from_names(
    names: Iterable[str],
    *,
    min_frames: int = SEQUENCE_MIN_FRAMES,
    bundle: str = "",
) -> list[SequenceGroup]:
    """把一组贴图名按“同前缀 + 数字帧号”聚合成序列帧组。"""
    by_base: dict[str, list[str]] = {}
    for name in names:
        match = SEQUENCE_RE.match(name)
        if not match or not match.group(1):
            continue
        by_base.setdefault(match.group(1), []).append(name)

    groups: list[SequenceGroup] = []
    for base, group_names in by_base.items():
        if len(group_names) >= min_frames:
            groups.append(SequenceGroup(bundle=bundle, base=base, names=tuple(group_names)))
    return groups


def sorted_sequence_names(names: Iterable[str]) -> list[str]:
    """按帧号升序排列序列帧名。"""
    return sorted(names, key=lambda name: int(_frame_index(name)))


def find_sequence_preview_texture(
    texture_names: Sequence[str],
    base_name: str,
) -> str | None:
    """在序列帧组里挑一张最靠前的贴图作为预览。"""
    candidates = [
        name for name in texture_names
        if SEQUENCE_RE.match(name) and SEQUENCE_RE.match(name).group(1) == base_name
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda name: int(_frame_index(name)))
