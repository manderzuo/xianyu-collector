param(
    [Parameter(Mandatory = $true)]
    [string]$PackageRoot,
    [string]$TempDirectory = ''
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

function Wait-ForImage([string]$Image, [string]$Name) {
    # Docker Desktop may finish unpacking a large OCI archive before its tag
    # becomes visible to a following inspect call. Treat that short interval
    # as a transient state instead of failing a valid offline installation.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        for ($attempt = 1; $attempt -le 30; $attempt++) {
            & docker image inspect $Image 1>$null 2>$null
            if ($LASTEXITCODE -eq 0) { return }
            Start-Sleep -Milliseconds 500
        }
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    Fail "Expected image is missing after import: $Image ($Name)"
}

$resolvedPackage = (Resolve-Path -LiteralPath $PackageRoot).Path.TrimEnd('\')
$imageRoot = Join-Path $resolvedPackage 'resources\images'
$manifestPath = Join-Path $imageRoot 'offline-manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) { Fail "Offline manifest not found: $manifestPath" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker CLI was not found.' }

$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if ($manifest.format_version -notin @(1, 2)) { Fail "Unsupported offline manifest version: $($manifest.format_version)" }
$tempBase = if ([string]::IsNullOrWhiteSpace($TempDirectory)) {
    Join-Path $resolvedPackage 'app\updates\work'
} else {
    [System.IO.Path]::GetFullPath($TempDirectory)
}
New-Item -ItemType Directory -Path $tempBase -Force | Out-Null
$tempRoot = Join-Path $tempBase ('xianyu-image-import-' + [guid]::NewGuid().ToString('N'))
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
        $expectedImageId = "$($entry.source_image_id)".Trim()
        $imageReferenceVisible = $false
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & docker image inspect ([string]$entry.image) 1>$null 2>$null
            $imageReferenceVisible = ($LASTEXITCODE -eq 0)
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        if (-not $imageReferenceVisible -and "$($entry.format)" -ne 'rootfs' -and $expectedImageId) {
            # Some Docker Desktop/containerd versions report the loaded image
            # in `docker image ls` before the archive's annotated tag is
            # addressable by `docker image inspect`. Re-attach the expected
            # immutable image ID to the target tag before declaring failure.
            Write-Host "[xianyu] Re-attaching imported image ID for $($entry.name)" -ForegroundColor DarkCyan
            & docker tag $expectedImageId ([string]$entry.image)
            if ($LASTEXITCODE -ne 0) { Fail "Could not attach imported image ID for $($entry.name)." }
        }
        Wait-ForImage ([string]$entry.image) ([string]$entry.name)
        if ($expectedImageId) {
            $actualImageId = (& docker image inspect ([string]$entry.image) --format '{{.Id}}' | Select-Object -First 1).ToString().Trim()
            if ($actualImageId -ne $expectedImageId) {
                Fail "Image identity changed during import for $($entry.name); incremental updates would not be reusable"
            }
        }
        $sourceImage = "$($entry.source_image)".Trim()
        if ($sourceImage -and $sourceImage -ne ([string]$entry.image).Trim() -and $sourceImage -notmatch '@') {
            & docker tag ([string]$entry.image) $sourceImage
            if ($LASTEXITCODE -ne 0) { Fail "Could not create registry-compatible tag for $($entry.name): $sourceImage" }
        }
        Remove-Item -LiteralPath $tarPath -Force
    }
    Write-Host '[xianyu] All offline images imported successfully.' -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue }
}

# Make the success status explicit for callers that invoke this script with
# the PowerShell call operator and inspect LASTEXITCODE afterwards.
exit 0
