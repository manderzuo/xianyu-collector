$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'
$EnvExample = Join-Path $ProjectRoot '.env.example'
$EnvFile = Join-Path $ProjectRoot '.env'

function Write-Step([string]$Message) {
    Write-Host "[xianyu] $Message" -ForegroundColor Cyan
}

function Fail([string]$Message) {
    Write-Host "[xianyu] ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Test-Command([string]$Name) {
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-EnvMap([string]$Path) {
    $map = @{}
    if (Test-Path $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
                $map[$Matches[1]] = $Matches[2]
            }
        }
    }
    return $map
}

function Set-EnvValue([string]$Path, [string]$Name, [string]$Value) {
    $lines = @()
    if (Test-Path $Path) { $lines = @(Get-Content -LiteralPath $Path) }
    $found = $false
    $newLines = foreach ($line in $lines) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=") {
            $found = $true
            "$Name=$Value"
        } else {
            $line
        }
    }
    if (-not $found) { $newLines += "$Name=$Value" }
    Set-Content -LiteralPath $Path -Value $newLines -Encoding utf8
}

function Test-PortFree([int]$Port) {
    try {
        $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
        return $null -eq $listener
    } catch {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $task = $client.ConnectAsync('127.0.0.1', $Port)
            if (-not $task.Wait(250)) { return $true }
            return -not $client.Connected
        } catch {
            return $true
        } finally {
            $client.Dispose()
        }
    }
}

function Get-FreePort([int]$StartPort, [int[]]$UsedPorts) {
    $candidate = $StartPort
    while ($true) {
        if (($UsedPorts -notcontains $candidate) -and (Test-PortFree $candidate)) { return $candidate }
        $candidate++
    }
}

if (-not (Test-Command 'docker')) { Fail 'Docker CLI was not found. Install and start Docker Desktop first.' }
if (-not (Test-Path $ComposeFile)) { Fail 'docker-compose.yml was not found beside this script.' }
if (-not (Test-Path $EnvExample)) { Fail '.env.example was not found.' }

try {
    docker info | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'docker info failed' }
    docker compose version | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'docker compose failed' }
} catch {
    Fail 'Docker Desktop is not running, or Docker Compose is unavailable.'
}

$newEnv = -not (Test-Path $EnvFile)
if ($newEnv) {
    Write-Step 'Creating local environment file.'
    Copy-Item -LiteralPath $EnvExample -Destination $EnvFile
}

$envMap = Get-EnvMap $EnvFile
if (-not $envMap.ContainsKey('XIANYU_CLOUD_AUTH_URL') -or [string]::IsNullOrWhiteSpace($envMap['XIANYU_CLOUD_AUTH_URL'])) {
    Set-EnvValue $EnvFile 'XIANYU_CLOUD_AUTH_URL' 'https://www.gemstory.cn'
}
$folderName = Split-Path -Leaf $ProjectRoot
$safeName = ($folderName.ToLower() -replace '[^a-z0-9]+', '-') -replace '(^-+|-+$)', ''
if ([string]::IsNullOrWhiteSpace($safeName)) { $safeName = 'xianyu-app' }
$safeName = $safeName.Substring(0, [Math]::Min(24, $safeName.Length))

if (-not $envMap.ContainsKey('XR_CONTAINER_PREFIX') -or [string]::IsNullOrWhiteSpace($envMap['XR_CONTAINER_PREFIX'])) {
    Set-EnvValue $EnvFile 'XR_CONTAINER_PREFIX' $safeName
}
if (-not $envMap.ContainsKey('XR_NETWORK_NAME') -or [string]::IsNullOrWhiteSpace($envMap['XR_NETWORK_NAME'])) {
    Set-EnvValue $EnvFile 'XR_NETWORK_NAME' "$safeName-net"
}
if (-not $envMap.ContainsKey('XR_VOLUME_PREFIX') -or [string]::IsNullOrWhiteSpace($envMap['XR_VOLUME_PREFIX'])) {
    Set-EnvValue $EnvFile 'XR_VOLUME_PREFIX' $safeName
}
if (-not $envMap.ContainsKey('COMPOSE_PROJECT_NAME') -or [string]::IsNullOrWhiteSpace($envMap['COMPOSE_PROJECT_NAME'])) {
    Set-EnvValue $EnvFile 'COMPOSE_PROJECT_NAME' $safeName
}

