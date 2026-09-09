param([string]$InstallPath = '')

$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$sourcePackageRoot = [IO.Path]::GetFullPath($PackageRoot).TrimEnd('\')

function Copy-PackageContents([string]$Source, [string]$Destination) {
    foreach ($entry in Get-ChildItem -LiteralPath $Source -Force) {
        if ($entry.Name -in @('.env', '.env.local', 'logs', 'backups', 'browser_data')) { continue }
        $target = Join-Path $Destination $entry.Name
        if ($entry.PSIsContainer) {
            New-Item -ItemType Directory -Path $target -Force | Out-Null
            Copy-PackageContents -Source $entry.FullName -Destination $target
        } else {
            Copy-Item -LiteralPath $entry.FullName -Destination $target -Force
        }
    }
}

if (-not [string]::IsNullOrWhiteSpace($InstallPath)) {
    $targetPackageRoot = [IO.Path]::GetFullPath($InstallPath).TrimEnd('\')
    if ($targetPackageRoot -ne $sourcePackageRoot) {
        if ($targetPackageRoot.StartsWith($sourcePackageRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or $sourcePackageRoot.StartsWith($targetPackageRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'The selected installation folder cannot contain or be contained by the current package folder.'
        }
        New-Item -ItemType Directory -Path $targetPackageRoot -Force | Out-Null
        Copy-PackageContents -Source $sourcePackageRoot -Destination $targetPackageRoot
        $PackageRoot = $targetPackageRoot
    }
}
$AppRoot = Join-Path $PackageRoot 'app'
$ComposeFile = Join-Path $AppRoot 'docker-compose.yml'
$EnvExample = Join-Path $AppRoot '.env.example'
$EnvFile = Join-Path $AppRoot '.env'
$DockerBootstrap = Join-Path $PackageRoot 'resources\docker-bootstrap.ps1'
$WslBootstrap = Join-Path $PackageRoot 'resources\prepare-wsl.ps1'
$RdpCleanup = Join-Path $PackageRoot 'resources\cleanup-rdp.ps1'
$OfflineImageImporter = Join-Path $PackageRoot 'resources\import-offline-image-bundle.ps1'
$OfflineImageManifest = Join-Path $PackageRoot 'resources\images\offline-manifest.json'
$DbCredentialSync = Join-Path $AppRoot 'deploy\sync-xianyu-db-credentials.ps1'
$ProtocolRegistrar = Join-Path $AppRoot 'deploy\register-xianyu-update-protocol.ps1'
$ErrorHelper = Join-Path $AppRoot 'deploy\windows-error-reporting.ps1'
if (Test-Path -LiteralPath $ErrorHelper) { . $ErrorHelper }
$LogPath = if (Get-Command Start-XianyuLogSession -ErrorAction SilentlyContinue) { Start-XianyuLogSession -ProjectRoot $AppRoot -Name 'install' } else { '' }

trap {
    if (Get-Command Complete-XianyuFailure -ErrorAction SilentlyContinue) {
        Complete-XianyuFailure -Context 'Installation failed. The window will remain open until you close it.' -ErrorRecord $_ -LogPath $LogPath
    }
    exit 1
}

function Fail([string]$Message) {
    throw $Message
}

function Get-EnvMap([string]$Path) {
    $map = @{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { $map[$Matches[1]] = $Matches[2] }
        }
    }
    return $map
}

function Set-EnvValue([string]$Path, [string]$Name, [string]$Value) {
    $lines = if (Test-Path -LiteralPath $Path) { @(Get-Content -LiteralPath $Path) } else { @() }
    $found = $false
    $newLines = foreach ($line in $lines) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=") {
            $found = $true
            "$Name=$Value"
        } else { $line }
    }
    if (-not $found) { $newLines += "$Name=$Value" }
    Set-Content -LiteralPath $Path -Value $newLines -Encoding UTF8
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
    $candidate = $StartPort
    while ($candidate -lt 65500) {
        if (($UsedPorts -notcontains $candidate) -and (Test-PortFree $candidate)) { return $candidate }
        $candidate++
    }
    throw 'No free port was found.'
}

if (-not (Test-Path -LiteralPath $AppRoot)) { Fail 'The package is incomplete: app directory is missing.' }
if (-not (Test-Path -LiteralPath $ComposeFile)) { Fail 'The package is incomplete: docker-compose.yml is missing.' }
if (-not (Test-Path -LiteralPath $EnvExample)) { Fail 'The package is incomplete: .env.example is missing.' }

if (Test-Path -LiteralPath $WslBootstrap) {
    powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $WslBootstrap -NonInteractive -TargetUserProfile $env:USERPROFILE
    $wslExitCode = $LASTEXITCODE
    if ($wslExitCode -eq 3010) {
        Write-Host '[xianyu] WSL setup needs a Windows restart before installation can continue.' -ForegroundColor Yellow
        exit 3010
    }
    if ($wslExitCode -ne 0) { throw "WSL preparation failed with exit code $wslExitCode." }
}
if (Test-Path -LiteralPath $RdpCleanup) {
    try { & $RdpCleanup -NonInteractive } catch { Write-Host "[xianyu] RDP residue cleanup skipped: $($_.Exception.Message)" -ForegroundColor Yellow }
}
try { & $DockerBootstrap -Install -NonInteractive } catch { Fail $_.Exception.Message }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker CLI is unavailable. Start Docker Desktop and run install.bat again.' }
$composeReady = $false
$composeLastError = ''
try {
    $composeOutput = @(& docker compose version 2>&1)
    $composeExitCode = $LASTEXITCODE
    $composeLastError = (($composeOutput | ForEach-Object { [string]$_ }) -join ' ').Trim()
    $composeReady = $composeExitCode -eq 0
} catch {
    $composeLastError = $_.Exception.Message
}
if (-not $composeReady) {
    if (-not $composeLastError) { $composeLastError = 'Docker Compose returned no diagnostic text.' }
    if ($composeLastError.Length -gt 500) { $composeLastError = $composeLastError.Substring(0, 500) + '...' }
    Fail "Docker Compose is unavailable: $composeLastError"
}
$dockerReady = $false
$dockerLastError = ''
for ($dockerAttempt = 1; $dockerAttempt -le 30; $dockerAttempt++) {
    try {
        $dockerInfoOutput = @(& docker info 2>&1)
        $dockerExitCode = $LASTEXITCODE
        $dockerLastError = (($dockerInfoOutput | ForEach-Object { [string]$_ }) -join ' ').Trim()
        if ($dockerExitCode -eq 0) {
            $dockerReady = $true
            break
        }
    } catch {
        $dockerLastError = $_.Exception.Message
    }
    if ($dockerAttempt -lt 30) { Start-Sleep -Seconds 2 }
}
if (-not $dockerReady) {
    if (-not $dockerLastError) { $dockerLastError = 'Docker returned no diagnostic text.' }
    if ($dockerLastError.Length -gt 500) { $dockerLastError = $dockerLastError.Substring(0, 500) + '...' }
    Fail "Docker Desktop engine is not ready after 60 seconds: $dockerLastError"
}

$newEnv = -not (Test-Path -LiteralPath $EnvFile)
if ($newEnv) {
    Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
    Set-EnvValue $EnvFile 'MYSQL_ROOT_PASSWORD' (New-Secret)
    Set-EnvValue $EnvFile 'MYSQL_PASSWORD' (New-Secret)
    Set-EnvValue $EnvFile 'REDIS_PASSWORD' (New-Secret)
    Set-EnvValue $EnvFile 'JWT_SECRET' (New-Secret 48)
}

$envMap = Get-EnvMap $EnvFile
if (-not $envMap.ContainsKey('XIANYU_CLOUD_AUTH_URL') -or [string]::IsNullOrWhiteSpace($envMap['XIANYU_CLOUD_AUTH_URL'])) {
    Set-EnvValue $EnvFile 'XIANYU_CLOUD_AUTH_URL' 'https://www.gemstory.cn'
}

# A package that carries the release public key must fail closed on unsigned
# manifests. Existing installations without the key retain legacy compatibility.
$publicKeyPath = Join-Path $AppRoot 'deploy\update-signing-public-key.xml'
if (Test-Path -LiteralPath $publicKeyPath) {
    Set-EnvValue $EnvFile 'UPDATE_REQUIRE_SIGNATURE' 'true'
    if (-not $envMap.ContainsKey('UPDATE_MANIFEST_PUBLIC_KEY_PATH') -or [string]::IsNullOrWhiteSpace($envMap['UPDATE_MANIFEST_PUBLIC_KEY_PATH'])) {
        Set-EnvValue $EnvFile 'UPDATE_MANIFEST_PUBLIC_KEY_PATH' 'deploy/update-signing-public-key.xml'
    }
    if (-not $envMap.ContainsKey('UPDATE_MANIFEST_SIGNATURE_URL') -or [string]::IsNullOrWhiteSpace($envMap['UPDATE_MANIFEST_SIGNATURE_URL'])) {
        Set-EnvValue $EnvFile 'UPDATE_MANIFEST_SIGNATURE_URL' 'https://www.gemstory.cn/release/xianyu/latest.json.sig'
    }
}

# A full offline package carries the exact image set used by this release.
# Import it before Compose starts so a first install never needs Docker Hub.
$envMap = Get-EnvMap $EnvFile
$deployMode = "$($envMap['XR_DEPLOY_MODE'])".Trim().ToLowerInvariant()
$offlinePackageAvailable = Test-Path -LiteralPath $OfflineImageManifest
$useOfflineImages = $offlinePackageAvailable -and ($newEnv -or $deployMode -in @('', 'local', 'offline'))
if ($useOfflineImages) {
    if (-not (Test-Path -LiteralPath $OfflineImageImporter)) {
        Fail 'Offline image manifest exists but the image importer is missing from the package.'
    }
    Write-Host '[xianyu] Offline image bundle detected. Docker image downloads will be skipped.' -ForegroundColor Cyan
    & $OfflineImageImporter -PackageRoot $PackageRoot
    $offlineImportSucceeded = $?
    $offlineImportExitCode = $LASTEXITCODE
    if (-not $offlineImportSucceeded) {
        if ($null -eq $offlineImportExitCode) { $offlineImportExitCode = 'unknown' }
        Fail "Offline image import failed with exit code $offlineImportExitCode."
    }
    $offlineManifest = Get-Content -LiteralPath $OfflineImageManifest -Raw | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace("$($offlineManifest.image_tag)")) {
        Fail 'Offline image manifest does not contain image_tag.'
    }
    Set-EnvValue $EnvFile 'XR_DEPLOY_MODE' 'offline'
    Set-EnvValue $EnvFile 'XR_IMAGE_REGISTRY' 'local'
    Set-EnvValue $EnvFile 'XR_IMAGE_NAMESPACE' 'xianyu'
    Set-EnvValue $EnvFile 'XR_IMAGE_TAG' "$($offlineManifest.image_tag)"
}
$folderName = Split-Path -Leaf $PackageRoot
$safeName = ($folderName.ToLower() -replace '[^a-z0-9]+', '-') -replace '(^-+|-+$)', ''
if ([string]::IsNullOrWhiteSpace($safeName)) { $safeName = 'xianyu-installer' }
if ($safeName.Length -gt 24) { $safeName = $safeName.Substring(0, 24) }
foreach ($entry in @(
    @{ Name = 'XR_CONTAINER_PREFIX'; Value = $safeName },
    @{ Name = 'XR_NETWORK_NAME'; Value = "$safeName-net" },
    @{ Name = 'XR_VOLUME_PREFIX'; Value = $safeName },
    @{ Name = 'COMPOSE_PROJECT_NAME'; Value = $safeName }
)) {
    $currentValue = "$($envMap[$entry.Name])"
    if ($newEnv -or [string]::IsNullOrWhiteSpace($currentValue) -or $currentValue -in @('xianyu-app', 'xianyu-app-net')) {
        Set-EnvValue $EnvFile $entry.Name $entry.Value
    }
}

