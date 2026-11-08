"""Create editable Cesium SDK projects and compile separate plugin packages."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path

from .catalog_mods import atomic_bytes

SDK_VERSION = "3.3.2"
SDK_ARCHIVE_URL = "https://github.com/higashitaniyume/CesiumLoader/releases/download/modloader-3.3.2/cesium-loader-3.3.2.zip"
SDK_ARCHIVE_SHA256 = "f419c0b269345f9584b9f88dbac387aa2aa79eae28c88a50ce3593b27737bb15"
SDK_DLL_SHA256 = "9d7e0bc793d194317646f2d8c582b809362fb111460d10cccf5de7012d74abcc"
SDK_LICENSE = "MIT License\n\nCopyright (c) 2026 Yume Higashitani\n\nPermission is hereby granted, free of charge, to any person obtaining a copy\nof this software and associated documentation files (the \"Software\"), to deal\nin the Software without restriction, including without limitation the rights\nto use, copy, modify, merge, publish, distribute, sublicense, and/or sell\ncopies of the Software, and to permit persons to whom the Software is\nfurnished to do so, subject to the following conditions:\n\nThe above copyright notice and this permission notice shall be included in all\ncopies or substantial portions of the Software.\n\nTHE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR\nIMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,\nFITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE\nAUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER\nLIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,\nOUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE\nSOFTWARE.\n"
MAX_SOURCE_BYTES = 512 * 1024


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _project_xml(mod_id: str) -> str:
    return f'''<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>netstandard2.0</TargetFramework>
    <LangVersion>latest</LangVersion>
    <AssemblyName>{mod_id}</AssemblyName>
    <RootNamespace>{mod_id}</RootNamespace>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="ModEntry.cs" />
    <Reference Include="CesiumLoader.SDK">
      <HintPath>refs/CesiumLoader.SDK.dll</HintPath>
      <Private>false</Private>
    </Reference>
  </ItemGroup>
</Project>
'''


def _source_code(options: dict) -> str:
    mod_id = options["id"]
    header = f'''using CesiumLoader.SDK.Manifests;
using CesiumLoader.SDK.Mods;
using CesiumLoader.SDK.Logging;
using CesiumLoader.SDK.Runtime;
'''
    manifest = f'''namespace {mod_id}
{{
    [ModManifest({json.dumps(options["name"], ensure_ascii=False)}, "1.0.0", {json.dumps(options["author"], ensure_ascii=False)}, "由吉星派对 Mod 助手创建", SdkVersion = "{SDK_VERSION}")]
    public static class ModEntry
    {{
'''
    if options["template"] == "frame_rate":
        body = f'''        private const int TargetFrameRate = {options["fps"]};

        public static void Main()
        {{
            // 等待游戏就绪后设置，每秒检查一次游戏是否重设了画质参数。
            ModBase.Run(ApplyFrameRate, KeepFrameRate, tag: "{mod_id}");
        }}

        private static void KeepFrameRate()
        {{
            MainThread.Run(ApplyFrameRate);
        }}

        private static void ApplyFrameRate()
        {{
            if (UnityFrameRate.VSync != 0)
            {{
                UnityFrameRate.SetVSync(0);
            }}
            if (UnityFrameRate.Target != TargetFrameRate)
            {{
                bool applied = UnityFrameRate.SetTarget(TargetFrameRate);
                if (applied)
                {{
                    SdkLog.Info("{mod_id}", "目标帧率已设为 " + TargetFrameRate);
                }}
            }}
        }}
'''
        header += "using CesiumLoader.SDK.Engine;\n\n"
    else:
        body = f'''        public static void Main()
        {{
            ModBase.Run(ShowNotification, tag: "{mod_id}");
        }}

        private static void ShowNotification()
        {{
            const string message = {json.dumps(options["message"], ensure_ascii=False)};
            UiService.Notify(message, UiNotificationLevel.Success, 4f);
            SdkLog.Info("{mod_id}", message);
        }}
'''
        header += "using CesiumLoader.SDK.UserInterface;\n\n"
    return header + manifest + body + "    }\n}\n"


class PluginWorkspace:
    def __init__(self, data_dir: Path, output_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.project: Path | None = None
        self.sdk_dir: Path | None = None
        self.settings_file = self.data_dir / "settings.json"
        if self.settings_file.is_file():
            try:
                settings = json.loads(self.settings_file.read_text(encoding="utf-8"))
                if settings.get("sdk"):
                    self.set_sdk(Path(settings["sdk"]), save=False)
            except (OSError, ValueError, KeyError):
                pass
        cached_sdk = self.data_dir / "sdk-3.3.2"
        if self.sdk_dir is None and (cached_sdk / "CesiumLoader.SDK.dll").is_file():
            self.set_sdk(cached_sdk, save=False)

    def set_sdk(self, directory: Path, *, save: bool = True) -> Path:
        directory = Path(directory).resolve()
        candidates = [directory, directory / "sdk", directory / "AstralParty_ModLoader" / "sdk"]
        sdk_dir = next((folder for folder in candidates if (folder / "CesiumLoader.SDK.dll").is_file()), None)
        if sdk_dir is None:
            raise ValueError("此目录没有 CesiumLoader.SDK.dll，请选择 SDK 或加载器目录。")
        if save:
            atomic_bytes(self.settings_file, _json_bytes({"sdk": str(sdk_dir)}))
        self.sdk_dir = sdk_dir
        return sdk_dir

    def fetch_sdk(self) -> Path:
        request = urllib.request.Request(SDK_ARCHIVE_URL, headers={"User-Agent": "JiXingModHelper"})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(8 * 1024 * 1024 + 1)
        if len(payload) > 8 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != SDK_ARCHIVE_SHA256:
            raise ValueError("官方 SDK 发布包摘要不匹配，下载未保存。")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        destination = self.data_dir / "sdk-3.3.2"
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            sdk = archive.read("AstralParty_ModLoader/sdk/CesiumLoader.SDK.dll")
            if hashlib.sha256(sdk).hexdigest() != SDK_DLL_SHA256:
                raise ValueError("SDK 程序集校验失败。")
            if destination.exists():
                if ((destination / "CesiumLoader.SDK.dll").read_bytes() != sdk
                        or (destination / "LICENSE.txt").read_text(encoding="utf-8") != SDK_LICENSE):
                    raise ValueError("已缓存的 SDK 内容已改变，请选择其他 SDK 目录。")
            else:
                with tempfile.TemporaryDirectory(prefix="sdk-", dir=self.data_dir) as temporary:
                    staged = Path(temporary) / "sdk"
                    atomic_bytes(staged / "CesiumLoader.SDK.dll", sdk)
                    atomic_bytes(staged / "LICENSE.txt", SDK_LICENSE.encode("utf-8"))
                    staged.replace(destination)
        self.set_sdk(destination)
        return destination

    def status(self, game_executable: Path | None = None) -> dict:
        dotnet = shutil.which("dotnet")
        has_sdk = False
        if dotnet:
            try:
                probe = subprocess.run([dotnet, "--list-sdks"], capture_output=True, timeout=5,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                has_sdk = probe.returncode == 0 and bool(probe.stdout.strip())
            except (OSError, subprocess.TimeoutExpired):
                pass
        loader = Path(game_executable).parent / "AstralParty_ModLoader" if game_executable else None
        loader_config = loader / "doorstop_config.json" if loader else None
        return {
            "dotnet": has_sdk, "sdk": str(self.sdk_dir or ""),
            "sdk_version": SDK_VERSION,
            "loader_present": bool(loader_config and loader_config.is_file()
                                   and (loader.parent / "version.dll").is_file()),
            "loader_path": str(loader or ""),
            "project": str(self.project or ""),
        }

    def create(self, options: dict) -> dict:
        mod_id = str(options.get("id", "")).strip()
        if not re.fullmatch(r"[A-Z][A-Za-z0-9_]{2,63}", mod_id):
            raise ValueError("工程标识需以大写英文字母开头，使用 3–64 个字母、数字或下划线。")
        template = options.get("template", "frame_rate")
        if template not in ("frame_rate", "notification"):
            raise ValueError("请选择帧率或加载通知模板。")
        fps = int(options.get("fps", 144))
        if fps != -1 and not 1 <= fps <= 1000:
            raise ValueError("目标帧率应为 1–1000，或使用 -1 表示不限帧。")
        metadata = {
            "schema": 1, "id": mod_id, "template": template,
            "name": str(options.get("name") or mod_id).strip()[:80],
            "author": str(options.get("author") or "").strip()[:80],
            "fps": fps, "message": str(options.get("message") or "插件已加载")[:500],
        }
        project = self.output_dir / f"{mod_id}-{uuid.uuid4().hex[:8]}"
        guide = f"# {metadata['name']}\n\n需要 .NET SDK 和 CesiumLoader SDK {SDK_VERSION} 或更新版本。\n\n在助手中选择 SDK 后编译，生成独立的 Cesium 插件 ZIP。也可在工程目录执行 `dotnet build -c Release`。\n\n将导出 ZIP 中的 mods 目录导入 CesiumLoader。帧率模板会关闭垂直同步并持续维护设定值，停用后重启游戏生效。与其他帧率插件同时使用时应统一设置。\n\nSDK 文档 https://github.com/higashitaniyume/CesiumLoader#开发模组\n"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="project-", dir=self.output_dir) as temporary:
            staged = Path(temporary) / "project"
            atomic_bytes(staged / "jixing-plugin.json", _json_bytes(metadata))
            atomic_bytes(staged / f"{mod_id}.csproj", _project_xml(mod_id).encode("utf-8"))
            atomic_bytes(staged / "ModEntry.cs", _source_code(metadata).encode("utf-8"))
            atomic_bytes(staged / "README.md", guide.encode("utf-8"))
            staged.replace(project)
        return self.open(project)

    def open(self, directory: Path) -> dict:
        directory = Path(directory).resolve()
        metadata = json.loads((directory / "jixing-plugin.json").read_text(encoding="utf-8"))
        mod_id = metadata.get("id", "")
        if metadata.get("schema") != 1 or not re.fullmatch(r"[A-Z][A-Za-z0-9_]{2,63}", mod_id):
            raise ValueError("此目录不是助手创建的插件工程。")
        if (directory / f"{mod_id}.csproj").read_text(encoding="utf-8") != _project_xml(mod_id):
            raise ValueError("工程构建文件已修改，请使用 .NET 工具构建，或在助手中另建工程。")
        source_file = directory / "ModEntry.cs"
        if source_file.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError("源码超过 512 KiB，请使用外部编辑器处理。")
        source = source_file.read_text(encoding="utf-8")
        self.project = directory
        return {"project": str(directory), "options": metadata, "source": source,
                "source_hash": hashlib.sha256(source.encode("utf-8")).hexdigest()}

    def save(self, source: str, expected_hash: str) -> dict:
        if self.project is None:
            raise ValueError("请先创建或选择插件工程。")
        current = self.open(self.project)
        project = Path(current["project"])
        if current["source_hash"] != expected_hash:
            raise ValueError("源码已被其他编辑器修改，请重新选择工程后再保存。")
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ValueError("源码超过 512 KiB。")
        atomic_bytes(project / "ModEntry.cs", source.encode("utf-8"))
        return self.open(project)

    def build(self) -> dict:
        if self.project is None or self.sdk_dir is None:
            raise ValueError("请先选择插件工程和 CesiumLoader SDK。")
        dotnet = shutil.which("dotnet")
        if dotnet is None:
            raise ValueError("未找到 .NET SDK，请安装后重新打开助手。")
        current = self.open(self.project)
        project = Path(current["project"])
        mod_id = current["options"]["id"]
        refs = project / "refs"
        atomic_bytes(refs / "CesiumLoader.SDK.dll", (self.sdk_dir / "CesiumLoader.SDK.dll").read_bytes())
        atomic_bytes(refs / "CesiumLoader.SDK-LICENSE.txt", SDK_LICENSE.encode("utf-8"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="build-", dir=self.data_dir) as temporary:
            build_dir = Path(temporary)
            arguments = [dotnet, "build", str(project / f"{mod_id}.csproj"),
                         "--configuration", "Release", "--output", str(build_dir / "bin"),
                         "--nologo", "-p:UseSharedCompilation=false",
                         "-p:ImportDirectoryBuildProps=false", "-p:ImportDirectoryBuildTargets=false",
                         f"-p:BaseIntermediateOutputPath={build_dir / 'obj'}{os.sep}"]
            process = subprocess.run(arguments, cwd=project, capture_output=True,
                                     encoding="utf-8", errors="replace", timeout=180,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log = (process.stdout + process.stderr)[-24_000:]
            atomic_bytes(project / "last-build.log", log.encode("utf-8"))
            if process.returncode:
                return {"success": False, "log": log, "project": str(project)}
            dll = build_dir / "bin" / f"{mod_id}.dll"
            if not dll.is_file() or dll.read_bytes()[:2] != b"MZ":
                raise ValueError("构建没有生成有效的插件程序集。")
            if self.open(project)["source_hash"] != current["source_hash"]:
                raise ValueError("构建期间源码发生变化，请重新编译。")
            sidecar = {key: current["options"][key] for key in ("id", "name", "author")}
            sidecar.update(version="1.0.0", sdkVersion=SDK_VERSION, dependencies=[],
                           description="使用吉星派对 Mod 助手制作的功能插件", permissions=0)
            package = io.BytesIO()
            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(f"mods/{mod_id}/{mod_id}.dll", dll.read_bytes())
                archive.writestr(f"mods/{mod_id}/{mod_id}.json", _json_bytes(sidecar))
                archive.writestr(f"mods/{mod_id}/README.md", (project / "README.md").read_bytes())
            output = project / f"{mod_id}-1.0.0.zip"
            atomic_bytes(output, package.getvalue())
        return {"success": True, "log": log, "project": str(project),
                "package": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
