param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot,
    [string]$Version = '',
    [string]$SourceRegistry = 'ghcr.io',
    [string]$SourceNamespace = 'manderzuo/xianyu-collector',
    [string]$SourceTag = '',
    [string]$TempDirectory = '',
    [switch]$SkipInfrastructure
)

$ErrorActionPreference = 'Stop'

function Get-FileSha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        } finally { $sha.Dispose() }
    } finally { $stream.Dispose() }
}

function Fail([string]$Message) {
    throw "Offline image bundle failed: $Message"
}

function Get-TextValue([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    return (Get-Content -LiteralPath $Path -Raw).Trim()
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

function Invoke-Docker([string[]]$Arguments, [string]$Label) {
    Write-Host "[xianyu] $Label" -ForegroundColor Cyan
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$Label failed with exit code $LASTEXITCODE" }
}

function Get-RepositoryDigest([string]$ImageRef) {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = & docker image inspect $ImageRef 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) { return '' }
        $image = $raw | ConvertFrom-Json | Select-Object -First 1
        foreach ($repoDigest in @($image.RepoDigests)) {
            $text = "$repoDigest".Trim()
            if ($text -match '@(sha256:[0-9a-fA-F]{64})$') {
                return $Matches[1].ToLowerInvariant()
            }
        }
    } catch { }
    finally { $ErrorActionPreference = $previousPreference }
    return ''
}

function Get-RootFsLayerIds([string]$ImageRef) {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $raw = & docker image inspect $ImageRef 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) { return @() }
        $image = $raw | ConvertFrom-Json | Select-Object -First 1
        return @($image.RootFS.Layers | ForEach-Object { "$($_)".Trim().ToLowerInvariant() })
    } catch { }
    finally { $ErrorActionPreference = $previousPreference }
    return @()
}

