"""资源分类：按游戏贴图命名拆细；浏览页类型下拉后再进细分类。

索引覆盖 Addressable 的 *.bundle。
筹码 = UT_Relic_* 等命名前缀（游戏内部资源名），界面只叫「筹码」。
货币 = UT_Item_Currency（星币等），与筹码分开。
格子 UT_Land_Center 会在多地图包重复出现。

「动画」相关有两层，别混：
- 类型下拉「动画」= Unity AnimationClip（Idle/Walk/Hit 等动作片段，可导出）
- 贴图细分类「角色动作帧」= 序列帧贴图 Fight_000xx / Walk_000xx（一张张图）
「3D模型」= Mesh 三角面模型，不是动画。
"""
from __future__ import annotations


# 浏览页类型下拉（贴图可再分子分类）
ASSET_TYPES: list[tuple[str, str]] = [
    ("texture", "贴图"),
    ("text", "文本"),
    ("mesh", "3D模型"),
    ("anim", "动画"),
]

ASSET_TYPE_LABEL = {tid: label for tid, label in ASSET_TYPES}

# (id, 中文名, 说明, 前缀元组) — 自上而下优先 startswith；仅贴图用
CATEGORY_RULES: list[tuple[str, str, str, tuple[str, ...]]] = [
    # —— 卡牌 / 角色 ——
    ("hand_card", "手牌 / 技能卡", "对局打出的卡 UT_HandCard，约 404×400", ("UT_HandCard",)),
    ("hero_card", "角色卡面", "角色展示卡 / 细卡", ("UT_Hero_Card", "UT_Hero_ThinCard", "UT_Hero_Card2", "UT_CardFront")),
    ("hero_bust", "角色半身像", "大图半身", ("UT_Hero_Bust",)),
    ("standing", "立绘小图", "StandingPainting 图标", ("UT_Item_StandingPainting",)),
    ("hero_photo", "角色头像 / 升级图", "Profile / RolePhoto / LevelUp", ("UT_Hero_ProfilePhoto", "UT_Hero_RolePhoto", "UT_Hero_LevelUp")),
    ("monster", "怪物卡 / 半身", "怪物相关", ("UT_Monster_",)),
    ("cardback", "卡背", "手牌背面", ("UT_Item_CardBack", "UT_CardBack")),
    # —— 事件 / 状态 / 筹码 / 货币 ——
    ("event", "事件卡 / 事件图", "UT_Event、UT_MapEvent 等", ("UT_Event_", "UT_MapEvent_", "PlatformEvent", "LandEvent")),
    ("buff", "Buff / 状态图标", "增益减益图标", ("UT_Buff_",)),
    ("chip", "筹码", "局内筹码（资源名含 Relic 前缀，界面只叫筹码）", ("UT_Relic_", "UT_SGRelic", "UT_Platform_Relic", "LandRelic", "BattleRelic")),
    ("currency", "货币", "星币等 UT_Item_Currency（不是筹码）", ("UT_Item_Currency", "UT_BattlePass_Currencys")),
    ("mutator", "突变 / 词条", "Mutator", ("UT_Mutator_",)),
    # —— 地图 ——
    ("land", "地图格子", "棋盘格子 UT_Land_*（Center 会在多地图重复）", ("UT_Land_", "UT_LandLottery")),
    ("platform", "平台 / 特殊格", "商店、金币格等 Platform", ("UT_Platform_",)),
    ("map_scene", "地图场景 / 预览", "MapScene、MapPreview", ("UT_MapScene_", "UT_MapPreview_", "UT_MapScene")),
    # —— 装扮 / 商店 ——
    ("emoji", "表情", "聊天表情", ("UT_Item_Emoji",)),
    ("dice", "骰子外观", "骰子皮肤", ("UT_Item_Dice",)),
    ("bg", "主页背景条", "账号横幅背景", ("UT_Item_AccountBackground", "UT_AccountBackground", "UT_Item_KV", "UT_StoreBG")),
    ("avatar", "头像 / 玩家照", "头像框与照片", ("UT_AccountHeadShot", "UT_Item_PlayerPhoto")),
    ("achieve", "成就图标", "成就与结算", ("UT_Achieve",)),
    ("gacha", "扭蛋 / 主题", "GachaTheme 等", ("UT_GachaTheme_", "UT_SkinGroup")),
    ("battlepass", "战令 / 活动", "BattlePass、Activity", ("UT_BattlePass_", "UT_Activity_", "UT_Item_Activity")),
    ("item", "道具 / 礼物 / 宝箱", "其它道具", (
        "UT_Item_Chest", "UT_Item_Gift", "UT_Item_Material", "UT_Item_Hero", "UT_Item_Exchange", "UT_Item_",
    )),
    ("ui_icon", "UI 小图标", "icon_blj / UI_blj", ("icon_blj", "UI_blj", "T_UI_")),
    # —— 角色动作帧（贴图序列，常被当成「动画」找）——
    ("sprite_anim", "角色动作帧", "贴图序列：Fight/Walk/Idle/Hit 等一帧一张图", (
        "Fight", "Walk", "Idle", "Hit", "Show", "Die", "Talent", "Eat", "Cry", "Cheer",
        "Hospitalized", "Commentary", "Electricshock",
    )),
    # —— 少碰 ——
    ("fx", "特效（少碰）", "粒子/光效", ("lizi_blj", "Glow_blj", "baozha_blj", "yuanhuan_blj", "Mask_blj", "Smoke_blj", "tiaodai_blj")),
    ("lightmap", "光照贴图（别动）", "场景光照", ("Lightmap", "T_Light", "T_Wenli", "T_Line")),
    ("other", "其它", "未归入上面的贴图", ()),
]

