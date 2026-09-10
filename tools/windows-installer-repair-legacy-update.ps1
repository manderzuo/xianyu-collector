param(
    [string]$PackageRoot = '',
    [string]$ManifestUrl = 'https://www.gemstory.cn/release/xianyu/latest.json',
    [string]$MinimumVersion = '1.2.0',
    [switch]$Preview,
    [switch]$RestartLauncher,
    [switch]$PauseOnError
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Resolve-RepairPackageRoot {
    param([string]$RequestedRoot)

    if (-not [string]::IsNullOrWhiteSpace($RequestedRoot)) {
        return [IO.Path]::GetFullPath($RequestedRoot).TrimEnd('\')
    }

    $scriptDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path.TrimEnd('\')
    $candidates = @(
        $scriptDirectory,
        (Split-Path -Parent $scriptDirectory),
        (Split-Path -Parent (Split-Path -Parent $scriptDirectory))
    )
    foreach ($candidate in $candidates) {
        if ((Test-Path -LiteralPath (Join-Path $candidate 'app\docker-compose.yml')) -and
            (Test-Path -LiteralPath (Join-Path $candidate 'app\.env'))) {
            return ([IO.Path]::GetFullPath($candidate)).TrimEnd('\')
        }
    }
    throw '无法自动定位安装目录。请使用 -PackageRoot "D:\xianyu2.0" 指定包含 app\docker-compose.yml 和 app\.env 的目录。'
}

$ResolvedPackageRoot = Resolve-RepairPackageRoot $PackageRoot
$AppRoot = Join-Path $ResolvedPackageRoot 'app'
$LogRoot = Join-Path $AppRoot 'logs'
$LogFile = Join-Path $LogRoot 'legacy-update-repair.log'
$PendingRoot = Join-Path $AppRoot 'updates\pending'
$ApplyScript = Join-Path $ResolvedPackageRoot 'scripts\apply-client-update.ps1'
$PublicKeyPath = Join-Path $AppRoot 'deploy\update-signing-public-key.xml'

function Write-RepairLog([string]$Message) {
    if (-not (Test-Path -LiteralPath $LogRoot)) {
        New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    }
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') [legacy-repair] $Message"
    for ($attempt = 1; $attempt -le 8; $attempt++) {
        $stream = $null
        $writer = $null
        try {
            $stream = New-Object System.IO.FileStream(
                $LogFile,
                [IO.FileMode]::OpenOrCreate,
                [IO.FileAccess]::Write,
                [IO.FileShare]::ReadWrite
            )
            $stream.Seek(0, [IO.SeekOrigin]::End) | Out-Null
            $writer = New-Object System.IO.StreamWriter($stream, (New-Object Text.UTF8Encoding($false)))
            $writer.WriteLine($line)
            $writer.Flush()
            $writer.Dispose()
            $stream.Dispose()
            return
        } catch {
            if ($writer) { $writer.Dispose() }
            if ($stream) { $stream.Dispose() }
            if ($attempt -eq 8) { throw }
            Start-Sleep -Milliseconds (100 * $attempt)
        }
    }
}

function Write-RepairMessage([string]$Message, [ConsoleColor]$Color = [ConsoleColor]::Cyan) {
    Write-Host "[xianyu] $Message" -ForegroundColor $Color
    Write-RepairLog $Message
}

function Test-RequiredInstallationFiles {
    foreach ($path in @(
        (Join-Path $AppRoot 'docker-compose.yml'),
        (Join-Path $AppRoot '.env'),
        $ApplyScript,
        $PublicKeyPath
    )) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "安装目录缺少必要文件：$path"
        }
    }
}

function Get-VersionParts([string]$Value) {
    if ($Value -notmatch '^\d+(?:\.\d+){1,3}$') { throw "版本号格式无效：$Value" }
    return @($Value.Split('.') | ForEach-Object { [int]$_ })
}

function Compare-Version([string]$Left, [string]$Right) {
    $leftParts = @(Get-VersionParts $Left)
    $rightParts = @(Get-VersionParts $Right)
    for ($index = 0; $index -lt 4; $index++) {
        $leftValue = if ($index -lt $leftParts.Count) { $leftParts[$index] } else { 0 }
        $rightValue = if ($index -lt $rightParts.Count) { $rightParts[$index] } else { 0 }
        if ($leftValue -gt $rightValue) { return 1 }
        if ($leftValue -lt $rightValue) { return -1 }
    }
    return 0
}

function Get-UriOrThrow([string]$Value, [string]$Label) {
    try { $uri = [Uri]$Value } catch { throw "$Label 地址格式无效：$Value" }
    if ($uri.Scheme -ne 'https' -or -not $uri.Host -or $uri.UserInfo) {
        throw "$Label 必须是安全的 HTTPS 地址：$Value"
    }
    return $uri
}

function Download-File([string]$Url, [string]$Path, [string]$Label, [int]$TimeoutSeconds = 120) {
    $uri = Get-UriOrThrow $Url $Label
    Write-RepairMessage "下载 $Label：$Url"
    Invoke-WebRequest -UseBasicParsing -Uri $uri.AbsoluteUri -TimeoutSec $TimeoutSeconds -OutFile $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf) -or (Get-Item -LiteralPath $Path).Length -le 0) {
        throw "$Label 下载结果为空"
    }
}

