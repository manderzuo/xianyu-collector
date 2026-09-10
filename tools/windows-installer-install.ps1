param(
    [string]$InstallPath = '',
    [switch]$CheckOnly,
    [switch]$OpenDocker,
    [switch]$ShortcutOnly,
    [bool]$CreateDesktopShortcut = $true,
    [switch]$NoOpenBrowser
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# GUI protocol channel. When the launcher sets XIANYU_GUI_PROTOCOL=1 the
# script emits machine-readable state events on stdout. The GUI renders those
# events verbatim and never infers state from human log text. Plain-console
# (BAT) runs keep the previous behaviour with no event lines.
#
# Error code contract (shared with XianyuLauncher.cs and diagnostics):
#   E_WSL_RESTART_REQUIRED  E_WSL_NOT_AVAILABLE
#   E_DOCKER_NOT_INSTALLED  E_DOCKER_ENGINE_NOT_READY  E_DOCKER_COMPOSE
#   E_NETWORK_UPDATE_SERVER E_NETWORK_REGISTRY
#   E_INSTALL_PACKAGE_MISSING  E_INSTALL_IMAGE_IMPORT  E_INSTALL_SERVICE_START
#   E_INSTALL_HEALTH  E_INSTALL_CONFIG  E_INSTALL  E_UPDATE_* (see updater)
# ---------------------------------------------------------------------------
$script:GuiProtocolEnabled = ("$env:XIANYU_GUI_PROTOCOL".Trim() -eq '1')
$script:CurrentStageId = 'env'

try {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $utf8NoBom
    $global:OutputEncoding = $utf8NoBom
} catch { }

function Write-GuiEvent {
    param([string]$Type, [hashtable]$Payload)
    if (-not $script:GuiProtocolEnabled) { return }
    try {
        $record = [ordered]@{ v = 1; type = $Type }
        foreach ($key in $Payload.Keys) {
            $value = $Payload[$key]
            if ($null -ne $value) { $record[$key] = $value }
        }
        [Console]::Out.WriteLine('@@XIANYU_UI@@' + ($record | ConvertTo-Json -Compress -Depth 8))
        [Console]::Out.Flush()
    } catch { }
}

function Write-Stage {
    param([string]$Stage, [string]$Status, [int]$Progress = -1, [string]$Detail = '', [string]$Code = '')
    Write-GuiEvent 'stage' @{
        operation = if ($CheckOnly) { 'precheck' } else { 'install' }
        stage     = $Stage
        status    = $Status
        progress  = $Progress
        detail    = $Detail
        code      = $Code
    }
}

function Write-Meta { param([string]$Key, [string]$Value) Write-GuiEvent 'meta' @{ key = $Key; value = $Value } }
function Write-Result {
    param([string]$Status, [string]$Detail = '', [string]$Code = '')
    Write-GuiEvent 'result' @{
        operation = if ($CheckOnly) { 'precheck' } else { 'install' }
        status    = $Status
        detail    = $Detail
        code      = $Code
    }
}

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
    return [Convert]::ToBase64String($buffer).Replace('+', '-').Replace('/', '_').TrimEnd('=')
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

function Get-DockerDesktopPath {
    $command = Get-Command 'Docker Desktop.exe' -ErrorAction SilentlyContinue
    if ($command -and $command.Source -and (Test-Path -LiteralPath $command.Source -PathType Leaf)) { return [string]$command.Source }
    $candidates = @(
        (Join-Path ${env:ProgramFiles} 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Docker\Docker\Docker Desktop.exe'),
        (Join-Path ${env:LOCALAPPDATA} 'Docker\Docker Desktop.exe'),
        (Join-Path ${env:LOCALAPPDATA} 'Programs\Docker\Docker Desktop.exe')
    )
    $registryPaths = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    foreach ($path in $registryPaths) {
        foreach ($entry in @(Get-ItemProperty -Path $path -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -and $_.DisplayName -match '(?i)^Docker Desktop(?:$|\s)' })) {
            if ($entry.InstallLocation) { $candidates += Join-Path ([string]$entry.InstallLocation) 'Docker Desktop.exe' }
            if ($entry.DisplayIcon) { $candidates += ([string]$entry.DisplayIcon -replace '^"|"$', '') }
        }
    }
    foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return [string]$candidate }
    }
    return ''
}

