"""安装 / 还原 mod：替换资源包前先备份原文件，随时可以一键还原。

解决三个痛点：替换麻烦、看不到实时图（预览在 bundles 里）、打了 mod 还原不了（这里备份）。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .bundles import BundleDirectories, bundle_file_map


@dataclass
class ModAnalysis:
    name: str
    mod_dir: Path
    files_map: dict[str, Path] = field(default_factory=dict)  # 资源包名 -> mod 里的实际路径
    matched: list[str] = field(default_factory=list)          # 当前游戏里存在、能替换的
    unmatched: list[str] = field(default_factory=list)        # 当前版本已不存在（版本对不上）

    @property
    def total(self) -> int:
        return len(self.files_map)


class ModManager:
    def __init__(self, aa_dirs: BundleDirectories, data_dir: str | Path):
        if isinstance(aa_dirs, (str, Path)):
            self.aa_dirs = (Path(aa_dirs),)
        else:
            self.aa_dirs = tuple(Path(directory) for directory in aa_dirs)
        self.aa_dir = self.aa_dirs[0]
        self.data_dir = Path(data_dir)
        self.backup_dir = self.data_dir / "backups"
        self.state_path = self.data_dir / "mods_state.json"
        self._bundle_paths = bundle_file_map(self.aa_dirs)

    @property
    def bundle_count(self) -> int:
        return len(self._bundle_paths)

    def bundle_path(self, file_name: str) -> Path | None:
        return self._bundle_paths.get(Path(file_name).name)

    # ---- 分析 ----
    def analyze_mod(self, mod_dir: str | Path) -> ModAnalysis:
        mod_dir = Path(mod_dir)
        files_map: dict[str, Path] = {}
        for path in sorted(mod_dir.rglob("*.bundle")):
            files_map.setdefault(path.name, path)
        matched = [name for name in files_map if self.bundle_path(name) is not None]
        unmatched = [name for name in files_map if self.bundle_path(name) is None]
        return ModAnalysis(
            name=mod_dir.name,
            mod_dir=mod_dir,
            files_map=files_map,
            matched=sorted(matched),
            unmatched=sorted(unmatched),
        )

    @staticmethod
    def _safe_mod_name(name: str) -> str:
        invalid = '<>:"/' + chr(92) + '|?*'
        cleaned = str(name or "").translate(str.maketrans({char: "_" for char in invalid})).strip(" .")
        return cleaned[:80] or "未命名 Mod"

    def _store_dir(self, mod_name: str) -> Path:
        return self.data_dir / "mod_store" / self._safe_mod_name(mod_name)

    @staticmethod
    def _mod_file_source(mod: dict, file_name: str) -> Path | None:
        store_value = mod.get("store")
        if store_value:
            candidate = Path(store_value) / file_name
            if candidate.exists():
                return candidate
        source_value = mod.get("source")
        if source_value:
            source = Path(source_value)
            if source.is_dir():
                candidate = source / file_name
                if candidate.exists():
                    return candidate
                found = next(source.rglob(file_name), None)
                if found:
                    return found
        return None

    def _apply_effective_files(self, state: dict, file_names) -> int:
        """按安装/启用顺序重算文件；后激活的 Mod 覆盖先激活的。"""
        mods = list(state.get("mods", {}).values())
        changed = 0
        for file_name in sorted(set(file_names)):
            source = None
            for mod in reversed(mods):
                if mod.get("disabled") or file_name not in mod.get("files", []):
                    continue
                source = self._mod_file_source(mod, file_name)
                if source:
                    break
            target = self.bundle_path(file_name)
            if target is None:
                continue
            if source:
                shutil.copy2(source, target)
                changed += 1
                continue
            backup = self.backup_dir / file_name
            if backup.exists():
                shutil.copy2(backup, target)
                changed += 1
        return changed

    # ---- 安装 ----
    def install_mod(self, mod_dir: str | Path, name: str | None = None) -> ModAnalysis:
        analysis = self.analyze_mod(mod_dir)
        mod_name = self._safe_mod_name(name or analysis.name)
        if not analysis.matched:
            raise RuntimeError("这个 mod 里没有一个资源包和当前游戏版本对得上，无法安装。")
        state = self._load_state()
        old_info = state.get("mods", {}).get(mod_name) or {}
        old_files = set(old_info.get("files", []))

        self.backup_dir.mkdir(parents=True, exist_ok=True)
        store = self._store_dir(mod_name)
        if store.exists():
            shutil.rmtree(store, ignore_errors=True)
        store.mkdir(parents=True, exist_ok=True)

        applied: list[str] = []
        for fname in analysis.matched:
            target = self.bundle_path(fname)
            if target is None:
                continue
            backup = self.backup_dir / fname
            if not backup.exists():  # 只在第一次替换时备份最原始的文件
                shutil.copy2(target, backup)
            shutil.copy2(analysis.files_map[fname], target)
            # 缓存一份，禁用后再启用不依赖临时解压目录
            shutil.copy2(analysis.files_map[fname], store / fname)
            applied.append(fname)

        # 重新安装同名 Mod 也算最后激活，确保覆盖顺序与用户操作一致。
        state["mods"].pop(mod_name, None)
        state["mods"][mod_name] = {
            "installed_at": datetime.now().isoformat(timespec="seconds"),
            "files": applied,
            "source": str(Path(mod_dir)),
            "store": str(store),
            "disabled": False,
        }
        self._apply_effective_files(state, old_files.difference(applied))
        self._save_state(state)
        return analysis

    # ---- 还原 ----
    def uninstall_mod(self, name: str) -> int:
        state = self._load_state()
        mod = state["mods"].get(name)
        if not mod:
            return 0
        affected = list(mod.get("files", []))
        del state["mods"][name]
        restored = self._apply_effective_files(state, affected)
        store = Path(mod.get("store") or self._store_dir(name))
        if store.exists():
            shutil.rmtree(store, ignore_errors=True)
        self._save_state(state)
        return restored

    def disable_mod(self, name: str) -> int:
        """禁用：把该 mod 改过的文件还原成备份，但保留记录，可再启用。"""
        state = self._load_state()
        mod = state["mods"].get(name)
        if not mod:
            raise RuntimeError(f"未找到已装 mod：{name}")
        if mod.get("disabled"):
            return 0
        mod["disabled"] = True
        state["mods"][name] = mod
        restored = self._apply_effective_files(state, mod.get("files", []))
        self._save_state(state)
        return restored

    def enable_mod(self, name: str) -> int:
        """启用：从 mod_store 或 source 再拷回游戏目录。"""
        state = self._load_state()
        mod = state["mods"].get(name)
        if not mod:
            raise RuntimeError(f"未找到已装 mod：{name}")
        if not mod.get("disabled"):
            return 0
        files = list(mod.get("files", []))
        unavailable = []
        for file_name in files:
            # 当前版本已移除的包不会参与替换，无须再检查它的来源。
            if self.bundle_path(file_name) is None:
                continue
            try:
                source = self._mod_file_source(mod, file_name)
                if source is None:
                    unavailable.append(file_name)
                    continue
                with source.open("rb") as bundle_file:
                    bundle_file.read(1)
            except OSError:
                unavailable.append(file_name)
        if unavailable:
            raise RuntimeError(
                "无法启用：以下资源包的文件缺失或无法读取："
                + "、".join(unavailable)
                + "。请重新安装该 Mod。"
            )
        mod["disabled"] = False
        # 再启用等同于最后激活：它应覆盖当前启用 Mod 的同名资源。
        state["mods"].pop(name, None)
        state["mods"][name] = mod
        applied = self._apply_effective_files(state, files)
        self._save_state(state)
        return applied

    def restore_all(self) -> int:
        """把所有备份过的原文件全部还原（终极还原按钮）。"""
        restored = 0
        for backup in self.backup_dir.glob("*.bundle"):
            target = self.bundle_path(backup.name)
            if target is not None:
                shutil.copy2(backup, target)
                restored += 1
        self._save_state({"mods": {}})
        shutil.rmtree(self.data_dir / "mod_store", ignore_errors=True)
        return restored

    def installed_mods(self) -> list[dict]:
        state = self._load_state()
        result = []
        for name, info in state.get("mods", {}).items():
            result.append({
                "name": name,
                "count": len(info.get("files", [])),
                "installed_at": info.get("installed_at", ""),
                "source": info.get("source", ""),
                "disabled": bool(info.get("disabled", False)),
                "store": info.get("store", ""),
            })
        return result

    def is_installed(self, name: str) -> bool:
        return name in self._load_state().get("mods", {})

    # ---- 状态存取 ----
    def _load_state(self) -> dict:
        if not self.state_path.exists():
            return {"mods": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            data.setdefault("mods", {})
            return data
        except (OSError, json.JSONDecodeError):
            return {"mods": {}}

    def _save_state(self, state: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)
