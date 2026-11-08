"""组件下载失败或哈希不符时，旧组件不能被覆盖，临时解压须收尾。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell is required")
class EmoteSetupTests(unittest.TestCase):
    def run_failed_setup(self, download_function, expected_error, *, api_url=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "fetch_ffmpeg.ps1"
            shutil.copy2(ROOT / "tools/emote/fetch_ffmpeg.ps1", script)
            previous = root / "ffmpeg/bin/ffmpeg.exe"
            previous.parent.mkdir(parents=True)
            previous.write_bytes(b"existing component must survive")
            previous_hash = hashlib.sha256(previous.read_bytes()).hexdigest()
            manifest = {
                "version": "test-build", "archive_name": "test.zip", "archive_size": 4,
                "archive_sha256": hashlib.sha256(b"safe").hexdigest(),
                "archive_url": "https://invalid.example.invalid/test.zip", "root_directory": "test",
                "files": [{"name": "bin/ffmpeg.exe", "size": 4,
                           "sha256": hashlib.sha256(b"new!").hexdigest()}], "licenses": [],
            }
            if api_url is not None:
                manifest["archive_api_url"] = api_url
            (root / "source.json").write_text(json.dumps(manifest), encoding="utf-8")
            wrapper = root / "run.ps1"
            wrapper.write_text(
                download_function + "\ntry {\n"
                "  & (Join-Path $PSScriptRoot 'fetch_ffmpeg.ps1')\n"
                "  throw 'Setup unexpectedly succeeded.'\n"
                "} catch {\n"
                f"  if ($_.Exception.Message -notmatch '{expected_error}') {{ throw }}\n"
                "}\n", encoding="utf-8")
            process = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper)],
                                     capture_output=True, timeout=20,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(hashlib.sha256(previous.read_bytes()).hexdigest(), previous_hash)
            self.assertFalse(list(root.glob("ffmpeg.previous-*")))
            self.assertFalse(list((root / "cache").glob("setup-*")))
            self.assertFalse((root / "cache/test.zip").exists())
            result = json.loads((root / "setup-result.json").read_text(encoding="utf-8-sig"))
            self.assertFalse(result["passed"])
            self.assertIn(expected_error.replace(".*", ""), result["error"])
            return result

    def test_bad_download_hash_preserves_previous_components(self):
        self.run_failed_setup(
            "function Invoke-WebRequest { param($Uri,$OutFile,[switch]$UseBasicParsing,$TimeoutSec,$Headers)\n"
            "  if ($Uri -ne 'https://invalid.example.invalid/test.zip') { throw 'Unexpected legacy archive URL' }\n"
            "  if ($Headers) { throw 'Legacy download unexpectedly received API headers' }\n"
            "  [IO.File]::WriteAllBytes($OutFile, [byte[]](1,2,3,4))\n}",
            "Downloaded archive differs")

    def test_network_failure_preserves_previous_components(self):
        self.run_failed_setup(
            "function Invoke-WebRequest { param($Uri,$OutFile,[switch]$UseBasicParsing,$TimeoutSec,$Headers)\n"
            "  throw 'Controlled network failure'\n}",
            "Controlled network failure")

    def test_api_asset_download_keeps_hash_validation_and_cleans_failed_cache(self):
        api_url = "https://api.github.com/repos/FFmpeg-Builds/FFmpeg-Builds/releases/assets/600950511"
        result = self.run_failed_setup(
            "function Invoke-WebRequest { param($Uri,$OutFile,[switch]$UseBasicParsing,$TimeoutSec,$Headers)\n"
            f"  if ($Uri -ne '{api_url}') {{ throw 'Unexpected asset API URL' }}\n"
            "  if ($Headers.Accept -ne 'application/octet-stream') { throw 'Missing asset download Accept header' }\n"
            "  [IO.File]::WriteAllBytes($OutFile, [byte[]](1,2,3,4))\n}",
            "Downloaded archive differs", api_url=api_url)
        self.assertEqual(result["cache_files_removed"], 1)
        self.assertEqual(result["cache_bytes_removed"], 4)


if __name__ == "__main__":
    unittest.main()
