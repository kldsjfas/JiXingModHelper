param([string]$Destination)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
if (-not $Destination) {
    $Destination = Join-Path $projectRoot ('dist\preview-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$outputRoot = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $outputRoot) {
    throw "输出目录已经存在，请指定一个新目录以保留旧版和作品集：$outputRoot"
}
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
$uvPath = if ($uvCommand) { $uvCommand.Source } else { Join-Path $env:LOCALAPPDATA 'hermes\bin\uv.exe' }
if (-not (Test-Path -LiteralPath $uvPath)) { throw '需要 uv 和 .NET 8 SDK 才能从源码打包。' }
$buildPath = Join-Path $projectRoot ('build\preview-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
Push-Location $projectRoot
try {
    & dotnet publish .\native_host\JiXingModHelperHost.csproj -c Release -r win-x64 --self-contained true --output .\native_host\publish
    if ($LASTEXITCODE -ne 0) { throw '原生窗口组件构建失败。' }
    & $uvPath run --python 3.11 --with-requirements requirements.txt --with pyinstaller==6.22.2 python -m PyInstaller --distpath $outputRoot --workpath $buildPath build_exe.spec
    if ($LASTEXITCODE -ne 0) { throw 'Python 程序打包失败。' }
    $appPath = Join-Path $outputRoot 'JiXingModHelper'
    foreach ($relative in @('JiXingModHelper.exe', '_internal\UnityPy\resources\lzma.tpk', '_internal\native_host\JiXingModHelperHost.exe', '_internal\archspec\json\cpu\microarchitectures.json', '_internal\astral_party_auto\webui\index.html', '_internal\astral_party_auto\webui\app.js', '_internal\astral_party_auto\webui\audio.js', '_internal\astral_party_auto\webui\updates.js', '_internal\astral_party_auto\webui\styles.css')) {
        if (-not (Test-Path -LiteralPath (Join-Path $appPath $relative))) { throw "缺少构建文件：$relative" }
    }
    # Only the setup materials are distributed; each user fetches the optional decoder.
    $audioSource = Join-Path $projectRoot 'tools\audio'
    $audioDestination = Join-Path $appPath 'tools\audio'
    New-Item -ItemType Directory -Path $audioDestination -Force | Out-Null
    foreach ($name in @('fetch_vgmstream.ps1', 'source.json', 'README.md', 'licenses')) {
        Copy-Item -LiteralPath (Join-Path $audioSource $name) -Destination $audioDestination -Recurse
    }
    Copy-Item -LiteralPath (Join-Path $audioSource '获取音频解码组件.cmd') -Destination $appPath
    $unexpectedAudioFiles = Get-ChildItem -LiteralPath $audioDestination -File -Recurse |
        Where-Object { $_.Extension -in '.exe', '.dll', '.zip' }
    if ($unexpectedAudioFiles -or (Test-Path -LiteralPath (Join-Path $appPath '_internal\tools\audio\vgmstream'))) {
        throw 'Audio runtime binaries must not be included in the preview package.'
    }
    Copy-Item -LiteralPath (Join-Path $projectRoot 'PREVIEW_GUIDE.md') -Destination (Join-Path $appPath '使用说明.md')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination (Join-Path $appPath 'LICENSE')
    Write-Output "本地构建包：$appPath"
} finally {
    Pop-Location
}
