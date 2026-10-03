param(
    [string]$Destination = (Join-Path $PSScriptRoot 'vgmstream')
)

$ErrorActionPreference = 'Stop'
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'source.json') -Raw | ConvertFrom-Json
$archiveDirectory = Join-Path $PSScriptRoot 'downloads'
$archivePath = Join-Path $archiveDirectory ("vgmstream-win64-{0}.zip" -f $manifest.version)
$stageDirectory = Join-Path $PSScriptRoot ('.setup-' + [guid]::NewGuid().ToString('N'))
$destinationPath = [System.IO.Path]::GetFullPath($Destination)

function Test-ArchiveHash {
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) { return $false }
    return (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -eq $manifest.archive_sha256
}

New-Item -ItemType Directory -Force -Path $archiveDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $stageDirectory | Out-Null
try {
    if (-not (Test-ArchiveHash)) {
        $downloadPath = Join-Path $stageDirectory 'download.zip'
        Write-Host ("Downloading official vgmstream {0} for Windows x64..." -f $manifest.version)
        Invoke-WebRequest -Uri $manifest.archive_url -OutFile $downloadPath -TimeoutSec 120
        $downloadHash = (Get-FileHash -LiteralPath $downloadPath -Algorithm SHA256).Hash
        if ($downloadHash -ne $manifest.archive_sha256) {
            throw 'The downloaded archive does not match source.json; it will not be installed.'
        }
        Copy-Item -LiteralPath $downloadPath -Destination $archivePath -Force
    }

    $extractedDirectory = Join-Path $stageDirectory 'extracted'
    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractedDirectory
    foreach ($file in $manifest.files) {
        $filePath = Join-Path $extractedDirectory $file.name
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            throw ("Missing runtime file: {0}" -f $file.name)
        }
        if ((Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash -ne $file.sha256) {
            throw ("Runtime file hash mismatch: {0}" -f $file.name)
        }
    }

    New-Item -ItemType Directory -Force -Path $destinationPath | Out-Null
    foreach ($file in $manifest.files) {
        Copy-Item -LiteralPath (Join-Path $extractedDirectory $file.name) -Destination (Join-Path $destinationPath $file.name) -Force
    }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'licenses') -Destination $destinationPath -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'source.json') -Destination $destinationPath -Force
    $decoderPath = Join-Path $destinationPath 'vgmstream-cli.exe'
    & $decoderPath -h
    if ($LASTEXITCODE -ne 0) { throw 'vgmstream could not start on this computer.' }
    Write-Host ("Installed and hash-checked: {0}" -f $decoderPath)
}
finally {
    # Only remove the unique staging directory created by this invocation.
    $stageFullPath = [System.IO.Path]::GetFullPath($stageDirectory)
    $toolsRoot = [System.IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
    if ($stageFullPath.StartsWith($toolsRoot, [System.StringComparison]::OrdinalIgnoreCase) -and
        [System.IO.Path]::GetFileName($stageFullPath).StartsWith('.setup-') -and
        (Test-Path -LiteralPath $stageFullPath)) {
        Remove-Item -LiteralPath $stageFullPath -Recurse -Force
    }
}