function Verify-ManifestSignature([byte[]]$ManifestBytes, [string]$ManifestAddress, [pscustomobject]$Manifest, [string]$TempRoot) {
    if (-not (Test-Path -LiteralPath $PublicKeyPath -PathType Leaf)) {
        throw "更新签名公钥不存在：$PublicKeyPath"
    }

    $declaredUrl = "$($Manifest.signature.url)".Trim()
    if (-not $declaredUrl) {
        $declaredUrl = [regex]::Replace($ManifestAddress, '(?i)\.json(?=$|\?)', '.json.sig')
    }
    $signaturePath = Join-Path $TempRoot 'latest.json.sig'
    Download-File $declaredUrl $signaturePath '更新签名' 120
    $signatureText = ([IO.File]::ReadAllText($signaturePath, [Text.Encoding]::UTF8) -replace '\s', '')
    try { $signatureBytes = [Convert]::FromBase64String($signatureText) } catch { throw '更新签名不是有效的 Base64 内容' }

    $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
    $hashAlgorithm = New-Object Security.Cryptography.SHA256CryptoServiceProvider
    try {
        $rsa.FromXmlString([IO.File]::ReadAllText($PublicKeyPath, [Text.Encoding]::UTF8))
        if (-not $rsa.VerifyData($ManifestBytes, $hashAlgorithm, $signatureBytes)) {
            throw '更新清单签名校验失败，已拒绝本次修复'
        }
    } finally {
        $hashAlgorithm.Dispose()
        $rsa.Dispose()
    }
    Write-RepairMessage '更新清单签名校验通过。' Green
}

function Stop-LegacyUpdaterProcesses {
    $rootPattern = [regex]::Escape($ResolvedPackageRoot.TrimEnd('\'))
    $processes = @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ProcessId -ne $PID -and
                $_.CommandLine -and
                $_.CommandLine -match $rootPattern -and
                (
                    ($_.Name -match '^(?i:powershell|pwsh)\.exe$' -and
                        $_.CommandLine -match '(?i)(scripts[\\/]update\.ps1|update-xianyu-gui\.ps1|check-xianyu-update\.ps1)') -or
                    ($_.Name -ieq 'docker.exe' -and $_.CommandLine -match '(?i)compose.*\bpull\b')
                )
            }
    )
    foreach ($processInfo in $processes) {
        try {
            Stop-Process -Id ([int]$processInfo.ProcessId) -Force -ErrorAction Stop
            Write-RepairMessage "已停止旧更新进程 PID=$($processInfo.ProcessId)。" Yellow
        } catch {
            Write-RepairLog "stop_legacy_updater_failed pid=$($processInfo.ProcessId) error=$($_.Exception.Message)"
        }
    }
    if ($processes.Count -eq 0) { Write-RepairLog 'stop_legacy_updater count=0' }
}

function Read-PackageManifest([string]$ArchivePath, [string]$TempRoot) {
    $extractRoot = Join-Path $TempRoot 'package'
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $extractRoot -Force
    $packageManifestPath = Join-Path $extractRoot 'package-manifest.json'
    if (-not (Test-Path -LiteralPath $packageManifestPath -PathType Leaf)) {
        throw '客户端维护包缺少 package-manifest.json'
    }
    $packageManifest = Get-Content -LiteralPath $packageManifestPath -Raw | ConvertFrom-Json
    foreach ($required in @('xianyu-launcher.exe', 'xianyu-updater.exe', 'app\deploy\update-xianyu-gui.ps1')) {
        if (-not (Test-Path -LiteralPath (Join-Path $extractRoot $required) -PathType Leaf)) {
            throw "客户端维护包缺少必要文件：$required"
        }
    }
    return $packageManifest
}

