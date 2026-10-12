"""Independent emote address overlays with version checks and owned file records."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .bundles import _latest_catalog_path

MANIFEST_NAME = "emote_addresses.json"
MANIFEST_FORMAT = "jixing-emote-addresses"
MAX_MANIFEST_BYTES = 128 * 1024
MAX_CATALOG_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_PATCHES = 128
VIDEO_KEY = re.compile(r"VEmoji_[0-9]+(?:_[0-9]+)+\Z")
OWNED_BUNDLE = re.compile(r"jixing_emote_([0-9]+)\.bundle\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def file_sha256(path: Path, limit: int = MAX_BUNDLE_BYTES) -> str:
    if not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise ValueError(f"文件缺失或超过允许大小：{path.name}")
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("表情地址清单包含重复字段。")
        result[key] = value
    return result


def validate_patch(patch: dict) -> dict:
    fields = {"emote_id", "video_name", "bundle_name", "template_video_key", "bundle_size"}
    if not isinstance(patch, dict) or set(patch) != fields:
        raise ValueError("表情地址清单字段不正确。")
    identifier, size = patch["emote_id"], patch["bundle_size"]
    if (not isinstance(identifier, int) or isinstance(identifier, bool)
            or not 120_000_000 < identifier < 130_000_000
            or not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BUNDLE_BYTES):
        raise ValueError("表情地址清单编号或大小不合法。")
    character, number = divmod(identifier - 120_000_000, 1000)
    if not 0 < character < 10_000 or not 0 < number < 1000:
        raise ValueError("表情地址清单角色或表情编号不合法。")
    if (patch["video_name"] != f"VEmoji_{character}_{number:02d}_999"
            or patch["bundle_name"] != f"jixing_emote_{identifier}.bundle"
            or not isinstance(patch["template_video_key"], str)
            or not VIDEO_KEY.fullmatch(patch["template_video_key"])):
        raise ValueError("表情地址清单必须使用本工具生成的独立名称。")
    return dict(patch)


def validate_patches(patches) -> list[dict]:
    if not isinstance(patches, list) or not 1 <= len(patches) <= MAX_PATCHES:
        raise ValueError("表情地址清单需要包含 1 到 128 项。")
    result = [validate_patch(patch) for patch in patches]
    if len({patch["video_name"] for patch in result}) != len(result):
        raise ValueError("表情地址清单包含重复的独立地址。")
    return result


def read_address_manifest(mod_dir: Path, files: dict[str, Path]) -> list[dict]:
    manifest = mod_dir / MANIFEST_NAME
    if not manifest.exists():
        if any(OWNED_BUNDLE.fullmatch(name) for name in files):
            raise ValueError("独立表情资源缺少本工具的地址清单，无法安全安装。")
        return []
    if not manifest.is_file() or not 0 < manifest.stat().st_size <= MAX_MANIFEST_BYTES:
        raise ValueError("表情地址清单缺失或超过 128 KB。")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("无法读取表情地址清单。") from exc
    if (not isinstance(value, dict) or set(value) != {"format", "version", "patches"}
            or value["format"] != MANIFEST_FORMAT or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("这不是本工具支持的表情地址清单。")
    patches = validate_patches(value["patches"])
    for patch in patches:
        source = files.get(patch["bundle_name"])
        if (source is None or source.resolve() != (mod_dir / patch["bundle_name"]).resolve()
                or not source.is_file() or source.stat().st_size != patch["bundle_size"]):
            raise ValueError("独立表情资源缺失、位置或大小与清单不一致。")
    declared = {patch["bundle_name"] for patch in patches}
    if any(OWNED_BUNDLE.fullmatch(name) and name not in declared for name in files):
        raise ValueError("分享包包含未声明的独立表情资源。")
    return patches


def write_address_manifest(mod_dir: str | Path, patches: list[dict]) -> Path:
    """Write the strict manifest used by the installer and exported packs."""
    checked = validate_patches(patches)
    target = Path(mod_dir) / MANIFEST_NAME
    value = {"format": MANIFEST_FORMAT, "version": 1, "patches": checked}
    atomic_bytes(target, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
    return target


def atomic_bytes(target: Path, payload: bytes, *, before_commit=None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix=".emote-catalog-", suffix=".tmp", dir=target.parent)
    pending = Path(name)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if before_commit is not None:
            before_commit()
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)


def active_catalog_path(aa_dirs: tuple[Path, ...]) -> Path:
    for directory in aa_dirs:
        candidate = _latest_catalog_path(directory)
        if candidate is not None:
            return candidate.resolve()
    raise ValueError("缺少可验证的本地热更新 catalog，请先启动并更新游戏资源后再制作独立表情。")


def _official_hash(path: Path) -> str:
    hash_path = path.with_suffix(".hash")
    if not hash_path.is_file() or not 0 < hash_path.stat().st_size <= 4096:
        raise ValueError("本地 catalog 缺少可验证的官方版本 hash，已停止安装。")
    value = hash_path.read_text(encoding="ascii").strip()
    if not value:
        raise ValueError("官方 catalog 版本 hash 为空，已停止安装。")
    return value


@dataclass
class CatalogChange:
    catalog_path: Path
    payload: bytes
    copies: list[tuple[Path, Path]] = field(default_factory=list)
    deletes: list[Path] = field(default_factory=list)
    expected_existing: dict[Path, str] = field(default_factory=dict)
    copy_hashes: dict[Path, str] = field(default_factory=dict)
    expected_official_hash: str | None = None
    committed_hashes: dict[Path, str | None] = field(default_factory=dict)

    @property
    def targets(self) -> list[Path]:
        return [self.catalog_path, *(target for _, target in self.copies), *self.deletes]

    @property
    def installed_hashes(self) -> dict[Path, str]:
        return {**self.copy_hashes, self.catalog_path: hashlib.sha256(self.payload).hexdigest()}

    def _check_version(self) -> None:
        if self.expected_official_hash is not None and _official_hash(self.catalog_path) != self.expected_official_hash:
            raise ValueError("官方 catalog 版本在操作途中改变，已停止操作并保留新版本 hash。")

    def _check_existing(self, expected: dict[Path, str]) -> None:
        self._check_version()
        for target, digest in expected.items():
            if file_sha256(target) != digest:
                raise ValueError("安装目标在检查后被修改，已停止操作并保留原文件。")

    def verify_applied(self) -> None:
        """Check drift again before the surrounding transaction can succeed."""
        self._check_existing(self.installed_hashes)
        if any(target.exists() or target.is_symlink() for target in self.deletes):
            raise ValueError("移除的独立表情路径又出现了文件，已停止操作并保留未知内容。")

    def apply(self) -> None:
        expected = dict(self.expected_existing)
        self._check_existing(expected)
        for source, target in self.copies:
            # Keep every rename on the destination volume and verify staged bytes.
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target not in self.expected_existing:
                raise ValueError("独立表情目标出现了未知文件，不能覆盖。")
            handle, name = tempfile.mkstemp(prefix=".emote-bundle-", suffix=".tmp", dir=target.parent)
            os.close(handle)
            pending = Path(name)
            try:
                shutil.copy2(source, pending)
                if (file_sha256(pending) != self.copy_hashes[target]
                        or file_sha256(source) != self.copy_hashes[target]):
                    raise ValueError("独立表情复制校验失败，已停止安装。")
                if target in self.expected_existing:
                    if file_sha256(target) != self.expected_existing[target]:
                        raise ValueError("独立表情目标在暂存期间被修改，不能覆盖。")
                    pending.replace(target)
                else:
                    # Same-volume linking publishes the fully staged file only if
                    # the destination is absent. A plain replace would overwrite a
                    # file created by another writer during staging.
                    try:
                        os.link(pending, target)
                    except FileExistsError as exc:
                        raise ValueError("独立表情目标出现了未知文件，不能覆盖。") from exc
                self.committed_hashes[target] = self.copy_hashes[target]
                expected[target] = self.copy_hashes[target]
            finally:
                pending.unlink(missing_ok=True)
        # Remove addresses before deleting their no-longer-used dependencies.
        atomic_bytes(self.catalog_path, self.payload, before_commit=lambda: self._check_existing(expected))
        self.committed_hashes[self.catalog_path] = self.installed_hashes[self.catalog_path]
        for target in self.deletes:
            if file_sha256(target) != self.expected_existing[target]:
                raise ValueError("待删除的独立表情已被修改，已停止卸载。")
            target.unlink()
            self.committed_hashes[target] = None


def prepare_catalog_change(aa_dirs: tuple[Path, ...], data_dir: Path, previous: dict,
                           proposed: dict, source_for) -> CatalogChange | None:
    """Check drift and prepare a composed overlay; mutate only proposed metadata."""
    old = previous.get("catalog_overlay")
    emote_mods = [mod for mod in proposed.get("mods", {}).values() if mod.get("catalog_patches")]
    if old is None and not emote_mods:
        return None
    target = active_catalog_path(aa_dirs)
    bundle_root = aa_dirs[0].resolve()
    if not bundle_root.is_dir():
        raise ValueError("独立表情目标资源目录不存在。")
    current_digest = file_sha256(target, MAX_CATALOG_BYTES)
    version_hash = _official_hash(target)
    backup_root = data_dir / "catalog_backups"
    if old is not None:
        if (not isinstance(old, dict) or type(old.get("version")) is not int or old.get("version") != 1
                or old.get("target") != str(target) or old.get("bundle_root") != str(bundle_root)
                or old.get("official_hash") != version_hash or old.get("applied_sha256") != current_digest):
            raise ValueError("游戏 catalog 已更新或被其他工具修改，不能用旧备份覆盖；请先检查更新或恢复副本。")
        baseline_digest = old.get("baseline_sha256", "")
        if not isinstance(baseline_digest, str) or not SHA256.fullmatch(baseline_digest):
            raise ValueError("独立表情 catalog 备份记录不合法。")
        baseline_path = backup_root / f"catalog-{baseline_digest}.json"
        if file_sha256(baseline_path, MAX_CATALOG_BYTES) != baseline_digest:
            raise ValueError("独立表情 catalog 原始备份校验失败。")
        baseline = baseline_path.read_bytes()
        owned = old.get("bundles")
        existence = old.get("original_exists")
        if (not isinstance(owned, dict) or not isinstance(existence, dict)
                or set(existence) != set(owned) or any(value is not False for value in existence.values())):
            raise ValueError("独立表情文件所有权记录不合法。")
    else:
        baseline_digest = current_digest
        baseline = target.read_bytes()
        baseline_path = backup_root / f"catalog-{baseline_digest}.json"
        if baseline_path.exists():
            if file_sha256(baseline_path, MAX_CATALOG_BYTES) != baseline_digest:
                raise ValueError("独立表情 catalog 原始备份校验失败。")
        else:
            atomic_bytes(baseline_path, baseline)
        owned = {}
    for name, digest in owned.items():
        if (not isinstance(name, str) or not OWNED_BUNDLE.fullmatch(name)
                or not isinstance(digest, str) or not SHA256.fullmatch(digest)
                or file_sha256(bundle_root / name) != digest):
            raise ValueError("已安装的独立表情文件被修改或缺失，不能覆盖或删除未知文件。")
    try:
        catalog = json.loads(baseline.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("原始 catalog 无法解析，已停止安装。") from exc
    if not isinstance(catalog, dict):
        raise ValueError("原始 catalog 结构不正确。")
    from .emote_addressables import apply_catalog_address_patch

    copies, addresses, effective = [], set(), {}
    for mod in emote_mods:
        if mod.get("disabled"):
            continue
        for patch in validate_patches(mod["catalog_patches"]):
            name = patch["bundle_name"]
            if patch["video_name"] in addresses or name in effective:
                raise ValueError("两个启用的 Mod 修改了同一个独立表情，请先禁用或卸载其中一个。")
            addresses.add(patch["video_name"])
            source = source_for(mod, name)
            expected = (mod.get("new_bundle_hashes") or {}).get(name)
            if (source is None or not source.is_file() or source.stat().st_size != patch["bundle_size"]
                    or file_sha256(source) != expected):
                raise ValueError("独立表情的安装来源缺失或校验失败，请重新导入。")
            destination = bundle_root / name
            if destination.exists() and name not in owned:
                raise ValueError("独立表情目标已存在未知文件，不能覆盖。")
            catalog = apply_catalog_address_patch(catalog, patch["template_video_key"], patch["video_name"],
                                                  name, str(destination), bundle_size=patch["bundle_size"])
            effective[name] = expected
            if name not in owned or owned[name] != expected:
                copies.append((source, destination))
    payload = (json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
               if effective else baseline)
    if len(payload) > MAX_CATALOG_BYTES:
        raise ValueError("合成的表情 catalog 超过 32 MB。")
    if emote_mods:
        proposed["catalog_overlay"] = {"version": 1, "target": str(target), "bundle_root": str(bundle_root),
                                       "official_hash": version_hash, "baseline_sha256": baseline_digest,
                                       "applied_sha256": hashlib.sha256(payload).hexdigest(), "bundles": effective,
                                       "original_exists": {name: False for name in effective}}
    else:
        proposed.pop("catalog_overlay", None)
    return CatalogChange(target, payload, copies, [bundle_root / name for name in owned if name not in effective],
                         {target: current_digest, **{bundle_root / name: digest for name, digest in owned.items()}},
                         {bundle_root / name: digest for name, digest in effective.items()}, version_hash)
