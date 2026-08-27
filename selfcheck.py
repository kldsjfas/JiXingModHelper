"""公开仓库用安全自检：默认只操作系统临时目录，不碰真实游戏与作品集。"""
from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

failures: list[str] = []


def check(name: str, action) -> None:
    started = time.perf_counter()
    try:
        action()
        elapsed = time.perf_counter() - started
        print(f"  OK  {name}  ({elapsed:.3f}s)")
    except Exception as exc:
        elapsed = time.perf_counter() - started
        print(f" FAIL {name}  ({elapsed:.3f}s): {exc}")
        failures.append(name)


def check_static_files() -> None:
    from astral_party_auto.web_app import WEB_DIR

    required = (
        WEB_DIR / "index.html",
        WEB_DIR / "app.js",
        WEB_DIR / "styles.css",
        ROOT / "native_host" / "Program.cs",
        ROOT / "native_host" / "JiXingModHelperHost.csproj",
    )
    missing = [str(path) for path in required if not path.exists()]
    assert not missing, f"缺少文件：{missing}"


def check_categories() -> None:
    from astral_party_auto.modkit.categories import (
        ASSET_TYPES,
        annotate_texture_name_duplicates,
        category_label,
    )

    assert category_label("chip") == "筹码"
    assert [type_id for type_id, _label in ASSET_TYPES] == [
        "texture",
        "text",
        "mesh",
        "anim",
    ]
    rows = annotate_texture_name_duplicates([
        ("a.bundle", "Same"),
        ("b.bundle", "Same"),
        ("c.bundle", "Only"),
    ])
    assert rows == [
        ("a.bundle", "Same", 2),
        ("b.bundle", "Same", 2),
        ("c.bundle", "Only", 1),
    ]


def check_migration_matching() -> None:
    import astral_party_auto.modkit.migration as migration

    old_records = [
        migration.MigrationTexture("HeroCard", 1024, 1024),
        migration.MigrationTexture("HeroIcon", 256, 256),
    ]
    current_records = {
        "new-main.bundle": old_records,
        "other.bundle": [migration.MigrationTexture("HeroCard", 1024, 1024)],
    }
    previous_scan = migration.scan_bundle_textures
    try:
        migration.scan_bundle_textures = lambda path: (
            old_records
            if Path(path).name == "old.bundle"
            else [old_records[0]]
            if Path(path).name == "single.bundle"
            else current_records.get(Path(path).name, [])
        )
        plan = migration.plan_bundle_migration(
            "old.bundle",
            {
                "new-main.bundle": ["HeroCard", "HeroIcon"],
                "other.bundle": ["HeroCard"],
            },
            lambda name: Path(name),
        )
        assert len(plan.matched) == 2
        assert {entry.target_bundle for entry in plan.matched} == {"new-main.bundle"}

        ambiguous = migration.plan_bundle_migration(
            "single.bundle",
            {"a.bundle": ["HeroCard"], "b.bundle": ["HeroCard"]},
            lambda name: Path(name),
        )
        assert len(ambiguous.ambiguous) == 1
        assert not ambiguous.matched
    finally:
        migration.scan_bundle_textures = previous_scan


