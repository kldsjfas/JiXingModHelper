"""组件定位优先使用助手自己的目录，保留显式路径和既有安装兼容。"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from astral_party_auto.modkit import emote_media


class EmoteRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.app = self.root / "app"
        self.internal = self.root / "app" / "_internal"
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        self.scope.enter_context(patch.multiple(emote_media, APP_ROOT=self.app, RESOURCE_ROOT=self.internal))
        self.scope.enter_context(patch.object(emote_media.sys, "executable", str(self.app / "JiXingModHelper.exe")))
        self.scope.enter_context(patch.object(emote_media.shutil, "which", return_value=None))
        self.scope.enter_context(patch.object(emote_media.Path, "home", return_value=self.root / "home"))
        self.runner = self.scope.enter_context(patch.object(emote_media.subprocess, "run", side_effect=self.run_version))

    @staticmethod
    def run_version(command, **_options):
        name = Path(command[0]).stem
        return subprocess.CompletedProcess(command, 0, stdout=(name + " version test").encode(), stderr=b"")

    def executable(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"component fixture")
        return path.resolve()

    def test_packaged_component_wins_over_adjacent_and_path_tools(self):
        dedicated = self.executable(self.app / "tools/emote/ffmpeg/bin/ffmpeg.exe")
        self.executable(self.app / "ffmpeg.exe")
        outside = self.executable(self.root / "system/ffmpeg.exe")
        with patch.object(emote_media.shutil, "which", return_value=str(outside)):
            self.assertEqual(emote_media.find_ffmpeg(), dedicated)
        command = self.runner.call_args.args[0]
        self.assertEqual(len(command), 2)
        self.assertTrue(Path(command[0]).samefile(dedicated))
        self.assertEqual(command[1], "-version")

    def test_resource_root_component_is_found_when_app_root_has_none(self):
        dedicated = self.executable(self.internal / "tools/emote/ffmpeg/bin/ffmpeg.exe")
        self.assertEqual(emote_media.find_ffmpeg(), dedicated)

    def test_explicit_component_path_is_respected(self):
        explicit = self.executable(self.root / "selected/ffmpeg.exe")
        self.executable(self.app / "tools/emote/ffmpeg/bin/ffmpeg.exe")
        self.assertEqual(emote_media.find_ffmpeg(explicit), explicit)

    def test_invalid_local_component_falls_back_to_existing_path_tool(self):
        local = self.executable(self.app / "tools/emote/ffmpeg/bin/ffmpeg.exe")
        outside = self.executable(self.root / "system/ffmpeg.exe")
        def version(command, **options):
            if Path(command[0]).samefile(local):
                raise OSError("invalid component")
            return self.run_version(command, **options)
        with patch.object(emote_media.shutil, "which", return_value=str(outside)), \
                patch.object(emote_media.subprocess, "run", side_effect=version):
            self.assertEqual(emote_media.find_ffmpeg(), outside)

    def test_probe_uses_same_component_directory(self):
        ffmpeg = self.executable(self.app / "tools/emote/ffmpeg/bin/ffmpeg.exe")
        ffprobe = self.executable(ffmpeg.with_name("ffprobe.exe"))
        outside = self.executable(self.root / "system/ffprobe.exe")
        with patch.object(emote_media.shutil, "which", return_value=str(outside)):
            self.assertEqual(emote_media.find_ffprobe(ffmpeg), ffprobe)

    def test_missing_component_names_the_setup_entry(self):
        with self.assertRaisesRegex(ValueError, "获取表情转换组件.cmd"):
            emote_media.find_ffmpeg()
        with self.assertRaisesRegex(ValueError, "获取表情转换组件.cmd"):
            emote_media.find_ffprobe(self.app / "ffmpeg.exe")


if __name__ == "__main__":
    unittest.main()
