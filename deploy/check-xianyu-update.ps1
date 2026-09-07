param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'
$EnvFile = Join-Path $ProjectRoot '.env'
$VersionFile = Join-Path $ProjectRoot 'VERSION.txt'
$BuildFile = Join-Path $ProjectRoot 'BUILD_ID.txt'
$LogDir = Join-Path $ProjectRoot 'logs'
$LogFile = Join-Path $LogDir 'update.log'
$envBackup = ''

function Test-Command([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Write-UpdateLog([string]$Message) {
    try {
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
        Add-Content -LiteralPath $LogFile -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $Message) -Encoding UTF8
    } catch { }
}

function Get-EnvMap([string]$Path) {
    $map = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { $map[$Matches[1]] = $Matches[2] }
    }
    return $map
}

function Set-EnvValue([string]$Path, [string]$Name, [string]$Value) {
    $lines = @(Get-Content -LiteralPath $Path)
    $found = $false
    $newLines = foreach ($line in $lines) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=") {
            $found = $true
            "$Name=$Value"
        } else { $line }
    }
    if (-not $found) { $newLines += "$Name=$Value" }
    Set-Content -LiteralPath $Path -Value $newLines -Encoding utf8
}

function Read-Text([string]$Path, [string]$Fallback) {
    if (Test-Path -LiteralPath $Path) {
        $value = (Get-Content -LiteralPath $Path -Raw).Trim()
        if ($value) { return $value }
    }
    return $Fallback
}

function Get-VersionParts([string]$Value) {
    $matches = [regex]::Matches([string]$Value, '\d+')
    if ($matches.Count -eq 0) { throw "Invalid version: $Value" }
    return @($matches | ForEach-Object { [int]$_.Value })
}

function Compare-Version([string]$Left, [string]$Right) {
    $a = @(Get-VersionParts $Left)
    $b = @(Get-VersionParts $Right)
    $width = [Math]::Max($a.Count, $b.Count)
    for ($i = 0; $i -lt $width; $i++) {
        $av = if ($i -lt $a.Count) { $a[$i] } else { 0 }
        $bv = if ($i -lt $b.Count) { $b[$i] } else { 0 }
        if ($av -gt $bv) { return 1 }
        if ($av -lt $bv) { return -1 }
    }
    return 0
}

function Show-UpdatePrompt([string]$Current, [string]$Latest, [string]$Notes) {
    $message = "New Xianyu version is available.`n`nCurrent: $Current`nLatest: $Latest"
    if ($Notes) { $message += "`n`n$Notes" }
    try {
        $shell = New-Object -ComObject WScript.Shell
        return $shell.Popup($message, 0, 'Xianyu Update', 4 + 32) -eq 6
    } catch {
        return $false
    }
}

function Test-Frontend([string]$Port) {
    if ($Port -notmatch '^\d+$') { return $true }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port" -TimeoutSec 5
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch { return $false }
}

try {
    if (-not (Test-Path $EnvFile)) { return }
    $envMap = Get-EnvMap $EnvFile
    if (-not $Force -and "$($envMap['UPDATE_CHECK_ON_START'])".ToLowerInvariant() -eq 'false') { return }
    $manifestUrl = "$($envMap['UPDATE_MANIFEST_URL'])".Trim()
    if (-not $manifestUrl) { return }
    if (-not (Test-Command docker)) { return }

    $headers = @{ 'Cache-Control' = 'no-cache'; 'Accept' = 'application/json' }
    $manifestToken = "$($envMap['UPDATE_MANIFEST_TOKEN'])".Trim()
    if ($manifestToken) { $headers['Authorization'] = "Bearer $manifestToken" }
    $requestUrl = $manifestUrl + ($(if ($manifestUrl.Contains('?')) { '&' } else { '?' })) + '_client_check=' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    Write-UpdateLog "manifest_request url=$manifestUrl"
    $manifest = (Invoke-WebRequest -UseBasicParsing -Uri $requestUrl -TimeoutSec 15 -Headers $headers).Content | ConvertFrom-Json
    $latestVersion = "$($manifest.version)".Trim()
    $latestBuild = "$($manifest.build_id)".Trim()
    $registry = "$($manifest.image_registry)".Trim()
    $namespace = "$($manifest.image_namespace)".Trim()
    $tag = "$($manifest.image_tag)".Trim()
    if (-not $latestVersion -or -not $registry -or -not $namespace -or -not $tag) { throw 'Manifest is incomplete.' }
    [void](Get-VersionParts $latestVersion)

    $currentVersion = Read-Text $VersionFile '0.0.0'
    $currentBuild = Read-Text $BuildFile ''
    $versionResult = Compare-Version $latestVersion $currentVersion
    $available = $versionResult -gt 0 -or ($versionResult -eq 0 -and $latestBuild -and $latestBuild -ne $currentBuild)
    Write-UpdateLog "compare current=$currentVersion/$currentBuild latest=$latestVersion/$latestBuild available=$available"
    if (-not $available) { return }

    $notes = "$($manifest.notes)".Trim()
    if (-not $Force -and -not (Show-UpdatePrompt $currentVersion $latestVersion $notes)) {
        Write-UpdateLog 'update_declined'
        return
    }

    $envBackup = Join-Path $LogDir ("env-before-update-" + (Get-Date -Format 'yyyyMMddHHmmss') + '.bak')
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    Copy-Item -LiteralPath $EnvFile -Destination $envBackup -Force
    Set-EnvValue $EnvFile 'XR_DEPLOY_MODE' 'remote'
    Set-EnvValue $EnvFile 'XR_IMAGE_REGISTRY' $registry
    Set-EnvValue $EnvFile 'XR_IMAGE_NAMESPACE' $namespace
    Set-EnvValue $EnvFile 'XR_IMAGE_TAG' $tag

    & (Join-Path $PSScriptRoot 'sync-xianyu-db-credentials.ps1') -ProjectRoot $ProjectRoot

    Write-UpdateLog 'pull_start'
    docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile pull
    if ($LASTEXITCODE -ne 0) { throw 'Image pull failed. Check registry login and network.' }
    docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile up -d --no-build
    if ($LASTEXITCODE -ne 0) { throw 'Service restart failed.' }

    $frontendPort = "$($envMap['FRONTEND_PORT'])"
    if (-not (Test-Frontend $frontendPort)) { throw 'Frontend health check failed after update.' }
    Set-Content -LiteralPath $VersionFile -Value $latestVersion -Encoding utf8
    if ($latestBuild) { Set-Content -LiteralPath $BuildFile -Value $latestBuild -Encoding utf8 }
    Write-UpdateLog "update_completed version=$latestVersion build=$latestBuild"
} catch {
    Write-UpdateLog "update_failed error=$($_.Exception.Message)"
    if (Test-Path $envBackup) {
        Copy-Item -LiteralPath $envBackup -Destination $EnvFile -Force
        try { docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile up -d --no-build | Out-Null } catch { }
    }
}
