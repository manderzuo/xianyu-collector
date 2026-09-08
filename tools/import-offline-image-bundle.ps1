param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    throw "Offline image import failed: $Message"
}

function Expand-GzipFile([string]$InputPath, [string]$OutputPath) {
    $inputStream = [System.IO.File]::OpenRead($InputPath)
    $gzipStream = New-Object System.IO.Compression.GZipStream(
        $inputStream,
        [System.IO.Compression.CompressionMode]::Decompress
    )
    $outputStream = [System.IO.File]::Create($OutputPath)
    try {
        $gzipStream.CopyTo($outputStream)
    } finally {
        $outputStream.Dispose()
        $gzipStream.Dispose()
        $inputStream.Dispose()
    }
}

function Invoke-DockerLoad([string]$TarPath, [string]$Name) {
    Write-Host "[xianyu] Importing $Name" -ForegroundColor Cyan
    & docker load --input $TarPath
    if ($LASTEXITCODE -ne 0) { Fail "docker load failed for $Name with exit code $LASTEXITCODE" }
}

function Invoke-DockerImport([string]$TarPath, [string]$Image, [object[]]$Changes, [string]$Name) {
    Write-Host "[xianyu] Importing $Name root filesystem" -ForegroundColor Cyan
    $arguments = @('import')
    foreach ($change in @($Changes)) {
        if (-not [string]::IsNullOrWhiteSpace([string]$change)) {
            $arguments += @('--change', [string]$change)
        }
    }
    $arguments += @($TarPath, $Image)
    & docker @arguments
    if ($LASTEXITCODE -ne 0) { Fail "docker import failed for $Name with exit code $LASTEXITCODE" }
}

$resolvedPackage = (Resolve-Path -LiteralPath $PackageRoot).Path.TrimEnd('\')
$imageRoot = Join-Path $resolvedPackage 'resources\images'
$manifestPath = Join-Path $imageRoot 'offline-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { Fail "Offline manifest not found: $manifestPath" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker CLI was not found.' }

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.format_version -ne 1) { Fail "Unsupported offline manifest version: $($manifest.format_version)" }
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('xianyu-image-import-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$imageRootWithSlash = $imageRoot.TrimEnd('\') + '\'
try {
    foreach ($entry in @($manifest.images)) {
        $archiveName = [string]$entry.archive
        if ([string]::IsNullOrWhiteSpace($archiveName) -or $archiveName -match '[\\/:]' -or $archiveName -in @('.', '..')) {
            Fail "Invalid archive name for $($entry.name)"
        }
        $archivePath = [System.IO.Path]::GetFullPath((Join-Path $imageRoot $archiveName))
        if (-not $archivePath.StartsWith($imageRootWithSlash, [System.StringComparison]::OrdinalIgnoreCase)) {
            Fail "Archive escapes image bundle directory: $archiveName"
        }
        if (-not (Test-Path -LiteralPath $archivePath)) { Fail "Archive is missing: $archivePath" }
        $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne "$($entry.sha256)".ToLowerInvariant()) {
            Fail "SHA-256 mismatch for $($entry.name)"
        }

        $tarPath = Join-Path $tempRoot "$([guid]::NewGuid().ToString('N')).tar"
        if ($archivePath.EndsWith('.gz', [System.StringComparison]::OrdinalIgnoreCase)) {
            Expand-GzipFile -InputPath $archivePath -OutputPath $tarPath
        } else {
            Copy-Item -LiteralPath $archivePath -Destination $tarPath -Force
        }
        if ("$($entry.format)" -eq 'rootfs') {
            Invoke-DockerImport -TarPath $tarPath -Image ([string]$entry.image) -Changes @($entry.runtime_changes) -Name "$($entry.name) ($($entry.image))"
        } else {
            Invoke-DockerLoad -TarPath $tarPath -Name "$($entry.name) ($($entry.image))"
        }
        & docker image inspect ([string]$entry.image) *> $null
        if ($LASTEXITCODE -ne 0) { Fail "Expected image is missing after import: $($entry.image)" }
        Remove-Item -LiteralPath $tarPath -Force
    }
    Write-Host '[xianyu] All offline images imported successfully.' -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