function Test-DockerEngineReady {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker info --format '{{.ServerVersion}}' 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally { $ErrorActionPreference = $previousPreference }
}

function Test-DockerComposeReady {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker compose version 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally { $ErrorActionPreference = $previousPreference }
}

function Test-RebootPending {
    try {
        if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { return $true }
        if (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update' -Name RebootRequired -ErrorAction SilentlyContinue) { return $true }
    } catch { }
    return $false
}

function Test-WslAvailable {
    $wslExe = Join-Path $env:SystemRoot 'System32\wsl.exe'
    if (-not (Test-Path -LiteralPath $wslExe)) { return $false }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $wslExe --status 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
    finally { $ErrorActionPreference = $previousPreference }
}

function New-TemporaryDirectory {
    return Join-Path ([IO.Path]::GetTempPath()) ('xianyu-install-' + [guid]::NewGuid().ToString('N'))
}

# ---------------------------------------------------------------------------
# Mode: open Docker Desktop only (used by the precheck screen action button).
# ---------------------------------------------------------------------------
if ($OpenDocker) {
    $dockerExe = Get-DockerDesktopPath
    if (-not $dockerExe) {
        Write-Host '[xianyu] Docker Desktop executable was not found on this PC.' -ForegroundColor Yellow
        exit 1
    }
    try {
        Start-Process -FilePath $dockerExe
        exit 0
    } catch {
        Write-Host "[xianyu] Could not start Docker Desktop: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Shared package paths (resolved from the package root for both modes).
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Mode: precheck (read-only environment verification for the GUI wizard).
# Never mutates WSL, Docker or package files; only probes and reports.
# Exit codes: 0 = can continue, 1 = blocking failure, 3010 = reboot required.
# ---------------------------------------------------------------------------
if ($CheckOnly) {
    $precheckFailed = $false
    $precheckRestart = $false

    # Row 1: Docker Desktop
    Write-Stage 'precheck_docker' 'running' 5 '正在检测 Docker Desktop...'
    $dockerPath = Get-DockerDesktopPath
    $dockerCli = $null -ne (Get-Command docker -ErrorAction SilentlyContinue)
    if (-not $dockerCli -and -not $dockerPath) {
        # Without Docker Desktop the installer would need the network to fetch
        # it; offline packages cannot bootstrap Docker, so that combination
        # blocks the installation entirely.
        $offlineBundlePresent = Test-Path -LiteralPath $OfflineImageManifest
        if ($offlineBundlePresent) {
            Write-Stage 'precheck_docker' 'failed' -1 '未检测到 Docker Desktop，离线安装包无法自动安装它，请先手动安装 Docker Desktop' 'E_DOCKER_NOT_INSTALLED'
            $precheckFailed = $true
        } else {
            Write-Stage 'precheck_docker' 'warning' -1 '未检测到 Docker Desktop，安装时将尝试在线自动安装' 'E_DOCKER_NOT_INSTALLED'
        }
    } elseif (-not $dockerCli) {
        Write-Stage 'precheck_docker' 'warning' -1 '已安装 Docker Desktop，但未找到 docker 命令行，安装时会重新刷新 PATH' 'E_DOCKER_NOT_INSTALLED'
    } else {
        $engineOk = Test-DockerEngineReady
        if (-not $engineOk) {
            # Give a just-started engine a short grace window before declaring
            # failure; the GUI offers an explicit relaunch/retry action.
            for ($probe = 1; $probe -le 10 -and -not $engineOk; $probe++) {
                Start-Sleep -Seconds 2
                $engineOk = Test-DockerEngineReady
            }
        }
        if ($engineOk) {
            if (-not (Test-DockerComposeReady)) {
                Write-Stage 'precheck_docker' 'failed' -1 'Docker Compose 插件不可用' 'E_DOCKER_COMPOSE'
                $precheckFailed = $true
            } else {
                Write-Stage 'precheck_docker' 'success' 100 'Docker Engine 运行正常'
            }
        } else {
            Write-Stage 'precheck_docker' 'failed' -1 '检测到 Docker Desktop，但 Docker Engine 当前不可用' 'E_DOCKER_ENGINE_NOT_READY'
            $precheckFailed = $true
        }
    }

    # Row 2: WSL 2
    Write-Stage 'precheck_wsl' 'running' 5 '正在检测 WSL 2...'
    if (Test-WslAvailable) {
        Write-Stage 'precheck_wsl' 'success' 100 'WSL 2 已就绪'
    } elseif (Test-RebootPending) {
        Write-Stage 'precheck_wsl' 'warning' 100 'Windows 需要重新启动后 WSL 2 才能使用' 'E_WSL_RESTART_REQUIRED'
        $precheckRestart = $true
    } else {
        Write-Stage 'precheck_wsl' 'warning' 100 'WSL 2 尚未配置，安装时会自动准备' 'E_WSL_NOT_AVAILABLE'
    }

    # Row 3: package resources
    Write-Stage 'precheck_resources' 'running' 5 '正在校验安装包...'
    $packageOk = (Test-Path -LiteralPath $AppRoot -PathType Container) -and
                 (Test-Path -LiteralPath $ComposeFile -PathType Leaf) -and
                 (Test-Path -LiteralPath $EnvExample -PathType Leaf)
    $offlineBundlePresent = $false
    $offlineArchiveCount = 0
    $offlineMissing = @()
    if (Test-Path -LiteralPath $OfflineImageManifest -PathType Leaf) {
        try {
            $offlineManifest = Get-Content -LiteralPath $OfflineImageManifest -Raw | ConvertFrom-Json
            $imagesRoot = Split-Path -Parent $OfflineImageManifest
            foreach ($entry in @($offlineManifest.images)) {
                $archivePath = Join-Path $imagesRoot ([string]$entry.archive)
                if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
                    $offlineArchiveCount++
                } else {
                    $offlineMissing += [string]$entry.archive
                }
            }
            $offlineBundlePresent = ($offlineMissing.Count -eq 0 -and $offlineArchiveCount -gt 0)
        } catch {
            $offlineBundlePresent = $false
        }
    }
    if (-not $packageOk) {
        Write-Stage 'precheck_resources' 'failed' 100 '安装包内容不完整（缺少 app 目录、docker-compose.yml 或 .env.example）' 'E_INSTALL_PACKAGE_MISSING'
        $precheckFailed = $true
    } elseif ($offlineBundlePresent) {
        Write-Stage 'precheck_resources' 'success' 100 "安装包完整，已包含离线镜像 $offlineArchiveCount 份"
    } elseif ($offlineMissing.Count -gt 0) {
        Write-Stage 'precheck_resources' 'failed' 100 "离线镜像归档缺失：$($offlineMissing -join '、')" 'E_INSTALL_PACKAGE_MISSING'
        $precheckFailed = $true
    } else {
        Write-Stage 'precheck_resources' 'success' 100 '标准安装包完整（未包含离线镜像归档）'
    }

    # Row 4: network (mode-aware; a full offline bundle needs no network)
    $packageEnv = Get-EnvMap $EnvFile
    if ($packageEnv.Count -eq 0) { $packageEnv = Get-EnvMap $EnvExample }
    $deployMode = "$($packageEnv['XR_DEPLOY_MODE'])".Trim().ToLowerInvariant()
    if (-not $deployMode) { $deployMode = 'local' }
    Write-Meta 'deploy_mode' $(if ($offlineBundlePresent) { 'offline' } else { $deployMode })
    if ($offlineBundlePresent -and $deployMode -in @('', 'local', 'offline')) {
        Write-Stage 'precheck_network' 'skipped' 100 '本次安装无需网络'
    } else {
        Write-Stage 'precheck_network' 'running' 5 '正在检测网络服务...'
        $updateServerUrl = "$($packageEnv['UPDATE_MANIFEST_URL'])".Trim()
        if (-not $updateServerUrl) { $updateServerUrl = 'https://www.gemstory.cn/release/xianyu/latest.json' }
        $updateServerOk = $false
        try {
            Invoke-WebRequest -UseBasicParsing -Method Head -Uri $updateServerUrl -TimeoutSec 8 | Out-Null
            $updateServerOk = $true
        } catch { }
        $registryHost = ''
        $registryOk = $true
        if ($deployMode -eq 'remote') {
            $registryHost = "$($packageEnv['XR_IMAGE_REGISTRY'])".Trim()
            if ($registryHost) {
                try {
                    Invoke-WebRequest -UseBasicParsing -Method Head -Uri "https://$registryHost/v2/" -TimeoutSec 8 | Out-Null
                } catch { $registryOk = $false }
            } else { $registryOk = $false }
        } else {
            $registryHost = 'registry-1.docker.io'
            $registryOk = $false
            $tcp = New-Object Net.Sockets.TcpClient
            try {
                $task = $tcp.ConnectAsync($registryHost, 443)
                $registryOk = $task.Wait(5000) -and $tcp.Connected
            } catch { $registryOk = $false } finally { $tcp.Dispose() }
        }
        if (-not $updateServerOk -and -not $registryOk) {
            Write-Stage 'precheck_network' 'failed' 100 '无法连接更新服务器和镜像源，请检查网络后重试' 'E_NETWORK_UPDATE_SERVER'
            $precheckFailed = $true
        } elseif (-not $updateServerOk) {
            Write-Stage 'precheck_network' 'warning' 100 '镜像源可访问，但更新服务器暂不可达（不影响安装，影响后续自动更新）' 'E_NETWORK_UPDATE_SERVER'
        } elseif (-not $registryOk) {
            Write-Stage 'precheck_network' 'failed' 100 "无法连接镜像源 $registryHost" 'E_NETWORK_REGISTRY'
            $precheckFailed = $true
        } else {
            $detail = if ($deployMode -eq 'remote') { "更新与镜像服务器 $registryHost 可访问" } else { '更新服务器与镜像源可访问' }
            Write-Stage 'precheck_network' 'success' 100 $detail
        }
    }

    if ($precheckRestart) {
        Write-Result 'restart_required' 'WSL 2 组件已启用，需要重启 Windows 后继续。' 'E_WSL_RESTART_REQUIRED'
        exit 3010
    }
    if ($precheckFailed) {
        Write-Result 'failed' '环境检测未通过，请先处理标记为失败的项目。' 'E_PRECHECK'
        exit 1
    }
    Write-Result 'completed' '环境检测通过'
    exit 0
}

# ---------------------------------------------------------------------------
# Mode: shortcut maintenance only (used by the completion page checkbox).
# ---------------------------------------------------------------------------
if ($ShortcutOnly) {
    $folderName = Split-Path -Leaf $PackageRoot
    $safeName = ($folderName.ToLower() -replace '[^a-z0-9]+', '-') -replace '(^-+|-+$)', ''
    if ([string]::IsNullOrWhiteSpace($safeName)) { $safeName = 'xianyu-installer' }
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shortcutTitle = -join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))
    $shortcutPath = Join-Path $desktop "$shortcutTitle.lnk"
    $iconPath = Join-Path $PackageRoot 'xianyu-launcher.ico'
    if (-not (Test-Path -LiteralPath $iconPath)) { $iconPath = Join-Path $PackageRoot 'assets\xianyu-launcher.ico' }
    if (-not $CreateDesktopShortcut) {
        if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath -Force }
        Write-Host '[xianyu] Desktop shortcut removed.'
        exit 0
    }
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $launcherPath = Join-Path $PackageRoot "$shortcutTitle.exe"
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
    Write-Host '[xianyu] Desktop shortcut created.'
    exit 0
}

