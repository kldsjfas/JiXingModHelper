# Build windowed exe: dist\JiXingModHelper\JiXingModHelper.exe  (NO console / NO cmd)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "==> deps"
python -m pip install -q -r requirements.txt
python -m pip install -q "pyinstaller==6.22.2"

Write-Host "==> clean"
# kill leftover Edge/app locking dist\modkit_data
Get-Process -Name JiXingModHelper -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'msedge.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
  if ($_.CommandLine -and ($_.CommandLine -like '*AstralPartyModHelper*' -or $_.CommandLine -like '*JiXingModHelper*' -or $_.CommandLine -like '*edge_profile*')) {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
}
Start-Sleep -Seconds 1
if (Test-Path build) { cmd /c "rmdir /s /q build" }
if (Test-Path "dist\JiXingModHelper") { cmd /c "rmdir /s /q dist\JiXingModHelper" }
if (Test-Path "dist\JiXingModHelper") {
  # last resort: rename locked tree out of the way
  $bak = "dist\_old_JiXingModHelper_" + (Get-Date -Format "HHmmss")
  Rename-Item "dist\JiXingModHelper" $bak -ErrorAction SilentlyContinue
}

Write-Host "==> native web host"
dotnet publish .\native_host\JiXingModHelperHost.csproj -c Release -r win-x64 --self-contained true --output .\native_host\publish
if ($LASTEXITCODE -ne 0) { throw "Native web host build failed" }

Write-Host "==> pyinstaller"
python -m PyInstaller --noconfirm --clean build_exe.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$out = Join-Path $PSScriptRoot "dist\JiXingModHelper"
$exe = Join-Path $out "JiXingModHelper.exe"
$tpk = Join-Path $out "_internal\UnityPy\resources\lzma.tpk"
$web = Join-Path $out "_internal\astral_party_auto\webui\index.html"
$nativeHost = Join-Path $out "_internal\native_host\JiXingModHelperHost.exe"
$arch = Join-Path $out "_internal\archspec\json\cpu\microarchitectures.json"

if (-not (Test-Path $exe)) { throw "missing exe" }
if (-not (Test-Path $tpk)) { throw "missing UnityPy resources\lzma.tpk - package broken" }
if (-not (Test-Path $web)) { throw "missing webui" }
if (-not (Test-Path $nativeHost)) { throw "missing native web host" }
if (-not (Test-Path $arch)) { throw "missing archspec json - texture preview will break" }

Write-Host "OK exe:" $exe
Write-Host "OK tpk:" $tpk
Write-Host "OK archspec:" $arch
Write-Host "OK native host:" $nativeHost
Write-Host "Double-click JiXingModHelper.exe (no cmd). Copy whole folder."
