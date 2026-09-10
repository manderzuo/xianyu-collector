param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot,
    [string]$Version = '',
    [string]$SourceRegistry = 'www.gemstory.cn',
    [string]$SourceNamespace = 'xianyu',
    [string]$SourceTag = ''
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    throw "Offline registry bundle failed: $Message"
}

function Write-Utf8Json([string]$Path, [object]$Value) {
    $json = $Value | ConvertTo-Json -Depth 12
    [IO.File]::WriteAllText($Path, $json, (New-Object Text.UTF8Encoding($false)))
}

function Get-BlobHex([string]$Digest) {
    if ($Digest -notmatch '^sha256:([0-9a-fA-F]{64})$') {
        Fail "Unsupported or invalid digest: $Digest"
    }
    return $Matches[1].ToLowerInvariant()
}

function Invoke-CurlDownload([string]$Url, [string]$OutputPath, [string]$Label, [string]$Accept = '') {
    $parent = Split-Path -Parent $OutputPath
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Write-Host "[xianyu] Downloading $Label" -ForegroundColor Cyan
    $curlArguments = @('--fail', '--location', '--retry', '4', '--retry-delay', '2', '--connect-timeout', '30')
    if (-not [string]::IsNullOrWhiteSpace($Accept)) { $curlArguments += @('-H', $Accept) }
    $curlArguments += @('--output', $OutputPath, $Url)
    & curl.exe @curlArguments
    if ($LASTEXITCODE -ne 0) { Fail "Download failed for $Label (exit code $LASTEXITCODE)." }
    if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) { Fail "Download produced no file for $Label." }
}

function Verify-Blob([string]$Path, [string]$Digest, [int64]$ExpectedSize, [string]$Label) {
    $actualDigest = 'sha256:' + (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualDigest -ne $Digest.ToLowerInvariant()) {
        Fail "SHA-256 mismatch for $Label. Expected $Digest, got $actualDigest."
    }
    if ($ExpectedSize -gt 0) {
        $actualSize = (Get-Item -LiteralPath $Path).Length
        if ($actualSize -ne $ExpectedSize) {
            Fail "Size mismatch for $Label. Expected $ExpectedSize, got $actualSize."
        }
    }
}