$envMap = Get-EnvMap $EnvFile
$containerPrefix = $envMap['XR_CONTAINER_PREFIX']

if (Test-Path -LiteralPath $DbCredentialSync) {
    & $DbCredentialSync -ProjectRoot $AppRoot
}
if (Test-Path -LiteralPath $ProtocolRegistrar) {
    & $ProtocolRegistrar -ProjectRoot $AppRoot
}
$ownedContainers = @(docker ps -a --format '{{.Names}}' | Where-Object { $_ -like "$containerPrefix-*" })
$ports = @()
foreach ($entry in @(
    @{ Name = 'FRONTEND_PORT'; Base = 20000 },
    @{ Name = 'BACKEND_WEB_PORT'; Base = 28089 },
    @{ Name = 'WEBSOCKET_PORT'; Base = 28090 },
    @{ Name = 'SCHEDULER_PORT'; Base = 28091 }
)) {
    $raw = "$($envMap[$entry.Name])"
    $port = 0
    if ($raw -match '^\d+$') { $port = [int]$raw }
    $owned = $ownedContainers.Count -gt 0
    if ($port -lt 1024 -or $port -gt 65500 -or ((-not $owned) -and -not (Test-PortFree $port)) -or ($ports -contains $port)) {
        $port = Get-FreePort $entry.Base $ports
        Set-EnvValue $EnvFile $entry.Name ([string]$port)
    }
    $ports += $port
}

