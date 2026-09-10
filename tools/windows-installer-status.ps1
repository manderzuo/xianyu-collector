param(
    [switch]$ProbeUpdate
)

# The launcher reads the machine-readable status channel as UTF-8. Windows
# PowerShell otherwise writes text using the active console code page, which
# turns Chinese service labels into mojibake in the desktop console.
try {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $utf8NoBom
    $global:OutputEncoding = $utf8NoBom
} catch { }

# Real service status source for the desktop console (XianyuLauncher).
# Emits exactly one line on stdout:
#   @@XIANYU_STATUS@@{"json":...}
# The GUI renders this JSON verbatim. Nothing here infers state from log
# text; all facts come from Docker, the filesystem or HTTP checks.

$ErrorActionPreference = 'Stop'
$PackageRoot = Split-Path -Parent $PSScriptRoot
$AppRoot = Join-Path $PackageRoot 'app'
$EnvFile = Join-Path $AppRoot '.env'
$ComposeFile = Join-Path $AppRoot 'docker-compose.yml'
$VersionFile = Join-Path $AppRoot 'VERSION.txt'
$BuildFile = Join-Path $AppRoot 'BUILD_ID.txt'

function Get-EnvMap([string]$Path) {
    $map = @{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') { $map[$Matches[1]] = $Matches[2] }
        }
    }
    return $map
}

function ConvertTo-JsonString([object]$Value) {
    return ($Value | ConvertTo-Json -Compress -Depth 8)
}

function Get-LocalFirstLine([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        try { return ((Get-Content -LiteralPath $Path -First 1) | Select-Object -First 1).Trim() } catch { }
    }
    return ''
}

$services = [ordered]@{}
$serviceSpecs = @(
    @{ Key = 'frontend';  Card = '前端服务' },
    @{ Key = 'backend';   Card = '后端服务' },
    @{ Key = 'websocket'; Card = '消息服务' },
    @{ Key = 'scheduler'; Card = '定时任务' }
)
$summaryLevel = 'unknown'
$activities = @()
$frontendUrl = ''
$deployMode = ''
$envExists = Test-Path -LiteralPath $EnvFile
$envMap = Get-EnvMap $EnvFile

foreach ($spec in $serviceSpecs) {
    $services[$spec.Key] = [ordered]@{ state = 'unknown'; text = '未知状态'; since = '' }
}

if (-not $envExists -or -not (Test-Path -LiteralPath $ComposeFile)) {
    # The portable package intentionally omits the generated .env file. It is
    # created by install.ps1 from .env.example. Do not report this normal
    # pre-install state as a Docker connectivity failure.
    foreach ($spec in $serviceSpecs) {
        $services[$spec.Key] = [ordered]@{ state = 'unknown'; text = '尚未完成安装'; since = '' }
    }
    $summaryLevel = 'unconfigured'
}

