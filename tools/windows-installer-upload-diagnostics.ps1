param(
    [Parameter(Mandatory = $true)][string]$PackageRoot,
    [string]$ErrorSummary = 'The operation failed.',
    [string]$ErrorStage = 'launcher'
)

$ErrorActionPreference = 'Stop'
$maxArchiveBytes = 8MB
$appRoot = Join-Path $PackageRoot 'app'
$envFile = Join-Path $appRoot '.env'
$logsRoot = Join-Path $appRoot 'logs'
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('xianyu-diagnostic-' + [guid]::NewGuid().ToString('N'))
$archivePath = Join-Path ([IO.Path]::GetTempPath()) ('xianyu-diagnostic-' + [guid]::NewGuid().ToString('N') + '.zip')

function Protect-Text {
    param([string]$Text)
    if ($null -eq $Text) { return '' }
    $value = $Text
    $patterns = @(
        '(?im)(["'']?(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|jwt[_-]?secret|cookie|set-cookie|authorization|encryption[_-]?key|private[_-]?key)["'']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|''(?:\\.|[^''\\])*''|[^\s,;}\r\n]+)'
        '(?im)(Bearer\s+)[A-Za-z0-9._~+/=-]+'
        '(?i)([?&](?:token|password|secret|key)=)[^&\s]+'
        '(?i)(session|csrf|auth)[_-]?(token|key)?\s*[=:]\s*[^\s,;]+'
        '(?is)-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----'
    )
    foreach ($pattern in $patterns) {
        $replacement = if ($pattern -match 'PRIVATE KEY') { '[REDACTED PRIVATE KEY]' } else { '$1[REDACTED]' }
        $value = [regex]::Replace($value, $pattern, $replacement)
    }
    return $value
}

function Write-CapturedFile {
    param([string]$Name, [scriptblock]$Command)
    $target = Join-Path $tempRoot $Name
    try {
        $output = (& $Command 2>&1 | Out-String -Width 240)
        Protect-Text $output | Set-Content -LiteralPath $target -Encoding UTF8
    } catch {
        Protect-Text ("command_failed: " + $_.Exception.Message) | Set-Content -LiteralPath $target -Encoding UTF8
    }
}

