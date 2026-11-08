"""资源分类：按游戏贴图命名拆细；浏览页类型下拉后再进细分类。

索引覆盖 Addressable 的 *.bundle。
筹码 = UT_Relic_* 等命名前缀（游戏内部资源名），界面只叫「筹码」。
货币 = UT_Item_Currency（星币等），与筹码分开。
格子 UT_Land_Center 会在多地图包重复出现。

「动画」相关有两层，别混：
- 类型下拉「动画」= Unity AnimationClip（Idle/Walk/Hit 等动作片段，可导出）
- 技能演出 VSkill 等 CRI USM 影片收在「动态图像」，不属于 AnimationClip。
- 贴图细分类「角色动作帧」= 序列帧贴图 Fight_000xx / Walk_000xx（一张张图）
「3D模型」= Mesh 三角面模型，不是动画。
"""
from __future__ import annotations

import re


# 浏览页类型下拉（贴图可再分子分类）
ASSET_TYPES: list[tuple[str, str]] = [
    ("texture", "贴图"),
    ("text", "文本"),
    ("mesh", "3D模型"),
    ("anim", "动画"),
    ("dynamic", "动态图像"),
]

ASSET_TYPE_LABEL = {tid: label for tid, label in ASSET_TYPES}

# (id, 中文名, 说明, 前缀元组) — 自上而下优先 startswith；仅贴图用
CATEGORY_RULES: list[tuple[str, str, str, tuple[str, ...]]] = [
    # —— 卡牌 / 角色 ——
    ("hand_card", "手牌 / 技能卡", "对局打出的卡与异画卡面", ("UT_HandCard", "UT_IAltArt", "UT_AltArt_CardFront")),
    ("hero_card", "角色卡面", "角色展示卡 / 细卡 / 局内卡面", ("UT_Hero_Card", "UT_Hero_ThinCard", "UT_Hero_InGameCard", "UT_CardFront")),
    ("hero_bust", "角色半身像", "大图半身与剧情角色图", ("UT_Hero_Bust", "UT_Hero_Story", "UT_Summon_")),
    ("standing", "立绘小图", "立绘列表中的预览小图", ("UT_Item_StandingPainting",)),
    ("hero_photo", "角色头像 / 升级图", "角色头像、照片与升级展示图", ("UT_Hero_ProfilePhoto", "UT_Hero_RolePhoto", "UT_Hero_LevelUp")),
    ("monster", "怪物卡 / 半身", "怪物卡面与剧情表情", ("UT_Monster_", "Story_Monster_")),
    ("cardback", "卡背", "手牌背面", ("UT_Item_CardBack", "UT_CardBack")),
    # —— 事件 / 状态 / 筹码 / 货币 ——
    ("event", "事件卡 / 事件图", "UT_Event、UT_MapEvent 等", ("UT_Event_", "UT_MapEvent_", "PlatformEvent", "LandEvent")),
    ("buff", "状态图标", "增益减益图标", ("UT_Buff_",)),
    ("chip", "筹码", "局内筹码（资源名含 Relic 前缀，界面只叫筹码）", ("UT_Relic_", "UT_SGRelic", "UT_Platform_Relic", "LandRelic", "BattleRelic")),
    ("currency", "货币", "星币等 UT_Item_Currency（不是筹码）", ("UT_Item_Currency", "UT_BattlePass_Currencys")),
    ("mutator", "突变 / 词条", "突变模式与词条展示图", ("UT_Mutator_",)),
    ("destiny", "命运 / 占卜卡", "命运与占卜资源图", ("UT_Destiny_", "UT_Divination_")),
    # —— 地图 ——
    ("land", "地图格子", "棋盘格子 UT_Land_*（Center 会在多地图重复）", ("UT_Land_", "UT_LandLottery")),
    ("platform", "平台 / 特殊格", "商店、金币格等 Platform", ("UT_Platform_",)),
    ("map_scene", "地图场景 / 预览", "MapScene、MapPreview", ("UT_MapScene_", "UT_MapPreview_", "UT_MapScene")),
    ("building", "建筑 / 单人模式设施", "地图建筑、单人模式建筑与卡包", ("UT_Build_", "UT_SGBBuilding_", "UT_SGPack_")),
    ("game_mode", "关卡 / 难度", "战役关卡与难度图标", ("UT_Campaign_", "UT_Difficulty_")),
    # —— 装扮 / 商店 ——
    ("emoji", "表情", "聊天表情", ("UT_Item_Emoji",)),
    ("dice", "骰子外观", "骰子皮肤", ("UT_Item_Dice",)),
    ("kill_effect", "击败特效图标", "击败特效的展示图", ("UT_KillVfx_",)),
    ("bg", "主页背景条", "账号横幅背景", ("UT_Item_AccountBackground", "UT_AccountBackground", "UT_Item_KV", "UT_StoreBG")),
    ("avatar", "头像 / 玩家照", "头像框与照片", ("UT_AccountHeadShot", "UT_Item_PlayerPhoto")),
    ("achieve", "成就图标", "成就与结算", ("UT_Achieve",)),
    ("gacha", "扭蛋 / 主题", "扭蛋主题与皮肤系列展示图", ("UT_GachaTheme_", "UT_SkinGroup")),
    ("battlepass", "战令 / 活动", "战令与活动相关图片", ("UT_BattlePass_", "UT_Activity_", "UT_Item_Activity")),
    ("store", "商店 / 兑换 / 充值", "商品、兑换与充值展示图", ("UT_Exchange_", "UT_Recharge_", "UT_ProductRecommendation_", "UT_Store_", "UT_SkinSell_")),
    ("promotion", "活动宣传 / 横幅", "联动活动、公告与宣传图", ("UT_Banner_", "UT_Collaboration_", "UT_Acquisition_", "UT_NGO_", "UT_MGWT_", "UT_Survey_")),
    ("item", "道具 / 礼物 / 宝箱", "其它道具", (
        "UT_Item_Chest", "UT_Item_Gift", "UT_Item_Material", "UT_Item_Hero", "UT_Item_Exchange", "UT_Item_",
    )),
    ("ui_icon", "界面小图标", "按钮、提示与界面小图标", ("icon_blj", "UI_blj", "T_UI_")),
    ("guide", "教程 / 引导插图", "游戏内教程配图", ("GuidePic_",)),
    ("ui_atlas", "界面图集", "多个界面元素共用的图集，替换时需保留其它区域", ()),
    ("font", "字体图集", "文字渲染使用的字形图集", ()),
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
ADVANCED_IDS = frozenset({"fx", "lightmap", "font", "other"})

_CATEGORY_GROUPS = {
    "角色": {"hero_card", "hero_bust", "standing", "hero_photo", "monster", "sprite_anim"},
    "卡牌与对局": {"hand_card", "cardback", "event", "buff", "chip", "currency", "mutator", "destiny"},
    "地图与关卡": {"land", "platform", "map_scene", "building", "game_mode"},
    "装扮与收藏": {"emoji", "dice", "kill_effect", "bg", "avatar", "achieve", "gacha", "item"},
    "界面与活动": {"ui_icon", "ui_atlas", "guide", "battlepass", "store", "promotion"},
    "特效与技术资源": {"fx", "lightmap", "font", "other"},
}

# 非贴图分类独立命名，避免把相同名字的配置、动作和图片混为一类。
_TYPED_CATEGORIES = {
    "text": [
        ("text_language", "语言 / 本地化", "语言翻译与本地化文本"),
        ("text_story", "剧情 / 对话", "剧情、对话与任务文本"),
        ("text_character", "角色 / 皮肤配置", "角色与皮肤的可读配置"),
        ("text_card", "卡牌 / 道具配置", "卡牌、道具与状态的可读配置"),
        ("text_map", "地图 / 关卡配置", "地图、关卡与单人模式的可读配置"),
        ("text_config", "通用配置", "其它可读配置与数据表"),
        ("text_other", "其它文本", "尚未识别用途的可读文本"),
    ],
    "anim": [
        ("anim_walk", "行走 / 移动", "行走、奔跑、转向与移动动作"),
        ("anim_fight", "攻击 / 技能", "攻击、战斗与技能动作"),
        ("anim_reaction", "受击 / 倒地", "受击、死亡与异常状态动作"),
        ("anim_idle", "待机 / 展示", "待机、展示与登场动作"),
        ("anim_emotion", "表情 / 情绪", "喜怒哀乐与角色表演动作"),
        ("anim_ui", "界面 / 镜头", "界面与镜头动作片段"),
        ("anim_other", "其它动作", "尚未识别用途的动作片段"),
    ],
    "mesh": [
        ("mesh_card", "卡牌模型", "卡面与卡牌演出使用的网格"),
        ("mesh_character", "角色 / 怪物模型", "角色、怪物与骨骼网格"),
        ("mesh_dice", "骰子模型", "骰子外观使用的网格"),
        ("mesh_map", "场景 / 建筑模型", "地图、背景与建筑网格"),
        ("mesh_effect", "特效模型", "粒子与特效使用的网格"),
        ("mesh_other", "其它模型", "尚未识别用途的网格"),
    ],
    "dynamic": [
        ("sequence", "序列帧动画组", "由连续图片组成的动画"),
        ("fairygui", "界面动画", "游戏界面包与界面动效"),
        ("cri_usm", "视频 / 技能演出", "已识别的 CRI 影片对象"),
        ("other", "其它动态资源", "其它动态资源候选"),
    ],
}

_ANIMATION_WORDS = {
    "anim_walk": {"walk", "run", "move", "approach", "turn", "jump"},
    "anim_fight": {"fight", "attack", "skill", "talent", "battle", "cast", "shoot"},
    "anim_reaction": {"hit", "hurt", "die", "death", "hospitalized", "electricshock", "fall"},
    "anim_idle": {"idle", "show", "appear", "stand", "display", "login"},
    "anim_emotion": {"cry", "cheer", "eat", "happy", "angry", "laugh", "commentary", "win", "lose"},
    "anim_ui": {"ui", "camera", "cam", "shake", "open", "close", "button"},
}

_DISPLAY_WORDS = {
    "walk": "行走", "run": "奔跑", "move": "移动", "approach": "接近", "turn": "转向", "jump": "跳跃",
    "fight": "战斗", "attack": "攻击", "skill": "技能", "talent": "天赋", "battle": "对局", "cast": "施放", "shoot": "射击",
    "hit": "受击", "hurt": "受伤", "die": "倒地", "death": "死亡", "fall": "跌倒",
    "hospitalized": "住院", "electricshock": "触电",
    "idle": "待机", "show": "展示", "appear": "登场", "stand": "站立", "display": "展示",
    "cry": "哭泣", "cheer": "欢呼", "eat": "进食", "happy": "开心", "angry": "生气", "laugh": "大笑",
    "commentary": "解说", "win": "胜利", "lose": "失败", "loop": "循环",
    "front": "正面", "back": "背面", "side": "侧面", "left": "左向", "right": "右向",
    "hero": "角色", "monster": "怪物", "card": "卡牌", "hand": "手牌", "skin": "皮肤", "item": "道具",
    "common": "公共", "internal": "内部", "external": "外部", "home": "主页", "main": "主界面",
    "account": "账号", "info": "信息", "activity": "活动", "pass": "通行证", "light": "灯光",
    "brochure": "手册", "login": "登录", "setting": "设置", "settings": "设置", "store": "商店",
    "story": "剧情", "dialogue": "对话", "dialog": "对话", "quest": "任务", "mission": "任务",
    "dice": "骰子", "map": "地图", "land": "格子", "scene": "场景", "background": "背景",
    "ground": "地面", "building": "建筑", "body": "身体", "head": "头部", "hair": "头发",
    "expression": "表情", "emoji": "表情", "friend": "好友", "list": "列表", "chat": "聊天",
    "room": "房间", "wait": "等待", "match": "匹配", "settlement": "结算", "reward": "奖励",
    "gacha": "扭蛋", "guide": "引导", "tutorial": "教程", "tips": "提示", "notice": "公告", "mail": "邮件",
    "camera": "镜头", "cam": "镜头", "shake": "震动", "open": "展开", "close": "收起", "button": "按钮",
    "language": "语言", "localization": "本地化", "simplified": "简体", "traditional": "繁体",
    "chinese": "中文", "english": "英文", "japanese": "日文", "korean": "韩文",
    "game": "游戏", "data": "数据", "config": "配置", "table": "数据表", "buff": "状态", "relic": "筹码",
    "level": "等级", "up": "提升", "profile": "个人", "photo": "照片", "currency": "货币",
    "cn": "简中", "tc": "繁中", "int": "国际版", "pc": "电脑端", "sfw": "和谐版",
    "regular": "常规", "bold": "粗体", "italic": "斜体", "pve": "合作模式", "pvp": "对战模式",
}

_DISPLAY_TITLES = {category_id: label.split(" / ", 1)[0] for category_id, label, _description, _prefixes in CATEGORY_RULES}
_DISPLAY_TITLES.update({"hand_card": "手牌", "other": "其它资源", "bg": "主页背景", "fx": "特效", "lightmap": "光照贴图"})
_DISPLAY_PREFIXES = {
    prefix.lower(): _DISPLAY_TITLES[category_id]
    for category_id, _label, _description, prefixes in CATEGORY_RULES
    for prefix in prefixes
}
_DISPLAY_PREFIXES.update({
    "ut_ialtart": "异画卡面", "ut_altart_cardfront": "异画卡面",
    "ut_hero_thincard": "角色窄卡面", "ut_hero_ingamecard": "局内角色卡",
    "ut_hero_profilephoto": "角色头像", "ut_hero_rolephoto": "角色照片", "ut_hero_levelup": "升级展示图",
    "ut_item_chest": "宝箱", "ut_item_gift": "礼物", "ut_item_material": "材料",
    "ut_item_hero": "角色道具", "ut_item_exchange": "兑换道具",
    "ut_exchange": "兑换商品", "ut_recharge": "充值商品", "ut_productrecommendation": "推荐商品",
    "ut_destiny": "命运卡", "ut_divination": "占卜卡", "ut_difficulty": "难度图标",
    "ut_campaign_level": "战役关卡", "ut_sgbbuilding": "单人模式建筑", "ut_sgpack": "单人模式卡包",
    "ut_hero_story": "剧情角色图", "ut_summon": "召唤角色图",
    "vemoji": "动态表情", "vskill": "技能演出", "vfightbg": "战斗背景动画",
})
_SORTED_DISPLAY_PREFIXES = sorted(_DISPLAY_PREFIXES.items(), key=lambda pair: len(pair[0]), reverse=True)
_TEXT_DISPLAY_NAMES = {
    "card": "卡牌配置", "skin": "皮肤配置", "hero": "角色配置", "monster": "怪物配置",
    "item": "道具配置", "buff": "状态配置", "relic": "筹码配置", "map": "地图配置",
    "story": "剧情文本", "dialogue": "对话文本", "storydialogue": "剧情对话",
    "config": "通用配置", "setting": "设置配置",
}
_DISPLAY_ACTION_WORDS = set().union(*_ANIMATION_WORDS.values()) - {"ui", "camera", "cam", "button"}

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
    if re.search(r"_atlas\d+$", name, re.IGNORECASE):
        return "ui_atlas"
    if name.lower().endswith("_tmp"):
        return "font"
    if width > 0 and height > 0:
        for tw, th, tol, cid in _SIZE_HINTS:
            if abs(width - tw) <= tol and abs(height - th) <= tol:
                return cid
    return "other"


def asset_category(name: str, asset_type: str, bundle: str = "") -> str:
    """按资源用途分类；不根据名称声明资源可编辑或可播放。"""
    if asset_type == "texture":
        return categorize(name)
    name = name or ""
    lowered = name.lower()
    separated = re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
    words = set(re.findall(r"[a-z]+", separated.lower()))
    if asset_type == "anim":
        for category_id, candidates in _ANIMATION_WORDS.items():
            if words & candidates:
                return category_id
        return "anim_other"
    if asset_type == "mesh":
        if "card" in lowered:
            return "mesh_card"
        if "dice" in lowered:
            return "mesh_dice"
        if words & {"map", "land", "scene", "building", "background", "ground", "terrain"}:
            return "mesh_map"
        if words & {"hero", "monster", "character", "body", "head", "hair", "skeleton"}:
            return "mesh_character"
        if words & {"fx", "vfx", "effect", "particle", "trail"}:
            return "mesh_effect"
        return "mesh_other"
    if asset_type == "text":
        if words & {"language", "localization", "translation", "chinese", "english", "japanese", "korean"}:
            return "text_language"
        if words & {"story", "dialog", "dialogue", "quest", "mission", "conversation"}:
            return "text_story"
        if words & {"hero", "character", "skin", "monster"}:
            return "text_character"
        if words & {"card", "item", "relic", "buff", "mutator", "destiny"}:
            return "text_card"
        if words & {"map", "land", "level", "campaign", "dungeon", "singleplayer"}:
            return "text_map"
        if words & {"config", "setting", "data", "table", "gamedata"} or lowered.endswith((".json", ".csv", ".xml", ".yaml", ".yml")):
            return "text_config"
        return "text_other"
    if asset_type == "dynamic" and ("_fui" in lowered or lowered.endswith("fui")):
        return "fairygui"
    return "other"


def _readable_name_parts(value: str) -> list[str]:
    parts = []
    for token in re.split(r"[\s_.\-/\\]+", value):
        if re.fullmatch(r"[0-9a-fA-F]{16,}", token):
            parts.append(f"标识 {token[:8]}")
            continue
        separated = re.sub(r"([a-z])([A-Z])", r"\1_\2", token)
        for word in re.findall(r"[^\W\d_]+|\d+", separated):
            if word.lower() in {"ut", "fui", "tmp", "sprite", "texture", "mesh", "animation", "anim"}:
                continue
            parts.append(_DISPLAY_WORDS.get(word.lower(), word))
    return parts


def resource_display_name(name: str, asset_type: str, bundle: str = "") -> str:
    """为资源列表提供中文用途名；编号和原始身份由调用方分别保留。"""
    original_name = str(name or "").strip()
    stem = re.sub(r"\.(png|jpe?g|webp|gif|apng|json|csv|xml|ya?ml|txt|anim|asset)$", "", original_name, flags=re.IGNORECASE)
    category_id = asset_category(original_name, asset_type, bundle)
    lowered = stem.lower()
    atlas = re.fullmatch(r"(.+)_atlas(\d+)", stem, re.IGNORECASE)
    if atlas:
        return " · ".join(["界面图集", *_readable_name_parts(atlas[1]), f"图集 {atlas[2]}"])
    if lowered.endswith("_tmp"):
        font_name = re.sub(r"[-_.](regular|bold|italic)$", lambda match: " · " + _DISPLAY_WORDS[match[1].lower()], stem[:-4], flags=re.IGNORECASE)
        return f"字体图集 · {font_name}"
    if "_fui" in lowered:
        package_name, suffix = re.split(r"_fui", stem, maxsplit=1, flags=re.IGNORECASE)
        return " · ".join(["界面动画", *_readable_name_parts(package_name + suffix)])
    if asset_type == "text" and lowered in _TEXT_DISPLAY_NAMES:
        return _TEXT_DISPLAY_NAMES[lowered]
    if asset_type == "text" and lowered.startswith("gamedata"):
        return " · ".join(["游戏数据", *_readable_name_parts(stem[8:])])
    if asset_type in {"anim", "dynamic"} or (asset_type == "texture" and not lowered.startswith(("ut_", "story_"))):
        separated = re.sub(r"([a-z])([A-Z])", r"\1_\2", stem)
        words = re.findall(r"[^\W\d_]+|\d+", separated)
        action = next((word for word in words if word.lower() in _DISPLAY_ACTION_WORDS), None)
        if action:
            words.remove(action)
            parts = _readable_name_parts("_".join(words))
            frame = re.search(r"[_.-](\d{3,})$", stem)
            if frame and parts and parts[-1] == frame[1]:
                parts[-1] = f"帧 {frame[1]}"
            return " · ".join([_DISPLAY_WORDS[action.lower()], *parts])
    for prefix, title in _SORTED_DISPLAY_PREFIXES:
        if lowered.startswith(prefix):
            tail = stem[len(prefix):].lstrip("_.- ")
            return " · ".join([title, *_readable_name_parts(tail)])
    title = _DISPLAY_TITLES.get(category_id, category_label(category_id))
    details = _readable_name_parts(stem)
    if category_id == "text_language" and details:
        return " · ".join(details)
    return " · ".join([title, *details[-4:]])


def category_group(cat_id: str, asset_type: str = "texture") -> str:
    if cat_id in ("all", ""):
        return "全部资源"
    if asset_type == "texture":
        return next((group for group, members in _CATEGORY_GROUPS.items() if cat_id in members), "其它资源")
    groups = {
        "text": "文本与配置",
        "anim": "角色与界面动作",
        "mesh": "模型与场景",
        "dynamic": "动态素材类型",
    }
    return groups.get(asset_type, "其它资源")


def category_label(cat_id: str) -> str:
    for cid, label, _d, _p in CATEGORY_RULES:
        if cid == cat_id:
            return label
    for definitions in _TYPED_CATEGORIES.values():
        for cid, label, _description in definitions:
            if cid == cat_id:
                return label
    return cat_id


def category_desc(cat_id: str) -> str:
    for cid, _label, desc, _p in CATEGORY_RULES:
        if cid == cat_id:
            return desc
    for definitions in _TYPED_CATEGORIES.values():
        for cid, _label, description in definitions:
            if cid == cat_id:
                return description
    return ""


def all_categories(*, include_advanced: bool = True, asset_type: str = "texture") -> list[tuple[str, str]]:
    if asset_type != "texture":
        return [(cid, label) for cid, label, _description in _TYPED_CATEGORIES.get(asset_type, ())]
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
