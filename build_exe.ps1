# Build windowed exe without removing previous builds or stopping running apps.
param(
  [string]$Destination,
  [switch]$SkipDependencyInstall
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $Destination) {
  $Destination = Join-Path $PSScriptRoot 'dist'
}
$outputRoot = [IO.Path]::GetFullPath($Destination)
$out = Join-Path $outputRoot 'JiXingModHelper'
if (Test-Path -LiteralPath $out) {
  throw "Output already exists: $out. Use -Destination with a new folder to preserve existing builds and user data."
}
$buildPath = Join-Path $PSScriptRoot ('build\release-' + [Guid]::NewGuid().ToString('N'))

if (-not $SkipDependencyInstall) {
  Write-Host "==> deps"
  python -m pip install -q -r requirements.txt
  if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
  python -m pip install -q "pyinstaller==6.22.2"
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed" }
}

Write-Host "==> native web host"
dotnet publish .\native_host\JiXingModHelperHost.csproj -c Release -r win-x64 --self-contained true --output .\native_host\publish
if ($LASTEXITCODE -ne 0) { throw "Native web host build failed" }

Write-Host "==> pyinstaller"
python -m PyInstaller --distpath $outputRoot --workpath $buildPath build_exe.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $out "JiXingModHelper.exe"
$tpk = Join-Path $out "_internal\UnityPy\resources\lzma.tpk"
$web = Join-Path $out "_internal\astral_party_auto\webui\index.html"
$nativeHost = Join-Path $out "_internal\native_host\JiXingModHelperHost.exe"
$arch = Join-Path $out "_internal\archspec\json\cpu\microarchitectures.json"

if (-not (Test-Path $exe)) { throw "missing exe" }
if (-not (Test-Path $tpk)) { throw "missing UnityPy resources\lzma.tpk - package broken" }
if (-not (Test-Path $web)) { throw "missing webui" }
foreach ($name in @('app.js', 'audio.js', 'updates.js', 'styles.css')) {
  if (-not (Test-Path -LiteralPath (Join-Path $out ('_internal\astral_party_auto\webui\' + $name)))) {
    throw "missing webui resource: $name"
  }
}
if (-not (Test-Path $nativeHost)) { throw "missing native web host" }
if (-not (Test-Path $arch)) { throw "missing archspec json - texture preview will break" }

# Ship setup materials only; local vgmstream binaries and download caches are excluded.
$audioSource = Join-Path $PSScriptRoot 'tools\audio'
$audioDestination = Join-Path $out 'tools\audio'
New-Item -ItemType Directory -Path $audioDestination -Force | Out-Null
foreach ($name in @('fetch_vgmstream.ps1', 'source.json', 'README.md', 'licenses')) {
  Copy-Item -LiteralPath (Join-Path $audioSource $name) -Destination $audioDestination -Recurse
}
Copy-Item -LiteralPath (Join-Path $audioSource '获取音频解码组件.cmd') -Destination $out
$unexpectedAudioFiles = Get-ChildItem -LiteralPath $audioDestination -File -Recurse |
  Where-Object { $_.Extension -in '.exe', '.dll', '.zip' }
if ($unexpectedAudioFiles -or (Test-Path -LiteralPath (Join-Path $out '_internal\tools\audio\vgmstream'))) {
  throw 'Audio runtime binaries must not be included in the release package.'
}

Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'PREVIEW_GUIDE.md') -Destination (Join-Path $out '使用说明.md')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSE') -Destination (Join-Path $out 'LICENSE')

Write-Host "OK exe:" $exe
Write-Host "OK tpk:" $tpk
Write-Host "OK archspec:" $arch
Write-Host "OK native host:" $nativeHost
Write-Host "Double-click JiXingModHelper.exe (no cmd). Copy whole folder."
