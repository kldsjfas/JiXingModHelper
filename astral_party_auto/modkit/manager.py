"""安装 / 还原 mod：替换资源包前先备份原文件，随时可以一键还原。

解决三个痛点：替换麻烦、看不到实时图（预览在 bundles 里）、打了 mod 还原不了（这里备份）。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import contextmanager
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
            if path.is_file():
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

    def _ensure_original_backup(self, file_name: str, target: Path, state: dict) -> None:
        backup = self.backup_dir / file_name
        if backup.exists():
            try:
                with backup.open("rb") as bundle_file:
                    bundle_file.read(1)
            except OSError as exc:
                raise RuntimeError(f"原始备份无法读取：{file_name}，请先检查备份。") from exc
            return
        if any(file_name in mod.get("files", []) for mod in state.get("mods", {}).values()):
            raise RuntimeError(
                f"原始备份缺失：{file_name}，不能把已经替换过的游戏文件当成原版备份。"
                "请先恢复备份或重新校验游戏。"
            )
        # 复制失败不能留下半个正式备份，否则下次安装会把它当成可靠的原文件。
        with tempfile.NamedTemporaryFile(prefix="backup-", dir=self.backup_dir, delete=False) as temporary:
            pending = Path(temporary.name)
        try:
            shutil.copy2(target, pending)
            pending.replace(backup)
        finally:
            pending.unlink(missing_ok=True)

    @staticmethod
    def _mod_file_source(mod: dict, file_name: str) -> Path | None:
        store_value = mod.get("store")
        if store_value:
            candidate = Path(store_value) / file_name
            if candidate.is_file():
                return candidate
        source_value = mod.get("source")
        if source_value:
            source = Path(source_value)
            if source.is_dir():
                found = next((path for path in sorted(source.rglob(file_name)) if path.is_file()), None)
                if found:
                    return found
        return None

    def _apply_effective_files(self, state: dict, file_names) -> int:
        """按安装/启用顺序重算文件；后激活的 Mod 覆盖先激活的。"""
        mods = list(state.get("mods", {}).values())
        copies: list[tuple[Path, Path]] = []
        unavailable: list[str] = []
        for file_name in sorted(set(file_names)):
            target = self.bundle_path(file_name)
            if target is None:
                continue
            source = None
            has_active_mod = False
            for mod in reversed(mods):
                if mod.get("disabled") or file_name not in mod.get("files", []):
                    continue
                has_active_mod = True
                source = self._mod_file_source(mod, file_name)
                # 有效层丢失时不能悄悄退回更早的 Mod，让状态与实际文件不一致。
                break
            if not has_active_mod:
                source = self.backup_dir / file_name
            try:
                if source is None or not source.is_file():
                    unavailable.append(file_name)
                    continue
                with source.open("rb") as bundle_file:
                    bundle_file.read(1)
            except OSError:
                unavailable.append(file_name)
                continue
            copies.append((source, target))
        if unavailable:
            raise RuntimeError(
                "无法更改 Mod 状态：以下资源包的 Mod 来源或原始备份缺失、无法读取："
                + "、".join(unavailable)
                + "。请检查备份，或重新安装对应 Mod。"
            )
        for source, target in copies:
            shutil.copy2(source, target)
        return len(copies)

    @contextmanager
    def _file_transaction(self, file_names):
        """复制或保存状态失败时，恢复这次操作前的游戏文件。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        transaction_dir = Path(tempfile.mkdtemp(prefix="mod-change-", dir=self.data_dir))
        snapshots: list[tuple[Path, Path]] = []
        keep_recovery = False
        try:
            for index, file_name in enumerate(sorted(set(file_names))):
                target = self.bundle_path(file_name)
                if target is None:
                    continue
                snapshot = transaction_dir / str(index)
                shutil.copy2(target, snapshot)
                snapshots.append((snapshot, target))
            (transaction_dir / "recovery.json").write_text(
                json.dumps(
                    [{"copy": snapshot.name, "target": str(target)} for snapshot, target in snapshots],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            try:
                yield
            except Exception as exc:
                failed: list[str] = []
                leftover_temporary: list[str] = []
                for snapshot, target in snapshots:
                    pending = None
                    try:
                        # 数据目录和游戏可能在不同盘，替换必须在游戏文件所在盘暂存。
                        with tempfile.NamedTemporaryFile(
                            prefix=".mod-restore-", dir=target.parent, delete=False,
                        ) as temporary:
                            pending = Path(temporary.name)
                        shutil.copy2(snapshot, pending)
                        pending.replace(target)
                    except OSError:
                        failed.append(target.name)
                    finally:
                        if pending is not None:
                            try:
                                pending.unlink(missing_ok=True)
                            except OSError:
                                leftover_temporary.append(str(pending))
                if failed or leftover_temporary:
                    keep_recovery = True
                    temporary_note = (
                        "。以下恢复临时文件暂时无法清理：" + "、".join(leftover_temporary)
                        if leftover_temporary else ""
                    )
                    raise RuntimeError(
                        "Mod 操作失败，部分文件暂时无法回退："
                        + "、".join(failed)
                        + f"。操作前的副本已保留在：{transaction_dir}"
                        + temporary_note
                    ) from exc
                raise
        finally:
            if not keep_recovery:
                shutil.rmtree(transaction_dir, ignore_errors=True)

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
        store_root = self.data_dir / "mod_store"
        store_root.mkdir(parents=True, exist_ok=True)
        # 新缓存先完整写入独立目录；同名重装不能先删除正在读取的旧来源。
        store = Path(tempfile.mkdtemp(prefix=f"{mod_name}-", dir=store_root))
        try:
            applied: list[str] = []
            for fname in analysis.matched:
                target = self.bundle_path(fname)
                if target is None:
                    continue
                shutil.copy2(analysis.files_map[fname], store / fname)
                self._ensure_original_backup(fname, target, state)
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
            affected = old_files.union(applied)
            with self._file_transaction(affected):
                self._apply_effective_files(state, affected)
                self._save_state(state)
        except Exception:
            shutil.rmtree(store, ignore_errors=True)
            raise
        old_store = Path(old_info.get("store") or self._store_dir(mod_name))
        if old_store != store and old_store.exists():
            shutil.rmtree(old_store, ignore_errors=True)
        return analysis

    # ---- 还原 ----
    def uninstall_mod(self, name: str) -> int:
        state = self._load_state()
        mod = state["mods"].get(name)
        if not mod:
            return 0
        affected = list(mod.get("files", []))
        del state["mods"][name]
        with self._file_transaction(affected):
            restored = self._apply_effective_files(state, affected)
            self._save_state(state)
        store = Path(mod.get("store") or self._store_dir(name))
        if store.exists():
            shutil.rmtree(store, ignore_errors=True)
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
        files = mod.get("files", [])
        with self._file_transaction(files):
            restored = self._apply_effective_files(state, files)
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
        with self._file_transaction(files):
            applied = self._apply_effective_files(state, files)
            self._save_state(state)
        return applied

    def restore_all(self) -> int:
        """把所有备份过的原文件全部还原（终极还原按钮）。"""
        state = self._load_state()
        files = {backup.name for backup in self.backup_dir.glob("*.bundle")}
        for mod in state.get("mods", {}).values():
            files.update(mod.get("files", []))
        clean_state = {"mods": {}}
        with self._file_transaction(files):
            restored = self._apply_effective_files(clean_state, files)
            self._save_state(clean_state)
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
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"mods": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"无法读取 Mod 状态文件：{self.state_path}。为保护游戏文件已停止操作，请先检查或恢复该文件。"
            ) from exc
        valid = isinstance(data, dict) and isinstance(data.get("mods"), dict)
        if valid:
            for name, mod in data["mods"].items():
                if not isinstance(name, str) or not name or not isinstance(mod, dict):
                    valid = False
                    break
                files = mod.get("files")
                if (
                    not isinstance(files, list)
                    or any(not isinstance(file_name, str) or not file_name
                           or Path(file_name).name != file_name for file_name in files)
                    or ("disabled" in mod and not isinstance(mod["disabled"], bool))
                    or any(mod.get(field) is not None and not isinstance(mod[field], str)
                           for field in ("store", "source"))
                ):
                    valid = False
                    break
        if not valid:
            raise RuntimeError(
                f"Mod 状态文件结构不正确：{self.state_path}。为保护游戏文件已停止操作，请先检查或恢复该文件。"
            )
        return data

    def _save_state(self, state: dict) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)
