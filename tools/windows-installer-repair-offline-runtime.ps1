param(
    [string]$PackageRoot = '',
    [switch]$ForceReimport,
    [switch]$PauseOnError
)

$ErrorActionPreference = 'Stop'

function Resolve-PackageRoot([string]$RequestedRoot) {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($RequestedRoot)) { $candidates += $RequestedRoot }
    $candidates += (Split-Path -Parent $PSScriptRoot)
    $candidates += (Get-Location).Path
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace([string]$candidate)) { continue }
        try {
            $resolved = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path.TrimEnd('\')
            if ((Test-Path -LiteralPath (Join-Path $resolved 'app\docker-compose.yml')) -and
                (Test-Path -LiteralPath (Join-Path $resolved 'resources\images\offline-manifest.json'))) {
                return $resolved
            }
        } catch { }
    }
    throw 'Could not find a complete Xianyu package. Pass -PackageRoot with the package folder.'
}

function Join-Output([object[]]$Value) {
    return (($Value | ForEach-Object { [string]$_ }) -join ' ').Trim()
}

$resolvedPackage = Resolve-PackageRoot $PackageRoot
$appRoot = Join-Path $resolvedPackage 'app'
$resourcesRoot = Join-Path $resolvedPackage 'resources'
$composeFile = Join-Path $appRoot 'docker-compose.yml'
$envFile = Join-Path $appRoot '.env'
$envExample = Join-Path $appRoot '.env.example'
$manifestPath = Join-Path $resourcesRoot 'images\offline-manifest.json'
$importerPath = Join-Path $resourcesRoot 'import-offline-image-bundle.ps1'
$logRoot = Join-Path $appRoot 'logs'
$logPath = Join-Path $logRoot ('repair-runtime-' + (Get-Date -Format yyyyMMddHHmmss) + '.log')
$offlineStatePath = Join-Path $appRoot 'runtime-offline-state.json'
$pendingMarkerPath = Join-Path $appRoot 'runtime-sync.pending.json'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null

function Write-RepairLog([string]$Message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') [offline-runtime] $Message"
    Write-Host $line
    try { Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue } catch { }
}

function Fail([string]$Message) {
    Write-RepairLog "ERROR $Message"
    throw $Message
}

function Get-EnvMap([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $map[$Matches[1]] = $Matches[2] }
    }
    return $map
}

function Set-EnvValue([string]$Path, [string]$Name, [string]$Value) {
    $lines = if (Test-Path -LiteralPath $Path) { @(Get-Content -LiteralPath $Path) } else { @() }
    $found = $false
    $updated = foreach ($line in $lines) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=") {
            $found = $true
            "$Name=$Value"
        } else { $line }
    }
    if (-not $found) { $updated += "$Name=$Value" }
    Set-Content -LiteralPath $Path -Value $updated -Encoding UTF8
}

function Invoke-Docker([string[]]$Arguments, [string]$Label) {
    Write-RepairLog "docker_start label=$Label args=$($Arguments -join ' ')"
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& docker @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } catch {
        $output = @($_)
        $exitCode = 1
    } finally { $ErrorActionPreference = $previousPreference }
    foreach ($line in $output) {
        if (-not [string]::IsNullOrWhiteSpace([string]$line)) { Write-RepairLog "docker_output label=$Label text=$line" }
    }
    Write-RepairLog "docker_end label=$Label exit_code=$exitCode"
    if ($exitCode -ne 0) { Fail "Docker operation failed: $Label (exit code $exitCode)." }
    return (Join-Output $output)
}

function Get-ImageId([string]$ImageRef) {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& docker image inspect $ImageRef --format '{{.Id}}' 2>$null)
        if ($LASTEXITCODE -eq 0) {
            $first = $output | Select-Object -First 1
            if ($null -ne $first) { return ([string]$first).Trim() }
        }
    } catch { }
    finally { $ErrorActionPreference = $previousPreference }
    return ''
}

function Test-Images([object]$Manifest) {
    $problems = New-Object System.Collections.Generic.List[string]
    foreach ($entry in @($Manifest.images)) {
        $image = ([string]$entry.image).Trim()
        $actual = Get-ImageId $image
        $expected = ([string]$entry.source_image_id).Trim()
        if (-not $actual) { [void]$problems.Add("$($entry.name): missing $image") }
        elseif ($expected -and $actual -ne $expected) { [void]$problems.Add("$($entry.name): image id mismatch") }
    }
    return [pscustomobject]@{ Ok = $problems.Count -eq 0; Problems = @($problems) }
}

function Ensure-OfflineEnvironment([string]$Tag) {
    if (-not (Test-Path -LiteralPath $envFile)) {
        if (-not (Test-Path -LiteralPath $envExample)) { Fail 'app\.env and app\.env.example are both missing.' }
        Copy-Item -LiteralPath $envExample -Destination $envFile -Force
        Write-RepairLog 'Created app\.env from app\.env.example.'
    }
    Set-EnvValue $envFile 'XR_DEPLOY_MODE' 'offline'
    Set-EnvValue $envFile 'XR_IMAGE_REGISTRY' 'local'
    Set-EnvValue $envFile 'XR_IMAGE_NAMESPACE' 'xianyu'
    Set-EnvValue $envFile 'XR_IMAGE_TAG' $Tag
}

