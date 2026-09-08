param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot,
    [string]$Version = '',
    [string]$SourceRegistry = 'ghcr.io',
    [string]$SourceNamespace = 'manderzuo/xianyu-collector',
    [string]$SourceTag = '',
    [switch]$SkipInfrastructure
)

$ErrorActionPreference = 'Stop'

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

function Get-RuntimeChanges([string]$Name) {
    switch ($Name) {
        'backend' { return @('WORKDIR /app') }
        'websocket' { return @('WORKDIR /app') }
        'scheduler' { return @('WORKDIR /app') }
        'frontend' { return @('ENTRYPOINT /docker-entrypoint.sh') }
        default { return @() }
    }
}

function Get-CreateCommand([string]$Name) {
    switch ($Name) {
        'backend' { return @('uvicorn', 'backend.main:app', '--host', '0.0.0.0', '--port', '8089') }
        'websocket' { return @('uvicorn', 'websocket.main:app', '--host', '0.0.0.0', '--port', '8090') }
        'scheduler' { return @('uvicorn', 'scheduler.main:app', '--host', '0.0.0.0', '--port', '8091') }
        'frontend' { return @('nginx', '-g', 'daemon off;') }
        default { return @() }
    }
}

$entries = @()
$imageSpecs = @(
    @{ Name = 'backend'; Source = "$($SourceRegistry.TrimEnd('/'))/$($SourceNamespace.Trim('/'))/xianyu-backend:$sourceTagValue"; Target = "local/xianyu/xianyu-backend:$Version"; Format = 'rootfs'; Changes = (Get-RuntimeChanges 'backend'); CreateCommand = (Get-CreateCommand 'backend') },
    @{ Name = 'websocket'; Source = "$($SourceRegistry.TrimEnd('/'))/$($SourceNamespace.Trim('/'))/xianyu-websocket:$sourceTagValue"; Target = "local/xianyu/xianyu-websocket:$Version"; Format = 'rootfs'; Changes = (Get-RuntimeChanges 'websocket'); CreateCommand = (Get-CreateCommand 'websocket') },
    @{ Name = 'scheduler'; Source = "$($SourceRegistry.TrimEnd('/'))/$($SourceNamespace.Trim('/'))/xianyu-scheduler:$sourceTagValue"; Target = "local/xianyu/xianyu-scheduler:$Version"; Format = 'rootfs'; Changes = (Get-RuntimeChanges 'scheduler'); CreateCommand = (Get-CreateCommand 'scheduler') },
    @{ Name = 'frontend'; Source = "$($SourceRegistry.TrimEnd('/'))/$($SourceNamespace.Trim('/'))/xianyu-frontend:$sourceTagValue"; Target = "local/xianyu/xianyu-frontend:$Version"; Format = 'rootfs'; Changes = (Get-RuntimeChanges 'frontend'); CreateCommand = (Get-CreateCommand 'frontend') }
)
if (-not $SkipInfrastructure) {
    $imageSpecs += @(
        @{ Name = 'mysql-8.0'; Source = 'mysql:8.0'; Target = 'mysql:8.0'; Format = 'docker'; Changes = @() },
        @{ Name = 'redis-7-alpine'; Source = 'redis:7-alpine'; Target = 'redis:7-alpine'; Format = 'docker'; Changes = @() }
    )
}

$staging = Join-Path ([System.IO.Path]::GetTempPath()) ('xianyu-image-bundle-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $staging -Force | Out-Null
try {
    foreach ($spec in $imageSpecs) {
        & docker image inspect $spec.Source *> $null
        if ($LASTEXITCODE -ne 0) { Fail "Source image is not available locally: $($spec.Source)" }

        $tarPath = Join-Path $staging "$($spec.Name).tar"
        $archiveName = "$($spec.Name).tar.gz"
        $archivePath = Join-Path $imageRoot $archiveName
        if ($spec.Format -eq 'rootfs') {
            $containerName = "xianyu-offline-export-$([guid]::NewGuid().ToString('N'))"
            $containerId = ''
            try {
                $createArguments = @('create', '--name', $containerName, $spec.Source) + @($spec.CreateCommand)
                $createOutput = & docker @createArguments 2>&1
                if ($LASTEXITCODE -ne 0) {
                    Fail "Could not create an export container for $($spec.Name): $($createOutput -join ' ')"
                }
                $containerId = ($createOutput | Out-String).Trim()
                if ([string]::IsNullOrWhiteSpace($containerId)) { Fail "Could not create an export container for $($spec.Name)." }
                Invoke-Docker @('export', '--output', $tarPath, $containerId) "Exporting $($spec.Name) root filesystem"
            } finally {
                if ($containerId) { & docker rm $containerId *> $null }
            }
        } else {
            if ($spec.Source -ne $spec.Target) {
                Invoke-Docker @('tag', $spec.Source, $spec.Target) "Tagging $($spec.Name) for offline use"
            }
            Invoke-Docker @('save', '--output', $tarPath, $spec.Target) "Exporting $($spec.Name) image"
            $tarListing = @(tar -tf $tarPath 2>$null)
            if ($tarListing.Count -lt 10 -or -not ($tarListing -match 'blobs/sha256/')) {
                Fail "Docker archive validation failed for $($spec.Name); it does not contain image layers."
            }
        }
        Compress-GzipFile -InputPath $tarPath -OutputPath $archivePath
        $hash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
        $archiveBytes = (Get-Item -LiteralPath $archivePath).Length
        $tarBytes = (Get-Item -LiteralPath $tarPath).Length
        if ($tarBytes -lt 1048576) { Fail "Exported archive for $($spec.Name) is unexpectedly small ($tarBytes bytes)." }
        $entries += [ordered]@{
            name = $spec.Name
            image = $spec.Target
            source_image = $spec.Source
            format = $spec.Format
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
        format_version = 1
        product = 'xianyu-rewrite'
        version = $Version
        created_at = [DateTime]::UtcNow.ToString('o')
        image_registry = 'local'
        image_namespace = 'xianyu'
        image_tag = $Version
        infrastructure_included = -not $SkipInfrastructure
        images = @($entries)
    }
    $manifestPath = Join-Path $imageRoot 'offline-manifest.json'
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    Write-Host "[xianyu] Offline image bundle created: $imageRoot" -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
}
