"""生成工坊审核候选记录；不会自动下载、运行投稿或发布目录。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astral_party_auto.modkit.workshop import inspect_package, validate_catalog, validate_metadata


def build_entry(package: Path, metadata: dict, tag: str) -> dict:
    details = validate_metadata(metadata)
    inspection = inspect_package(package)
    entry = {**details, "revoked": True, "asset": {
        "tag": tag, "name": package.name, "size": inspection["size"], "sha256": inspection["sha256"]}}
    validate_catalog({"format": "jixing-workshop", "version": 1, "mods": [entry]})
    return entry


def _read_metadata(path: Path) -> dict:
    if not path.is_file() or not 0 < path.stat().st_size <= 64 * 1024:
        raise ValueError("作品资料文件缺失或太大。")
    def unique_fields(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("作品资料有重复字段。")
            result[name] = value
        return result
    value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_fields)
    # 客户端审核报告可以直接传入；其他报告字段不作为信任依据。
    return value.get("metadata", value) if isinstance(value, dict) else value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="结构校验，不能替代杀毒或人工审核")
    inspect.add_argument("package", type=Path)
    entry = commands.add_parser("entry", help="生成默认撤回的候选条目，完成审核后人工上架")
    entry.add_argument("package", type=Path)
    entry.add_argument("--metadata", type=Path, required=True)
    entry.add_argument("--tag", required=True)
    entry.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            result = inspect_package(args.package)
            print(json.dumps({"sha256": result["sha256"], "size": result["size"],
                              "expanded_size": result["expanded_size"], "file_count": result["file_count"],
                              "risk_note": result["risk_note"]}, ensure_ascii=False, indent=2))
        else:
            result = build_entry(args.package, _read_metadata(args.metadata), args.tag)
            with args.output.open("x", encoding="utf-8") as output:
                output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print("已生成审核候选条目（revoked=true）。完成来源审查、杀毒和兼容性测试后，人工改为 false 并合入可信目录。")
        return 0
    except (OSError, ValueError, KeyError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