# ---------------------------------------------------------------------------
# Mode: full installation.
# ---------------------------------------------------------------------------
if (-not [string]::IsNullOrWhiteSpace($InstallPath)) {
    $targetPackageRoot = [IO.Path]::GetFullPath($InstallPath).TrimEnd('\')
    if ($targetPackageRoot -ne $sourcePackageRoot) {
        if ($targetPackageRoot.StartsWith($sourcePackageRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or $sourcePackageRoot.StartsWith($targetPackageRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'The selected installation folder cannot contain or be contained by the current package folder.'
        }
        New-Item -ItemType Directory -Path $targetPackageRoot -Force | Out-Null
        Copy-PackageContents -Source $sourcePackageRoot -Destination $targetPackageRoot
        $PackageRoot = $targetPackageRoot
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
    }
}

if (Test-Path -LiteralPath $ErrorHelper) { . $ErrorHelper }
$LogPath = if (Get-Command Start-XianyuLogSession -ErrorAction SilentlyContinue) { Start-XianyuLogSession -ProjectRoot $AppRoot -Name 'install' } else { '' }

function Fail([string]$Message, [string]$Code = 'E_INSTALL') {
    throw (New-Object System.Management.Automation.PSInvalidOperationException("$Code|$Message"))
}

function Get-FailCode([System.Management.Automation.ErrorRecord]$ErrorRecord) {
    $text = "$($ErrorRecord.Exception.Message)"
    $match = [regex]::Match($text, '^E_[A-Z0-9_]+\|')
    if ($match.Success) { return $text.Substring(2, $text.IndexOf('|') - 2) }
    return 'E_INSTALL'
}

trap {
    $record = $_
    if ($script:GuiProtocolEnabled) {
        try {
            $failCode = Get-FailCode $record
            Write-Stage $script:CurrentStageId 'failed' -1 "$($record.Exception.Message)" $failCode
            Write-Result 'failed' "$($record.Exception.Message)" $failCode
        } catch { }
    }
    if (Get-Command Complete-XianyuFailure -ErrorAction SilentlyContinue) {
        Complete-XianyuFailure -Context 'Installation failed. The window will remain open until you close it.' -ErrorRecord $record -LogPath $LogPath
    }
    exit 1
}

if (-not (Test-Path -LiteralPath $AppRoot)) { Fail 'The package is incomplete: app directory is missing.' 'E_INSTALL_PACKAGE_MISSING' }
if (-not (Test-Path -LiteralPath $ComposeFile)) { Fail 'The package is incomplete: docker-compose.yml is missing.' 'E_INSTALL_PACKAGE_MISSING' }
if (-not (Test-Path -LiteralPath $EnvExample)) { Fail 'The package is incomplete: .env.example is missing.' 'E_INSTALL_PACKAGE_MISSING' }

# ---- Stage 1: system environment ------------------------------------------
$script:CurrentStageId = 'env'
Write-Stage 'env' 'running' 2 '正在准备系统环境...'
if (Test-Path -LiteralPath $WslBootstrap) {
    powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File $WslBootstrap -NonInteractive -TargetUserProfile $env:USERPROFILE
    $wslExitCode = $LASTEXITCODE
    if ($wslExitCode -eq 3010) {
        Write-Host '[xianyu] WSL setup needs a Windows restart before installation can continue.' -ForegroundColor Yellow
        Write-Stage 'env' 'warning' -1 'WSL 2 组件已启用，需要重启 Windows' 'E_WSL_RESTART_REQUIRED'
        Write-Result 'restart_required' 'WSL 2 组件已启用，需要重启 Windows 后继续安装。' 'E_WSL_RESTART_REQUIRED'
        Stop-XianyuLogSession
        exit 3010
    }
    if ($wslExitCode -ne 0) { Fail "WSL preparation failed with exit code $wslExitCode." 'E_WSL_NOT_AVAILABLE' }
}
if (Test-Path -LiteralPath $RdpCleanup) {
    try { & $RdpCleanup -NonInteractive } catch { Write-Host "[xianyu] RDP residue cleanup skipped: $($_.Exception.Message)" -ForegroundColor Yellow }
}
Write-Stage 'env' 'running' 5 '正在安装或启动 Docker Desktop...'
try { & $DockerBootstrap -Install -NonInteractive } catch { Fail $_.Exception.Message 'E_DOCKER_NOT_INSTALLED' }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker CLI is unavailable. Start Docker Desktop and run install.bat again.' 'E_DOCKER_NOT_INSTALLED' }
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
    Fail "Docker Compose is unavailable: $composeLastError" 'E_DOCKER_COMPOSE'
}
Write-Stage 'env' 'running' 8 '正在等待 Docker Engine 上线...'
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
    Fail "Docker Desktop engine is not ready after 60 seconds: $dockerLastError" 'E_DOCKER_ENGINE_NOT_READY'
}
Write-Stage 'env' 'success' 10 '运行环境已就绪'

# ---- Stage 2: application configuration ------------------------------------
$script:CurrentStageId = 'config'
Write-Stage 'config' 'running' 12 '正在创建应用配置...'
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
Write-Stage 'config' 'success' 20 '应用配置已生成'

# ---- Stage 3+4: images ------------------------------------------------------
# A full offline package carries the exact image set used by this release.
# Import it before Compose starts so a first install never needs Docker Hub.
$deployMode = "$($envMap['XR_DEPLOY_MODE'])".Trim().ToLowerInvariant()
$offlinePackageAvailable = Test-Path -LiteralPath $OfflineImageManifest
$useOfflineImages = $offlinePackageAvailable -and ($newEnv -or $deployMode -in @('', 'local', 'offline'))
if ($useOfflineImages) {
    if (-not (Test-Path -LiteralPath $OfflineImageImporter)) {
        Fail 'Offline image manifest exists but the image importer is missing from the package.' 'E_INSTALL_PACKAGE_MISSING'
    }
    Write-Host '[xianyu] Offline image bundle detected. Docker image downloads will be skipped.' -ForegroundColor Cyan
    $script:CurrentStageId = 'images_base'
    Write-Stage 'images_base' 'running' 25 '正在导入基础镜像...'
    & $OfflineImageImporter -PackageRoot $PackageRoot
    $offlineImportSucceeded = $?
    $offlineImportExitCode = $LASTEXITCODE
    if (-not $offlineImportSucceeded) {
        if ($null -eq $offlineImportExitCode) { $offlineImportExitCode = 'unknown' }
        Fail "Offline image import failed with exit code $offlineImportExitCode." 'E_INSTALL_IMAGE_IMPORT'
    }
    $offlineManifest = Get-Content -LiteralPath $OfflineImageManifest -Raw | ConvertFrom-Json
    if ([string]::IsNullOrWhiteSpace("$($offlineManifest.image_tag)")) {
        Fail 'Offline image manifest does not contain image_tag.' 'E_INSTALL_IMAGE_IMPORT'
    }
    Set-EnvValue $EnvFile 'XR_DEPLOY_MODE' 'offline'
    Set-EnvValue $EnvFile 'XR_IMAGE_REGISTRY' 'local'
    Set-EnvValue $EnvFile 'XR_IMAGE_NAMESPACE' 'xianyu'
    Set-EnvValue $EnvFile 'XR_IMAGE_TAG' "$($offlineManifest.image_tag)"
    $envMap = Get-EnvMap $EnvFile
}

# ---- (Ports/prefix config already handled above) ---------------------------
Write-XianyuLog -LogPath $LogPath -Message "install_begin package_root=$PackageRoot new_environment=$newEnv"
Write-Host "[xianyu] Package root: $PackageRoot" -ForegroundColor Cyan
Write-Host "[xianyu] Frontend: http://127.0.0.1:$($envMap['FRONTEND_PORT'])" -ForegroundColor Cyan
Write-Host '[xianyu] Old port 19000 is not used.' -ForegroundColor Cyan

docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile config *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose configuration validation failed.' 'E_INSTALL_CONFIG' }
$envMap = Get-EnvMap $EnvFile
$deployMode = "$($envMap['XR_DEPLOY_MODE'])".Trim().ToLowerInvariant()
if ($useOfflineImages) {
    Write-Stage 'images_app' 'running' 40 '正在校验业务镜像...'
    Write-Stage 'images_app' 'success' 65 '业务镜像已就绪'
} elseif ($deployMode -eq 'remote') {
    if ([string]::IsNullOrWhiteSpace("$($envMap['XR_IMAGE_REGISTRY'])") -or [string]::IsNullOrWhiteSpace("$($envMap['XR_IMAGE_NAMESPACE'])") -or [string]::IsNullOrWhiteSpace("$($envMap['XR_IMAGE_TAG'])")) {
        Fail 'Remote mode requires XR_IMAGE_REGISTRY, XR_IMAGE_NAMESPACE and XR_IMAGE_TAG in app\.env.' 'E_INSTALL_CONFIG'
    }
    Write-Stage 'images_base' 'skipped' 25 '远程模式无需导入基础镜像'
    $script:CurrentStageId = 'images_app'
    Write-Stage 'images_app' 'running' -1 '正在拉取远程服务镜像...'
    Write-Host '[xianyu] Pulling remote application images.' -ForegroundColor Cyan
    docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile pull
    if ($LASTEXITCODE -ne 0) { Fail 'Remote image pull failed. Check the image registry address, credentials and network.' 'E_NETWORK_REGISTRY' }
    Write-Stage 'images_app' 'success' 65 '远程服务镜像已就绪'
} else {
    Write-Stage 'images_base' 'running' 25 '正在拉取基础镜像...'
    Write-Host '[xianyu] Checking and preloading Docker base images.' -ForegroundColor Cyan
    $baseImages = @('python:3.11-slim', 'mysql:8.0', 'redis:7-alpine', 'node:20-alpine', 'nginx:alpine')
    $baseIndex = 0
    foreach ($baseImage in $baseImages) {
        $baseIndex++
        Write-Stage 'images_base' 'running' -1 "正在拉取基础镜像 $baseImage（$baseIndex / $($baseImages.Count)）"
        $pulled = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            Write-Host "[xianyu] Pulling $baseImage (attempt $attempt/3)." -ForegroundColor DarkCyan
            docker pull $baseImage
            if ($LASTEXITCODE -eq 0) { $pulled = $true; break }
            Start-Sleep -Seconds 5
        }
        if (-not $pulled) {
            Fail "Cannot pull $baseImage from Docker Hub. Configure Docker Desktop proxy/network access, or set XR_DEPLOY_MODE=remote with Tencent registry image settings in app\.env." 'E_NETWORK_REGISTRY'
        }
    }
    Write-Stage 'images_base' 'success' 45 '基础镜像已就绪'
    $script:CurrentStageId = 'images_app'
    Write-Stage 'images_app' 'running' -1 '正在构建业务镜像并启动服务...'
    Write-Host '[xianyu] Building and starting services. The first run may take several minutes.' -ForegroundColor Cyan
}
$envMap = Get-EnvMap $EnvFile

