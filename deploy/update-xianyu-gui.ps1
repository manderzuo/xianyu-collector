param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'
$EnvFile = Join-Path $ProjectRoot '.env'
$VersionFile = Join-Path $ProjectRoot 'VERSION.txt'
$BuildFile = Join-Path $ProjectRoot 'BUILD_ID.txt'
$LogDir = Join-Path $ProjectRoot 'logs'
$LogFile = Join-Path $LogDir 'update.log'

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
$fontSmall = New-Object System.Drawing.Font('Microsoft YaHei UI', 9)
$fontTitle = New-Object System.Drawing.Font('Microsoft YaHei UI', 18, [System.Drawing.FontStyle]::Bold)
$navy = [System.Drawing.Color]::FromArgb(15, 23, 42)
$card = [System.Drawing.Color]::FromArgb(30, 41, 59)
$border = [System.Drawing.Color]::FromArgb(71, 85, 105)
$white = [System.Drawing.Color]::FromArgb(241, 245, 249)
$muted = [System.Drawing.Color]::FromArgb(148, 163, 184)
$blue = [System.Drawing.Color]::FromArgb(59, 130, 246)
$green = [System.Drawing.Color]::FromArgb(34, 197, 94)

$worker = {
    param([string]$Root, [string]$Mode, [string]$ManifestJson)

    $ErrorActionPreference = 'Stop'
    $compose = Join-Path $Root 'docker-compose.yml'
    $envFile = Join-Path $Root '.env'
    $versionFile = Join-Path $Root 'VERSION.txt'
    $buildFile = Join-Path $Root 'BUILD_ID.txt'
    $logDir = Join-Path $Root 'logs'
    $logFile = Join-Path $logDir 'update.log'
    if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

    function Send-Message([string]$Kind, [string]$Message = '', [int]$Percent = -1, [string]$Data = '') {
        [pscustomobject]@{ Kind = $Kind; Message = $Message; Percent = $Percent; Data = $Data }
    }

    function Write-Detail([string]$Message) {
        $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') [$env:COMPUTERNAME] $Message"
        Add-Content -LiteralPath $logFile -Value $line -Encoding UTF8
        Send-Message 'log' $line
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
        $updated = foreach ($line in $lines) {
            if ($line -match "^\s*$([regex]::Escape($Name))\s*=") {
                $found = $true
                "$Name=$Value"
            } else { $line }
        }
        if (-not $found) { $updated += "$Name=$Value" }
        Set-Content -LiteralPath $Path -Value $updated -Encoding UTF8
    }

    function Get-VersionParts([string]$Value) {
        $matches = [regex]::Matches([string]$Value, '\d+')
        if ($matches.Count -eq 0) { throw "版本号无效：$Value" }
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

    function Invoke-Docker([string[]]$Arguments, [string]$Label) {
        Write-Detail "docker_start label=$Label args=$($Arguments -join ' ')"
        $output = & docker compose --project-directory $Root --env-file $envFile -f $compose @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        foreach ($line in $output) { Write-Detail "docker_output label=$Label text=$line" }
        Write-Detail "docker_end label=$Label exit_code=$exitCode"
        if ($exitCode -ne 0) { throw "Docker 操作失败：$Label（退出码 $exitCode）" }
    }

    try {
        Write-Detail "update_start mode=$Mode root=$Root"
        if (-not (Test-Path -LiteralPath $compose) -or -not (Test-Path -LiteralPath $envFile)) { throw '缺少 docker-compose.yml 或 .env' }
        $envMap = Get-EnvMap $envFile
        $manifestUrl = "$($envMap['UPDATE_MANIFEST_URL'])".Trim()
        if (-not $manifestUrl) { throw '未配置更新清单地址' }
        $requestUrl = $manifestUrl + ($(if ($manifestUrl.Contains('?')) { '&' } else { '?' })) + '_client_check=' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        Write-Detail "manifest_request url=$manifestUrl request_url=$requestUrl"
        $headers = @{ 'Cache-Control' = 'no-cache'; 'Accept' = 'application/json' }
        $manifestToken = "$($envMap['UPDATE_MANIFEST_TOKEN'])".Trim()
        if ($manifestToken) { $headers['Authorization'] = "Bearer $manifestToken"; Write-Detail 'manifest_auth configured=true' }
        $manifest = if ($Mode -eq 'check') { (Invoke-WebRequest -UseBasicParsing -Uri $requestUrl -TimeoutSec 20 -Headers $headers).Content | ConvertFrom-Json } else { $ManifestJson | ConvertFrom-Json }
        $latestVersion = "$($manifest.version)".Trim()
        $latestBuild = "$($manifest.build_id)".Trim()
        $registry = "$($manifest.image_registry)".Trim()
        $namespace = "$($manifest.image_namespace)".Trim()
        $tag = "$($manifest.image_tag)".Trim()
        Write-Detail "manifest_response version=$latestVersion build=$latestBuild registry=$registry namespace=$namespace tag=$tag"
        if (-not $latestVersion -or -not $registry -or -not $namespace -or -not $tag) { throw '更新清单字段不完整' }
        [void](Get-VersionParts $latestVersion)
        $currentVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
        $currentBuild = if (Test-Path -LiteralPath $buildFile) { (Get-Content -LiteralPath $buildFile -Raw).Trim() } else { '' }
        $versionResult = Compare-Version $latestVersion $currentVersion
        $available = $versionResult -gt 0 -or ($versionResult -eq 0 -and $latestBuild -and $latestBuild -ne $currentBuild)
        Write-Detail "version_compare current=$currentVersion/$currentBuild latest=$latestVersion/$latestBuild available=$available"
        if ($Mode -eq 'check') {
            if ($available) { Send-Message 'available' "发现新版本 $latestVersion" 8 ($manifest | ConvertTo-Json -Compress) } else { Send-Message 'latest' "当前已是最新版本 $currentVersion" 100 }
            return
        }
        if (-not $available) { Send-Message 'latest' "当前已是最新版本 $currentVersion" 100; return }
        Set-EnvValue $envFile 'XR_DEPLOY_MODE' 'remote'
        Set-EnvValue $envFile 'XR_IMAGE_REGISTRY' $registry
        Set-EnvValue $envFile 'XR_IMAGE_NAMESPACE' $namespace
        Set-EnvValue $envFile 'XR_IMAGE_TAG' $tag
        Send-Message 'phase' '正在准备更新环境...' 15
        Invoke-Docker @('pull') '拉取应用镜像'
        Send-Message 'phase' '镜像拉取完成，正在重启服务...' 78
        Invoke-Docker @('up', '-d', '--no-build') '重启应用服务'
        Send-Message 'phase' '正在检查容器状态...' 90
        Invoke-Docker @('ps') '检查容器状态'
        $port = "$($envMap['FRONTEND_PORT'])"
        if ($port -notmatch '^\d+$') { throw '前端端口配置无效' }
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port" -TimeoutSec 15
        Write-Detail "frontend_check status=$($response.StatusCode) port=$port"
        if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 500) { throw "前端健康检查失败，HTTP $($response.StatusCode)" }
        Set-Content -LiteralPath $versionFile -Value $latestVersion -Encoding UTF8
        Set-Content -LiteralPath $buildFile -Value $latestBuild -Encoding UTF8
        Write-Detail "update_completed version=$latestVersion build=$latestBuild"
        Send-Message 'completed' "更新完成，当前版本 $latestVersion" 100
    } catch {
        $detail = $_.Exception.ToString()
        Write-Detail "update_failed error=$detail"
        try { Invoke-Docker @('ps') '失败后检查容器状态' } catch { Write-Detail "docker_status_failed error=$($_.Exception.ToString())" }
        Send-Message 'failed' '更新失败，请查看下方详细日志' 100 $detail
    }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = '闲鱼管理系统更新'
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object System.Drawing.Size(760, 560)
$form.BackColor = $navy
$form.ForeColor = $white
$form.Font = $font
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$title = New-Object System.Windows.Forms.Label
$title.Text = '闲鱼管理系统更新'
$title.Font = $fontTitle
$title.ForeColor = $white
$title.Location = New-Object System.Drawing.Point(32, 26)
$title.AutoSize = $true
$form.Controls.Add($title)

$status = New-Object System.Windows.Forms.Label
$status.Text = '正在检查更新...'
$status.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 12, [System.Drawing.FontStyle]::Bold)
$status.ForeColor = $white
$status.Location = New-Object System.Drawing.Point(32, 74)
$status.AutoSize = $true
$form.Controls.Add($status)