$envMap = Get-EnvMap $EnvFile
Write-XianyuLog -LogPath $LogPath -Message "install_begin package_root=$PackageRoot new_environment=$newEnv"
Write-Host "[xianyu] Package root: $PackageRoot" -ForegroundColor Cyan
Write-Host "[xianyu] Frontend: http://127.0.0.1:$($envMap['FRONTEND_PORT'])" -ForegroundColor Cyan
Write-Host '[xianyu] Old port 19000 is not used.' -ForegroundColor Cyan

docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile config *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose configuration validation failed.' }
$envMap = Get-EnvMap $EnvFile
$deployMode = "$($envMap['XR_DEPLOY_MODE'])".Trim().ToLowerInvariant()
if ($deployMode -eq 'offline') {
    Write-Host '[xianyu] Starting services from locally imported images.' -ForegroundColor Cyan
    docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile up -d --no-build
    if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose could not start the offline image set.' }
} elseif ($deployMode -eq 'remote') {
    if ([string]::IsNullOrWhiteSpace("$($envMap['XR_IMAGE_REGISTRY'])") -or [string]::IsNullOrWhiteSpace("$($envMap['XR_IMAGE_NAMESPACE'])") -or [string]::IsNullOrWhiteSpace("$($envMap['XR_IMAGE_TAG'])")) {
        Fail 'Remote mode requires XR_IMAGE_REGISTRY, XR_IMAGE_NAMESPACE and XR_IMAGE_TAG in app\.env.'
    }
    Write-Host '[xianyu] Pulling remote application images.' -ForegroundColor Cyan
    docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile pull
    if ($LASTEXITCODE -ne 0) { Fail 'Remote image pull failed. Check the image registry address, credentials and network.' }
    docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile up -d --no-build
} else {
    Write-Host '[xianyu] Checking and preloading Docker base images.' -ForegroundColor Cyan
    foreach ($baseImage in @('python:3.11-slim', 'mysql:8.0', 'redis:7-alpine', 'node:20-alpine', 'nginx:alpine')) {
        $pulled = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            Write-Host "[xianyu] Pulling $baseImage (attempt $attempt/3)." -ForegroundColor DarkCyan
            docker pull $baseImage
            if ($LASTEXITCODE -eq 0) { $pulled = $true; break }
            Start-Sleep -Seconds 5
        }
        if (-not $pulled) {
            Fail "Cannot pull $baseImage from Docker Hub. Configure Docker Desktop proxy/network access, or set XR_DEPLOY_MODE=remote with Tencent registry image settings in app\.env."
        }
    }
    Write-Host '[xianyu] Building and starting services. The first run may take several minutes.' -ForegroundColor Cyan
    docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile up -d --build
}
if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose failed to start the application.' }

