"""Wwise 音频工作台：只在草稿中改包，安装仍交给现有 ModManager。"""
from __future__ import annotations

import hashlib
import gc
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import wave
from datetime import datetime
from pathlib import Path

from ..core.config import APP_ROOT, RESOURCE_ROOT
from .bundles import bundle_source_key, iter_bundle_entries


INDEX_SCHEMA_VERSION = 1


CATEGORIES = [
    {"id": "all", "label": "全部音频"},
    {"id": "music", "label": "音乐库"},
    {"id": "voice", "label": "语音库"},
    {"id": "effects", "label": "音效库"},
    {"id": "other", "label": "其他"},
]


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_json(path: Path, value) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _category(bank: dict) -> str:
    label = str(bank.get("name", "")).lower()
    language = str(bank.get("language", "")).lower()
    if language and language not in ("sfx", "none"):
        return "voice"
    if any(word in label for word in ("bgm", "music", "battle", "lobby")):
        return "music"
    if any(word in label for word in ("sfx", "effect", "ui", "sound")):
        return "effects"
    return "other"


def _group_identity(row: dict) -> tuple:
    return (row.get("bank_name", row.get("name", "")), row.get("language", ""),
            row.get("media_id"), row.get("sha256", row.get("id")))


def _group_rows(rows) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault(_group_identity(row), []).append(row)
    result = []
    for copies in groups.values():
        copies.sort(key=lambda row: (row["bundle"], str(row["object_id"]), row["id"]))
        leader = dict(copies[0])
        leader["copies"] = [dict(copy) for copy in copies]
        leader["copy_count"] = len(copies)
        blocked = next((copy for copy in copies if not copy.get("editable", False)), None)
        if blocked:
            leader.update(editable=False, reason=blocked.get("reason") or "关联音频副本不支持安全替换。")
        result.append(leader)
    return result