$version = New-Object System.Windows.Forms.Label
$version.Text = '当前版本：读取中'
$version.ForeColor = $muted
$version.Location = New-Object System.Drawing.Point(32, 108)
$version.AutoSize = $true
$form.Controls.Add($version)

$notesTitle = New-Object System.Windows.Forms.Label
$notesTitle.Text = '更新说明'
$notesTitle.ForeColor = $white
$notesTitle.Location = New-Object System.Drawing.Point(32, 145)
$notesTitle.AutoSize = $true
$form.Controls.Add($notesTitle)

$notes = New-Object System.Windows.Forms.TextBox
$notes.Location = New-Object System.Drawing.Point(32, 172)
$notes.Size = New-Object System.Drawing.Size(696, 58)
$notes.Multiline = $true
$notes.ReadOnly = $true
$notes.BackColor = $card
$notes.ForeColor = $white
$notes.BorderStyle = 'FixedSingle'
$notes.Font = $fontSmall
$notes.Text = '正在获取更新说明...'
$form.Controls.Add($notes)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(32, 250)
$progress.Size = New-Object System.Drawing.Size(696, 18)
$progress.Minimum = 0
$progress.Maximum = 100
$form.Controls.Add($progress)

$progressText = New-Object System.Windows.Forms.Label
$progressText.Text = '检查更新  ·  下载更新  ·  重启应用'
$progressText.ForeColor = $muted
$progressText.Location = New-Object System.Drawing.Point(32, 276)
$progressText.AutoSize = $true
$form.Controls.Add($progressText)

