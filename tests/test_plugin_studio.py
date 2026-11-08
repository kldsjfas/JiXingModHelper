"""Plugin project drafts, pinned SDK downloads and isolated package builds."""
from __future__ import annotations

import hashlib
import io
import json
import subprocess
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from astral_party_auto.modkit import plugin_studio
from astral_party_auto.web_app import DesktopApi


class PluginStudioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="jixing-plugin-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = plugin_studio.PluginWorkspace(self.root / "data", self.root / "projects")

    def create(self, **options):
        return self.workspace.create({"id": "ExampleMod", "name": "示例插件", **options})

    def sdk(self):
        folder = self.root / "sdk"
        folder.mkdir(exist_ok=True)
        (folder / "CesiumLoader.SDK.dll").write_bytes(b"MZ isolated test SDK")
        self.workspace.set_sdk(folder)
        return folder

    def run_build(self, *, exit_code=0, before_finish=None, payload=b"MZ isolated test plugin"):
        def build(arguments, **kwargs):
            output = Path(arguments[arguments.index("--output") + 1])
            output.mkdir(parents=True)
            (output / "ExampleMod.dll").write_bytes(payload)
            if before_finish:
                before_finish()
            return SimpleNamespace(returncode=exit_code, stdout="build output", stderr="")
        with patch.object(plugin_studio.shutil, "which", return_value="dotnet"), \
                patch.object(plugin_studio.subprocess, "run", side_effect=build):
            return self.workspace.build()

    def test_both_templates_create_readable_editable_projects(self):
        for template in ("frame_rate", "notification"):
            with self.subTest(template=template):
                result = self.create(template=template, message='有引号 " 与换行\n的通知')
                self.assertTrue(Path(result["project"], "ExampleMod.csproj").is_file())
                self.assertIn("ModManifest", result["source"])
                self.assertIn("ModBase.Run", result["source"])
                self.assertEqual(result["source_hash"], hashlib.sha256(result["source"].encode()).hexdigest())

    def test_invalid_project_identity_never_creates_an_output_directory(self):
        for mod_id in ("../Escaped", "ab", "lowercase", "A/B", "<Injected>"):
            with self.subTest(mod_id=mod_id), self.assertRaises(ValueError):
                self.create(id=mod_id)
        self.assertFalse((self.root / "projects").exists())

    def test_save_rejects_external_source_changes_and_keeps_them(self):
        created = self.create()
        source = Path(created["project"], "ModEntry.cs")
        source.write_text("// external editor change\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "其他编辑器"):
            self.workspace.save("// stale window", created["source_hash"])
        self.assertEqual(source.read_text(encoding="utf-8"), "// external editor change\n")

    def test_save_validates_limit_before_writing_and_returns_a_new_hash(self):
        created = self.create()
        with self.assertRaisesRegex(ValueError, "512"):
            self.workspace.save("喵" * (plugin_studio.MAX_SOURCE_BYTES // 3 + 1), created["source_hash"])
        saved = self.workspace.save(created["source"] + "\n// editable\n", created["source_hash"])
        self.assertNotEqual(saved["source_hash"], created["source_hash"])

    def test_modified_build_file_is_rejected_without_switching_project(self):
        created = self.create()
        project = Path(created["project"])
        project.joinpath("ExampleMod.csproj").write_text("<Project />", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "构建文件"):
            self.workspace.open(project)

    def test_sdk_persistence_failure_keeps_previous_sdk(self):
        previous = self.sdk()
        replacement = self.root / "other-sdk"
        replacement.mkdir()
        replacement.joinpath("CesiumLoader.SDK.dll").write_bytes(b"MZ replacement test SDK")
        with patch.object(plugin_studio, "atomic_bytes", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.workspace.set_sdk(replacement)
        self.assertEqual(self.workspace.sdk_dir, previous.resolve())

    def test_failed_download_keeps_sdk_settings_and_creates_no_partial_cache(self):
        previous = self.sdk()
        settings = self.workspace.settings_file.read_bytes()
        response = io.BytesIO(b"unexpected release contents")
        with patch.object(plugin_studio.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "摘要"):
                self.workspace.fetch_sdk()
        self.assertEqual(self.workspace.sdk_dir, previous.resolve())
        self.assertEqual(self.workspace.settings_file.read_bytes(), settings)
        self.assertFalse((self.workspace.data_dir / "sdk-3.3.2").exists())

    def test_successful_download_checks_both_archive_and_dll_digest(self):
        sdk = b"MZ pinned SDK fixture"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("AstralParty_ModLoader/sdk/CesiumLoader.SDK.dll", sdk)
            archive.writestr("../unwanted.txt", "not extracted")
        payload = buffer.getvalue()
        with patch.object(plugin_studio, "SDK_ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest()), \
                patch.object(plugin_studio, "SDK_DLL_SHA256", hashlib.sha256(sdk).hexdigest()), \
                patch.object(plugin_studio.urllib.request, "urlopen", return_value=io.BytesIO(payload)):
            result = self.workspace.fetch_sdk()
        self.assertEqual((result / "CesiumLoader.SDK.dll").read_bytes(), sdk)
        self.assertTrue((result / "LICENSE.txt").is_file())
        self.assertFalse((self.workspace.data_dir / "unwanted.txt").exists())

    def test_sdk_staging_write_failure_leaves_no_incomplete_download(self):
        previous = self.sdk()
        sdk = b"MZ pinned SDK fixture"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("AstralParty_ModLoader/sdk/CesiumLoader.SDK.dll", sdk)
        payload = buffer.getvalue()
        original_write = plugin_studio.atomic_bytes
        def fail_license(path, content):
            if Path(path).name == "LICENSE.txt":
                raise OSError("license write failed")
            return original_write(path, content)
        with patch.object(plugin_studio, "SDK_ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest()), \
                patch.object(plugin_studio, "SDK_DLL_SHA256", hashlib.sha256(sdk).hexdigest()), \
                patch.object(plugin_studio.urllib.request, "urlopen", return_value=io.BytesIO(payload)), \
                patch.object(plugin_studio, "atomic_bytes", side_effect=fail_license):
            with self.assertRaisesRegex(OSError, "license write failed"):
                self.workspace.fetch_sdk()
        self.assertEqual(self.workspace.sdk_dir, previous.resolve())
        self.assertFalse((self.workspace.data_dir / "sdk-3.3.2").exists())
        self.assertFalse(list(self.workspace.data_dir.glob("sdk-*")))

    def test_project_staging_failure_keeps_previous_project_and_no_partial_output(self):
        self.create()
        previous = self.workspace.project
        original_write = plugin_studio.atomic_bytes
        def fail_source(path, content):
            if Path(path).name == "ModEntry.cs":
                raise OSError("source write failed")
            return original_write(path, content)
        with patch.object(plugin_studio, "atomic_bytes", side_effect=fail_source):
            with self.assertRaisesRegex(OSError, "source write failed"):
                self.create(id="AnotherMod")
        self.assertEqual(self.workspace.project, previous)
        self.assertEqual(list(self.workspace.output_dir.iterdir()), [previous])

    def test_plugin_apis_use_workspace_without_modifying_game_selection(self):
        api = object.__new__(DesktopApi)
        api._controller_lock = threading.RLock()
        api._append_log = lambda message: None
        api._plugin_workspace = self.workspace
        api.controller = SimpleNamespace(game_executable=None, selection={"name": "keep"})
        created = api.plugin_create_project({"id": "ExampleMod", "template": "notification"})
        self.assertTrue(created["ok"])
        project = created["data"]
        saved = api.plugin_save_source(project["source"] + "\n// edited", project["source_hash"])
        self.assertTrue(saved["ok"])
        stale = api.plugin_save_source("// stale", project["source_hash"])
        self.assertFalse(stale["ok"])
        self.assertEqual(api.controller.selection, {"name": "keep"})

    def test_build_exports_loader_layout_and_keeps_project_source(self):
        created = self.create()
        self.sdk()
        result = self.run_build()
        self.assertTrue(result["success"])
        package = Path(result["package"])
        with zipfile.ZipFile(package) as archive:
            self.assertEqual(set(archive.namelist()), {
                "mods/ExampleMod/ExampleMod.dll", "mods/ExampleMod/ExampleMod.json", "mods/ExampleMod/README.md"})
            self.assertTrue(archive.read("mods/ExampleMod/ExampleMod.dll").startswith(b"MZ"))
            metadata = json.loads(archive.read("mods/ExampleMod/ExampleMod.json"))
            self.assertEqual(metadata["sdkVersion"], plugin_studio.SDK_VERSION)
        self.assertEqual(Path(created["project"], "ModEntry.cs").read_text(encoding="utf-8"), created["source"])
        self.assertFalse(list(self.workspace.data_dir.glob("build-*")))

    def test_failed_build_preserves_previous_package_and_records_log(self):
        self.create()
        self.sdk()
        package = Path(self.run_build()["package"])
        before = package.read_bytes()
        result = self.run_build(exit_code=1)
        self.assertFalse(result["success"])
        self.assertEqual(package.read_bytes(), before)
        self.assertTrue((self.workspace.project / "last-build.log").is_file())
        self.assertFalse(list(self.workspace.data_dir.glob("build-*")))

    def test_source_changed_during_build_does_not_replace_previous_package(self):
        self.create()
        self.sdk()
        package = Path(self.run_build()["package"])
        before = package.read_bytes()
        def change_source():
            with (self.workspace.project / "ModEntry.cs").open("a", encoding="utf-8") as stream:
                stream.write("\n// changed during build\n")
        with self.assertRaisesRegex(ValueError, "源码发生变化"):
            self.run_build(before_finish=change_source)
        self.assertEqual(package.read_bytes(), before)

    def test_build_timeout_keeps_previous_package_and_cleans_build_cache(self):
        self.create()
        self.sdk()
        package = Path(self.run_build()["package"])
        before = package.read_bytes()
        with patch.object(plugin_studio.shutil, "which", return_value="dotnet"), \
                patch.object(plugin_studio.subprocess, "run", side_effect=subprocess.TimeoutExpired("dotnet", 180)):
            with self.assertRaises((subprocess.TimeoutExpired, ValueError, RuntimeError)):
                self.workspace.build()
        self.assertEqual(package.read_bytes(), before)
        self.assertFalse(list(self.workspace.data_dir.glob("build-*")))


if __name__ == "__main__":
    unittest.main()