# 默认折叠的少碰类（仍可点开，但限量加载）
ADVANCED_IDS = frozenset({"fx", "lightmap", "other"})

_SIZE_HINTS: list[tuple[int, int, int, str]] = [
    (404, 400, 8, "hand_card"),
    (760, 180, 12, "bg"),
    (220, 220, 8, "hero_photo"),
    (1568, 900, 20, "hero_bust"),
]


def categorize(texture_name: str, width: int = 0, height: int = 0) -> str:
    name = texture_name or ""
    for cat_id, _label, _desc, prefixes in CATEGORY_RULES:
        if cat_id == "other":
            continue
        for p in prefixes:
            if name.startswith(p):
                return cat_id
    if width > 0 and height > 0:
        for tw, th, tol, cid in _SIZE_HINTS:
            if abs(width - tw) <= tol and abs(height - th) <= tol:
                return cid
    return "other"


def category_label(cat_id: str) -> str:
    for cid, label, _d, _p in CATEGORY_RULES:
        if cid == cat_id:
            return label
    return cat_id


def category_desc(cat_id: str) -> str:
    for cid, _label, desc, _p in CATEGORY_RULES:
        if cid == cat_id:
            return desc
    return ""


def all_categories(*, include_advanced: bool = True) -> list[tuple[str, str]]:
    rows = []
    for cid, label, _d, _p in CATEGORY_RULES:
        if not include_advanced and cid in ADVANCED_IDS:
            continue
        rows.append((cid, label))
    return rows


def filter_by_category(
    index: dict[str, list[str]],
    cat_id: str,
    query: str = "",
    limit: int = 5000,
) -> list[tuple[str, str]]:
    q = (query or "").strip().lower()
    out: list[tuple[str, str]] = []
    for bundle, names in index.items():
        for tex in names:
            if cat_id not in ("all", "") and categorize(tex) != cat_id:
                continue
            if q and q not in tex.lower() and q not in bundle.lower():
                continue
            out.append((bundle, tex))
    out.sort(key=lambda x: x[1].lower())
    return out[:limit]


def count_by_category(index: dict[str, list[str]]) -> dict[str, int]:
    counts = {cid: 0 for cid, _, _, _ in CATEGORY_RULES}
    counts["all"] = 0
    for names in index.values():
        for tex in names:
            counts["all"] += 1
            cid = categorize(tex)
            counts[cid] = counts.get(cid, 0) + 1
    return counts


def count_assets(index: dict[str, list[str]]) -> int:
    return sum(len(names) for names in index.values())


def describe_selection(texture_name: str, width: int = 0, height: int = 0) -> str:
    cid = categorize(texture_name, width, height)
    label = category_label(cid)
    size = f" · {width}×{height}" if width and height else ""
    return f"{label} · {texture_name}{size}"


def dedupe_by_texture_name(
    rows: list[tuple[str, str]],
) -> list[tuple[str, str, int]]:
    """同名资源在多 bundle 出现时合并：[(bundle, name, 出现次数), ...]。"""
    order: list[str] = []
    first: dict[str, str] = {}
    count: dict[str, int] = {}
    for bundle, tex in rows:
        if tex not in first:
            first[tex] = bundle
            count[tex] = 1
            order.append(tex)
        else:
            count[tex] += 1
    return [(first[t], t, count[t]) for t in order]


def annotate_texture_name_duplicates(
    rows: list[tuple[str, str]],
) -> list[tuple[str, str, int]]:
    """保留每个 bundle，仅附上同名资源数量，供界面区分选择。"""
    counts: dict[str, int] = {}
    for _bundle, texture_name in rows:
        counts[texture_name] = counts.get(texture_name, 0) + 1
    return [(bundle, texture_name, counts[texture_name]) for bundle, texture_name in rows]
