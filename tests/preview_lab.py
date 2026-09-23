"""用用户指定的游戏资源副本验证工作台。只在临时目录中保存作品集。"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from wsgiref.simple_server import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image
from astral_party_auto import mod_controller as control, web_app
from astral_party_auto.modkit.bundles import read_bundle_asset_names
from astral_party_auto.modkit.manager import ModManager

SAMPLES = {
    "013ae6e8eb4823d799bda4799ca36756.bundle": ("dynamic", "lianxutu_blj"),
    "0359139aab7abf8c1bcbb67ebc8cd3d3.bundle": ("text", "Common_fui"),
    "025248d9524b4b3d2551b1630254afa8.bundle": ("anim", "Cry"),
}


def checked(result):
    assert result["ok"], result
    return result.get("data")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    hashes = {name: hashlib.sha256((args.assets / name).read_bytes()).hexdigest() for name in SAMPLES}
    with TemporaryDirectory(prefix="jixing-studio-qa-") as temp:
        root = Path(temp)
        game, data, made = root / "game", root / "data", root / "made"
        for directory in (game, data, made):
            directory.mkdir()
        for name in SAMPLES:
            shutil.copy2(args.assets / name, game / name)
        animation = root / "替换动画.gif"
        frames = [Image.new("RGBA", (64, 64), color) for color in ("red", "green", "blue")]
        frames[0].save(animation, save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=0)
        from astral_party_auto.modkit.export_assets import export_by_type
        clip_bundle = list(SAMPLES)[2]
        clip_file = export_by_type("anim", game / clip_bundle, "Cry", root / "Cry.animbin")

        def detect(controller):
            controller.aa_dir = game
            controller.aa_dirs = (game,)
            controller.game_install = SimpleNamespace(name="隔离测试资源", cn_exe=root / "AstralParty_CN.exe", int_exe=None, install_dir=root)
            controller.manager = ModManager((game,), data)
            for name in SAMPLES:
                assets = read_bundle_asset_names(game / name)
                for kind in controller.typed_index:
                    controller.typed_index[kind][name] = assets.get(kind, [])

        changes = {"DATA_DIR": data, "MADE_DIR": made, "PREVIEW_DIR": data / "previews", "DRAFT_META": data / "draft.json",
                   "INDEX_CACHE": data / "index.json", "CHARACTER_LABELS_PATH": data / "labels.json", "DYNAMIC_VALID_PATH": data / "dynamic.json"}
        with patch.multiple(control, **changes), patch.multiple(web_app, DATA_DIR=data, MADE_DIR=made), patch.object(control.ModController, "refresh_detection", detect):
            api = web_app.DesktopApi()
            api._pick_path = lambda kind, **kwargs: clip_file if (api.controller.selection or {}).get("asset_type") == "anim" else animation
            if args.serve:
                token = secrets.token_urlsafe(32)
                server = make_server("127.0.0.1", 0, web_app._build_server(api, token))
                print(f"http://127.0.0.1:{server.server_port}/#token={token}", flush=True)
                server.serve_forever()
            else:
                bundle = list(SAMPLES)[1]
                checked(api.select_asset("text", bundle, "Common_fui"))
                state = checked(api.get_studio_state())
                texts = json.loads(state["full_text"])
                key = next(key for key, value in texts.items() if value == "确定")
                texts[key] = "确认测试"
                checked(api.commit_replacement(json.dumps(texts, ensure_ascii=False)))
                detail = checked(api.get_draft_detail(0))
                assert json.loads(detail["modified_text"])[key] == "确认测试"
                assert json.loads(detail["original_text"])[key] == "确定"
                assert json.loads(checked(api.get_studio_state())["full_text"])[key] == "确认测试"
                checked(api.commit_replacement(state["original_text"]))
                assert json.loads(checked(api.get_draft_detail(0))["modified_text"])[key] == "确定"
                bundle = list(SAMPLES)[0]
                selection = checked(api.select_asset("dynamic", bundle, "lianxutu_blj"))
                assert selection["editable"]
                before = checked(api.get_animation_preview(bundle, "lianxutu_blj", "original", 12))
                assert len(before["frames"]) == 10
                assert abs(before["durations"][0] - 1000 / 12) < 1
                checked(api.choose_replacement())
                checked(api.commit_replacement("", 12))
                detail = checked(api.get_draft_detail(1))
                assert detail["original_animation"]["frames"] != detail["modified_animation"]["frames"]
                assert detail["modified_animation"]["total"] == 10
                checked(api.remove_draft_item(1))
                assert len(api.controller.draft_items) == 1
                assert not (made / "_draft" / bundle).exists()
                selected = checked(api.select_asset("anim", clip_bundle, "Cry"))
                assert selected["playable"] and selected["animation_kind"] == "sprite_clip"
                cry = checked(api.get_animation_preview(clip_bundle, "Cry"))
                assert cry["total"] == 30 and abs(sum(cry["durations"]) - 1000) < 1
                candidate = checked(api.choose_replacement())
                assert candidate["animation"]["frames"] == cry["frames"]
                checked(api.commit_replacement())
                detail = checked(api.get_draft_detail(1))
                assert detail["original_animation"]["frames"] == detail["modified_animation"]["frames"]
                selected = checked(api.select_asset("dynamic", clip_bundle, "Cry"))
                assert selected["playable"] and selected["asset_type"] == "anim"
                checked(api.remove_draft_item(1))
                for name in SAMPLES:
                    assert hashlib.sha256((game / name).read_bytes()).hexdigest() == hashes[name]
                print("PASS: 文字修改/恢复、10帧序列替换、Cry原动画/候选/草稿30帧预览、动画与动态图像双入口；副本游戏包未改写。")
        for name in SAMPLES:
            assert hashlib.sha256((args.assets / name).read_bytes()).hexdigest() == hashes[name]


if __name__ == "__main__":
    main()
