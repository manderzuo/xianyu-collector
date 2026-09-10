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

function Get-Text([object[]]$Value) {
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
$logDirectory = Join-Path $appRoot 'logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory ('repair-install-' + (Get-Date -Format yyyyMMddHHmmss) + '.log')

function Write-RepairLog([string]$Message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') [repair] $Message"
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
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $map[$Matches[1]] = $Matches[2]
        }
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

function New-Secret([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($buffer).Replace('+','-').Replace('/','_').TrimEnd('=')
}

function Test-PortFree([int]$Port) {
    try {
        $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
        return $null -eq $listener
    } catch {
        $client = New-Object Net.Sockets.TcpClient
        try {
            $task = $client.ConnectAsync('127.0.0.1', $Port)
            if (-not $task.Wait(300)) { return $true }
            return -not $client.Connected
        } catch { return $true } finally { $client.Dispose() }
    }
}

function Get-FreePort([int]$StartPort, [int[]]$UsedPorts) {
    for ($candidate = $StartPort; $candidate -lt 65500; $candidate++) {
        if (($UsedPorts -notcontains $candidate) -and (Test-PortFree $candidate)) { return $candidate }
    }
    Fail "No free port was found after $StartPort."
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
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    foreach ($line in $output) {
        if (-not [string]::IsNullOrWhiteSpace([string]$line)) { Write-RepairLog "docker_output label=$Label text=$line" }
    }
    Write-RepairLog "docker_end label=$Label exit_code=$exitCode"
    return [pscustomobject]@{ ExitCode = $exitCode; Output = (Get-Text $output) }
}

function Get-DockerProbe {
    try {
        $output = @(& docker info 2>&1)
        $exitCode = $LASTEXITCODE
        return [pscustomobject]@{ Ready = ($exitCode -eq 0); ExitCode = $exitCode; Output = (Get-Text $output) }
    } catch {
        return [pscustomobject]@{ Ready = $false; ExitCode = 1; Output = $_.Exception.Message }
    }
}

function Wait-DockerReady {
    for ($attempt = 1; $attempt -le 45; $attempt++) {
        $probe = Get-DockerProbe
        if ($probe.Ready) {
            Write-RepairLog 'Docker engine is ready.'
            return $true
        }
        if (($attempt % 5) -eq 0) {
            Write-RepairLog "Waiting for Docker engine attempt=$attempt/45 exit_code=$($probe.ExitCode) reason=$($probe.Output)"
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Test-OfflineImages([object]$Manifest) {
    $missing = New-Object System.Collections.Generic.List[string]
    foreach ($entry in @($Manifest.images)) {
        $image = [string]$entry.image
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = @(& docker image inspect $image --format '{{.Id}}' 2>&1)
            $exitCode = $LASTEXITCODE
        } catch {
            $output = @($_)
            $exitCode = 1
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        $actualId = (Get-Text $output)
        $expectedId = ([string]$entry.source_image_id).Trim()
        if ($exitCode -ne 0 -or [string]::IsNullOrWhiteSpace($actualId)) {
            [void]$missing.Add("$($entry.name): image is missing ($image)")
            continue
        }
        if ($expectedId -and $actualId -ne $expectedId) {
            [void]$missing.Add("$($entry.name): image identity mismatch ($image)")
        }
    }
    return [pscustomobject]@{ Ok = $missing.Count -eq 0; Missing = @($missing) }
}

function Ensure-Environment([object]$Manifest) {
    $newEnv = -not (Test-Path -LiteralPath $envFile)
    if ($newEnv) {
        if (-not (Test-Path -LiteralPath $envExample)) { Fail 'app\.env.example is missing.' }
        Copy-Item -LiteralPath $envExample -Destination $envFile -Force
        Write-RepairLog 'Created app\.env from .env.example.'
    }

    $envMap = Get-EnvMap $envFile
    foreach ($item in @(
        @{ Name = 'MYSQL_ROOT_PASSWORD'; Bytes = 32 },
        @{ Name = 'MYSQL_PASSWORD'; Bytes = 32 },
        @{ Name = 'REDIS_PASSWORD'; Bytes = 32 },
        @{ Name = 'JWT_SECRET'; Bytes = 48 }
    )) {
        $value = "$($envMap[$item.Name])"
        if ([string]::IsNullOrWhiteSpace($value) -or $value -match '^replace-with-') {
            Set-EnvValue $envFile $item.Name (New-Secret $item.Bytes)
        }
    }

    $manifestTag = "$($Manifest.image_tag)".Trim()
    if ([string]::IsNullOrWhiteSpace($manifestTag)) { Fail 'Offline manifest does not contain image_tag.' }
    Set-EnvValue $envFile 'XR_DEPLOY_MODE' 'offline'
    Set-EnvValue $envFile 'XR_IMAGE_REGISTRY' 'local'
    Set-EnvValue $envFile 'XR_IMAGE_NAMESPACE' 'xianyu'
    Set-EnvValue $envFile 'XR_IMAGE_TAG' $manifestTag
    $envMap = Get-EnvMap $envFile
    if ([string]::IsNullOrWhiteSpace("$($envMap['XIANYU_CLOUD_AUTH_URL'])")) {
        Set-EnvValue $envFile 'XIANYU_CLOUD_AUTH_URL' 'https://www.gemstory.cn'
    }

    $folderName = Split-Path -Leaf $resolvedPackage
    $safeName = ($folderName.ToLower() -replace '[^a-z0-9]+', '-') -replace '(^-+|-+$)', ''
    if ([string]::IsNullOrWhiteSpace($safeName)) { $safeName = 'xianyu-installer' }
    if ($safeName.Length -gt 24) { $safeName = $safeName.Substring(0, 24) }
    $envMap = Get-EnvMap $envFile
    foreach ($item in @(
        @{ Name = 'XR_CONTAINER_PREFIX'; Value = $safeName },
        @{ Name = 'XR_NETWORK_NAME'; Value = "$safeName-net" },
        @{ Name = 'XR_VOLUME_PREFIX'; Value = $safeName },
        @{ Name = 'COMPOSE_PROJECT_NAME'; Value = $safeName }
    )) {
        $current = "$($envMap[$item.Name])".Trim()
        if ([string]::IsNullOrWhiteSpace($current) -or $current -in @('xianyu-app', 'xianyu-app-net')) {
            Set-EnvValue $envFile $item.Name $item.Value
        }
    }

    $envMap = Get-EnvMap $envFile
    $containerPrefix = "$($envMap['XR_CONTAINER_PREFIX'])".Trim()
    $ownedContainers = @(docker ps -a --format '{{.Names}}' 2>$null | Where-Object { $_ -like "$containerPrefix-*" })
    $usedPorts = @()
    foreach ($item in @(
        @{ Name = 'FRONTEND_PORT'; Base = 20000 },
        @{ Name = 'BACKEND_WEB_PORT'; Base = 28089 },
        @{ Name = 'WEBSOCKET_PORT'; Base = 28090 },
        @{ Name = 'SCHEDULER_PORT'; Base = 28091 }
    )) {
        $raw = "$($envMap[$item.Name])".Trim()
        $port = 0
        if ($raw -match '^\d+$') { $port = [int]$raw }
        if ($port -lt 1024 -or $port -gt 65500 -or (($ownedContainers.Count -eq 0) -and -not (Test-PortFree $port)) -or ($usedPorts -contains $port)) {
            $port = Get-FreePort $item.Base $usedPorts
            Set-EnvValue $envFile $item.Name ([string]$port)
        }
        $usedPorts += $port
    }
    $envMap = Get-EnvMap $envFile
    if ($newEnv -or "$($envMap['CORS_ORIGINS'])" -match '20000') {
        Set-EnvValue $envFile 'CORS_ORIGINS' "http://127.0.0.1:$($envMap['FRONTEND_PORT']),http://localhost:$($envMap['FRONTEND_PORT'])"
    }
    return Get-EnvMap $envFile
}

function Create-DesktopShortcut([string]$FrontendUrl) {
    $desktop = [Environment]::GetFolderPath('Desktop')
    if ([string]::IsNullOrWhiteSpace($desktop) -or -not (Test-Path -LiteralPath $desktop)) { return }
    $title = -join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))
    $shortcutPath = Join-Path $desktop "$title.lnk"
    $launcherCandidates = @(
        (Join-Path $resolvedPackage "$title.exe"),
        (Join-Path $resolvedPackage 'xianyu-launcher.exe')
    )
    $launcherPath = $launcherCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $launcherPath) { Write-RepairLog 'Launcher executable was not found; shortcut creation skipped.'; return }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $launcherPath
        $shortcut.WorkingDirectory = $resolvedPackage
        $shortcut.Description = $title
        $iconPath = Join-Path $resolvedPackage 'xianyu-launcher.ico'
        if (-not (Test-Path -LiteralPath $iconPath)) { $iconPath = Join-Path $resolvedPackage 'assets\xianyu-launcher.ico' }
        if (Test-Path -LiteralPath $iconPath) { $shortcut.IconLocation = "$iconPath,0" }
        $shortcut.Save()
        Write-RepairLog "Desktop shortcut repaired: $shortcutPath"
    } catch { Write-RepairLog "Desktop shortcut repair skipped: $($_.Exception.Message)" }
}

try {
    Write-RepairLog "repair_start package_root=$resolvedPackage force_reimport=$ForceReimport"
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker CLI was not found.' }
    if (-not (Wait-DockerReady)) { Fail 'Docker engine is not ready. Open Docker Desktop, resolve its displayed error, and run this repair script again.' }

    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.format_version -notin @(1, 2)) { Fail "Unsupported offline manifest version: $($manifest.format_version)" }
    $imageCheck = Test-OfflineImages $manifest
    if ($ForceReimport -or -not $imageCheck.Ok) {
        if (-not (Test-Path -LiteralPath $importerPath)) { Fail 'Offline image importer is missing from the package.' }
        if (-not $imageCheck.Ok) { Write-RepairLog ('Images needing repair: ' + ($imageCheck.Missing -join '; ')) }
        Write-RepairLog 'Importing the offline image bundle. This is only needed when an image is missing or -ForceReimport is used.'
        & $importerPath -PackageRoot $resolvedPackage
        $importSucceeded = $?
        if (-not $importSucceeded) { Fail 'Offline image importer reported a failure.' }
        $imageCheck = Test-OfflineImages $manifest
        if (-not $imageCheck.Ok) { Fail ('Images are still unavailable: ' + ($imageCheck.Missing -join '; ')) }
    } else {
        Write-RepairLog 'All offline images are already present; image import skipped.'
    }

    $envMap = Ensure-Environment $manifest
    $syncScript = Join-Path $appRoot 'deploy\sync-xianyu-db-credentials.ps1'
    if (Test-Path -LiteralPath $syncScript) {
        try { & $syncScript -ProjectRoot $appRoot } catch { Write-RepairLog "Database credential sync skipped: $($_.Exception.Message)" }
    }
    $protocolScript = Join-Path $appRoot 'deploy\register-xianyu-update-protocol.ps1'
    if (Test-Path -LiteralPath $protocolScript) {
        try { & $protocolScript -ProjectRoot $appRoot } catch { Write-RepairLog "Update protocol registration skipped: $($_.Exception.Message)" }
    }

    $composeArgs = @('compose', '--project-directory', $appRoot, '--env-file', $envFile, '-f', $composeFile, 'config')
    $configResult = Invoke-Docker $composeArgs 'validate_compose'
    if ($configResult.ExitCode -ne 0) { Fail "Docker Compose configuration is invalid: $($configResult.Output)" }

    $upArgs = @('compose', '--project-directory', $appRoot, '--env-file', $envFile, '-f', $composeFile, 'up', '-d', '--no-build', '--pull', 'never')
    $upResult = Invoke-Docker $upArgs 'start_offline_services'
    if ($upResult.ExitCode -ne 0) { Fail "Docker Compose could not start the offline services: $($upResult.Output)" }

    $frontendUrl = "http://127.0.0.1:$($envMap['FRONTEND_PORT'])"
    $frontendReady = $false
    for ($attempt = 1; $attempt -le 90; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $frontendUrl -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { $frontendReady = $true; break }
        } catch { }
        if (($attempt % 10) -eq 0) { Write-RepairLog "Waiting for frontend attempt=$attempt/90" }
        Start-Sleep -Seconds 2
    }
    if (-not $frontendReady) { Fail "Frontend did not respond at $frontendUrl after 180 seconds." }
    Create-DesktopShortcut $frontendUrl
    Write-RepairLog "repair_completed frontend_url=$frontendUrl"
    Write-Host ''
    Write-Host "Repair completed. Open: $frontendUrl" -ForegroundColor Green
    Write-Host "Repair log: $logPath" -ForegroundColor Green
    try { Start-Process $frontendUrl } catch { }
    exit 0
} catch {
    Write-Host ''
    Write-Host "Repair failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Repair log: $logPath" -ForegroundColor Yellow
    if ($PauseOnError) { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}