if ($envExists -and (Test-Path -LiteralPath $ComposeFile)) {
    $deployMode = "$($envMap['XR_DEPLOY_MODE'])".Trim().ToLowerInvariant()
    $frontendPort = "$($envMap['FRONTEND_PORT'])".Trim()
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $rows = @()
    try {
        # Keep the Docker template free of embedded quotes. Windows
        # PowerShell can strip those quotes before invoking docker.exe,
        # causing a valid engine to be reported as unreachable. The generated
        # Compose container prefix is stable and is sufficient to identify
        # each service without relying on labels.
        $rows = & docker ps -a --format '{{.State}}|{{.Status}}|{{.RunningFor}}|{{.Names}}' 2>$null
        $dockerOk = ($LASTEXITCODE -eq 0)
    } catch { $dockerOk = $false }
    finally { $ErrorActionPreference = $previousPreference }

    if ($dockerOk -and $rows.Count -ge 0) {
        $envPrefix = "$($envMap['XR_CONTAINER_PREFIX'])".Trim()
        $projectName = "$($envMap['COMPOSE_PROJECT_NAME'])".Trim()
        foreach ($spec in $serviceSpecs) {
            $matchRow = $null
            foreach ($row in $rows) {
                $parts = "$row".Trim() -split '\|', 5
                if ($parts.Count -lt 4) { continue }
                $containerName = $parts[3].Trim()
                $serviceHit = $false
                if ($envPrefix) {
                    $serviceHit = ($containerName -eq "$envPrefix-$($spec.Key)") -or ($containerName -like "$envPrefix-$($spec.Key)-*")
                }
                if ($serviceHit) { $matchRow = $parts; break }
            }
            if ($null -ne $matchRow) {
                $state = "$($matchRow[0])".Trim().ToLowerInvariant()
                $statusText = "$($matchRow[1])".Trim()
                $since = "$($matchRow[2])".Trim()
                switch ($state) {
                    'running' {
                        if ($statusText -match 'unhealthy') {
                            $services[$spec.Key] = [ordered]@{ state = 'error'; text = '异常'; since = $since }
                        } else {
                            $services[$spec.Key] = [ordered]@{ state = 'running'; text = '运行正常'; since = $since }
                        }
                    }
                    'restarting' { $services[$spec.Key] = [ordered]@{ state = 'starting'; text = '正在重启'; since = $since } }
                    'created' { $services[$spec.Key] = [ordered]@{ state = 'starting'; text = '等待启动'; since = $since } }
                    'exited' { $services[$spec.Key] = [ordered]@{ state = 'stopped'; text = '已停止'; since = $since } }
                    'paused' { $services[$spec.Key] = [ordered]@{ state = 'stopped'; text = '已暂停'; since = $since } }
                    default { $services[$spec.Key] = [ordered]@{ state = 'error'; text = '异常'; since = $since } }
                }
            } else {
                $services[$spec.Key] = [ordered]@{ state = 'stopped'; text = '未创建'; since = '' }
            }
        }
        $runningCount = @($serviceSpecs | Where-Object { $services[$_.Key].state -eq 'running' }).Count
        $errorCount = @($serviceSpecs | Where-Object { $services[$_.Key].state -eq 'error' }).Count
        if ($errorCount -gt 0) { $summaryLevel = 'error' }
        elseif ($runningCount -eq $serviceSpecs.Count) { $summaryLevel = 'ok' }
        elseif ($runningCount -gt 0) { $summaryLevel = 'partial' }
        else { $summaryLevel = 'down' }
    } else {
        # Engine not reachable: report unknown with a clear reason; the GUI
        # shows 检测不到 Docker 而不是假装正常。
        foreach ($spec in $serviceSpecs) {
            $services[$spec.Key] = [ordered]@{ state = 'unknown'; text = '无法连接 Docker'; since = '' }
        }
        $summaryLevel = 'unknown'
    }

    # Frontend HTTP check is the authoritative "can the user open the app" signal.
    if ($frontendPort -match '^\d+$') {
        $frontendUrl = "http://127.0.0.1:$frontendPort"
        $probe = $false
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $frontendUrl -TimeoutSec 3
            $probe = ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
        } catch { $probe = $false }
        finally { $ErrorActionPreference = $previousPreference }
        if (-not $probe) { $frontendUrl = "$frontendUrl|unreachable" }
    }

    # Recent activity: real container start/die events from the last 24 hours.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $eventRows = & docker events --since 24h --until now --format '{{.TimeUnix}}|{{.Action}}|{{.Actor.Attributes.name}}' 2>$null
        if ($LASTEXITCODE -eq 0 -and $eventRows) {
            $cardNames = @{}
            foreach ($spec in $serviceSpecs) { $cardNames[$spec.Key] = $spec.Card }
            $lines = @()
            foreach ($row in @($eventRows | Sort-Object -Descending | Select-Object -First 40)) {
                $parts = "$row".Trim() -split '\|', 3
                if ($parts.Count -lt 3) { continue }
                $unix = 0L
                if (-not [long]::TryParse($parts[0].Trim(), [ref]$unix)) { continue }
                $action = $parts[1].Trim().ToLowerInvariant()
                $name = $parts[2].Trim()
                $serviceKey = ''
                foreach ($key in $cardNames.Keys) {
                    if ($name -like "*-$key-*" -or $name -like "*$key*") { $serviceKey = $key; break }
                }
                if (-not $serviceKey) { continue }
                $label = $cardNames[$serviceKey]
                $kind = 'info'
                $text = ''
                switch ($action) {
                    'start' { $text = "$label 启动成功"; $kind = 'success' }
                    'die' { $text = "$label 已停止"; $kind = 'info' }
                    'stop' { $text = "$label 停止请求"; $kind = 'info' }
                    default { continue }
                }
                $lines += [ordered]@{ kind = $kind; text = $text; time = ([DateTimeOffset]::FromUnixTimeSeconds($unix).LocalDateTime.ToString('yyyy-MM-dd HH:mm:ss')) }
                if ($lines.Count -ge 5) { break }
            }
            $activities = @($lines)
        }
    } catch { }
    finally { $ErrorActionPreference = $previousPreference }

    # A pending client maintenance package is a real, actionable state.
    try {
        $pendingDir = Join-Path $AppRoot 'updates\pending'
        if ((Test-Path -LiteralPath $pendingDir) -and @(Get-ChildItem $pendingDir -Filter '*.zip').Count -gt 0) {
            $activities = @([ordered]@{ kind = 'info'; text = '客户端维护包已就绪，重启后应用'; time = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss') }) + @($activities)
        }
    } catch { }
    if ($activities.Count -gt 5) { $activities = @($activities | Select-Object -First 5) }
}

$payload = [ordered]@{
    v             = 1
    version       = Get-LocalFirstLine $VersionFile
    build         = Get-LocalFirstLine $BuildFile
    deploy_mode   = $deployMode
    env_exists    = $envExists
    installed     = $envExists
    frontend_url  = $frontendUrl
    services      = $services
    summary       = [ordered]@{
        level = $summaryLevel
        online = @($serviceSpecs | Where-Object { $services[$_.Key].state -eq 'running' }).Count
        total = $serviceSpecs.Count
    }
    activities    = @($activities | Select-Object -First 5)
    update_probe  = [ordered]@{ checked = $false; available = $false; latest_version = ''; latest_build = ''; reason = '' }
}

if ($ProbeUpdate -and $envExists) {
    try {
        $manifestUrl = "$($envMap['UPDATE_MANIFEST_URL'])".Trim()
        if ($manifestUrl) {
            $tempFile = Join-Path ([IO.Path]::GetTempPath()) ('xianyu-latest-' + [guid]::NewGuid().ToString('N') + '.json')
            try {
                Invoke-WebRequest -UseBasicParsing -Uri ($manifestUrl + $(if ($manifestUrl.Contains('?')) { '&' } else { '?' }) + '_status_probe=' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) -TimeoutSec 6 -OutFile $tempFile -Headers @{ 'Cache-Control' = 'no-cache' }
                $remote = (Get-Content -LiteralPath $tempFile -Raw -Encoding UTF8) | ConvertFrom-Json
                $payload.update_probe.checked = $true
                $payload.update_probe.latest_version = "$($remote.version)".Trim()
                $payload.update_probe.latest_build = "$($remote.build_id)".Trim()
                $localVersion = $payload.version
                $localBuild = $payload.build
                $verNew = $false
                try {
                    $a = @([regex]::Matches($payload.update_probe.latest_version, '\d+') | ForEach-Object { [int]$_.Value })
                    $b = @([regex]::Matches($localVersion, '\d+') | ForEach-Object { [int]$_.Value })
                    $width = [Math]::Max($a.Count, $b.Count)
                    for ($i = 0; $i -lt $width; $i++) {
                        $av = if ($i -lt $a.Count) { $a[$i] } else { 0 }
                        $bv = if ($i -lt $b.Count) { $b[$i] } else { 0 }
                        if ($av -gt $bv) { $verNew = $true; break }
                        if ($av -lt $bv) { break }
                    }
                } catch { }
                $buildNew = ($payload.update_probe.latest_build -and $localBuild -and $payload.update_probe.latest_build -ne $localBuild)
                $payload.update_probe.available = ($verNew -or (-not $verNew -and $buildNew))
            } finally {
                Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue
            }
        } else {
            $payload.update_probe.reason = 'manifest_url_missing'
        }
    } catch {
        $payload.update_probe.checked = $true
        $payload.update_probe.reason = 'probe_failed'
    }
}

[Console]::Out.WriteLine('@@XIANYU_STATUS@@' + (ConvertTo-JsonString $payload))
[Console]::Out.Flush()
exit 0