$frontendUrl = "http://127.0.0.1:$($envMap['FRONTEND_PORT'])"
$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        $response = Invoke-WebRequest -Uri $frontendUrl -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) { Write-Host '[xianyu] WARNING: frontend did not respond within the wait period.' -ForegroundColor Yellow }

$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutTitle = -join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))
$shortcutPath = Join-Path $desktop "$shortcutTitle.lnk"
$launcherPath = Join-Path $PackageRoot "$shortcutTitle.exe"
$oldShortcutPath = Join-Path $desktop 'Xianyu System.lnk'
$iconPath = Join-Path $PackageRoot 'xianyu-launcher.ico'
if (-not (Test-Path -LiteralPath $iconPath)) {
    $iconPath = Join-Path $PackageRoot 'assets\xianyu-launcher.ico'
}
try {
    $shortcutShell = New-Object -ComObject WScript.Shell
    Get-ChildItem -LiteralPath $desktop -Filter '*.lnk' -File -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.FullName -eq $shortcutPath) { return }
        try {
            $candidate = $shortcutShell.CreateShortcut($_.FullName)
            $identity = "$($candidate.TargetPath)`n$($candidate.Arguments)`n$($candidate.IconLocation)"
            if ($identity -match '(?is)xianyu' -and $identity -match '(?is)(start-xianyu\.ps1|[\\/]start\.bat)') {
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            }
        } catch { }
    }
} catch { }
if (Test-Path -LiteralPath $oldShortcutPath) {
    Remove-Item -LiteralPath $oldShortcutPath -Force
}
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
if (Test-Path -LiteralPath $launcherPath) {
    $shortcut.TargetPath = $launcherPath
    $shortcut.Arguments = ''
} else {
    $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $shortcut.Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$(Join-Path $PackageRoot 'scripts\start.ps1')`""
}
$shortcut.WorkingDirectory = $PackageRoot
$shortcut.Description = $shortcutTitle
if (Test-Path -LiteralPath $iconPath) { $shortcut.IconLocation = "$iconPath,0" }
$shortcut.Save()

Write-Host '[xianyu] Installation completed.' -ForegroundColor Green
Write-Host "[xianyu] Open: $frontendUrl" -ForegroundColor Green
Write-XianyuLog -LogPath $LogPath -Message "install_completed frontend_url=$frontendUrl"
Stop-XianyuLogSession
Start-Process $frontendUrl
exit 0
