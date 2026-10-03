"""用显式指定的游戏音频包副本验证完整工作流；不会安装到真实游戏。"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import shutil
import sys
import time
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from wsgiref.simple_server import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astral_party_auto import mod_controller as control, web_app
from astral_party_auto.modkit.audio_bank import extract_bank_media
from astral_party_auto.modkit.manager import ModManager


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked(result):
    assert result["ok"], result
    return result["data"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source_hash = digest(args.bundle)
    with TemporaryDirectory(prefix="jixing-audio-qa-") as folder, TemporaryDirectory(prefix="jixing-audio-exports-") as exports:
        root = Path(folder)
        game, data, made = (root / name for name in ("game", "data", "made"))
        for path in (game, data, made):
            path.mkdir()
        sample = game / "aa-audio-test.bundle"
        duplicate = game / "zz-audio-copy.bundle"
        shutil.copy2(args.bundle, sample)
        shutil.copy2(args.bundle, duplicate)

        def detect(controller):
            controller.aa_dir = game
            controller.aa_dirs = (game,)
            controller.game_install = SimpleNamespace(name="音频隔离测试", cn_exe=root / "AstralParty_CN.exe", int_exe=None, install_dir=root)
            controller.manager = ModManager(game, data)

        changes = {"DATA_DIR": data, "MADE_DIR": made, "PREVIEW_DIR": data / "previews", "DRAFT_META": data / "draft.json",
                   "INDEX_CACHE": data / "index.json", "CHARACTER_LABELS_PATH": data / "labels.json", "DYNAMIC_VALID_PATH": data / "dynamic.json"}
        with patch.multiple(control, **changes), patch.multiple(web_app, DATA_DIR=data, MADE_DIR=made), patch.object(control.ModController, "refresh_detection", detect):
            api = web_app.DesktopApi()
            checked(api.audio_catalog())
            workspace = api._audio()
            deadline = time.monotonic() + 60
            while workspace._status["scanning"] and time.monotonic() < deadline:
                time.sleep(.1)
            assert not workspace._status["scanning"], workspace._status
            catalog = checked(api.audio_catalog(limit=100))
            assert len(workspace._rows) == catalog["total"] * 2
            rows = [row for row in catalog["items"] if row["editable"] and not row["music_track"]]
            first = rows[0]
            assert first["copy_count"] == 2
            second = next(row for row in rows[1:] if row["object_id"] == first["object_id"] and
                          row["channels"] == first["channels"] and row["sample_rate"] == first["sample_rate"])
            original = {row["id"]: extract_bank_media(sample, int(row["object_id"]), row["media_id"]) for row in (first, second)}
            candidate = root / "试听替换.wem"
            candidate.write_bytes(original[second["id"]])
            api._pick_path = lambda kind, **kwargs: candidate if kind == "open" else Path(exports) / kwargs.get("save_filename", "export.wav")
            if args.serve:
                token = secrets.token_urlsafe(32)
                from socketserver import ThreadingMixIn
                from wsgiref.simple_server import WSGIServer
                class ThreadingServer(ThreadingMixIn, WSGIServer):
                    daemon_threads = True
                server = make_server("127.0.0.1", 0, web_app._build_server(api, token), server_class=ThreadingServer)
                print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/#token={token}", "first_id": first["id"], "second_id": second["id"], "root": str(root)}, ensure_ascii=False), flush=True)
                server.serve_forever()
                return

            info = checked(api.audio_preview(first["id"]))
            assert info["duration"] > 0
            prepared = checked(api.audio_choose_replacement(first["id"]))
            assert not api.audio_commit(second["id"], prepared["ticket"])["ok"]
            checked(api.audio_commit(first["id"], prepared["ticket"]))
            assert not api.audio_commit(first["id"], prepared["ticket"])["ok"]
            candidate.write_bytes(original[first["id"]])
            prepared = checked(api.audio_choose_replacement(second["id"]))
            checked(api.audio_commit(second["id"], prepared["ticket"]))
            draft = workspace.draft_dir / sample.name
            assert workspace._bytes(first, "draft") == original[second["id"]]
            assert workspace._bytes(second, "draft") == original[first["id"]]
            checked(api.audio_preview(first["id"], "draft"))
            checked(api.audio_remove(first["id"]))
            assert extract_bank_media(draft, int(first["object_id"]), first["media_id"]) == original[first["id"]]
            assert workspace._bytes(second, "draft") == original[first["id"]]
            before = digest(draft)
            candidate.write_bytes(b"invalid MP3 data")
            assert not api.audio_choose_replacement(second["id"])["ok"]
            assert digest(draft) == before
            exported = checked(api.audio_export_pack())
            with zipfile.ZipFile(exported["path"]) as archive:
                assert sample.name in archive.namelist() and duplicate.name in archive.namelist() and "mod_info.json" in archive.namelist()
            assert digest(sample) == source_hash
            checked(api.audio_install())
            assert digest(sample) == digest(draft)
            assert extract_bank_media(duplicate, int(second["object_id"]), second["media_id"]) == original[first["id"]]
            # 安装之后原版来源转为备份；继续制作仍须保留已存在的另一段修改。
            candidate.write_bytes(original[second["id"]])
            prepared = checked(api.audio_choose_replacement(first["id"]))
            checked(api.audio_commit(first["id"], prepared["ticket"]))
            checked(api.audio_install())
            for installed_copy in (sample, duplicate):
                assert extract_bank_media(installed_copy, int(first["object_id"]), first["media_id"]) == original[second["id"]]
                assert extract_bank_media(installed_copy, int(second["object_id"]), second["media_id"]) == original[first["id"]]
            checked(api.audio_remove(first["id"]))
            checked(api.audio_install())
            for installed_copy in (sample, duplicate):
                assert extract_bank_media(installed_copy, int(first["object_id"]), first["media_id"]) == original[first["id"]]
                assert extract_bank_media(installed_copy, int(second["object_id"]), second["media_id"]) == original[first["id"]]
            api.controller.disable_mod("音频替换")
            assert digest(sample) == source_hash
            assert digest(duplicate) == source_hash
            api.controller.enable_mod("音频替换")
            assert digest(sample) == digest(draft)
            api.controller.uninstall("音频替换")
            assert digest(sample) == source_hash
            assert digest(duplicate) == source_hash
            checked(api.audio_remove(second["id"]))
            assert not draft.exists() and not workspace.items
            report = {"passed": True, "sample": args.bundle.name, "media_count": len(workspace._rows),
                      "checks": ["真实音频解码", "绑定资源的单次票据", "同包两段互换", "移除单项保留另一项", "错误文件不污染草稿", "同源副本合并与完整ZIP", "安装后继续编辑并重装双包", "安装后移除单项保留其他修改", "双包隔离安装禁用启用卸载", "源包哈希不变"],
                      "source_sha256": source_hash, "duration_seconds": info["duration"]}
            assert digest(args.bundle) == source_hash
            if args.report:
                args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
