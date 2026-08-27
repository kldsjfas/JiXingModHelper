"""角色/怪物名单读取与运行时释放。"""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

from .config import APP_ROOT, RESOURCE_ROOT

CHARACTER_CSV_NAME = "角色表.csv"


def _candidate_builtin_paths() -> list[Path]:
    return [
        RESOURCE_ROOT / CHARACTER_CSV_NAME,
        Path(__file__).resolve().parents[2] / CHARACTER_CSV_NAME,
    ]


def ensure_character_csv() -> Path | None:
    """确保 exe 所在目录存在可编辑的角色表；不存在则从内置资源释放一份。"""
    app_path = APP_ROOT / CHARACTER_CSV_NAME
    if app_path.exists():
        return app_path

    for src in _candidate_builtin_paths():
        if not src.exists():
            continue
        try:
            APP_ROOT.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, app_path)
            return app_path
        except OSError:
            # 释放失败时退回只读内置文件
            return src
    return app_path if app_path.exists() else None


def load_character_list() -> list[str]:
    """从 exe 所在目录（或内置资源）的角色表读取角色/怪物/分类名，返回去重后的列表。"""
    path = ensure_character_csv()
    if path is None:
        return []
    for encoding in ("utf-8-sig", "gb18030", "gbk"):
        try:
            with path.open(encoding=encoding, newline="") as f:
                reader = csv.reader(f)
                names: list[str] = []
                seen: set[str] = set()
                for row in reader:
                    for cell in row:
                        name = (cell or "").strip()
                        if name and name not in seen:
                            seen.add(name)
                            names.append(name)
                if names:
                    return names
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    return []
