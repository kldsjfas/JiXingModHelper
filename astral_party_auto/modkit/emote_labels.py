"""Friendly names verified through Character's localized name/title references.

Source evidence: qa-evidence/emote-media-oct12/character-labels.json. Unknown
characters keep their explicit number instead of borrowing a nearby name.
"""
from __future__ import annotations

import re


_CHARACTERS = {
    101: ("帕露南", "商业之主"),
    102: ("芬妮", "古怪神探"),
    103: ("阿兰娜", "社恐修女"),
    104: ("小町", "暗影忍者"),
    105: ("派德曼", "社员叔叔"),
    106: ("帕帕拉", "猩红辣妹"),
    107: ("恋", "游戏大师"),
    108: ("米米", "看板娘"),
    109: ("Z3000", "垃圾箱"),
    110: ("潘大猛", "肉弹战车"),
    111: ("墨影", "小猎手"),
    112: ("璐璐", "史莱姆"),
    113: ("姬梦枫", "旗袍娘"),
    114: ("蓝海晴", "命运少女"),
    115: ("美咲", "太刀使"),
    116: ("娜蒂斯", "绿洲女王"),
    117: ("茉莉", "家政机器人"),
    118: ("阿尔", "暗区少主"),
    119: ("星魅琉华", "午夜闪光"),
    120: ("南希露", "网络魅影"),
    121: ("凛", "新人调查员"),
    122: ("梅加斯", "机械超人"),
    123: ("姬梦朝", "风水师"),
    124: ("照", "三神御主"),
    125: ("摩西", "枪匠"),
    126: ("真梦梓", "沼之蛟龙"),
    127: ("邦妮", "毒苹果"),
    128: ("玲玲", "怪力乱神"),
    129: ("赛克斯", "魔渊幼体"),
    301: ("超绝最可爱天使酱", "超天酱"),
    302: ("主播女孩", "糖糖"),
    303: ("吉尔·斯汀雷", "吉尔"),
    304: ("多萝西·海兹", "多萝西"),
    305: ("远野汉娜", "汉娜"),
    306: ("橘雪莉", "雪莉"),
}
_VIDEO = re.compile(r"VEmoji_([0-9]{1,4})_([0-9]{1,3})((?:_[0-9]{1,3})*)\Z")


def _character_label(character: int) -> str:
    known = _CHARACTERS.get(character)
    if known is None:
        return f"角色{character}"
    name, title = known
    return f"{name}（{title}）"


def video_label(name: str) -> str:
    """Describe an internal video address without exposing it as UI prose."""
    match = _VIDEO.fullmatch(name) if isinstance(name, str) else None
    if match is None:
        return "未识别的动态表情"
    character, number = int(match[1]), int(match[2])
    if character == 0 or number == 0:
        return "未识别的动态表情"
    suffix = "".join(f"-{int(part):02d}" for part in match[3].split("_") if part)
    return f"{_character_label(character)} · 动态表情{number:02d}{suffix}"


def emote_label(identifier: int) -> str:
    """Describe a verified-style item id; callers still validate membership."""
    if isinstance(identifier, bool) or not isinstance(identifier, int):
        return "未识别的表情"
    character, number = divmod(identifier - 120_000_000, 1000)
    if not 0 < character < 10_000 or not 0 < number < 1000:
        return "未识别的表情"
    return f"{_character_label(character)} · 表情{number:02d}"