$logTitle = New-Object System.Windows.Forms.Label
$logTitle.Text = '详细日志'
$logTitle.ForeColor = $white
$logTitle.Location = New-Object System.Drawing.Point(32, 312)
$logTitle.AutoSize = $true
$form.Controls.Add($logTitle)

$logBox = New-Object System.Windows.Forms.RichTextBox
$logBox.Location = New-Object System.Drawing.Point(32, 338)
$logBox.Size = New-Object System.Drawing.Size(696, 132)
$logBox.ReadOnly = $true
$logBox.BackColor = [System.Drawing.Color]::FromArgb(2, 6, 23)
$logBox.ForeColor = [System.Drawing.Color]::FromArgb(186, 230, 253)
$logBox.BorderStyle = 'FixedSingle'
$logBox.Font = New-Object System.Drawing.Font('Consolas', 8.5)
$form.Controls.Add($logBox)

$later = New-Object System.Windows.Forms.Button
$later.Text = '稍后更新'
$later.Location = New-Object System.Drawing.Point(454, 490)
$later.Size = New-Object System.Drawing.Size(128, 40)
$later.FlatStyle = 'Flat'
$later.FlatAppearance.BorderColor = $border
$later.BackColor = $card
$later.ForeColor = $white
$later.Enabled = $false
$form.Controls.Add($later)

$now = New-Object System.Windows.Forms.Button
$now.Text = '立即更新'
$now.Location = New-Object System.Drawing.Point(600, 490)
$now.Size = New-Object System.Drawing.Size(128, 40)
$now.FlatStyle = 'Flat'
$now.FlatAppearance.BorderColor = $blue
$now.BackColor = $blue
$now.ForeColor = $white
$now.Enabled = $false
$form.Controls.Add($now)

$job = $null
$manifestJson = ''
$phase = 'checking'
$startedUpdate = $false

function Add-Log([string]$Text) {
    if ($Text) {
        $logBox.AppendText($Text + [Environment]::NewLine)
        $logBox.SelectionStart = $logBox.TextLength
        $logBox.ScrollToCaret()
    }
}