function Get-RegistryManifest([string]$Registry, [string]$Repository, [string]$Reference, [string]$StagingPath, [string]$Label) {
    $base = "https://$($Registry.TrimEnd('/'))/v2/$($Repository.Trim('/'))"
    $accept = 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
    $requestedPath = Join-Path $StagingPath "$Label-requested.json"
    $accept = 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'
    Invoke-CurlDownload "$base/manifests/$Reference" $requestedPath "$Label manifest" $accept
    $requested = Get-Content -LiteralPath $requestedPath -Raw | ConvertFrom-Json

    if ($requested.manifests) {
        $descriptor = @($requested.manifests | Where-Object {
            $_.platform -and $_.platform.os -eq 'linux' -and $_.platform.architecture -eq 'amd64'
        } | Select-Object -First 1)
        if (-not $descriptor) { $descriptor = @($requested.manifests | Select-Object -First 1) }
        if (-not $descriptor) { Fail "$Label manifest list contains no image manifest." }
        $selectedDigest = [string]$descriptor[0].digest
        $selectedPath = Join-Path $StagingPath "$Label-selected.json"
        Invoke-CurlDownload "$base/manifests/$selectedDigest" $selectedPath "$Label linux/amd64 manifest" $accept
        Verify-Blob $selectedPath $selectedDigest ([int64]$descriptor[0].size) "$Label selected manifest"
        $manifest = Get-Content -LiteralPath $selectedPath -Raw | ConvertFrom-Json
        return [pscustomobject]@{
            Manifest = $manifest
            ManifestPath = $selectedPath
            Digest = $selectedDigest.ToLowerInvariant()
        }
    }

    $requestedDigest = 'sha256:' + (Get-FileHash -LiteralPath $requestedPath -Algorithm SHA256).Hash.ToLowerInvariant()
    return [pscustomobject]@{
        Manifest = $requested
        ManifestPath = $requestedPath
        Digest = $requestedDigest
    }
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

$resolvedPackage = (Resolve-Path -LiteralPath $PackageRoot -ErrorAction Stop).Path.TrimEnd('\')
$appRoot = Join-Path $resolvedPackage 'app'
$versionPath = Join-Path $appRoot 'VERSION.txt'
if ([string]::IsNullOrWhiteSpace($Version) -and (Test-Path -LiteralPath $versionPath)) {
    $Version = (Get-Content -LiteralPath $versionPath -Raw).Trim()
}
if ($Version -notmatch '^\d+(\.\d+){1,3}$') { Fail "Invalid version: $Version" }
$tag = if ([string]::IsNullOrWhiteSpace($SourceTag)) { $Version } else { $SourceTag.Trim() }
$imageRoot = Join-Path $resolvedPackage 'resources\images'
$workRoot = Join-Path $resolvedPackage '.offline-registry-work'
$cacheRoot = Join-Path $workRoot 'blob-cache'
$stagedImageRoot = Join-Path $workRoot 'output'
New-Item -ItemType Directory -Path $imageRoot, $cacheRoot, $stagedImageRoot -Force | Out-Null

$specs = @(
    @{ Name = 'backend'; Repository = "$($SourceNamespace.Trim('/'))/xianyu-backend"; Target = "local/xianyu/xianyu-backend:$Version" },
    @{ Name = 'websocket'; Repository = "$($SourceNamespace.Trim('/'))/xianyu-websocket"; Target = "local/xianyu/xianyu-websocket:$Version" },
    @{ Name = 'scheduler'; Repository = "$($SourceNamespace.Trim('/'))/xianyu-scheduler"; Target = "local/xianyu/xianyu-scheduler:$Version" },
    @{ Name = 'frontend'; Repository = "$($SourceNamespace.Trim('/'))/xianyu-frontend"; Target = "local/xianyu/xianyu-frontend:$Version" }
)
$entries = @()
try {
    foreach ($spec in $specs) {
        $label = $spec.Name
        $stagingRoot = Join-Path $workRoot "$label-$([guid]::NewGuid().ToString('N'))"
        $ociRoot = Join-Path $stagingRoot 'oci'
        $blobRoot = Join-Path $ociRoot 'blobs\sha256'
        New-Item -ItemType Directory -Path $blobRoot -Force | Out-Null
        try {
            $result = Get-RegistryManifest $SourceRegistry $spec.Repository $tag $stagingRoot $label
            $manifest = $result.Manifest
            $manifestDigest = $result.Digest
            $manifestPath = $result.ManifestPath
            $manifestHex = Get-BlobHex $manifestDigest
            Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $blobRoot $manifestHex) -Force

            $descriptors = @($manifest.config) + @($manifest.layers)
            if (-not $manifest.config -or @($manifest.layers).Count -eq 0) {
                Fail "$label manifest has no config or layers."
            }
            foreach ($descriptor in $descriptors) {
                $digest = ([string]$descriptor.digest).ToLowerInvariant()
                $hex = Get-BlobHex $digest
                $cachePath = Join-Path $cacheRoot $hex
                $blobPath = Join-Path $blobRoot $hex
                if (Test-Path -LiteralPath $cachePath -PathType Leaf) {
                    try { Verify-Blob $cachePath $digest ([int64]$descriptor.size) "$label cached blob $hex" }
                    catch { Remove-Item -LiteralPath $cachePath -Force; throw }
                    Write-Host "[xianyu] Reusing cached blob $hex for $label" -ForegroundColor DarkCyan
                } else {
                    Invoke-CurlDownload "https://$($SourceRegistry.TrimEnd('/'))/v2/$($spec.Repository)/blobs/$digest" $cachePath "$label blob $hex"
                    Verify-Blob $cachePath $digest ([int64]$descriptor.size) "$label blob $hex"
                }
                Copy-Item -LiteralPath $cachePath -Destination $blobPath -Force
            }

            $refName = [ordered]@{
                'org.opencontainers.image.ref.name' = $spec.Target
                'org.opencontainers.image.version' = $Version
            }
            $index = [ordered]@{
                schemaVersion = 2
                mediaType = 'application/vnd.oci.image.index.v1+json'
                manifests = @([ordered]@{
                    mediaType = if ($manifest.mediaType) { [string]$manifest.mediaType } else { 'application/vnd.docker.distribution.manifest.v2+json' }
                    digest = $manifestDigest
                    size = [int64](Get-Item -LiteralPath $manifestPath).Length
                    annotations = $refName
                })
            }
            Write-Utf8Json (Join-Path $ociRoot 'index.json') $index
            Write-Utf8Json (Join-Path $ociRoot 'oci-layout') ([ordered]@{ imageLayoutVersion = '1.0.0' })

            $tarPath = Join-Path $stagingRoot "$label.tar"
            $archiveName = "$label.tar.gz"
            $archivePath = Join-Path $stagedImageRoot $archiveName
            & tar.exe -cf $tarPath -C $ociRoot .
            if ($LASTEXITCODE -ne 0) { Fail "Could not create OCI archive for $label." }
            $listing = @(tar.exe -tf $tarPath 2>$null)
            if ($listing.Count -lt 10 -or -not ($listing -match 'blobs/sha256/')) {
                Fail "OCI archive validation failed for $label; layer blobs are missing."
            }
            Compress-GzipFile $tarPath $archivePath
            $archiveBytes = (Get-Item -LiteralPath $archivePath).Length
            if ($archiveBytes -lt 1048576) { Fail "OCI archive for $label is unexpectedly small ($archiveBytes bytes)." }
            $layerIds = @($manifest.layers | ForEach-Object { ([string]$_.digest).ToLowerInvariant() })
            $entries += [ordered]@{
                name = $label
                image = $spec.Target
                source_image = "$($SourceRegistry.TrimEnd('/'))/$($spec.Repository):$tag"
                source_image_id = $manifestDigest
                source_image_digest = $manifestDigest
                rootfs_layer_ids = $layerIds
                format = 'docker-oci'
                preserves_registry_layers = $true
                runtime_changes = @()
                archive = $archiveName
                sha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
                archive_bytes = [int64]$archiveBytes
                expanded_bytes = [int64](Get-Item -LiteralPath $tarPath).Length
            }
            Write-Host "[xianyu] $label archive created: $archiveBytes bytes" -ForegroundColor Green
        } finally {
            if (Test-Path -LiteralPath $stagingRoot) { Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue }
        }
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
        application_images_only = $true
        infrastructure_included = $false
        images = @($entries)
    }
    $stagedManifestPath = Join-Path $stagedImageRoot 'offline-manifest.json'
    Write-Utf8Json $stagedManifestPath $manifest
    $currentArchives = @($entries | ForEach-Object { [string]$_.archive })
    $commitBackup = Join-Path $workRoot 'commit-backup'
    New-Item -ItemType Directory -Path $commitBackup -Force | Out-Null
    $commitFiles = @($currentArchives + 'offline-manifest.json')
    $committed = New-Object System.Collections.Generic.List[object]
    try {
        foreach ($name in $commitFiles) {
            $destination = Join-Path $imageRoot $name
            $backup = Join-Path $commitBackup $name
            if (Test-Path -LiteralPath $destination) {
                Move-Item -LiteralPath $destination -Destination $backup -Force
            }
            [void]$committed.Add([pscustomobject]@{ Destination = $destination; Backup = $backup })
            Move-Item -LiteralPath (Join-Path $stagedImageRoot $name) -Destination $destination -Force
        }
        Get-ChildItem -LiteralPath $imageRoot -Filter '*.tar.gz' -File -Force -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -match '^(backend|websocket|scheduler|frontend)(-.+)?\.tar\.gz$' -and
                $currentArchives -notcontains $_.Name
            } |
            ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
    } catch {
        foreach ($item in @($committed | Sort-Object -Property Destination -Descending)) {
            if (Test-Path -LiteralPath $item.Destination) { Remove-Item -LiteralPath $item.Destination -Force -ErrorAction SilentlyContinue }
            if (Test-Path -LiteralPath $item.Backup) { Move-Item -LiteralPath $item.Backup -Destination $item.Destination -Force -ErrorAction SilentlyContinue }
        }
        throw
    }
    Write-Host '[xianyu] Old offline image archives removed; only the current release is retained.' -ForegroundColor DarkCyan
    Write-Host "[xianyu] Complete offline bundle created: $imageRoot" -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $workRoot) { Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue }
}