def check_hot_draft_bundle_names() -> None:
    import astral_party_auto.mod_controller as module

    class FakeManager:
        def __init__(self, paths):
            self.paths = paths

        def bundle_path(self, name):
            return self.paths.get(name)

    with TemporaryDirectory(prefix="jixing_hot_draft_") as root_value:
        root = Path(root_value)
        data_dir = root / "data"
        made_dir = root / "made"
        data_dir.mkdir()
        made_dir.mkdir()
        hot_a = root / "cache-a" / ("a" * 32) / "__data"
        hot_b = root / "cache-b" / ("b" * 32) / "__data"
        hot_a.parent.mkdir(parents=True)
        hot_b.parent.mkdir(parents=True)
        hot_a.write_bytes(b"A")
        hot_b.write_bytes(b"B")
        image = root / "replacement.png"
        image.write_bytes(b"PNG")
        logical_a = "a" * 32 + ".bundle"
        logical_b = "b" * 32 + ".bundle"
        paths = {logical_a: hot_a, logical_b: hot_b}

        previous = (
            module.DATA_DIR,
            module.MADE_DIR,
            module.DRAFT_META,
            module.PREVIEW_DIR,
            module.replace_bundle_texture,
        )

        def fake_replace(src, _image, out, target_name=None, **_kwargs):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(Path(src).read_bytes() + b":" + str(target_name).encode())
            return str(target_name)

        try:
            module.DATA_DIR = data_dir
            module.MADE_DIR = made_dir
            module.DRAFT_META = data_dir / "draft.json"
            module.PREVIEW_DIR = data_dir / "previews"
            module.replace_bundle_texture = fake_replace

            controller = object.__new__(module.ModController)
            controller.aa_dirs = (root,)
            controller.manager = FakeManager(paths)
            controller.draft_items = []
            controller.draft_name = "自检"
            controller.log = lambda _message: None

            controller.add_texture_to_draft(
                hot_a,
                image,
                "TextureA",
                bundle_name=logical_a,
            )
            controller.add_texture_to_draft(
                hot_b,
                image,
                "TextureB",
                bundle_name=logical_b,
            )

            assert {item["bundle"] for item in controller.draft_items} == {logical_a, logical_b}
            assert (made_dir / "_draft" / logical_a).exists()
            assert (made_dir / "_draft" / logical_b).exists()
            assert not (made_dir / "_draft" / "__data").exists()
            assert controller.original_bundle_path(logical_a) == hot_a
        finally:
            (
                module.DATA_DIR,
                module.MADE_DIR,
                module.DRAFT_META,
                module.PREVIEW_DIR,
                module.replace_bundle_texture,
            ) = previous


def check_hot_update_cache() -> None:
    from astral_party_auto.modkit.bundles import (
        aa_dir_for_exe,
        bundle_dirs_for_exe,
        bundle_file_map,
        hot_update_dir_for_exe,
    )
    from astral_party_auto.modkit.manager import ModManager

    with TemporaryDirectory(prefix="jixing_cache_") as root_value:
        root = Path(root_value)
        executable = root / "game" / "8vJXn6CN" / "AstralParty_CN.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"")

        legacy_dir = (
            executable.parent
            / "AstralParty_CN_Data"
            / "StreamingAssets"
            / "aa"
            / "StandaloneWindows64"
        )
        legacy_dir.mkdir(parents=True)
        profile = root / "profile"
        active_name = "a" * 32 + ".bundle"
        stale_name = "b" * 32 + ".bundle"
        legacy_only_name = "c" * 32 + ".bundle"
        obsolete_name = "d" * 32 + ".bundle"
        (legacy_dir / active_name).write_bytes(b"LEGACY_ACTIVE")
        (legacy_dir / legacy_only_name).write_bytes(b"LEGACY_ONLY")
        (legacy_dir / obsolete_name).write_bytes(b"OBSOLETE")
        assert aa_dir_for_exe(executable, user_profile=profile) == legacy_dir
        assert bundle_dirs_for_exe(executable, user_profile=profile) == (legacy_dir,)

        cache_dir = hot_update_dir_for_exe(executable, user_profile=profile)
        active_data = cache_dir / ("1" * 32) / Path(active_name).stem / "__data"
        stale_data = cache_dir / ("2" * 32) / Path(stale_name).stem / "__data"
        active_data.parent.mkdir(parents=True)
        stale_data.parent.mkdir(parents=True)
        active_data.write_bytes(b"ORIGINAL")
        stale_data.write_bytes(b"STALE")

        catalog = cache_dir.parent / "catalog_3.2.0.json"
        catalog.write_text(
            json.dumps({
                "m_InternalIds": [
                    rf"{{App.WebServerConfig.Path}}\{active_name}",
                    rf"{{App.WebServerConfig.Path}}\{legacy_only_name}",
                ]
            }),
            encoding="utf-8",
        )
        catalog.with_suffix(".hash").write_text("test-catalog-hash", encoding="ascii")

        assert aa_dir_for_exe(executable, user_profile=profile) == cache_dir
        resource_dirs = bundle_dirs_for_exe(executable, user_profile=profile)
        assert resource_dirs == (cache_dir, legacy_dir)
        bundle_paths = bundle_file_map(resource_dirs)
        assert bundle_paths == {
            active_name: active_data,
            legacy_only_name: legacy_dir / legacy_only_name,
        }

        mod_dir = root / "mod"
        mod_dir.mkdir()
        (mod_dir / active_name).write_bytes(b"MODIFIED")
        (mod_dir / legacy_only_name).write_bytes(b"MODIFIED_LEGACY")
        manager = ModManager(resource_dirs, root / "data")
        manager.install_mod(mod_dir, "缓存布局测试")
        assert active_data.read_bytes() == b"MODIFIED"
        assert (legacy_dir / active_name).read_bytes() == b"LEGACY_ACTIVE"
        assert (legacy_dir / legacy_only_name).read_bytes() == b"MODIFIED_LEGACY"
        manager.disable_mod("缓存布局测试")
        assert active_data.read_bytes() == b"ORIGINAL"
        assert (legacy_dir / legacy_only_name).read_bytes() == b"LEGACY_ONLY"
        manager.enable_mod("缓存布局测试")
        assert active_data.read_bytes() == b"MODIFIED"
        assert (legacy_dir / legacy_only_name).read_bytes() == b"MODIFIED_LEGACY"
        manager.restore_all()
        assert active_data.read_bytes() == b"ORIGINAL"
        assert (legacy_dir / legacy_only_name).read_bytes() == b"LEGACY_ONLY"