function Start-Worker([string]$Mode, [string]$Data = '') {
    if ($script:job) { Remove-Job -Job $script:job -Force -ErrorAction SilentlyContinue }
    $script:job = Start-Job -ScriptBlock $worker -ArgumentList $ProjectRoot, $Mode, $Data
    $now.Enabled = $false
    $later.Enabled = $false
}

function Finish-Check([string]$Kind) {
    if ($Kind -eq 'available') {
        $script:phase = 'available'
        $status.Text = '发现新版本'
        $status.ForeColor = $blue
        $now.Text = '立即更新'
        $now.Enabled = $true
        $later.Text = '稍后更新'
        $later.Enabled = $true
        if ($Force -and -not $startedUpdate) {
            $script:startedUpdate = $true
            Start-Worker 'update' $manifestJson
            $script:phase = 'updating'
            $status.Text = '正在下载更新...'
        }
    } elseif ($Kind -eq 'latest') {
        $script:phase = 'latest'
        $status.Text = '当前已是最新版本'
        $status.ForeColor = $green
        $now.Text = '关闭'
        $now.Enabled = $true
        $later.Text = '关闭'
        $later.Enabled = $true
    }
}

$now.Add_Click({
    if ($phase -eq 'available') {
        $script:startedUpdate = $true
        $script:phase = 'updating'
        $status.Text = '正在下载更新...'
        $status.ForeColor = $white
        Start-Worker 'update' $manifestJson
    } elseif ($phase -eq 'failed') {
        $script:phase = 'checking'
        $status.Text = '正在重新检查更新...'
        $status.ForeColor = $white
        Start-Worker 'check'
    } else {
        $form.Close()
    }
})
$later.Add_Click({ $form.Close() })
$form.Add_FormClosing({ if ($job) { Stop-Job -Job $job -ErrorAction SilentlyContinue; Remove-Job -Job $job -Force -ErrorAction SilentlyContinue } })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 200
$timer.Add_Tick({
    if (-not $job) { return }
    $messages = @(Receive-Job -Job $job -ErrorAction SilentlyContinue)
    foreach ($message in $messages) {
        if ($message.Kind -eq 'log') { Add-Log $message.Message; continue }
        if ($message.Percent -ge 0 -and $message.Percent -le 100) {
            $progress.Value = $message.Percent
            $progressText.Text = "$($message.Percent)%  $($message.Message)"
        }
        if ($message.Kind -eq 'available') {
            $manifestJson = $message.Data
            try {
                $manifest = $manifestJson | ConvertFrom-Json
                $version.Text = "当前版本：$((Get-Content -LiteralPath $VersionFile -Raw).Trim())    最新版本：$($manifest.version)"
                $notes.Text = if ($manifest.notes) { $manifest.notes } else { '本次更新包含功能优化和稳定性修复。' }
            } catch { }
            Finish-Check 'available'
        } elseif ($message.Kind -eq 'latest') {
            $version.Text = "当前版本：$((Get-Content -LiteralPath $VersionFile -Raw).Trim())"
            $notes.Text = '当前已经是最新版本，无需更新。'
            Finish-Check 'latest'
        } elseif ($message.Kind -eq 'completed') {
            $script:phase = 'completed'
            $status.Text = $message.Message
            $status.ForeColor = $green
            $now.Text = '关闭'
            $now.Enabled = $true
            $later.Text = '关闭'
            $later.Enabled = $true
        } elseif ($message.Kind -eq 'failed') {
            $script:phase = 'failed'
            $status.Text = '更新失败，请查看详细日志'
            $status.ForeColor = [System.Drawing.Color]::FromArgb(248, 113, 113)
            $now.Text = '重试'
            $now.Enabled = $true
            $later.Text = '关闭'
            $later.Enabled = $true
            Add-Log $message.Data
        }
    }
    if ($job.State -in @('Completed', 'Failed', 'Stopped') -and $phase -ne 'updating') {
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        $script:job = $null
    } elseif ($job.State -in @('Completed', 'Failed', 'Stopped') -and $phase -eq 'updating') {
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        $script:job = $null
    }
})

$form.Add_Shown({
    Add-Log "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') [GUI] 开始检查更新"
    Start-Worker 'check'
    $timer.Start()
})

[void]$form.ShowDialog()
$timer.Stop()