$envMap = Get-EnvMap $EnvFile
$containerPrefix = $envMap['XR_CONTAINER_PREFIX']
$ownedContainers = @()
try {
    $ownedContainers = @(docker ps -a --format '{{.Names}}' | Where-Object { $_ -like "$containerPrefix-*" })
} catch { }
$ports = @()
foreach ($name in @('FRONTEND_PORT', 'BACKEND_WEB_PORT', 'WEBSOCKET_PORT', 'SCHEDULER_PORT')) {
    $raw = $envMap[$name]
    $port = 0
    if ($raw -match '^\d+$') { $port = [int]$raw }
    $portOwnedByThisDeployment = $ownedContainers.Count -gt 0
    if ($port -lt 1024 -or $port -gt 65500 -or ((-not $portOwnedByThisDeployment) -and (-not (Test-PortFree $port))) -or ($ports -contains $port)) {
        $base = switch ($name) {
            'FRONTEND_PORT' { 20000 }
            'BACKEND_WEB_PORT' { 28089 }
            'WEBSOCKET_PORT' { 28090 }
            'SCHEDULER_PORT' { 28091 }
        }
        $port = Get-FreePort $base $ports
        Set-EnvValue $EnvFile $name ([string]$port)
    }
    $ports += $port
}

$envMap = Get-EnvMap $EnvFile
Write-Step "Project root: $ProjectRoot"
Write-Step "Frontend: http://127.0.0.1:$($envMap['FRONTEND_PORT'])"
Write-Step "Backend:  http://127.0.0.1:$($envMap['BACKEND_WEB_PORT'])"
Write-Step "WebSocket: http://127.0.0.1:$($envMap['WEBSOCKET_PORT'])"
Write-Step "Scheduler: http://127.0.0.1:$($envMap['SCHEDULER_PORT'])"

Write-Step 'Validating compose configuration.'
docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile config | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose configuration validation failed.' }

if (($envMap['XR_DEPLOY_MODE'] -as [string]).ToLowerInvariant() -eq 'remote') {
    Write-Step 'Pulling remote images.'
    docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile pull
    if ($LASTEXITCODE -ne 0) { Fail 'Remote image pull failed. Check the GHCR image visibility, image address and network.' }
    docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile up -d --no-build
} else {
    Write-Step 'Building and starting local images.'
    docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile up -d --build
}
if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose failed to start the application.' }

Write-Step 'Waiting for the frontend.'
$frontendUrl = "http://127.0.0.1:$($envMap['FRONTEND_PORT'])"
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $response = Invoke-WebRequest -Uri $frontendUrl -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) { Write-Host '[xianyu] WARNING: frontend did not respond within the wait period.' -ForegroundColor Yellow }

Write-Step 'Creating desktop launcher.'
$startScript = Join-Path $PSScriptRoot 'start-xianyu.ps1'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutTitle = -join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))
$shortcutPath = Join-Path $desktop "$shortcutTitle.lnk"
$oldShortcutPath = Join-Path $desktop 'Xianyu System.lnk'
$iconPath = Join-Path $ProjectRoot 'assets\xianyu-launcher.ico'
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
$shortcut.TargetPath = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$startScript`""
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.Description = $shortcutTitle
if (Test-Path -LiteralPath $iconPath) { $shortcut.IconLocation = "$iconPath,0" }
$shortcut.Save()

Write-Step 'Installation completed.'
Write-Host "Open: $frontendUrl" -ForegroundColor Green
Start-Process $frontendUrl
exit 0