function Invoke-ClientApply([string]$ArchivePath) {
    New-Item -ItemType Directory -Path $PendingRoot -Force | Out-Null
    $targetArchive = Join-Path $PendingRoot ([IO.Path]::GetFileName($ArchivePath))
    Copy-Item -LiteralPath $ArchivePath -Destination $targetArchive -Force
    Write-RepairMessage '客户端维护包已放入待应用目录。开始替换更新器文件。'

    Stop-LegacyUpdaterProcesses
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $ApplyScript -PackageRoot $ResolvedPackageRoot
    $applyExitCode = $LASTEXITCODE
    if ($applyExitCode -ne 0) { throw "客户端维护包应用失败，退出码：$applyExitCode" }

    $remaining = @(Get-ChildItem -LiteralPath $PendingRoot -Filter '*.zip' -File -ErrorAction SilentlyContinue)
    if ($remaining.Count -gt 0) {
        throw "客户端维护包未应用成功，待处理文件仍存在：$($remaining[0].Name)"
    }
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('xianyu-legacy-repair-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$success = $false
try {
    Test-RequiredInstallationFiles
    Write-RepairMessage "目标安装目录：$ResolvedPackageRoot"
    Write-RepairMessage '本脚本只更新客户端维护文件，不执行 docker compose pull。'

    $manifestPath = Join-Path $tempRoot 'latest.json'
    Download-File $ManifestUrl $manifestPath '更新清单' 120
    $manifestBytes = [IO.File]::ReadAllBytes($manifestPath)
    $manifest = ([Text.Encoding]::UTF8.GetString($manifestBytes) | ConvertFrom-Json)
    $latestVersion = "$($manifest.version)".Trim()
    $latestBuild = "$($manifest.build_id)".Trim()
    [void](Get-VersionParts $latestVersion)
    if ((Compare-Version $latestVersion $MinimumVersion) -lt 0) {
        throw "线上版本 $latestVersion 低于要求的桥接版本 $MinimumVersion"
    }
    $packageUrl = "$($manifest.client.package_url)".Trim()
    $expectedHash = "$($manifest.client.package_sha256)".Trim().ToLowerInvariant()
    if (-not $packageUrl -or $expectedHash -notmatch '^[0-9a-f]{64}$') {
        throw '更新清单缺少有效的客户端维护包地址或 SHA-256'
    }

    Verify-ManifestSignature $manifestBytes $ManifestUrl $manifest $tempRoot
    Write-RepairMessage "线上客户端维护版本：$latestVersion（$latestBuild）" Green
    if ($Preview) {
        Write-RepairMessage '预览模式完成：未下载、未替换文件，也未操作 Docker。' Yellow
        $success = $true
        return
    }

    $archivePath = Join-Path $tempRoot 'client.zip'
    Download-File $packageUrl $archivePath '客户端维护包' 900
    $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $expectedHash) {
        throw "客户端维护包 SHA-256 校验失败：实际=$actualHash，期望=$expectedHash"
    }
    Write-RepairMessage "客户端维护包校验通过，大小=$((Get-Item -LiteralPath $archivePath).Length) 字节。" Green
    $packageManifest = Read-PackageManifest $archivePath $tempRoot
    if ("$($packageManifest.version)".Trim() -ne $latestVersion) {
        throw '客户端维护包版本与线上清单不一致'
    }

    Invoke-ClientApply $archivePath

    # Force the new updater to perform the deferred per-service image sync on
    # its next start. This marker is intentionally written after client apply
    # so an old updater can never consume it and run a full compose pull.
    $runtimeMarker = [ordered]@{
        protocol = 1
        version = $latestVersion
        build_id = $latestBuild
        reason = 'legacy-client-first-repair'
        created_at = [DateTime]::UtcNow.ToString('o')
    }
    $runtimeMarker | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $AppRoot 'runtime-sync.pending.json') -Encoding UTF8
    Write-RepairMessage '客户端更新器已替换，已登记下一次启动的逐服务镜像校准。' Green
    Write-RepairMessage '请重新打开最新版启动器；新更新器不会拉取 MySQL/Redis，只会按摘要同步业务镜像。' Cyan

    if ($RestartLauncher) {
        $launcher = Join-Path $ResolvedPackageRoot 'xianyu-launcher.exe'
        if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { $launcher = Join-Path $ResolvedPackageRoot '闲鱼管理系统.exe' }
        if (Test-Path -LiteralPath $launcher -PathType Leaf) {
            Start-Process -FilePath $launcher -WorkingDirectory $ResolvedPackageRoot -WindowStyle Normal | Out-Null
            Write-RepairMessage '最新版启动器已重新启动。' Green
        } else {
            Write-RepairMessage '未找到启动器，请手动双击安装目录中的新版启动器。' Yellow
        }
    }
    $success = $true
} catch {
    $detail = $_.Exception.ToString()
    Write-RepairLog "repair_failed error=$detail"
    Write-Host ''
    Write-Host '修复失败，现有容器和数据库未被本脚本修改。' -ForegroundColor Red
    Write-Host "详细日志：$LogFile" -ForegroundColor Yellow
    Write-Host $detail -ForegroundColor Red
    if ($PauseOnError) {
        Read-Host '按回车键关闭窗口'
    }
    exit 1
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

if ($success) {
    Write-RepairLog 'repair_completed'
    exit 0
}