function Get-OfflineStateEntry([object]$Entry) {
    $imageRef = ([string]$Entry.image).Trim()
    $sourceRef = ([string]$Entry.source_image).Trim()
    $imageId = Get-ImageId $imageRef
    return [ordered]@{
        service = ([string]$Entry.name).Trim()
        local_image = $imageRef
        registry_image = $sourceRef
        image_id = $imageId
        remote_digest = ([string]$Entry.source_image_digest).Trim().ToLowerInvariant()
        rootfs_layer_ids = @($Entry.rootfs_layer_ids | ForEach-Object { ([string]$_).Trim().ToLowerInvariant() })
    }
}

try {
    Write-RepairLog "repair_start package_root=$resolvedPackage force_reimport=$ForceReimport"
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker CLI was not found.' }
    Invoke-Docker @('info', '--format', '{{.ServerVersion}}') 'check_docker_ready' | Out-Null

    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.format_version -notin @(1, 2)) { Fail "Unsupported offline manifest version: $($manifest.format_version)" }
    $expectedServices = @('backend', 'websocket', 'scheduler', 'frontend')
    $actualServices = @($manifest.images | ForEach-Object { ([string]$_.name).Trim() })
    foreach ($service in $expectedServices) {
        if ($actualServices -notcontains $service) { Fail "Offline manifest is missing application image: $service" }
    }
    Write-RepairLog 'No network request is made by this repair script. The offline manifest is authoritative for this repair.'

    $imageCheck = Test-Images $manifest
    if ($ForceReimport -or -not $imageCheck.Ok) {
        if (-not (Test-Path -LiteralPath $importerPath)) { Fail 'Offline image importer is missing from the package.' }
        if (-not $imageCheck.Ok) { Write-RepairLog ('images_to_import=' + ($imageCheck.Problems -join '; ')) }
        & $importerPath -PackageRoot $resolvedPackage
        if (-not $?) { Fail 'Offline image importer reported a failure.' }
        $imageCheck = Test-Images $manifest
        if (-not $imageCheck.Ok) { Fail ('Images are still unavailable: ' + ($imageCheck.Problems -join '; ')) }
    } else {
        Write-RepairLog 'All four application images are already present; import skipped.'
    }

    $taggedState = @()
    foreach ($entry in @($manifest.images)) {
        $localImage = ([string]$entry.image).Trim()
        $registryImage = ([string]$entry.source_image).Trim()
        if ($registryImage -and $registryImage -ne $localImage -and $registryImage -notmatch '@') {
            Invoke-Docker @('tag', $localImage, $registryImage) "tag_$($entry.name)_for_future_incremental_updates" | Out-Null
        }
        $taggedState += Get-OfflineStateEntry $entry
    }

    $tag = ([string]$manifest.image_tag).Trim()
    if (-not $tag) { Fail 'Offline manifest does not contain image_tag.' }
    Ensure-OfflineEnvironment $tag
    if (Test-Path -LiteralPath $pendingMarkerPath) {
        Remove-Item -LiteralPath $pendingMarkerPath -Force
        Write-RepairLog 'Removed runtime-sync.pending.json; no immediate online image validation will run.'
    }

    $state = [ordered]@{
        protocol = 1
        source = 'offline-bundle'
        version = ([string]$manifest.version).Trim()
        build_id = if (Test-Path -LiteralPath (Join-Path $appRoot 'BUILD_ID.txt')) { (Get-Content -LiteralPath (Join-Path $appRoot 'BUILD_ID.txt') -Raw).Trim() } else { '' }
        image_tag = $tag
        created_at = [DateTime]::UtcNow.ToString('o')
        remote_validation = 'deferred-until-a-new-release'
        images = @($taggedState)
    }
    $state | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $offlineStatePath -Encoding UTF8
    Write-RepairLog "offline_state_written path=$offlineStatePath"

    $composeArgs = @('compose', '--project-directory', $appRoot, '--env-file', $envFile, '-f', $composeFile, 'up', '-d', '--no-build', '--pull', 'never')
    Invoke-Docker $composeArgs 'start_offline_services' | Out-Null
    Write-RepairLog "repair_completed images=$(@($manifest.images).Count) network_validation=skipped"
    Write-Host ''
    Write-Host 'Offline runtime repair completed. No image pull or remote version validation was performed.' -ForegroundColor Green
    Write-Host "Repair log: $logPath" -ForegroundColor Green
    exit 0
} catch {
    Write-Host ''
    Write-Host "Offline runtime repair failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Repair log: $logPath" -ForegroundColor Yellow
    if ($PauseOnError) { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}