def check_taptap_exe_detection() -> None:
    from astral_party_auto.core.detector import _build_install

    with TemporaryDirectory(prefix="jixing_taptap_") as root_value:
        root = Path(root_value)
        exe = root / "吉星派对.exe"
        exe.write_bytes(b"")
        install = _build_install("taptap", "吉星派对", root, launcher="taptap")
        assert install.cn_exe == exe
        assert install.int_exe is None
        assert install.launcher == "taptap"


def check_mod_layering() -> None:
    from astral_party_auto.modkit.manager import ModManager

    with TemporaryDirectory(prefix="jixing_layer_") as root_value:
        root = Path(root_value)
        game = root / "game"
        data = root / "data"
        mod_a = root / "mod_a"
        mod_b = root / "mod_b"
        for folder in (game, mod_a, mod_b):
            folder.mkdir(parents=True)
        (game / "same.bundle").write_bytes(b"ORIGINAL")
        (mod_a / "same.bundle").write_bytes(b"MOD_A")
        (mod_b / "same.bundle").write_bytes(b"MOD_B")

        manager = ModManager(game, data)
        manager.install_mod(mod_a, "A")
        manager.install_mod(mod_b, "B")
        assert (game / "same.bundle").read_bytes() == b"MOD_B"

        manager.disable_mod("A")
        assert (game / "same.bundle").read_bytes() == b"MOD_B"
        manager.enable_mod("A")
        assert (game / "same.bundle").read_bytes() == b"MOD_A"
        manager.disable_mod("A")
        assert (game / "same.bundle").read_bytes() == b"MOD_B"

        manager.disable_mod("B")
        assert (game / "same.bundle").read_bytes() == b"ORIGINAL"
        manager.enable_mod("B")
        assert (game / "same.bundle").read_bytes() == b"MOD_B"
        manager.uninstall_mod("B")
        assert (game / "same.bundle").read_bytes() == b"ORIGINAL"
        assert manager.is_installed("A")
        manager.uninstall_mod("A")
        assert (game / "same.bundle").read_bytes() == b"ORIGINAL"