try {
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    $machineValue = ''
    try { $machineValue = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Cryptography' -Name MachineGuid -ErrorAction Stop).MachineGuid } catch { }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $deviceId = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes([string]$machineValue))).Replace('-', '').ToLowerInvariant()).Substring(0, 24) } finally { $sha.Dispose() }

    $version = ''
    $build = ''
    try { $version = (Get-Content -LiteralPath (Join-Path $appRoot 'VERSION.txt') -TotalCount 1).Trim() } catch { }
    try { $build = (Get-Content -LiteralPath (Join-Path $appRoot 'BUILD_ID.txt') -TotalCount 1).Trim() } catch { }
    $metadata = [ordered]@{
        summary = Protect-Text $ErrorSummary
        stage = $ErrorStage
        severity = 'error'
        version = $version
        build = $build
        component = 'windows-launcher'
        client_type = 'windows-docker-installer'
        os = [Environment]::OSVersion.VersionString
        python = ''
        docker = ''
        network = ''
        device_id = $deviceId
        message = Protect-Text $ErrorSummary
    }
    ($metadata | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath (Join-Path $tempRoot 'metadata.json') -Encoding UTF8

    if (Test-Path -LiteralPath $logsRoot) {
        $logFiles = Get-ChildItem -LiteralPath $logsRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -in @('.log', '.txt', '.json') } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 12
        foreach ($file in $logFiles) {
            $relative = $file.FullName.Substring($logsRoot.Length).TrimStart('\', '/')
            $safeName = ($relative -replace '[^A-Za-z0-9._-]', '_')
            $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
            if ($text) {
                $maxText = 524288
                if ($text.Length -gt $maxText) { $text = "[log truncated; latest section retained]`r`n" + $text.Substring($text.Length - $maxText) }
                Protect-Text $text | Set-Content -LiteralPath (Join-Path $tempRoot $safeName) -Encoding UTF8
            }
        }
    }

    if (Test-Path -LiteralPath $envFile) {
        $allowed = @('FRONTEND_PORT', 'BACKEND_WEB_PORT', 'WEBSOCKET_PORT', 'SCHEDULER_PORT', 'XIANYU_CLOUD_AUTH_URL', 'XR_DEPLOY_MODE', 'XR_IMAGE_REGISTRY', 'XR_IMAGE_NAMESPACE', 'XR_IMAGE_TAG')
        Get-Content -LiteralPath $envFile | ForEach-Object {
            if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$' -and $allowed -contains $Matches[1]) { "$($Matches[1])=$($Matches[2])" }
        } | Set-Content -LiteralPath (Join-Path $tempRoot 'safe-environment.txt') -Encoding UTF8
    }

    Write-CapturedFile 'docker-info.txt' { docker info }
    if (Test-Path -LiteralPath (Join-Path $appRoot 'docker-compose.yml')) {
        Write-CapturedFile 'compose-status.txt' { docker compose --project-directory $appRoot --env-file $envFile -f (Join-Path $appRoot 'docker-compose.yml') ps -a }
        Write-CapturedFile 'compose-logs.txt' { docker compose --project-directory $appRoot --env-file $envFile -f (Join-Path $appRoot 'docker-compose.yml') logs --no-color --tail=120 backend websocket scheduler }
    }

    Compress-Archive -Path (Join-Path $tempRoot '*') -DestinationPath $archivePath -CompressionLevel Optimal -Force
    $archiveBytes = [IO.File]::ReadAllBytes($archivePath)
    if ($archiveBytes.Length -gt $maxArchiveBytes) { throw 'Diagnostic archive exceeds the upload limit.' }
    $baseUrl = 'https://www.gemstory.cn'
    if (Test-Path -LiteralPath $envFile) {
        $cloudLine = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*XIANYU_CLOUD_AUTH_URL\s*=' } | Select-Object -First 1
        if ($cloudLine -match '=\s*(.+)$' -and $Matches[1].Trim()) { $baseUrl = $Matches[1].Trim().TrimEnd('/') }
    }
    try { $baseUri = [Uri]$baseUrl } catch { throw '云端诊断服务地址格式无效。' }
    if (-not $baseUri.Host -or $baseUri.UserInfo) { throw '云端诊断服务地址不安全。' }
    $allowInsecure = $false
    if (Test-Path -LiteralPath $envFile) {
        $allowLine = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^\s*XIANYU_ALLOW_INSECURE_CLOUD_AUTH\s*=' } | Select-Object -First 1
        if ($allowLine -match '=\s*(.+)$') { $allowInsecure = $Matches[1].Trim().ToLowerInvariant() -in @('1', 'true', 'yes', 'on') }
    }
    if ($baseUri.Scheme -ne 'https' -and -not ($baseUri.Scheme -eq 'http' -and $allowInsecure)) {
        throw '云端诊断服务必须使用 HTTPS，请检查 XIANYU_CLOUD_AUTH_URL。'
    }
    $metadata.message = $metadata.summary
    $body = @{ metadata = $metadata; archive_base64 = [Convert]::ToBase64String($archiveBytes) } | ConvertTo-Json -Depth 8 -Compress
    $response = Invoke-WebRequest -Uri ($baseUrl.TrimEnd('/') + '/api/xianyu/auth/diagnostics_upload') -Method Post -ContentType 'application/json; charset=utf-8' -Body $body -UseBasicParsing -TimeoutSec 90
    $result = $response.Content | ConvertFrom-Json
    if (-not $result.ok) {
        $message = if ($result.message) { [string]$result.message } else { 'Diagnostic upload rejected.' }
        throw $message
    }
    Write-Output ('[xianyu] Diagnostic report uploaded: ' + $result.report.report_code)
    exit 0
} catch {
    Write-Error ('[xianyu] Diagnostic upload failed: ' + $_.Exception.Message)
    exit 2
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $archivePath -Force -ErrorAction SilentlyContinue
}