# ---- Stage 5: services -------------------------------------------------------
$script:CurrentStageId = 'start'
Write-Stage 'start' 'running' 70 '正在启动服务...'
if ($deployMode -eq 'offline') {
    Write-Host '[xianyu] Starting services from locally imported images.' -ForegroundColor Cyan
    docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile up -d --no-build --pull never
} elseif ($deployMode -eq 'remote') {
    docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile up -d --no-build
} else {
    docker compose --project-directory $AppRoot --env-file $EnvFile -f $ComposeFile up -d --build
}
if ($LASTEXITCODE -ne 0) { Fail 'Docker Compose failed to start the application.' 'E_INSTALL_SERVICE_START' }
Write-Stage 'start' 'success' 80 '服务已启动'

# ---- Stage 6: health ---------------------------------------------------------
$script:CurrentStageId = 'health'
Write-Stage 'health' 'running' 85 '正在检查前端服务...'
$frontendUrl = "http://127.0.0.1:$($envMap['FRONTEND_PORT'])"
$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    try {
        $response = Invoke-WebRequest -Uri $frontendUrl -UseBasicParsing -TimeoutSec 3
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) { $ready = $true; break }
    } catch { }
    if (-not $ready -and ($i % 10) -eq 9) { Write-Stage 'health' 'running' -1 "等待前端响应（$($i + 1) / 90）" }
}
if (-not $ready) {
    Write-Host '[xianyu] WARNING: frontend did not respond within the wait period.' -ForegroundColor Yellow
    Write-Stage 'health' 'warning' 95 '前端未在预期时间内响应，可稍后在控制台查看服务状态' 'E_INSTALL_HEALTH'
} else {
    Write-Stage 'health' 'success' 100 '前端服务响应正常'
}