$resolvedPackage = (Resolve-Path -LiteralPath $PackageRoot).Path.TrimEnd('\')
$appRoot = Join-Path $resolvedPackage 'app'
$versionFile = Join-Path $appRoot 'VERSION.txt'
if ([string]::IsNullOrWhiteSpace($Version)) { $Version = Get-TextValue $versionFile }
if ([string]::IsNullOrWhiteSpace($Version) -or $Version -notmatch '^\d+(\.\d+){1,3}$') {
    Fail "A valid version is required. Got: $Version"
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker CLI was not found.' }
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Docker Desktop is not running.' }

$sourceTagValue = if ([string]::IsNullOrWhiteSpace($SourceTag)) { $Version } else { $SourceTag }
$imageRoot = Join-Path $resolvedPackage 'resources\images'
New-Item -ItemType Directory -Path $imageRoot -Force | Out-Null

$entries = @()
$imageSpecs = @(
    @{ Name = 'backend'; Source = "$($SourceRegistry.TrimEnd('/'))/$($SourceNamespace.Trim('/'))/xianyu-backend:$sourceTagValue"; Target = "local/xianyu/xianyu-backend:$Version"; Format = 'docker'; Changes = @() },
    @{ Name = 'websocket'; Source = "$($SourceRegistry.TrimEnd('/'))/$($SourceNamespace.Trim('/'))/xianyu-websocket:$sourceTagValue"; Target = "local/xianyu/xianyu-websocket:$Version"; Format = 'docker'; Changes = @() },
    @{ Name = 'scheduler'; Source = "$($SourceRegistry.TrimEnd('/'))/$($SourceNamespace.Trim('/'))/xianyu-scheduler:$sourceTagValue"; Target = "local/xianyu/xianyu-scheduler:$Version"; Format = 'docker'; Changes = @() },
    @{ Name = 'frontend'; Source = "$($SourceRegistry.TrimEnd('/'))/$($SourceNamespace.Trim('/'))/xianyu-frontend:$sourceTagValue"; Target = "local/xianyu/xianyu-frontend:$Version"; Format = 'docker'; Changes = @() }
)
if (-not $SkipInfrastructure) {
    $imageSpecs += @(
        @{ Name = 'mysql-8.0'; Source = 'mysql:8.0'; Target = 'mysql:8.0'; Format = 'docker'; Changes = @() },
        @{ Name = 'redis-7-alpine'; Source = 'redis:7-alpine'; Target = 'redis:7-alpine'; Format = 'docker'; Changes = @() }
    )
}

$usesPackageTemp = [string]::IsNullOrWhiteSpace($TempDirectory)
$tempBase = if ($usesPackageTemp) {
    # Image exports can be several gigabytes. Keep the default staging area on
    # the same disk as the explicitly selected package instead of silently
    # consuming the system drive's TEMP directory.
    Join-Path $resolvedPackage '.offline-build-temp'
} else {
    $resolvedTempBase = [System.IO.Path]::GetFullPath($TempDirectory)
    New-Item -ItemType Directory -Path $resolvedTempBase -Force | Out-Null
    $resolvedTempBase
}
$staging = Join-Path $tempBase ('xianyu-image-bundle-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $staging -Force | Out-Null
$stagedImageRoot = Join-Path $staging 'output'
New-Item -ItemType Directory -Path $stagedImageRoot -Force | Out-Null
try {
    foreach ($spec in $imageSpecs) {
        & docker image inspect $spec.Source *> $null
        if ($LASTEXITCODE -ne 0) { Fail "Source image is not available locally: $($spec.Source)" }
        $sourceImageId = (& docker image inspect $spec.Source --format '{{.Id}}' | Select-Object -First 1).ToString().Trim()
        if ($sourceImageId -notmatch '^sha256:[0-9a-f]{64}$') { Fail "Could not resolve source image ID for $($spec.Name)" }
        $sourceImageDigest = Get-RepositoryDigest $spec.Source
        if ($spec.Source -notmatch '^local/' -and -not $sourceImageDigest) {
            Fail "Could not resolve registry digest for $($spec.Name). Build the bundle from a directly pulled registry image, not a flattened local image."
        }
        $rootFsLayerIds = @(Get-RootFsLayerIds $spec.Source)

        $tarPath = Join-Path $staging "$($spec.Name).tar"
        $archiveName = "$($spec.Name).tar.gz"
        $archivePath = Join-Path $stagedImageRoot $archiveName
        if ($spec.Source -ne $spec.Target) {
            Invoke-Docker @('tag', $spec.Source, $spec.Target) "Tagging $($spec.Name) for offline use"
        }
        Invoke-Docker @('save', '--output', $tarPath, $spec.Target) "Exporting $($spec.Name) image with original layers"
        $tarListing = @(tar -tf $tarPath 2>$null)
        if ($tarListing.Count -lt 10 -or -not ($tarListing -match 'blobs/sha256/')) {
            Fail "Docker archive validation failed for $($spec.Name); it does not contain image layers."
        }
        Compress-GzipFile -InputPath $tarPath -OutputPath $archivePath
        $hash = Get-FileSha256 $archivePath
        $archiveBytes = (Get-Item -LiteralPath $archivePath).Length
        $tarBytes = (Get-Item -LiteralPath $tarPath).Length
        if ($tarBytes -lt 1048576) { Fail "Exported archive for $($spec.Name) is unexpectedly small ($tarBytes bytes)." }
        $entries += [ordered]@{
            name = $spec.Name
            image = $spec.Target
            source_image = $spec.Source
            source_image_id = $sourceImageId
            source_image_digest = $sourceImageDigest
            rootfs_layer_ids = @($rootFsLayerIds)
            format = $spec.Format
            preserves_registry_layers = $true
            runtime_changes = @($spec.Changes)
            archive = $archiveName
            sha256 = $hash
            archive_bytes = [int64]$archiveBytes
            expanded_bytes = [int64]$tarBytes
        }
        Remove-Item -LiteralPath $tarPath -Force
        Write-Host "[xianyu] $($spec.Name): $archiveBytes bytes compressed" -ForegroundColor DarkCyan
    }

    $manifest = [ordered]@{
        format_version = 2
        product = 'xianyu-rewrite'
        version = $Version
        created_at = [DateTime]::UtcNow.ToString('o')
        image_registry = 'local'
        image_namespace = 'xianyu'
        image_tag = $Version
        preserves_registry_layers = $true
        application_images_only = [bool]$SkipInfrastructure
        infrastructure_included = -not $SkipInfrastructure
        images = @($entries)
    }
    $manifestPath = Join-Path $stagedImageRoot 'offline-manifest.json'
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    $currentArchives = @($entries | ForEach-Object { [string]$_.archive })
    $commitBackup = Join-Path $staging 'commit-backup'
    New-Item -ItemType Directory -Path $commitBackup -Force | Out-Null
    $commitFiles = @($currentArchives + 'offline-manifest.json')
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
            Where-Object { $currentArchives -notcontains $_.Name } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
    } catch {
        foreach ($item in @($committed | Sort-Object -Property Destination -Descending)) {
            if (Test-Path -LiteralPath $item.Destination) { Remove-Item -LiteralPath $item.Destination -Force -ErrorAction SilentlyContinue }
            if (Test-Path -LiteralPath $item.Backup) { Move-Item -LiteralPath $item.Backup -Destination $item.Destination -Force -ErrorAction SilentlyContinue }
        }
        throw
    }
    Write-Host '[xianyu] Old offline image archives removed; only the current release is retained.' -ForegroundColor DarkCyan
    Write-Host "[xianyu] Offline image bundle created: $imageRoot" -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
    if ($usesPackageTemp -and (Test-Path -LiteralPath $tempBase)) { Remove-Item -LiteralPath $tempBase -Recurse -Force -ErrorAction SilentlyContinue }
}
