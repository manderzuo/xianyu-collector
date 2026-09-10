param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot,
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    throw "Offline infrastructure bundle failed: $Message"
}

function Write-Utf8Json([string]$Path, [object]$Value) {
    $json = $Value | ConvertTo-Json -Depth 12
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($false)))
}

function Compress-GzipFile([string]$InputPath, [string]$OutputPath) {
    $inputStream = [System.IO.File]::OpenRead($InputPath)
    $outputStream = [System.IO.File]::Create($OutputPath)
    $gzipStream = New-Object System.IO.Compression.GZipStream(
        $outputStream,
        [System.IO.Compression.CompressionMode]::Compress
    )
    try {
        $inputStream.CopyTo($gzipStream)
    } finally {
        $gzipStream.Dispose()
        $outputStream.Dispose()
        $inputStream.Dispose()
    }
}

function Get-ImageJson([string]$ImageRef) {
    $raw = & docker image inspect $ImageRef 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { Fail "Source image is not available locally: $ImageRef" }
    return ($raw | ConvertFrom-Json | Select-Object -First 1)
}

function Get-RepositoryDigest([object]$Image) {
    foreach ($repoDigest in @($Image.RepoDigests)) {
        $text = "$repoDigest".Trim()
        if ($text -match '@(sha256:[0-9a-fA-F]{64})$') { return $Matches[1].ToLowerInvariant() }
    }
    return ''
}

$resolvedPackage = (Resolve-Path -LiteralPath $PackageRoot -ErrorAction Stop).Path.TrimEnd('\')
$appRoot = Join-Path $resolvedPackage 'app'
$imageRoot = Join-Path $resolvedPackage 'resources\images'
$manifestPath = Join-Path $imageRoot 'offline-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { Fail "Application offline manifest is missing: $manifestPath" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker CLI was not found.' }
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Docker Desktop is not running.' }

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.format_version -notin @(1, 2)) { Fail "Unsupported offline manifest version: $($manifest.format_version)" }
if ([string]::IsNullOrWhiteSpace($Version)) { $Version = ([string]$manifest.version).Trim() }
if ($Version -notmatch '^\d+(\.\d+){1,3}$') { Fail "Invalid version: $Version" }
if (([string]$manifest.version).Trim() -ne $Version) { Fail 'Version does not match the existing application manifest.' }
New-Item -ItemType Directory -Path $imageRoot -Force | Out-Null

$tempRoot = Join-Path $resolvedPackage '.offline-infrastructure-temp'
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$stagedImageRoot = Join-Path $tempRoot 'output'
New-Item -ItemType Directory -Path $stagedImageRoot -Force | Out-Null
$specs = @(
    @{ Name = 'mysql-8.0'; Source = 'mysql:8.0' },
    @{ Name = 'redis-7-alpine'; Source = 'redis:7-alpine' }
)
$newEntries = @()
try {
    foreach ($spec in $specs) {
        $image = Get-ImageJson $spec.Source
        $sourceId = ([string]$image.Id).Trim().ToLowerInvariant()
        if ($sourceId -notmatch '^sha256:[0-9a-f]{64}$') { Fail "Could not resolve image ID for $($spec.Name)." }
        $archiveName = "$($spec.Name).tar.gz"
        $tarPath = Join-Path $tempRoot "$($spec.Name).tar"
        $archivePath = Join-Path $stagedImageRoot $archiveName
        Write-Host "[xianyu] Exporting $($spec.Name) with original Docker layers" -ForegroundColor Cyan
        & docker save --output $tarPath $spec.Source
        if ($LASTEXITCODE -ne 0) { Fail "docker save failed for $($spec.Name) with exit code $LASTEXITCODE." }
        $listing = @(tar.exe -tf $tarPath 2>$null)
        if ($listing.Count -lt 10 -or -not ($listing -match 'blobs/sha256/')) {
            Fail "Docker archive validation failed for $($spec.Name); layer blobs are missing."
        }
        Compress-GzipFile -InputPath $tarPath -OutputPath $archivePath
        $archiveBytes = (Get-Item -LiteralPath $archivePath).Length
        $expandedBytes = (Get-Item -LiteralPath $tarPath).Length
        if ($expandedBytes -lt 1048576) { Fail "Exported archive for $($spec.Name) is unexpectedly small." }
        $newEntries += [ordered]@{
            name = $spec.Name
            image = $spec.Source
            source_image = $spec.Source
            source_image_id = $sourceId
            source_image_digest = Get-RepositoryDigest $image
            rootfs_layer_ids = @($image.RootFS.Layers | ForEach-Object { ([string]$_).Trim().ToLowerInvariant() })
            format = 'docker'
            preserves_registry_layers = $true
            runtime_changes = @()
            archive = $archiveName
            sha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
            archive_bytes = [int64]$archiveBytes
            expanded_bytes = [int64]$expandedBytes
        }
        Write-Host "[xianyu] $($spec.Name) archive created: $archiveBytes bytes" -ForegroundColor Green
    }

    $combined = @($manifest.images | Where-Object { @('mysql-8.0', 'redis-7-alpine') -notcontains ([string]$_.name) }) + @($newEntries)
    $output = [ordered]@{}
    foreach ($property in $manifest.PSObject.Properties) { $output[$property.Name] = $property.Value }
    $output.format_version = 2
    $output.version = $Version
    $output.application_images_only = $false
    $output.infrastructure_included = $true
    $output.images = @($combined)
    $stagedManifestPath = Join-Path $stagedImageRoot 'offline-manifest.json'
    Write-Utf8Json $stagedManifestPath $output

    $currentInfrastructure = @($newEntries | ForEach-Object { [string]$_.archive })
    $commitBackup = Join-Path $tempRoot 'commit-backup'
    New-Item -ItemType Directory -Path $commitBackup -Force | Out-Null
    $commitFiles = @($currentInfrastructure + 'offline-manifest.json')
    $committed = New-Object System.Collections.Generic.List[object]
    try {
        foreach ($name in $commitFiles) {
            $destination = Join-Path $imageRoot $name
            $backup = Join-Path $commitBackup $name
            if (Test-Path -LiteralPath $destination) { Move-Item -LiteralPath $destination -Destination $backup -Force }
            [void]$committed.Add([pscustomobject]@{ Destination = $destination; Backup = $backup })
            Move-Item -LiteralPath (Join-Path $stagedImageRoot $name) -Destination $destination -Force
        }
        Get-ChildItem -LiteralPath $imageRoot -Filter '*.tar.gz' -File -Force -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -match '^(mysql|redis)(-.+)?\.tar\.gz$' -and $currentInfrastructure -notcontains $_.Name
            } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
    } catch {
        foreach ($item in @($committed | Sort-Object -Property Destination -Descending)) {
            if (Test-Path -LiteralPath $item.Destination) { Remove-Item -LiteralPath $item.Destination -Force -ErrorAction SilentlyContinue }
            if (Test-Path -LiteralPath $item.Backup) { Move-Item -LiteralPath $item.Backup -Destination $item.Destination -Force -ErrorAction SilentlyContinue }
        }
        throw
    }
    Write-Host '[xianyu] Old infrastructure image archives removed; only the current release is retained.' -ForegroundColor DarkCyan
    Write-Host '[xianyu] Complete offline image set now includes application, MySQL and Redis images.' -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
