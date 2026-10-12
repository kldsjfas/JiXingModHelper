param([string]$Destination = (Join-Path $PSScriptRoot 'ffmpeg'))

$ErrorActionPreference = 'Stop'
$utilityModule = Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1'
Import-Module -Name $utilityModule -Force -ErrorAction Stop
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'source.json') -Raw | ConvertFrom-Json
$toolsRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$destinationPath = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
$cacheRoot = Join-Path $toolsRoot 'cache'
$archivePath = Join-Path $cacheRoot $manifest.archive_name
$stagePath = $null
$previousPath = $null
$runtimeReady = $false
$result = [ordered]@{passed=$false;version=$manifest.version;destination=$destinationPath;previous_component='';cache_bytes_removed=0;cache_files_removed=0}

function Get-ComponentPath([string]$Root, [string]$Relative) {
    $rootPath = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $path = [IO.Path]::GetFullPath((Join-Path $rootPath $Relative))
    if (-not $path.StartsWith($rootPath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Component file escaped its root.' }
    return $path
}

function Test-ComponentFiles([string]$Root) {
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return $false }
    if ((Get-Item -LiteralPath $Root).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Component directory must not be a reparse point.' }
    foreach ($file in $manifest.files) {
        $path = Get-ComponentPath $Root $file.name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
        $item = Get-Item -LiteralPath $path
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Component file must not be a reparse point.' }
        if ($item.Length -ne $file.size -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $file.sha256) { return $false }
    }
    return $true
}

function Confirm-ComponentStarts([string]$Root) {
    foreach ($name in @('ffmpeg', 'ffprobe')) {
        $path = Get-ComponentPath $Root ('bin\' + $name + '.exe')
        $versionOutput = @(& $path -version 2>&1)
        if ($LASTEXITCODE -ne 0 -or -not $versionOutput.Count -or
            -not ([string]$versionOutput[0]).StartsWith($name + ' version ' + $manifest.version)) {
            throw ("{0} could not start with the pinned version on this computer." -f $name)
        }
    }
}

function Test-Archive {
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) { return $false }
    $item = Get-Item -LiteralPath $archivePath
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Download cache must not contain a reparse point.' }
    return $item.Length -eq $manifest.archive_size -and (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -eq $manifest.archive_sha256
}

function Remove-OwnedStage {
    if (-not $stagePath -or -not (Test-Path -LiteralPath $stagePath)) { return }
    $stageFullPath = [IO.Path]::GetFullPath($stagePath)
    if ([IO.Path]::GetDirectoryName($stageFullPath) -ne $cacheRoot -or
        -not [IO.Path]::GetFileName($stageFullPath).StartsWith('setup-')) { throw 'Refusing cleanup outside the owned cache stage.' }
    $items = @(Get-Item -LiteralPath $stageFullPath; Get-ChildItem -LiteralPath $stageFullPath -Force -Recurse)
    if (@($items | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }).Count) { throw 'Owned cache contains a reparse point; preserve it for inspection.' }
    $files = @($items | Where-Object { -not $_.PSIsContainer })
    $bytes = [long]0
    foreach ($file in $files) { $bytes += [long]$file.Length }
    Remove-Item -LiteralPath $stageFullPath -Recurse -Force
    if (Test-Path -LiteralPath $stageFullPath) { throw 'Owned cache cleanup was incomplete.' }
    $result.cache_files_removed += $files.Count
    $result.cache_bytes_removed += $bytes
}

try {
    if ([IO.Path]::GetDirectoryName($destinationPath) -ne $toolsRoot -or $destinationPath -eq $cacheRoot) {
        throw 'Install components into a direct child of this tools/emote directory.'
    }
    if ([IO.Path]::GetFileName($manifest.archive_name) -ne $manifest.archive_name) { throw 'Invalid archive name in source.json.' }
    foreach ($license in $manifest.licenses) {
        $path = Get-ComponentPath (Join-Path $toolsRoot 'licenses') $license.name
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $license.sha256) { throw 'Bundled license text differs from source.json.' }
    }
    if (Test-ComponentFiles $destinationPath) {
        Confirm-ComponentStarts $destinationPath
        $runtimeReady = $true
        $result.passed = $true
        Write-Host 'Pinned FFmpeg and FFprobe are already installed and hash-checked.'
        return
    }
    if (Test-Path -LiteralPath $destinationPath) {
        $existing = Get-Item -LiteralPath $destinationPath
        if (-not $existing.PSIsContainer -or $existing.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw 'Existing component path is not an ordinary directory; it was left unchanged.'
        }
    }
    $null = New-Item -ItemType Directory -Path $cacheRoot -Force
    if ((Get-Item -LiteralPath $cacheRoot).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Cache directory must not be a reparse point.' }
    $stagePath = Join-Path $cacheRoot ('setup-' + [Guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $stagePath
    if (-not (Test-Archive)) {
        $downloadPath = Join-Path $stagePath 'download.zip'
        Write-Host ("Downloading pinned FFmpeg {0} from BtbN GitHub ({1:N1} MB)..." -f $manifest.version, ($manifest.archive_size / 1MB))
        Invoke-WebRequest -UseBasicParsing -Uri $manifest.archive_url -OutFile $downloadPath -TimeoutSec 300
        $download = Get-Item -LiteralPath $downloadPath
        if ($download.Length -ne $manifest.archive_size -or
            (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash -ne $manifest.archive_sha256) {
            throw 'Downloaded archive differs from the pinned SHA256 manifest; existing components were not changed.'
        }
        Copy-Item -LiteralPath $downloadPath -Destination $archivePath -Force
    }
    $payloadPath = Join-Path $stagePath 'runtime'
    $null = New-Item -ItemType Directory -Path $payloadPath
    $archive = [IO.Compression.ZipFile]::OpenRead($archivePath)
    try {
        foreach ($file in $manifest.files) {
            $entry = $archive.GetEntry($manifest.root_directory + '/' + $file.name)
            if (-not $entry -or $entry.Length -ne $file.size) { throw ("Missing or invalid archive entry: {0}" -f $file.name) }
            $target = Get-ComponentPath $payloadPath $file.name
            $null = New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target)
        }
    } finally { $archive.Dispose() }
    if (-not (Test-ComponentFiles $payloadPath)) { throw 'Extracted component files failed SHA256 validation.' }
    Copy-Item -LiteralPath (Join-Path $toolsRoot 'licenses') -Destination $payloadPath -Recurse
    Copy-Item -LiteralPath (Join-Path $toolsRoot 'source.json') -Destination $payloadPath
    Confirm-ComponentStarts $payloadPath
    if (Test-Path -LiteralPath $destinationPath) {
        $previousPath = Join-Path $toolsRoot ('ffmpeg.previous-' + [Guid]::NewGuid().ToString('N'))
        Move-Item -LiteralPath $destinationPath -Destination $previousPath
        $result.previous_component = $previousPath
    }
    try { Move-Item -LiteralPath $payloadPath -Destination $destinationPath }
    catch {
        if ($previousPath -and -not (Test-Path -LiteralPath $destinationPath)) {
            try { Move-Item -LiteralPath $previousPath -Destination $destinationPath }
            catch { throw ("Install and rollback failed. Preserve previous components at {0}." -f $previousPath) }
        }
        throw
    }
    $runtimeReady = $true
    $result.passed = $true
    Write-Host ("Installed and hash-checked: {0}" -f (Join-Path $destinationPath 'bin\ffmpeg.exe'))
    if ($previousPath) { Write-Host ("Previous components were preserved at: {0}" -f $previousPath) }
}
catch {
    $result.error = $_.Exception.Message
    throw
}
finally {
    Remove-OwnedStage
    if ($runtimeReady -and (Test-Archive)) {
        $archiveBytes = (Get-Item -LiteralPath $archivePath).Length
        Remove-Item -LiteralPath $archivePath -Force
        if (Test-Path -LiteralPath $archivePath) { throw 'Verified download cache cleanup was incomplete.' }
        $result.cache_files_removed += 1
        $result.cache_bytes_removed += [long]$archiveBytes
    }
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $toolsRoot 'setup-result.json') -Encoding utf8
}
