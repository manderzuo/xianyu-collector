param([switch]$NoOpen)

$ErrorActionPreference = 'Continue'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $PackageRoot 'app'
$ComposeFile = Join-Path $AppRoot 'docker-compose.yml'
$EnvFile = Join-Path $AppRoot '.env'
$LogDir = Join-Path $AppRoot 'logs'
if (-not (Test-Path -LiteralPath $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ReportPath = Join-Path $LogDir "diagnostics-$timestamp.txt"
$LatestPath = Join-Path $LogDir 'diagnostics-latest.txt'
$report = New-Object System.Collections.Generic.List[string]

function Add-Report([string]$Text = '') {
    $script:report.Add($Text)
    Write-Host $Text
}

function Add-Section([string]$Title) {
    Add-Report ''
    Add-Report (('=' * 20) + " $Title " + ('=' * 20))
}

function Capture-Command([string]$Title, [scriptblock]$Command) {
    Add-Section $Title
    try {
        $output = & $Command 2>&1 | Out-String -Width 260
        $exitCode = $LASTEXITCODE
        if ($output.Trim()) { Add-Report $output.TrimEnd() }
        if ($null -ne $exitCode) { Add-Report "ExitCode: $exitCode" }
    } catch {
        Add-Report ($_ | Format-List * -Force | Out-String).TrimEnd()
    }
}

Add-Report 'Xianyu Management System - Diagnostic Report'
Add-Report "CreatedAt: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff zzz')"
Add-Report "Computer: $env:COMPUTERNAME"
Add-Report "User: $env:USERNAME"
Add-Report "PackageRoot: $PackageRoot"
Add-Report "PowerShell: $($PSVersionTable.PSVersion)"
Add-Report "Windows: $([Environment]::OSVersion.VersionString)"
Add-Report "DockerCommandAvailable: $([bool](Get-Command docker -ErrorAction SilentlyContinue))"
Add-Report "ComposeFileExists: $(Test-Path -LiteralPath $ComposeFile)"
Add-Report "EnvironmentFileExists: $(Test-Path -LiteralPath $EnvFile)"

if (Test-Path -LiteralPath $EnvFile) {
    Add-Section 'Non-secret configuration'
    $safeNames = @('FRONTEND_PORT', 'BACKEND_WEB_PORT', 'WEBSOCKET_PORT', 'SCHEDULER_PORT', 'XIANYU_CLOUD_AUTH_URL', 'XIANYU_CLOUD_AUTH_MAX_ATTEMPTS', 'XIANYU_CLOUD_AUTH_CONNECT_TIMEOUT', 'XIANYU_CLOUD_AUTH_READ_TIMEOUT', 'XIANYU_CLOUD_AUTH_WRITE_TIMEOUT', 'XIANYU_CLOUD_AUTH_POOL_TIMEOUT', 'XIANYU_CLOUD_AUTH_RETRY_BACKOFF', 'XIANYU_CLOUD_AUTH_MAX_RETRY_BACKOFF', 'XR_DEPLOY_MODE', 'XR_IMAGE_REGISTRY', 'XR_IMAGE_NAMESPACE', 'XR_IMAGE_TAG', 'UPDATE_MANIFEST_URL')
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$' -and $safeNames -contains $Matches[1]) {
            Add-Report "$($Matches[1])=$($Matches[2])"
        }
    }
}

Capture-Command 'Docker version' { docker version }
Capture-Command 'Docker info' { docker info }
Capture-Command 'Docker Compose version' { docker compose version }

if ((Test-Path -LiteralPath $EnvFile) -and (Test-Path -LiteralPath $ComposeFile)) {
    Capture-Command 'Compose configuration validation' { docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile config --quiet }
    Capture-Command 'Compose service status' { docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile ps -a }
    Capture-Command 'Container image and health summary' {
        docker ps -a --filter 'name=xianyu' --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
    }
    foreach ($service in @('mysql', 'redis', 'backend', 'websocket', 'scheduler', 'frontend')) {
        Capture-Command "Last 200 log lines: $service" {
            docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile logs --no-color --tail=200 $service
        }
    }
    Capture-Command 'Cloud auth health from backend container' {
        $pythonCode = @'
import httpx
import time

url = "https://www.gemstory.cn/api/xianyu/auth/health"
last_error = None
for attempt in range(1, 4):
    try:
        response = httpx.get(url, timeout=httpx.Timeout(60, connect=20))
        print(f"attempt {attempt} status {response.status_code}")
        print(response.text)
        break
    except httpx.HTTPError as error:
        last_error = error
        print(f"attempt {attempt} failed {type(error).__name__}: {str(error)[:300]}")
        if attempt < 3:
            time.sleep(min(attempt * 3, 10))
else:
    raise last_error
'@
        docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile exec -T backend python -c $pythonCode
    }
}

Add-Section 'Host network checks'
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Add-Report 'TLS protocol for host checks: TLS 1.2'
} catch {
    Add-Report "TLS protocol setup warning: $($_.Exception.Message)"
}

foreach ($url in @('https://www.gemstory.cn/api/xianyu/auth/health', 'https://www.gemstory.cn/release/xianyu/latest.json')) {
    $succeeded = $false
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            $response = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 60
            Add-Report "$url -> HTTP $($response.StatusCode) (attempt $attempt)"
            Add-Report $response.Content
            $succeeded = $true
            break
        } catch {
            Add-Report "$url -> attempt $attempt failed: $($_.Exception.Message)"
            if ($attempt -lt 3) { Start-Sleep -Seconds ([Math]::Min($attempt * 3, 10)) }
        }
    }
    if (-not $succeeded) {
        Add-Report "$url -> FAILED after 3 attempts"
    }
}

$report | Set-Content -LiteralPath $ReportPath -Encoding UTF8
Copy-Item -LiteralPath $ReportPath -Destination $LatestPath -Force
Write-Host ''
Write-Host "Diagnostic report saved: $ReportPath" -ForegroundColor Green
Write-Host "Latest report: $LatestPath" -ForegroundColor Green
if (-not $NoOpen) {
    try { Start-Process notepad.exe -ArgumentList "`"$LatestPath`"" } catch { }
}