def check_draft_removal() -> None:
    import astral_party_auto.mod_controller as module

    with TemporaryDirectory(prefix="jixing_draft_") as root_value:
        root = Path(root_value)
        previous = (module.MADE_DIR, module.DRAFT_META, module.PREVIEW_DIR)
        try:
            module.MADE_DIR = root / "made_mods"
            module.DRAFT_META = root / "draft.json"
            module.PREVIEW_DIR = root / "previews"
            draft_dir = module.MADE_DIR / "_draft"
            draft_dir.mkdir(parents=True)
            removed_bundle = draft_dir / "removed.bundle"
            removed_bundle.write_bytes(b"MODIFIED")

            controller = object.__new__(module.ModController)
            controller.draft_items = [
                {"kind": "texture", "bundle": "removed.bundle", "name": "TextureA"},
                {"kind": "texture", "bundle": "kept.bundle", "name": "TextureB"},
            ]
            controller.draft_name = "自检作品集"
            controller.manager = None
            controller.log = lambda _message: None
            controller.remove_draft_item(0)

            assert not removed_bundle.exists()
            assert len(controller.draft_items) == 1
        finally:
            module.MADE_DIR, module.DRAFT_META, module.PREVIEW_DIR = previous


def check_archive_cleanup() -> None:
    from astral_party_auto.mod_controller import ModController

    class FakeManager:
        @staticmethod
        def analyze_mod(mod_dir: Path):
            assert (Path(mod_dir) / "demo.bundle").exists()
            return SimpleNamespace(matched=["demo.bundle"], unmatched=[], files_map={})

        @staticmethod
        def install_mod(mod_dir: Path, _name: str):
            assert (Path(mod_dir) / "demo.bundle").exists()
            return SimpleNamespace(matched=["demo.bundle"], unmatched=[])

    with TemporaryDirectory(prefix="jixing_archive_") as root_value:
        root = Path(root_value)
        game = root / "game"
        game.mkdir()
        archive = root / "demo.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("demo.bundle", b"DEMO")

        controller = object.__new__(ModController)
        controller.aa_dir = game
        controller.aa_dirs = (game,)
        controller.manager = FakeManager()
        controller.log = lambda _message: None
        controller._pending_extract_dir = None
        controller._pending_extract_source = None

        controller.analyze(archive)
        pending = controller._pending_extract_dir
        assert pending is not None and pending.exists()
        controller.install(archive, "Demo")
        assert not pending.exists()
        assert controller._pending_extract_dir is None

        controller.analyze(archive)
        pending = controller._pending_extract_dir
        assert pending is not None and pending.exists()
        controller.close()
        assert not pending.exists()


def check_game_readonly() -> None:
    from astral_party_auto.mod_controller import ModController

    controller = ModController(lambda _message: None)
    try:
        assert controller.has_game, "未检测到游戏"
        if not controller.index_ready:
            print("      INFO 资源索引尚未建立，跳过贴图预览")
            return
        rows = controller.browse("hand_card", "", 3, asset_type="texture")
        assert rows, "手牌分类为空"
        selection = controller.set_selection(*rows[0], asset_type="texture")
        assert Path(selection["preview"]).exists()
    finally:
        controller.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--with-game",
        action="store_true",
        help="额外运行只读游戏检测与预览；默认不访问游戏目录。",
    )
    args = parser.parse_args()

    print("=== safe selfcheck ===")
    check("static files", check_static_files)
    check("categories", check_categories)
    check("migration matching", check_migration_matching)
    check("hot-cache draft bundle names", check_hot_draft_bundle_names)
    check("hot-update cache layout", check_hot_update_cache)
    check("taptap exe detection", check_taptap_exe_detection)
    check("mod layering", check_mod_layering)
    check("draft removal", check_draft_removal)
    check("archive cleanup", check_archive_cleanup)
    if args.with_game:
        check("game read-only preview", check_game_readonly)

    if failures:
        print("FAILED:", ", ".join(failures))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
