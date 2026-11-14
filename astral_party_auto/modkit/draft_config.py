"""作品集里 GameData 配置副本与当前游戏版本的同步。

动态表情与动态卡面/立绘都会把整份 GameData 配置包复制进作品集。游戏更新后包名可能不变，
旧副本若继续作为制作底稿或被安装，会把新版本的全部配置退回旧版本，新卡牌也无法制作。
这里只重放工具自己写入的修改（Character 表情、Card/Skin 图片地址），其余内容一律取当前游戏版本。
"""
from __future__ import annotations

from pathlib import Path

from .animation import _load_bundle, _release_readers
from .emote_workspace import item_bundle_names

# 动态表情与动态卡面工具会改写的配置表；比较版本时忽略这些表，其余对象必须与当前游戏一致。
EDITED_TABLES = frozenset(("Card", "Skin", "Character"))
_REPLAYABLE_KINDS = ("emote", "visual_animation")


def _config_items(draft_items: list[dict], config_name: str) -> list[tuple[int, dict]]:
    return [(position, item) for position, item in enumerate(draft_items)
            if isinstance(item, dict) and config_name in item_bundle_names(item)]


def replayable(draft_items: list[dict], config_name: str) -> bool:
    """作品集对配置包的修改全部来自可重放的工具时才允许自动重建。"""
    return all(item.get("bundle") == config_name and item.get("kind") in _REPLAYABLE_KINDS
               for _, item in _config_items(draft_items, config_name))


@_release_readers
def config_matches_game(draft: Path, original: Path) -> bool:
    """除工具改写的表外，草稿配置的每个对象都与当前游戏原始配置逐字节一致。"""
    def untouched(path: Path) -> dict:
        result = {}
        for obj in _load_bundle(path).objects:
            if obj.type.name == "TextAsset" and obj.peek_name() in EDITED_TABLES:
                continue
            key = (obj.assets_file.name, obj.path_id)
            if key in result:
                raise ValueError("资源包包含重复对象标识。")
            result[key] = (obj.type.name, obj.get_raw_data())
        return result

    return untouched(draft) == untouched(original)


def rebuild_draft_config(draft_items: list[dict], config_name: str, original: Path, folder: Path) -> Path:
    """以当前游戏配置为底，按顺序重放作品集里已有的表情和动态卡面配置修改。"""
    from .emote_config import replace_emote_config_bundle
    from .visual_config import replace_visual_config_bundle

    current = Path(original)
    for position, item in _config_items(draft_items, config_name):
        kind = item.get("kind")
        if item.get("bundle") != config_name or kind not in _REPLAYABLE_KINDS:
            raise ValueError(f"作品集中的「{item.get('name') or kind}」也修改了游戏配置，无法自动合并")
        if kind == "emote":
            output = Path(folder) / f"config-rebuild-{position}.bundle"
            replace_emote_config_bundle(current, output, item["emote_id"], item["video_name"])
            current = output
            continue
        tables = item.get("tables")
        if not tables or not isinstance(item.get("name"), str) or not isinstance(item.get("movieclip_url"), str):
            raise ValueError(f"动态作品「{item.get('name')}」记录不完整")
        for index, table in enumerate(tables):
            output = Path(folder) / f"config-rebuild-{position}-{index}.bundle"
            replace_visual_config_bundle(current, output, table, item["name"], item["movieclip_url"])
            current = output
    return current


def current_draft_config(draft_items: list[dict], config_name: str, draft: Path, original: Path,
                         folder: Path, log=None) -> Path:
    """返回可作为制作底稿的配置：草稿与当前游戏版本一致时用草稿，过时则重建。

    作品集里还有无法重放的配置修改（例如文本编辑）时保持原有行为，直接使用草稿。
    """
    draft, original = Path(draft), Path(original)
    if not draft.is_file():
        return original
    if not replayable(draft_items, config_name) or config_matches_game(draft, original):
        return draft
    rebuilt = rebuild_draft_config(draft_items, config_name, original, folder)
    if log is not None:
        log("作品集中的游戏配置早于当前游戏版本，已按当前版本重建并保留已有的表情和动态作品。")
    return rebuilt