class AudioWorkspace:
    CACHE_LIMIT = 512 * 1024 * 1024

    def __init__(self, controller, data_dir: Path, made_dir: Path):
        self.controller = controller
        self.root = data_dir / "audio"
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_dir = self.root / "previews"
        self.cache_dir.mkdir(exist_ok=True)
        self.draft_dir = self.root / "draft"
        self.draft_dir.mkdir(exist_ok=True)
        self.made_dir = made_dir
        self._lock = threading.RLock()
        self._decode_lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        self._source_key = ""
        self._source_stamps: dict[str, tuple[int, int] | None] = {}
        self._status = {"scanning": False, "done": 0, "total": 0, "message": "尚未扫描音频"}
        self._tickets: dict[str, dict] = {}
        self._media: dict[str, Path] = {}
        self.items: list[dict] = []
        metadata = self.root / "draft.json"
        if metadata.exists():
            try:
                payload = json.loads(metadata.read_text(encoding="utf-8"))
                self.items = [row for row in payload if isinstance(row, dict) and self._safe_bundle(row.get("bundle", ""))]
            except (ValueError, TypeError):
                controller.log("音频草稿记录无法读取，原草稿文件已保留。")

    @staticmethod
    def _safe_bundle(name: str) -> bool:
        return bool(re.fullmatch(r"[\w.-]+\.bundle", str(name)))

    def _original(self, row: dict) -> Path:
        path = self.controller.original_bundle_path(row["bundle"])
        if path is None or not path.is_file():
            raise RuntimeError("音频资源包已不存在，请刷新游戏检测和音频索引。")
        return path

    def _source_stamp(self, bundle: str) -> tuple[int, int] | None:
        try:
            stat = self._original({"bundle": bundle}).stat()
            return stat.st_size, stat.st_mtime_ns
        except (OSError, RuntimeError):
            return None

    def _start_scan(self, refresh: bool) -> None:
        key = bundle_source_key(self.controller.aa_dirs)
        with self._lock:
            if self._status["scanning"]:
                return
            if self._source_key == key and not refresh:
                # 游戏也可能原地更新同名包，目录/catalog 的版本标识不会改变。
                # 只检查已发现的音频包，避免每次搜索都遍历全部游戏资源。
                if all(self._source_stamp(bundle) == stamp for bundle, stamp in self._source_stamps.items()):
                    return
                refresh = True
            self._source_key = key
            self._rows = {}
            self._source_stamps = {}
            self._status = {"scanning": True, "done": 0, "total": 0, "message": "正在扫描 Wwise 音频库…"}
        directories = tuple(self.controller.aa_dirs)
        threading.Thread(target=self._scan, args=(directories, key, refresh), daemon=True, name="audio-index").start()

    def _scan(self, directories, key: str, refresh: bool) -> None:
        from .audio_bank import list_audio_banks

        cache = self.root / "index.json"
        try:
            entries = list(iter_bundle_entries(directories))
            # 包时间戳和大小也参与缓存失效，游戏更新或安装 Mod 后不会沿用旧媒体列表。
            signature = hashlib.sha256(json.dumps([
                (entry.name, str(entry.path), entry.path.stat().st_size, entry.path.stat().st_mtime_ns)
                for entry in entries
            ]).encode()).hexdigest()
            if not refresh and cache.exists():
                try:
                    saved = json.loads(cache.read_text(encoding="utf-8"))
                    if (saved.get("schema_version") == INDEX_SCHEMA_VERSION
                            and saved.get("key") == key and saved.get("signature") == signature):
                        with self._lock:
                            self._rows = {row["id"]: row for row in saved["items"]}
                            self._source_stamps = {bundle: self._source_stamp(bundle)
                                                   for bundle in {row["bundle"] for row in self._rows.values()}}
                            self._status = {"scanning": False, "done": len(entries), "total": len(entries), "message": f"已载入 {len(self._rows)} 段音频"}
                        return
                except (ValueError, KeyError, TypeError):
                    pass
            skipped = 0
            for index, entry in enumerate(entries):
                try:
                    reference = self.controller.original_bundle_path(entry.name) or entry.path
                    reference_stat = reference.stat()
                    banks = list_audio_banks(reference)
                    new_rows = []
                    for bank in banks:
                        for media in bank.get("media", []):
                            if not media.get("codec"):
                                continue
                            identity = f"{entry.name}:{bank['object_id']}:{media['media_id']}"
                            row = dict(media)
                            row.update({
                                "id": hashlib.sha256(identity.encode()).hexdigest()[:32],
                                "bundle": entry.name, "object_id": str(bank["object_id"]),
                                "bank_name": bank["name"], "language": bank.get("language", ""),
                                "category": _category(bank),
                                "name": f"{bank['name']} · {media['media_id']}",
                                "description": f"{bank.get('language', 'SFX')} · {media.get('codec', '')} · {media.get('sample_rate', 0)} Hz · {media.get('channels', 0)} 声道",
                                "events": bank.get("event_names", []),
                            })
                            new_rows.append(row)
                    with self._lock:
                        self._rows.update({row["id"]: row for row in new_rows})
                        if new_rows:
                            self._source_stamps[entry.name] = (reference_stat.st_size, reference_stat.st_mtime_ns)
                except Exception:
                    skipped += 1
                with self._lock:
                    self._status = {"scanning": True, "done": index + 1, "total": len(entries), "message": f"扫描 {index + 1}/{len(entries)} 个资源包，已找到 {len(self._rows)} 段音频"}
                if index % 24 == 0:
                    gc.collect()
            with self._lock:
                _write_json(cache, {"schema_version": INDEX_SCHEMA_VERSION, "key": key,
                                    "signature": signature, "items": list(self._rows.values())})
                self._status.update(scanning=False, message=f"找到 {len(self._rows)} 段音频" + (f"；{skipped} 个包无法读取" if skipped else ""))
        except Exception as exc:
            with self._lock:
                self._status.update(scanning=False, error=str(exc), message=f"音频扫描失败：{exc}")
                self._source_key = ""

    def catalog(self, query="", category="all", offset=0, limit=40, refresh=False) -> dict:
        if not self.controller.has_game:
            raise RuntimeError("没有检测到游戏，请先刷新游戏检测。")
        self._start_scan(bool(refresh))
        needle = str(query).strip().lower()
        with self._lock:
            rows = [row for row in _group_rows(self._rows.values()) if
                    (category == "all" or row["category"] == category) and
                    (not needle or needle in (row["name"] + " " + row["language"] + " " + " ".join(row["events"])).lower())]
            rows.sort(key=lambda row: (row["bank_name"], row["language"], row["media_id"], row["bundle"]))
            start = max(0, int(offset))
            page = rows[start:start + max(1, min(100, int(limit)))]
            return {"items": page, "total": len(rows), "categories": CATEGORIES, "draft": self.draft_state()["items"], "status": dict(self._status)}

    def row(self, audio_id: str) -> dict:
        with self._lock:
            row = self._rows.get(str(audio_id))
            if row:
                copies = [candidate for candidate in self._rows.values()
                          if _group_identity(candidate) == _group_identity(row)]
                return _group_rows(copies)[0]
        # 重启后草稿仍可试听、移除和导出，不要求先完整扫包。
        row = next((item for item in self.items if item["id"] == audio_id), None)
        if row:
            return dict(row)
        raise RuntimeError("音频条目已失效，请重新扫描并选择。")

    def _bytes(self, row: dict, source: str) -> bytes:
        from .audio_bank import extract_bank_media
        if source not in ("original", "draft"):
            raise ValueError("未知音频来源。")
        path = self._original(row)
        if source == "draft":
            if not any(item["id"] == row["id"] for item in self.items):
                raise RuntimeError("此音频还没有替换草稿。")
            path = self.draft_dir / row["bundle"]
        return extract_bank_media(path, int(row["object_id"]), row["media_id"])

    def _decoder(self) -> Path:
        # 正式包的组件由用户从官方来源获取，放在 exe 旁；兼容旧试用包内置的位置。
        for root in (APP_ROOT, RESOURCE_ROOT):
            path = root / "tools" / "audio" / "vgmstream" / "vgmstream-cli.exe"
            if path.is_file():
                return path
        raise RuntimeError(
            "缺少音频解码组件。请双击程序旁的「获取音频解码组件.cmd」，完成后重新试听；"
            "源码用户运行 tools/audio/fetch_vgmstream.ps1。"
        )

    def _decode(self, raw: bytes) -> Path:
        # Browser requests and exports can ask for the same cache entry at once.
        with self._decode_lock:
            return self._decode_once(raw)

    def _decode_once(self, raw: bytes) -> Path:
        key = hashlib.sha256(raw).hexdigest()
        output = self.cache_dir / f"{key}.wav"
        if output.exists():
            output.touch()
            self._prune_cache(output)
            return output
        if len(raw) > 96 * 1024 * 1024:
            raise RuntimeError("单段音频超过 96 MB，暂不生成试听。")
        source = self.cache_dir / f"{key}.wem"
        temporary = self.cache_dir / f"{key}.part.wav"
        source.write_bytes(raw)
        try:
            result = subprocess.run([str(self._decoder()), "-i", "-o", str(temporary), str(source)],
                                    capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode != 0 or not temporary.exists():
                raise RuntimeError("此音频无法解码试听（可能是仅包含开头的流式片段）。")
            with wave.open(str(temporary), "rb") as stream:
                if stream.getnframes() == 0:
                    raise RuntimeError("音频没有可播放的采样。")
            if temporary.stat().st_size > self.CACHE_LIMIT:
                raise RuntimeError("解码后的音频超过 512 MB，暂不生成试听。")
            os.replace(temporary, output)
            self._prune_cache(output)
            return output
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("解码超过 60 秒，已停止；请改选较短的音频。") from exc
        finally:
            source.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)

    def _prune_cache(self, keep: Path) -> None:
        entries = []
        for path in self.cache_dir.glob("*.wav"):
            if path.name.endswith(".part.wav"):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime_ns, stat.st_size, path))
        total = sum(size for _modified, size, _path in entries)
        for _modified, size, path in sorted(entries):
            if total <= self.CACHE_LIMIT:
                break
            if path == keep:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # A current playback may still hold the file open on Windows.
                continue
            total -= size
            with self._lock:
                self._media = {handle: stored for handle, stored in self._media.items() if stored != path}

    def media_info(self, path: Path, name: str) -> dict:
        handle = secrets.token_urlsafe(24)
        with self._lock:
            self._media[handle] = path
            while len(self._media) > 128:
                self._media.pop(next(iter(self._media)))
        with wave.open(str(path), "rb") as stream:
            return {"url": f"/audio-media/{handle}", "name": name, "duration": stream.getnframes() / stream.getframerate(),
                    "sample_rate": stream.getframerate(), "channels": stream.getnchannels()}

    def media_path(self, handle: str) -> Path | None:
        with self._lock:
            return self._media.get(handle)

    def preview(self, audio_id: str, source="original") -> dict:
        row = self.row(audio_id)
        info = self.media_info(self._decode(self._bytes(row, source)), row["name"])
        info.update(editable=row.get("editable", False), reason=row.get("reason", ""),
                    note="按音频库和媒体编号显示；事件名用于搜索，不代表媒体与事件一一对应。")
        return info

    def prepare(self, audio_id: str, candidate: Path) -> dict:
        from .audio_bank import replace_bank_media
        if self._status["scanning"]:
            raise RuntimeError("请等待音频扫描完成，再制作替换，以确保同时处理所有资源副本。")
        row = self.row(audio_id)
        audio_id = row["id"]
        if not row.get("editable", False):
            raise RuntimeError(row.get("reason") or "该音频暂不支持替换。")
        if candidate.stat().st_size > 96 * 1024 * 1024:
            raise RuntimeError("替换音频超过 96 MB。")
        raw = candidate.read_bytes()
        # 候选先在临时包里完整试写；试听通过后才发放仅绑定当前条目的票据。
        token = secrets.token_urlsafe(24)
        checked = self.root / f"candidate-{token}.bundle"
        original = self._original(row)
        original_hash = _digest(original)
        try:
            replace_bank_media(original, int(row["object_id"]), row["media_id"], raw, checked,
                               expected_sha256=row.get("sha256"))
            preview = self.media_info(self._decode(raw), candidate.name)
            if _digest(original) != original_hash:
                raise RuntimeError("检查替换文件期间游戏资源发生变化，请重新选择。")
        finally:
            checked.unlink(missing_ok=True)
        payload = self.root / f"candidate-{token}.wem"
        payload.write_bytes(raw)
        for old_token, old in list(self._tickets.items()):
            if old["id"] == audio_id or len(self._tickets) >= 8:
                old["path"].unlink(missing_ok=True)
                del self._tickets[old_token]
        self._tickets[token] = {"id": audio_id, "path": payload, "name": candidate.name,
                                "original_hash": original_hash, "row": row}
        return {**preview, "ticket": token}

    def draft_state(self) -> dict:
        return {"items": [dict(item) for item in self.items]}

    def _check_draft(self, bundle: str | None = None) -> None:
        recorded = {item["bundle"] for item in self.items}
        present = {path.relative_to(self.draft_dir).as_posix() for path in self.draft_dir.rglob("*.bundle")}
        if present - recorded:
            raise RuntimeError("音频草稿目录含有未记录的资源包，请清空音频草稿后重新制作，避免带入旧修改。")
        seen = set()
        for item in self.items:
            if (bundle and item["bundle"] != bundle) or item["bundle"] in seen:
                continue
            if not (self.draft_dir / item["bundle"]).is_file():
                raise RuntimeError("音频草稿包已丢失，请移除该项后重新制作。")
            if _digest(self._original(item)) != item["original_hash"]:
                raise RuntimeError("游戏原音频包已更新，旧草稿不能继续覆盖；请移除旧草稿并重新制作。")
            seen.add(item["bundle"])

    def commit(self, audio_id: str, ticket: str) -> dict:
        from .audio_bank import replace_bank_media
        row = self.row(audio_id)
        audio_id = row["id"]
        candidate = self._tickets.get(ticket)
        if not candidate or candidate["id"] != audio_id:
            raise RuntimeError("替换文件与当前音频不匹配，请重新选择文件。")
        row = candidate.get("row", row)
        original = self._original(row)
        if _digest(original) != candidate["original_hash"]:
            raise RuntimeError("选择替换文件后游戏资源发生变化，请重新选择。")
        self._check_draft(row["bundle"])
        target = self.draft_dir / row["bundle"]
        source = target if target.exists() else original
        replace_bank_media(source, int(row["object_id"]), row["media_id"], candidate["path"].read_bytes(), target)
        item = {**row, "note": candidate["name"], "original_hash": candidate["original_hash"]}
        self.items = [existing for existing in self.items if existing["id"] != audio_id] + [item]
        _write_json(self.root / "draft.json", self.items)
        candidate["path"].unlink(missing_ok=True)
        del self._tickets[ticket]
        self.controller.log(f"音频已加入草稿：{row['name']} ← {item['note']}")
        return self.draft_state()

    def remove(self, audio_id: str) -> dict:
        from .audio_bank import replace_bank_media
        item = next((item for item in self.items if item["id"] == audio_id), None)
        if item is None:
            raise RuntimeError("音频草稿项不存在。")
        remaining = [row for row in self.items if row["id"] != audio_id]
        target = self.draft_dir / item["bundle"]
        if any(row["bundle"] == item["bundle"] for row in remaining):
            self._check_draft(item["bundle"])
            replace_bank_media(target, int(item["object_id"]), item["media_id"], self._bytes(item, "original"), target)
        else:
            target.unlink(missing_ok=True)
        self.items = remaining
        _write_json(self.root / "draft.json", self.items)
        return self.draft_state()

    def clear_draft(self) -> dict:
        # This directory belongs solely to the audio workspace. Installed mods
        # and original backups are stored elsewhere and remain untouched.
        for path in self.draft_dir.rglob("*.bundle"):
            path.unlink()
        (self.draft_dir / "mod_info.json").unlink(missing_ok=True)
        _write_json(self.root / "draft.json", [])
        self.items = []
        for candidate in self._tickets.values():
            candidate["path"].unlink(missing_ok=True)
        self._tickets.clear()
        self.controller.log("已清空音频制作草稿，游戏资源和已安装 Mod 保持不变。")
        return self.draft_state()

    def export_audio(self, audio_id: str, source: str, format: str, output: Path) -> Path:
        if format not in ("wav", "wem"):
            raise ValueError("只支持 WAV 或原始 WEM 导出。")
        row = self.row(audio_id)
        output = Path(output)
        resolved = output.resolve()
        protected_roots = [self.root.parent, *(Path(path) for path in self.controller.aa_dirs)]
        game_install = getattr(self.controller, "game_install", None)
        if game_install is not None and getattr(game_install, "install_dir", None):
            protected_roots.append(Path(game_install.install_dir))
        original = self._original(row)
        if (any(resolved.is_relative_to(path.resolve()) for path in protected_roots)
                or resolved == original.resolve()
                or (output.exists() and output.samefile(original))):
            raise ValueError("导出位置不能覆盖游戏资源、原版备份或制作草稿，请选择其他文件夹。")
        raw = self._bytes(row, source)
        decoded = self._decode(raw) if format == "wav" else None
        # Writing beside the destination and replacing it also avoids modifying a
        # game file through an existing hard link chosen in the save dialog.
        descriptor, temporary_name = tempfile.mkstemp(prefix=".audio-export-", suffix=".tmp", dir=output.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                if decoded is None:
                    stream.write(raw)
                else:
                    with decoded.open("rb") as cached:
                        shutil.copyfileobj(cached, stream)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        return output

    def export_pack(self) -> Path:
        from .maker import export_mod_pack
        if not self.items:
            raise RuntimeError("请先加入音频替换草稿。")
        self._check_draft()
        self.made_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        output = self.made_dir / f"音频替换_{stamp}.zip"
        with tempfile.TemporaryDirectory(prefix="audio-pack-", dir=self.root) as temporary:
            prepared = Path(temporary)
            self._build_pack(prepared)
            export_mod_pack(prepared, output, pack_name="音频替换", items=self.items)
        return output

    def _build_pack(self, directory: Path) -> None:
        from .audio_bank import replace_bank_media
        for item in self.items:
            replacement = self._bytes(item, "draft")
            for copy in item.get("copies") or [item]:
                if not self._safe_bundle(copy.get("bundle", "")):
                    raise RuntimeError("音频副本的资源包记录无效，请清空草稿后重新制作。")
                output = directory / copy["bundle"]
                source = output if output.exists() else self._original(copy)
                replace_bank_media(source, int(copy["object_id"]), copy["media_id"], replacement, output,
                                   expected_sha256=copy.get("sha256"))

    def install(self) -> None:
        if not self.items:
            raise RuntimeError("请先加入音频替换草稿。")
        self._check_draft()
        with tempfile.TemporaryDirectory(prefix="audio-install-", dir=self.root) as temporary:
            prepared = Path(temporary)
            self._build_pack(prepared)
            self.controller.install(prepared, name="音频替换")