# ---- Desktop shortcut --------------------------------------------------------
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutTitle = -join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))
$shortcutPath = Join-Path $desktop "$shortcutTitle.lnk"
$launcherPath = Join-Path $PackageRoot "$shortcutTitle.exe"
$oldShortcutPath = Join-Path $desktop 'Xianyu System.lnk'
$iconPath = Join-Path $PackageRoot 'xianyu-launcher.ico'
if (-not (Test-Path -LiteralPath $iconPath)) {
    $iconPath = Join-Path $PackageRoot 'assets\xianyu-launcher.ico'
}
$shortcutCreated = $false
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
if ($CreateDesktopShortcut) {
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
    $shortcutCreated = $true
}
Write-Meta 'shortcut_created' $(if ($shortcutCreated) { 'true' } else { 'false' })

Write-Host '[xianyu] Installation completed.' -ForegroundColor Green
Write-Host "[xianyu] Open: $frontendUrl" -ForegroundColor Green
Write-XianyuLog -LogPath $LogPath -Message "install_completed frontend_url=$frontendUrl"
Write-Meta 'frontend_url' $frontendUrl
Write-Meta 'install_path' $PackageRoot
Write-Result 'completed' '闲鱼管理系统已成功安装'
Stop-XianyuLogSession
if (-not $NoOpenBrowser) { Start-Process $frontendUrl }
exit 0
