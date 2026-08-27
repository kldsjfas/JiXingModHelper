"""扫描本机游戏资源中 *_sfw 贴图及其无后缀版本，验证一键替换能找到配对。

用法（在仓库根目录运行）：
    python tests/sfw_scan/scan_sfw_textures.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from astral_party_auto.core.detector import find_game_install
from astral_party_auto.modkit.bundles import (
    bundle_dirs_for_exe,
    iter_bundle_entries,
    read_bundle_asset_names,
)


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

    # 包名 -> 贴图名列表
    texture_index: dict[str, list[str]] = {}
    for entry in entries:
        try:
            names_by_type = read_bundle_asset_names(entry.path)
        except Exception as exc:
            print("  跳过无法读取的包:", entry.name, exc)
            continue
        names = names_by_type.get("texture") or []
        if names:
            texture_index[entry.name] = names

    # 全局“无 _sfw 贴图名 -> 所在包”
    base_sources: dict[str, str] = {}
    for bundle_name, names in texture_index.items():
        for name in names:
            if not name.lower().endswith("_sfw") and name not in base_sources:
                base_sources[name] = bundle_name

    # 跨包匹配 *_sfw -> 无后缀
    pairs: list[tuple[str, str, str, str]] = []  # target_bundle, sfw_name, source_bundle, base_name
    for bundle_name, names in texture_index.items():
        for name in names:
            if not name.lower().endswith("_sfw"):
                continue
            base = name[: -len("_sfw")]
            source_bundle = base_sources.get(base)
            if source_bundle:
                pairs.append((bundle_name, name, source_bundle, base))

    print("\n匹配结果:")
    print("  找到 _sfw 贴图数量:", sum(1 for b, n, _, _ in pairs if n.lower().endswith('_sfw')))
    print("  找到可配对数量:", len(pairs))
    print("  涉及目标资源包数量:", len({p[0] for p in pairs}))
    print("  涉及源资源包数量:", len({p[2] for p in pairs}))

    if pairs:
        print("\n前 30 条配对示例:")
        for target_bundle, sfw_name, source_bundle, base_name in pairs[:30]:
            print(f"    {target_bundle}  [{sfw_name}]")
            print(f"      <- {source_bundle}  [{base_name}]")
    else:
        print("  没有找到任何 *_sfw 与无后缀贴图的配对。")
        print("  请确认当前游戏版本是否包含这类资源，或先运行一次“刷新索引”。")

    return 0 if pairs else 2


if __name__ == "__main__":
    raise SystemExit(main())
