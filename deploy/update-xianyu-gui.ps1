param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$Force,
    [switch]$Headless,
    [switch]$CheckOnly
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

try {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [Console]::OutputEncoding = $utf8NoBom
    $global:OutputEncoding = $utf8NoBom
} catch { }

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
    function Get-FileSha256([string]$Path) {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($Path))) -replace '-', '').ToLowerInvariant()
        } finally { $sha.Dispose() }
    }
    $compose = Join-Path $Root 'docker-compose.yml'
    $envFile = Join-Path $Root '.env'
    $versionFile = Join-Path $Root 'VERSION.txt'
    $buildFile = Join-Path $Root 'BUILD_ID.txt'
    $packageRoot = Split-Path -Parent $Root
    $launcherFile = Join-Path $packageRoot 'xianyu-launcher.exe'
    $frontendIndexFile = Join-Path $Root 'frontend\dist\index.html'
    $logDir = Join-Path $Root 'logs'
    $logFile = Join-Path $logDir 'update.log'
    $runtimeSyncMarker = Join-Path $Root 'runtime-sync.pending.json'
    $runtimeOfflineStateFile = Join-Path $Root 'runtime-offline-state.json'
    if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

    function Send-Message([string]$Kind, [string]$Message = '', [int]$Percent = -1, [string]$Data = '') {
        [pscustomobject]@{ Kind = $Kind; Message = $Message; Percent = $Percent; Data = $Data }
    }

    # Structured GUI protocol records. The C# launcher renders these verbatim
    # (type/stage/status/detail/code). They travel on the information stream so
    # they never contaminate function return values or the legacy Send-Message
    # contract that the in-process PowerShell dialog still relies on.
    $script:UpdateStage = 'fetch'
    function Emit-GuiEvent([string]$Json) {
        Write-Information -MessageData ([pscustomobject]@{ Kind = 'uievent'; Json = $Json }) -Tags 'XianyuGuiEvent' -InformationAction Continue
    }
    function Send-Stage([string]$Stage, [string]$Status, [string]$Detail = '', [string]$Code = '') {
        if ($Status -eq 'running') { $script:UpdateStage = $Stage }
        $payload = [ordered]@{ v = 1; type = 'stage'; operation = 'update'; stage = $Stage; status = $Status; progress = -1; detail = $Detail; code = $Code }
        Emit-GuiEvent ($payload | ConvertTo-Json -Compress)
    }
    function Send-Meta([string]$Key, [string]$Value) {
        $payload = [ordered]@{ v = 1; type = 'meta'; key = $Key; value = "$Value" }
        Emit-GuiEvent ($payload | ConvertTo-Json -Compress)
    }
    function Send-Result([string]$Status, [string]$Detail = '', [string]$Code = '') {
        $payload = [ordered]@{ v = 1; type = 'result'; operation = 'update'; status = $Status; detail = $Detail; code = $Code }
        Emit-GuiEvent ($payload | ConvertTo-Json -Compress)
    }
    function Get-FailureCode([string]$Stage, [string]$Message) {
        $text = "$Message"
        if ($text -match '签名') { return 'E_UPDATE_SIGNATURE' }
        switch ($Stage) {
            'fetch'   { if ($text -match '清单') { return 'E_UPDATE_MANIFEST' } return 'E_NETWORK_UPDATE_SERVER' }
            'prepare' { return 'E_UPDATE_MANIFEST' }
            'protect' { return 'E_ROLLBACK_FAILED' }
            'apply'   {
                if ($text -match 'SHA-256|下载|download') { return 'E_UPDATE_DOWNLOAD' }
                if ($text -match '客户端') { return 'E_UPDATE_CLIENT' }
                return 'E_UPDATE_IMAGE'
            }
            'restart' { return 'E_UPDATE_SERVICE_START' }
            'health'  { return 'E_UPDATE_HEALTH' }
            'rollback' { return 'E_ROLLBACK_FAILED' }
            default   { return 'E_UPDATE' }
        }
    }
    function Test-RollbackCapability([string]$Registry, [string]$Namespace, [string]$Tag) {
        if (-not (Test-Path -LiteralPath $envFile) -or -not (Test-Path -LiteralPath $compose)) { return 'unavailable:config_missing' }
        if (-not $Registry -or -not $Namespace -or -not $Tag) { return 'unavailable:previous_reference_unknown' }
        $missing = @()
        foreach ($service in @('backend', 'websocket', 'scheduler', 'frontend')) {
            $ref = '{0}/{1}/xianyu-{2}:{3}' -f $Registry, $Namespace, $service, $Tag
            if (-not (Get-LocalImageId $ref)) { $missing += $service }
        }
        if ($missing.Count -gt 0) { return ('unavailable:image_missing:' + ($missing -join ',')) }
        return 'available'
    }

    function Write-Detail([string]$Message) {
        $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') [$env:COMPUTERNAME] $Message"
        # Multiple updater/check processes and a live Get-Content monitor can
        # otherwise lock update.log. Logging must never abort an update.
        $written = $false
        for ($attempt = 1; $attempt -le 12 -and -not $written; $attempt++) {
            try {
                $bytes = [Text.Encoding]::UTF8.GetBytes($line + [Environment]::NewLine)
                $stream = [IO.File]::Open($logFile, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
                try {
                    $stream.Seek(0, [IO.SeekOrigin]::End) | Out-Null
                    $stream.Write($bytes, 0, $bytes.Length)
                    $stream.Flush()
                    $written = $true
                } finally { $stream.Dispose() }
            } catch {
                if ($attempt -lt 12) { Start-Sleep -Milliseconds 100 }
            }
        }
        if (-not $written) {
            try {
                $fallback = Join-Path $logDir ("update-$PID.log")
                Add-Content -LiteralPath $fallback -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
            } catch { }
        }
        # GUI log records must not use the success-output stream. Several
        # worker helpers return scalar values; success-stream log objects made
        # those return values arrays and caused valid signature URLs to be
        # rejected as mismatches. The information stream keeps the live GUI
        # log without contaminating function return values.
        Write-Information -MessageData ([pscustomobject]@{
            Kind = 'log'; Message = $line; Percent = -1; Data = ''
        }) -Tags 'XianyuGuiLog' -InformationAction Continue
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

    function New-UpdateTempDirectory([string]$Name) {
        $configured = ''
        try {
            $envValues = Get-EnvMap $envFile
            $configured = "$($envValues['XIANYU_UPDATE_TEMP_DIR'])".Trim()
        } catch { $configured = '' }
        $base = if ($configured) {
            if ([IO.Path]::IsPathRooted($configured)) { $configured } else { Join-Path $Root $configured }
        } else {
            Join-Path $Root 'updates\work'
        }
        New-Item -ItemType Directory -Path $base -Force | Out-Null
        $path = Join-Path $base ("xianyu-$Name-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $path -Force | Out-Null
        return $path
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

    function Test-ClientIntegrity($Manifest) {
        $expectedLauncher = "$($Manifest.client.launcher_sha256)".Trim().ToLowerInvariant()
        $expectedFrontend = "$($Manifest.client.frontend_index_sha256)".Trim().ToLowerInvariant()
        $launcherMismatch = $false
        $frontendMismatch = $false

        if ($expectedLauncher -match '^[0-9a-f]{64}$') {
            if (-not (Test-Path -LiteralPath $launcherFile -PathType Leaf)) {
                $launcherMismatch = $true
            } else {
                $actualLauncher = Get-FileSha256 $launcherFile
                $launcherMismatch = $actualLauncher -ne $expectedLauncher
            }
        }
        if ($expectedFrontend -match '^[0-9a-f]{64}$') {
            if (-not (Test-Path -LiteralPath $frontendIndexFile -PathType Leaf)) {
                $frontendMismatch = $true
            } else {
                $actualFrontend = Get-FileSha256 $frontendIndexFile
                $frontendMismatch = $actualFrontend -ne $expectedFrontend
            }
        }
        return [pscustomobject]@{
            LauncherMismatch = $launcherMismatch
            FrontendMismatch = $frontendMismatch
            RepairNeeded = $launcherMismatch -or $frontendMismatch
        }
    }

    function Get-OfflineState([string]$ExpectedVersion = '', [string]$ExpectedTag = '') {
        if (-not (Test-Path -LiteralPath $runtimeOfflineStateFile -PathType Leaf)) { return $null }
        try {
            $state = Get-Content -LiteralPath $runtimeOfflineStateFile -Raw | ConvertFrom-Json
            if ("$($state.source)".Trim() -ne 'offline-bundle') { return $null }
            if ($ExpectedVersion -and "$($state.version)".Trim() -ne $ExpectedVersion) { return $null }
            if ($ExpectedTag -and "$($state.image_tag)".Trim() -ne $ExpectedTag) { return $null }
            return $state
        } catch {
            Write-Detail "runtime_offline_state_invalid path=$runtimeOfflineStateFile reason=$($_.Exception.Message)"
            return $null
        }
    }

    function Get-OfflineStateDigest([string]$Service, [string]$Tag) {
        $state = Get-OfflineState '' $Tag
        if ($null -eq $state) { return '' }
        foreach ($entry in @($state.images)) {
            if ("$($entry.service)".Trim() -eq $Service) {
                $digest = "$($entry.remote_digest)".Trim().ToLowerInvariant()
                if ($digest -match '^sha256:[0-9a-f]{64}$') { return $digest }
            }
        }
        return ''
    }

    function Test-OfflineRuntimeState($Manifest, [string]$Version, [string]$Tag) {
        $state = Get-OfflineState $Version $Tag
        if ($null -eq $state) { return $false }
        $stateBuild = "$($state.build_id)".Trim()
        $manifestBuild = "$($Manifest.build_id)".Trim()
        if ($manifestBuild -and $stateBuild -and $manifestBuild -ne $stateBuild) { return $false }
        foreach ($service in @('backend', 'websocket', 'scheduler', 'frontend')) {
            $stateEntry = @($state.images | Where-Object { "$($_.service)".Trim() -eq $service }) | Select-Object -First 1
            if ($null -eq $stateEntry) { return $false }
            $localRef = "$($stateEntry.local_image)".Trim()
            if (-not $localRef) { $localRef = "local/xianyu/xianyu-${service}:${Tag}" }
            $expectedId = "$($stateEntry.image_id)".Trim()
            $actualId = Get-LocalImageId $localRef
            if (-not $actualId -or ($expectedId -and $actualId -ne $expectedId)) { return $false }
            $trustedDigest = "$($stateEntry.remote_digest)".Trim()
            Write-Detail "runtime_image_compare service=$service local_image_id=$actualId trusted_digest=$trustedDigest action=offline_match"
        }
        Write-Detail "runtime_offline_state_verified version=$Version tag=$Tag remote_validation=deferred"
        return $true
    }

    function Invoke-Docker([string[]]$Arguments, [string]$Label) {
        Write-Detail "docker_start label=$Label args=$($Arguments -join ' ')"
        # Docker Compose writes normal progress/status lines (for example
        # "Container ... Running") to stderr. Windows PowerShell turns those
        # lines into ErrorRecord objects and, with Stop enabled, may abort an
        # otherwise successful update. Capture them with Continue and decide
        # success exclusively from the native process exit code.
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = & docker compose --project-directory $Root --env-file $envFile -f $compose @Arguments 2>&1
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        foreach ($line in $output) { Write-Detail "docker_output label=$Label text=$line" }
        Write-Detail "docker_end label=$Label exit_code=$exitCode"
        if ($exitCode -ne 0) { throw "Docker 操作失败：$Label（退出码 $exitCode）" }
    }

    function Test-RuntimeManifestImages($Manifest, [string]$Registry, [string]$Namespace, [string]$Tag) {
        if ($null -eq $Manifest.images) { throw '更新清单缺少运行时镜像列表' }
        foreach ($service in @('backend', 'websocket', 'scheduler', 'frontend')) {
            $property = $Manifest.images.PSObject.Properties[$service]
            $declared = if ($null -ne $property) { "$($property.Value)".Trim() } else { '' }
            $expected = '{0}/{1}/xianyu-{2}:{3}' -f $Registry, $Namespace, $service, $Tag
            if ($declared -ne $expected) {
                throw "更新清单中的 $service 镜像地址不一致：声明=$declared，期望=$expected"
            }
        }
        Write-Detail "runtime_manifest_verified registry=$Registry namespace=$Namespace tag=$Tag"
    }

    function Test-RuntimeContainers([string]$Registry, [string]$Namespace, [string]$Tag) {
        foreach ($service in @('backend', 'websocket', 'scheduler', 'frontend')) {
            $previousPreference = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                $ids = @(& docker compose --project-directory $Root --env-file $envFile -f $compose ps -q $service 2>$null)
                $composeExitCode = $LASTEXITCODE
                $firstId = $ids | Select-Object -First 1
                $containerId = if ($null -ne $firstId) { "$firstId".Trim() } else { '' }
                if ($composeExitCode -ne 0 -or -not $containerId) {
                    throw "未找到服务 $service 的运行容器"
                }
                $images = @(& docker inspect $containerId --format '{{.Config.Image}}' 2>$null)
                $inspectExitCode = $LASTEXITCODE
                $firstImage = $images | Select-Object -First 1
                $actualImage = if ($null -ne $firstImage) { "$firstImage".Trim() } else { '' }
                $expectedImage = '{0}/{1}/xianyu-{2}:{3}' -f $Registry, $Namespace, $service, $Tag
                if ($inspectExitCode -ne 0 -or -not $actualImage) {
                    throw "无法读取服务 $service 的实际镜像"
                }
                if ($actualImage -ne $expectedImage) {
                    throw "服务 $service 仍在运行旧镜像：实际=$actualImage，期望=$expectedImage"
                }
                $containerImageId = @(& docker inspect $containerId --format '{{.Image}}' 2>$null) | Select-Object -First 1
                $containerImageId = if ($null -ne $containerImageId) { "$containerImageId".Trim() } else { '' }
                $expectedImageId = Get-LocalImageId $expectedImage
                if (-not $containerImageId -or -not $expectedImageId -or $containerImageId -ne $expectedImageId) {
                    throw "服务 $service 未加载刚拉取的镜像：容器=$containerImageId，本机标签=$expectedImageId"
                }
                Write-Detail "runtime_image_verified service=$service image=$actualImage"
                Write-Detail "runtime_container_image_id_verified service=$service image_id=$containerImageId"
            } finally {
                $ErrorActionPreference = $previousPreference
            }
        }
    }

    function Invoke-DockerCli([string[]]$Arguments, [string]$Label) {
        Write-Detail "docker_cli_start label=$Label args=$($Arguments -join ' ')"
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = & docker @Arguments 2>&1
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        foreach ($line in $output) { Write-Detail "docker_cli_output label=$Label text=$line" }
        Write-Detail "docker_cli_end label=$Label exit_code=$exitCode"
        if ($exitCode -ne 0) { throw "Docker 操作失败：$Label（退出码 $exitCode）" }
    }

    function Get-LocalImageId([string]$ImageRef, [int]$TimeoutSeconds = 5) {
        # docker image inspect can wait forever when Docker Desktop is stopping,
        # starting, or has a stale engine socket. The updater must report a
        # limited rollback capability instead of leaving the GUI at
        # “回滚保护检测中”.
        $process = New-Object System.Diagnostics.Process
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = 'docker.exe'
        $startInfo.Arguments = 'image inspect "' + ($ImageRef -replace '"', '\\"') + '" --format "{{.Id}}"'
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process.StartInfo = $startInfo
        try {
            if (-not $process.Start()) { return '' }
            if (-not $process.WaitForExit([Math]::Max(1, $TimeoutSeconds) * 1000)) {
                try { $process.Kill() } catch { }
                Write-Detail "docker_image_inspect_timeout image=$ImageRef timeout_seconds=$TimeoutSeconds"
                return ''
            }
            $result = $process.StandardOutput.ReadToEnd()
            if ($process.ExitCode -eq 0 -and $result) {
                $first = $result -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1
                if ($null -ne $first) { return $first.ToString().Trim() }
            }
        } catch {
            Write-Detail "docker_image_inspect_failed image=$ImageRef error=$($_.Exception.Message)"
        } finally {
            $process.Dispose()
        }
        return ''
    }

    function Get-LocalImageDigest([string]$ImageRef) {
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $result = & docker image inspect $ImageRef --format '{{json .RepoDigests}}' 2>$null
            if ($LASTEXITCODE -eq 0) {
                $first = $result | Select-Object -First 1
                if ($null -ne $first) {
                    $digests = @($first.ToString() | ConvertFrom-Json)
                    foreach ($digest in $digests) {
                        $text = "$digest".Trim()
                        if ($text -match '@(sha256:[0-9a-fA-F]{64})$') { return $Matches[1].ToLowerInvariant() }
                    }
                }
            }
        } catch { }
        finally { $ErrorActionPreference = $previousPreference }
        return ''
    }

    function Get-RemoteImageDigest([string]$Registry, [string]$Namespace, [string]$Service, [string]$Tag) {
        $url = "https://$Registry/v2/$Namespace/xianyu-$Service/manifests/$Tag"
        $headers = @{
            Accept = 'application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json'
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Method Head -Uri $url -TimeoutSec 60 -Headers $headers
            $headerValue = $response.Headers['Docker-Content-Digest']
            $digest = @($headerValue | ForEach-Object { "$($_)".Trim() } | Where-Object { $_ -match '^sha256:[0-9a-fA-F]{64}$' } | Select-Object -First 1)
            if ($digest.Count -gt 0) { return $digest[0].ToLowerInvariant() }
            Write-Detail "runtime_image_digest_unavailable service=$Service reason=header_missing url=$url"
        } catch {
            # Some reverse proxies expose GET but not the Docker HEAD header.
            # Keep the update moving, but still pull only this service and
            # require the post-pull tag/image/container checks below.
            Write-Detail "runtime_image_digest_unavailable service=$Service reason=$($_.Exception.Message) url=$url"
        }
        return ''
    }

    function Sync-RuntimeImages($Manifest, [string]$Registry, [string]$Namespace, [string]$Tag, [string]$PreviousRegistry, [string]$PreviousNamespace, [string]$PreviousTag) {
        $serviceList = @('backend', 'websocket', 'scheduler', 'frontend')
        $serviceIndex = 0
        foreach ($service in $serviceList) {
            $serviceIndex++
            Send-Stage 'apply' 'running' ("正在准备 $service 服务镜像（$serviceIndex / $($serviceList.Count)）")
            $imageRef = '{0}/{1}/xianyu-{2}:{3}' -f $Registry, $Namespace, $service, $Tag
            $remoteDigest = Get-RemoteImageDigest $Registry $Namespace $service $Tag
            $localDigest = Get-LocalImageDigest $imageRef
            if ($remoteDigest -and $localDigest -eq $remoteDigest) {
                Write-Detail "runtime_image_compare service=$service remote_digest=$remoteDigest local_digest=$localDigest action=skip"
                continue
            }
            if ($remoteDigest -and $PreviousRegistry -and $PreviousNamespace -and $PreviousTag) {
                $previousImageRef = '{0}/{1}/xianyu-{2}:{3}' -f $PreviousRegistry, $PreviousNamespace, $service, $PreviousTag
                $previousDigest = Get-LocalImageDigest $previousImageRef
                if (-not $previousDigest) { $previousDigest = Get-OfflineStateDigest $service $PreviousTag }
                if ($previousDigest -and $previousDigest -eq $remoteDigest) {
                    Invoke-DockerCli @('tag', $previousImageRef, $imageRef) "复用 $service 未变化镜像"
                    $reusedId = Get-LocalImageId $imageRef
                    if (-not $reusedId) { throw "复用镜像后未找到目标标签：$imageRef" }
                    Write-Detail "runtime_image_compare service=$service remote_digest=$remoteDigest previous_digest=$previousDigest action=reuse source=$previousImageRef image_id=$reusedId"
                    continue
                }
                Write-Detail "runtime_image_compare service=$service remote_digest=$remoteDigest previous_digest=$previousDigest action=compare"
            }
            $reason = if ($remoteDigest) { "digest_mismatch remote=$remoteDigest local=$localDigest" } else { 'remote_digest_unavailable' }
            Write-Detail "runtime_image_compare service=$service action=pull reason=$reason image=$imageRef"
            Invoke-DockerCli @('pull', $imageRef) "拉取 $service 变更镜像"
            $pulledId = Get-LocalImageId $imageRef
            if (-not $pulledId) { throw "镜像拉取后未找到目标标签：$imageRef" }
            $pulledDigest = Get-LocalImageDigest $imageRef
            if ($remoteDigest -and $pulledDigest -ne $remoteDigest) {
                throw "镜像拉取后 digest 不一致：$service（本机=$pulledDigest，远端=$remoteDigest）"
            }
            Write-Detail "runtime_image_pulled service=$service image=$imageRef image_id=$pulledId digest=$pulledDigest"
        }
    }

    function Find-LocalImageRefById([string]$ExpectedId) {
        if (-not $ExpectedId) { return '' }
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $rows = & docker image ls --no-trunc --format '{{.Repository}}:{{.Tag}}|{{.ID}}' 2>$null
            if ($LASTEXITCODE -ne 0) { return '' }
            foreach ($row in $rows) {
                $parts = "$row".Trim() -split '\|', 2
                if ($parts.Count -eq 2 -and $parts[1].Trim() -eq $ExpectedId -and $parts[0].Trim() -notmatch '^<none>') {
                    return $parts[0].Trim()
                }
            }
        } finally {
            $ErrorActionPreference = $previousPreference
        }
        return ''
    }

    function Test-TrueValue([string]$Value) {
        return "$Value".Trim().ToLowerInvariant() -in @('1', 'true', 'yes', 'on', 'required')
    }

    function Assert-SecureUrl([string]$Value, [string]$Label, [hashtable]$EnvMap) {
        try { $uri = [Uri]$Value } catch { throw "$Label 地址格式无效：$Value" }
        if (-not $uri.Host) { throw "$Label 地址缺少主机名：$Value" }
        if ($uri.UserInfo) { throw "$Label 地址不安全，请使用不含账号密码的地址" }
        if ($uri.Scheme -eq 'https') { return $uri }
        if ($uri.Scheme -eq 'http' -and (Test-TrueValue "$($EnvMap['UPDATE_ALLOW_INSECURE_HTTP'])")) {
            Write-Detail "$($Label)_insecure_http_allowed host=$($uri.Host)"
            return $uri
        }
        throw "$Label 必须使用 HTTPS：$Value"
    }

    function Resolve-ConfiguredPath([string]$Root, [string]$Value, [string]$Fallback) {
        $candidate = "$Value".Trim()
        if (-not $candidate) { $candidate = $Fallback }
        if ([IO.Path]::IsPathRooted($candidate)) { return $candidate }
        return (Join-Path $Root $candidate)
    }

    function Verify-ManifestSignature([byte[]]$ManifestBytes, [string]$ManifestUrl, $Headers, $EnvMap, [string]$DeclaredSignatureUrl = '') {
        $required = Test-TrueValue "$($EnvMap['UPDATE_REQUIRE_SIGNATURE'])"
        $keyPath = Resolve-ConfiguredPath $Root "$($EnvMap['UPDATE_MANIFEST_PUBLIC_KEY_PATH'])" 'deploy\update-signing-public-key.xml'
        if (-not (Test-Path -LiteralPath $keyPath)) {
            if ($required) { throw "更新签名公钥不存在：$keyPath" }
            Write-Detail "manifest_signature_skipped required=$required reason=public_key_missing path=$keyPath"
            return [pscustomobject]@{ Verified = $false; Required = $false; Url = ''; Algorithm = '' }
        }

        $signatureUrl = "$DeclaredSignatureUrl".Trim()
        if (-not $signatureUrl) { $signatureUrl = "$($EnvMap['UPDATE_MANIFEST_SIGNATURE_URL'])".Trim() }
        if (-not $signatureUrl) {
            if ($ManifestUrl -match '(?i)\.json(?=$|\?)') {
                $signatureUrl = [regex]::Replace($ManifestUrl, '(?i)\.json(?=$|\?)', '.json.sig')
            } else { $signatureUrl = "$ManifestUrl.sig" }
        }
        $signatureUri = Assert-SecureUrl $signatureUrl '更新签名' $EnvMap
        $signatureTempDir = New-UpdateTempDirectory 'manifest-signature'
        $signaturePath = Join-Path $signatureTempDir 'manifest.sig'
        try {
            Write-Detail "manifest_signature_request url=$signatureUrl"
            Invoke-WebRequest -UseBasicParsing -Uri $signatureUri.AbsoluteUri -TimeoutSec 20 -Headers $Headers -OutFile $signaturePath
            $signatureText = ([IO.File]::ReadAllText($signaturePath, [Text.Encoding]::UTF8) -replace '\s', '')
            if (-not $signatureText) { throw '更新签名文件为空' }
            try { $signatureBytes = [Convert]::FromBase64String($signatureText) } catch { throw '更新签名不是有效的 Base64 内容' }
            $publicKeyXml = [IO.File]::ReadAllText($keyPath, [Text.Encoding]::UTF8)
            $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
            $hashAlgorithm = New-Object Security.Cryptography.SHA256CryptoServiceProvider
            try {
                $rsa.FromXmlString($publicKeyXml)
                $valid = $rsa.VerifyData($ManifestBytes, $hashAlgorithm, $signatureBytes)
            } finally {
                $hashAlgorithm.Dispose()
                $rsa.Dispose()
            }
            if (-not $valid) { throw '更新清单签名校验失败，已拒绝本次更新' }
            $signatureHash = Get-FileSha256 $signaturePath
            Write-Detail "manifest_signature_verified algorithm=RSA-SHA256 sha256=$signatureHash"
            return [pscustomobject]@{ Verified = $true; Required = $required; Url = $signatureUrl; Algorithm = 'RSA-SHA256' }
        } finally {
            Remove-Item -LiteralPath $signatureTempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    function Install-ImageArtifacts($Manifest) {
        if ($null -eq $Manifest.image_artifacts) { return $false }
        $artifacts = @($Manifest.image_artifacts | Where-Object { $null -ne $_ })
        if ($artifacts.Count -eq 0) { return $false }
        $tempDir = New-UpdateTempDirectory 'update'
        try {
            $index = 0
            foreach ($artifact in $artifacts) {
                $index++
                $url = "$($artifact.url)".Trim()
                $imageRef = "$($artifact.image_ref)".Trim()
                $expectedId = "$($artifact.image_id)".Trim()
                $expectedHash = "$($artifact.sha256)".Trim().ToLowerInvariant()
                if (-not $url -or -not $imageRef -or -not $expectedHash) { throw "更新包清单第 $index 项字段不完整" }
                $currentId = Get-LocalImageId $imageRef
                if ($expectedId -and $currentId -and $currentId -eq $expectedId) {
                    Send-Stage 'apply' 'running' "$($artifact.service) 镜像已是最新，跳过"
                    Write-Detail "artifact_skip service=$($artifact.service) reason=image_id_match image_id=$currentId"
                    continue
                }
                if ($expectedId) {
                    $existingRef = Find-LocalImageRefById $expectedId
                    if ($existingRef -and $existingRef -ne $imageRef) {
                        Send-Stage 'apply' 'running' "正在复用本机已有的 $($artifact.service) 镜像"
                        Invoke-DockerCli @('tag', $existingRef, $imageRef) "复用 $($artifact.service) 未变化镜像"
                        Write-Detail "artifact_skip service=$($artifact.service) reason=local_image_id_match source_ref=$existingRef image_id=$expectedId"
                        continue
                    }
                }
                $uri = Assert-SecureUrl $url '镜像更新包' $EnvMap
                $archive = Join-Path $tempDir ('image-' + $index + '.tar.gz')
                $serviceName = "$($artifact.service)"
                Send-Stage 'apply' 'running' "正在下载 $serviceName 镜像更新包（$index / $($artifacts.Count)）"
                Write-Detail "artifact_download service=$serviceName url=$url"
                Invoke-WebRequest -UseBasicParsing -Uri $uri.AbsoluteUri -TimeoutSec 900 -OutFile $archive
                $actualHash = Get-FileSha256 $archive
                if ($actualHash -ne $expectedHash) { throw "更新包校验失败：$serviceName" }
                Write-Detail "artifact_verified service=$serviceName sha256=$actualHash bytes=$((Get-Item -LiteralPath $archive).Length)"
                Send-Stage 'apply' 'running' "正在导入 $serviceName 镜像（$index / $($artifacts.Count)）"
                Invoke-DockerCli @('load', '--input', $archive) "导入 $serviceName 镜像"
                $loadedId = Get-LocalImageId $imageRef
                if (-not $loadedId) { throw "镜像导入后未找到目标标签：$imageRef" }
                if ($expectedId -and $loadedId -ne $expectedId) { throw "镜像导入后 ID 不一致：$serviceName" }
            }
            return $true
        } finally {
            Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    function Install-ClientMaintenance($Manifest, [string]$Version, [string]$Build) {
        if ($null -eq $Manifest.client) { return '' }
        $url = "$($Manifest.client.package_url)".Trim()
        $expectedHash = "$($Manifest.client.package_sha256)".Trim().ToLowerInvariant()
        if (-not $url -and -not $expectedHash) { return '' }
        if (-not $url -or $expectedHash -notmatch '^[0-9a-f]{64}$') {
            throw '客户端维护包清单字段不完整'
        }
        $uri = Assert-SecureUrl $url '客户端维护包' $envMap
        $tempDir = New-UpdateTempDirectory 'client'
        $tempArchive = Join-Path $tempDir 'client.zip'
        $pendingRoot = Join-Path $Root 'updates\pending'
        New-Item -ItemType Directory -Path $pendingRoot -Force | Out-Null
        try {
            Write-Detail "client_package_download url=$url"
            Invoke-WebRequest -UseBasicParsing -Uri $uri.AbsoluteUri -TimeoutSec 900 -OutFile $tempArchive
                $actualHash = Get-FileSha256 $tempArchive
            if ($actualHash -ne $expectedHash) { throw '客户端维护包 SHA-256 校验失败' }
            $safeBuild = ($Build -replace '[^A-Za-z0-9._-]', '-')
            if (-not $safeBuild) { $safeBuild = $Version }
            $pendingPath = Join-Path $pendingRoot "client-$safeBuild.zip"
            Copy-Item -LiteralPath $tempArchive -Destination $pendingPath -Force
            Write-Detail "client_package_staged path=$pendingPath sha256=$actualHash bytes=$((Get-Item -LiteralPath $pendingPath).Length)"
            return $pendingPath
        } finally {
            Remove-Item -LiteralPath $tempDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    try {
        Write-Detail "update_start mode=$Mode root=$Root"
        if (-not (Test-Path -LiteralPath $compose)) { throw '缺少 docker-compose.yml' }
        if (-not (Test-Path -LiteralPath $envFile)) {
            # A direct updater launch can happen before the first installer run
            # creates app\.env. Do not run Docker commands with a missing env
            # file and do not show a false update failure; the next normal
            # application start will retry after installation initializes it.
            Write-Detail "update_skipped reason=environment_missing path=$envFile"
            Send-Message 'latest' '本地环境尚未初始化，已跳过本次更新检查' 100
            Send-Result 'latest' '本地环境尚未初始化，已跳过本次更新检查'
            return
        }
        $envMap = Get-EnvMap $envFile
        $manifestUrl = "$($envMap['UPDATE_MANIFEST_URL'])".Trim()
        if (-not $manifestUrl) { throw '未配置更新清单地址' }
        $manifestUri = Assert-SecureUrl $manifestUrl '更新清单' $envMap
        $requestUrl = $manifestUri.AbsoluteUri + ($(if ($manifestUri.AbsoluteUri.Contains('?')) { '&' } else { '?' })) + '_client_check=' + [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        Write-Detail "manifest_request url=$manifestUrl request_url=$requestUrl"
        $headers = @{ 'Cache-Control' = 'no-cache'; 'Accept' = 'application/json' }
        $manifestToken = "$($envMap['UPDATE_MANIFEST_TOKEN'])".Trim()
        if ($manifestToken) { $headers['Authorization'] = "Bearer $manifestToken"; Write-Detail 'manifest_auth configured=true' }
        $signatureInfo = [pscustomobject]@{ Verified = $false; Required = (Test-TrueValue "$($envMap['UPDATE_REQUIRE_SIGNATURE'])"); Url = ''; Algorithm = '' }
        $pendingClientPath = ''
        if ($Mode -eq 'check') {
            Send-Stage 'fetch' 'running' '正在获取并校验更新清单...'
            $manifestTempDir = New-UpdateTempDirectory 'manifest'
            $manifestPath = Join-Path $manifestTempDir 'latest.json'
            try {
                Invoke-WebRequest -UseBasicParsing -Uri $requestUrl -TimeoutSec 20 -Headers $headers -OutFile $manifestPath
                $manifestBytes = [IO.File]::ReadAllBytes($manifestPath)
                $manifestRaw = [Text.Encoding]::UTF8.GetString($manifestBytes)
                $manifest = $manifestRaw | ConvertFrom-Json
                $declaredSignatureUrl = "$($manifest.signature.url)".Trim()
                $signatureInfo = Verify-ManifestSignature $manifestBytes $manifestUrl $headers $envMap $declaredSignatureUrl
            } finally {
                Remove-Item -LiteralPath $manifestTempDir -Recurse -Force -ErrorAction SilentlyContinue
            }
            Send-Stage 'fetch' 'success' $(if ($signatureInfo.Verified) { '更新清单已获取并通过签名校验' } else { '更新清单已获取' })
        } else {
            Send-Stage 'fetch' 'running' '正在载入已校验的更新清单...'
            $received = $ManifestJson | ConvertFrom-Json
            if ($null -ne $received.manifest) {
                $manifest = $received.manifest
                $signatureInfo = [pscustomobject]@{
                    Verified = [bool]$received.signature_verified
                    Required = (Test-TrueValue "$($envMap['UPDATE_REQUIRE_SIGNATURE'])")
                    Url = "$($received.signature_url)"
                    Algorithm = "$($received.signature_algorithm)"
                }
            } else {
                $manifest = $received
            }
            if ($signatureInfo.Required -and -not $signatureInfo.Verified) {
                throw '更新清单未通过签名校验，已拒绝本次更新'
            }
            Send-Stage 'fetch' 'success' '更新清单已就绪'
        }
        $latestVersion = "$($manifest.version)".Trim()
        $latestBuild = "$($manifest.build_id)".Trim()
        $registry = "$($manifest.image_registry)".Trim()
        $namespace = "$($manifest.image_namespace)".Trim()
        $tag = "$($manifest.image_tag)".Trim()
        $runtimeImagesRequired = $true
        if ($null -ne $manifest.runtime_images_required) {
            $runtimeImagesRequired = Test-TrueValue "$($manifest.runtime_images_required)"
        }
        $runtimeImagesDeferred = $false
        if ($null -ne $manifest.runtime_images_deferred) {
            $runtimeImagesDeferred = Test-TrueValue "$($manifest.runtime_images_deferred)"
        }
        $runtimeSyncNeeded = Test-Path -LiteralPath $runtimeSyncMarker -PathType Leaf
        Send-Stage 'prepare' 'running' '正在校验本机文件与版本状态...'
        $offlineRuntimeMatch = Test-OfflineRuntimeState $manifest $latestVersion $tag
        Write-Detail "manifest_response version=$latestVersion build=$latestBuild registry=$registry namespace=$namespace tag=$tag"
        if (-not $latestVersion -or -not $registry -or -not $namespace -or -not $tag) { throw '更新清单字段不完整' }
        if (($runtimeImagesRequired -or $runtimeSyncNeeded) -and -not $offlineRuntimeMatch) {
            Test-RuntimeManifestImages $manifest $registry $namespace $tag
        }
        $declaredSignatureUrl = "$($manifest.signature.url)".Trim()
        if ($signatureInfo.Verified -and $declaredSignatureUrl -and $declaredSignatureUrl -ne $signatureInfo.Url) {
            throw '更新清单声明的签名地址与实际校验地址不一致'
        }
        [void](Get-VersionParts $latestVersion)
        $currentVersion = (Get-Content -LiteralPath $versionFile -Raw).Trim()
        $currentBuild = if (Test-Path -LiteralPath $buildFile) { (Get-Content -LiteralPath $buildFile -Raw).Trim() } else { '' }
        $versionResult = Compare-Version $latestVersion $currentVersion
        $clientIntegrity = Test-ClientIntegrity $manifest
        $versionAvailable = $versionResult -gt 0 -or ($versionResult -eq 0 -and $latestBuild -and $latestBuild -ne $currentBuild)
        $available = $versionAvailable -or $clientIntegrity.RepairNeeded -or $runtimeSyncNeeded
        Write-Detail "version_compare current=$currentVersion/$currentBuild latest=$latestVersion/$latestBuild version_available=$versionAvailable client_repair_needed=$($clientIntegrity.RepairNeeded) launcher_mismatch=$($clientIntegrity.LauncherMismatch) frontend_mismatch=$($clientIntegrity.FrontendMismatch) available=$available"
        $updateReason = 'none'
        if ($versionResult -gt 0) { $updateReason = 'version' }
        elseif ($versionAvailable) { $updateReason = 'build' }
        elseif ($runtimeSyncNeeded) { $updateReason = 'runtime_sync' }
        elseif ($clientIntegrity.RepairNeeded) { $updateReason = 'client_repair' }
        Send-Meta 'latest_version' $latestVersion
        Send-Meta 'latest_build' $latestBuild
        Send-Meta 'update_reason' $updateReason
        Send-Meta 'notes' $(if ($manifest.notes) { "$($manifest.notes)" } else { '' })
        Send-Stage 'prepare' 'success' '本机校验完成'
        if ($Mode -eq 'check') {
            if ($available) {
                $payload = [ordered]@{ manifest = $manifest; signature_verified = [bool]$signatureInfo.Verified; signature_url = $signatureInfo.Url; signature_algorithm = $signatureInfo.Algorithm }
                $message = if ($versionAvailable) { "发现新版本 $latestVersion" } elseif ($runtimeSyncNeeded) { '检测到运行时镜像尚未同步，准备校准' } else { '检测到本机客户端文件不完整，准备修复' }
                Send-Message 'available' $message 8 ($payload | ConvertTo-Json -Compress -Depth 20)
                Send-Result 'available' $message
            } else {
                Send-Message 'latest' "当前已是最新版本 $currentVersion" 100
                Send-Result 'latest' "当前已是最新版本 $currentVersion"
            }
            return
        }
        if (-not $available) { Send-Message 'latest' "当前已是最新版本 $currentVersion" 100; Send-Result 'latest' "当前已是最新版本 $currentVersion"; return }
        $previousDeployMode = "$($envMap['XR_DEPLOY_MODE'])"
        $previousRegistry = "$($envMap['XR_IMAGE_REGISTRY'])"
        $previousNamespace = "$($envMap['XR_IMAGE_NAMESPACE'])"
        $previousTag = "$($envMap['XR_IMAGE_TAG'])"
        Send-Stage 'protect' 'running' '正在保护当前版本（配置与镜像状态快照）...'
        $rollbackCapability = 'unavailable:unknown'
        try {
            $snapshotDir = Join-Path $Root 'updates'
            if (-not (Test-Path -LiteralPath $snapshotDir)) { New-Item -ItemType Directory -Path $snapshotDir -Force | Out-Null }
            $imageSnapshots = [ordered]@{}
            foreach ($service in @('backend', 'websocket', 'scheduler', 'frontend')) {
                $ref = if ($previousRegistry -and $previousNamespace -and $previousTag) { '{0}/{1}/xianyu-{2}:{3}' -f $previousRegistry, $previousNamespace, $service, $previousTag } else { '' }
                $imageSnapshots[$service] = if ($ref) { Get-LocalImageId $ref } else { '' }
            }
            $snapshot = [ordered]@{
                captured_at     = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
                version         = $currentVersion
                build_id        = $currentBuild
                deploy_mode     = $previousDeployMode
                image_registry  = $previousRegistry
                image_namespace = $previousNamespace
                image_tag       = $previousTag
                images          = $imageSnapshots
            }
            Set-Content -LiteralPath (Join-Path $snapshotDir 'pre-update-state.json') -Value ($snapshot | ConvertTo-Json -Depth 6) -Encoding UTF8
            $rollbackCapability = Test-RollbackCapability $previousRegistry $previousNamespace $previousTag
        } catch {
            Write-Detail "protect_snapshot_failed error=$($_.Exception.ToString())"
            $rollbackCapability = 'unavailable:snapshot_failed'
        }
        Send-Meta 'rollback_capability' $rollbackCapability
        if ($rollbackCapability -eq 'available') {
            Send-Stage 'protect' 'success' '当前版本快照已保存，可完整回滚'
        } else {
            Send-Stage 'protect' 'warning' "回滚保护不完整：$rollbackCapability" 'E_ROLLBACK_CAPABILITY'
        }
        Send-Stage 'apply' 'running' '正在应用更新...'
        Send-Message 'phase' '正在准备更新环境...' 15
        $runtimeSyncRequired = ($runtimeImagesRequired -or $runtimeSyncNeeded) -and -not $offlineRuntimeMatch
        if ($runtimeSyncRequired) {
            Set-EnvValue $envFile 'XR_DEPLOY_MODE' 'remote'
            Set-EnvValue $envFile 'XR_IMAGE_REGISTRY' $registry
            Set-EnvValue $envFile 'XR_IMAGE_NAMESPACE' $namespace
            Set-EnvValue $envFile 'XR_IMAGE_TAG' $tag
            if (-not (Install-ImageArtifacts $manifest)) {
                Sync-RuntimeImages $manifest $registry $namespace $tag $previousRegistry $previousNamespace $previousTag
            }
            Send-Message 'phase' '镜像拉取完成，正在重启服务...' 78
        } else {
            Write-Detail 'runtime_image_update_skipped reason=host_overlay_release'
            Send-Message 'phase' '业务补丁已下载，正在重启服务...' 78
        }
        # Images have already been imported or pulled above. Never let the
        # service restart phase initiate an implicit network pull.
        Send-Stage 'apply' 'success' '镜像与更新内容已应用'
        Send-Stage 'restart' 'running' '正在重启业务服务...'
        Invoke-Docker @('up', '-d', '--force-recreate', '--no-build', '--pull', 'never') '重启应用服务'
        Send-Stage 'restart' 'success' '业务服务已重启'
        Send-Message 'phase' '正在检查容器状态...' 90
        Send-Stage 'health' 'running' '正在检查容器与前端服务...'
        Invoke-Docker @('ps') '检查容器状态'
        if ($runtimeSyncRequired) { Test-RuntimeContainers $registry $namespace $tag }
        $port = "$($envMap['FRONTEND_PORT'])"
        if ($port -notmatch '^\d+$') { throw '前端端口配置无效' }
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port" -TimeoutSec 15
        Write-Detail "frontend_check status=$($response.StatusCode) port=$port"
        if ($response.StatusCode -lt 200 -or $response.StatusCode -ge 500) { throw "前端健康检查失败，HTTP $($response.StatusCode)" }
        Send-Stage 'health' 'success' '容器与前端服务检查通过'
        $needsClientPackage = $versionAvailable -or $clientIntegrity.RepairNeeded
        if ($needsClientPackage) {
            $pendingClientPath = Install-ClientMaintenance $manifest $latestVersion $latestBuild
        }
        if ($pendingClientPath) {
            # The client package contains the launcher EXE and the frontend
            # maintenance files. Do not advance the local version marker until
            # that package has been applied; otherwise an old executable can
            # report the new version after a failed file replacement.
            Write-Detail "client_update_pending version=$latestVersion build=$latestBuild path=$pendingClientPath"
            Write-Detail "update_completed runtime_version=$latestVersion client_version_pending=true"
            Send-Message 'completed' "更新包已下载，请关闭并重新启动启动器以应用新版界面" 100
        } else {
            if ($runtimeSyncNeeded -and $runtimeSyncRequired) {
                Remove-Item -LiteralPath $runtimeSyncMarker -Force -ErrorAction SilentlyContinue
                Write-Detail 'runtime_sync_marker_removed'
            }
            Set-Content -LiteralPath $versionFile -Value $latestVersion -Encoding UTF8
            Set-Content -LiteralPath $buildFile -Value $latestBuild -Encoding UTF8
            Write-Detail "update_completed version=$latestVersion build=$latestBuild"
            Send-Message 'completed' "更新完成，当前版本 $latestVersion" 100
        }
    } catch {
        $detail = $_.Exception.ToString()
        $failedStage = if ($script:UpdateStage) { $script:UpdateStage } else { 'prepare' }
        $failureCode = Get-FailureCode $failedStage $_.Exception.Message
        Send-Stage $failedStage 'failed' "$($_.Exception.Message)" $failureCode
        Write-Detail "update_failed stage=$failedStage code=$failureCode error=$detail"
        if ($pendingClientPath -and (Test-Path -LiteralPath $pendingClientPath)) {
            Remove-Item -LiteralPath $pendingClientPath -Force -ErrorAction SilentlyContinue
            Write-Detail "client_package_discarded path=$pendingClientPath"
        }
        if ($previousDeployMode -ne $null) {
            Send-Stage 'rollback' 'running' '正在恢复原版本...'
            $rollbackOk = $false
            try {
                Set-EnvValue $envFile 'XR_DEPLOY_MODE' $previousDeployMode
                Set-EnvValue $envFile 'XR_IMAGE_REGISTRY' $previousRegistry
                Set-EnvValue $envFile 'XR_IMAGE_NAMESPACE' $previousNamespace
                Set-EnvValue $envFile 'XR_IMAGE_TAG' $previousTag
                Write-Detail "rollback_config_restored deploy_mode=$previousDeployMode registry=$previousRegistry namespace=$previousNamespace tag=$previousTag"
                # Rollback must be local-only. If the old image is unavailable,
                # report that fact instead of downloading a large image while
                # handling the original failure.
                Invoke-Docker @('up', '-d', '--force-recreate', '--no-build', '--pull', 'never') '失败后恢复旧版本'
                $rollbackOk = $true
                Send-Stage 'rollback' 'success' '已恢复原版本配置并重启旧服务'
            } catch {
                Write-Detail "rollback_failed error=$($_.Exception.ToString())"
                Send-Stage 'rollback' 'failed' '自动恢复未完成，部分服务可能没有正常启动' 'E_ROLLBACK_FAILED'
            }
            if ($rollbackOk) {
                Send-Result 'rolled_back' $(if ($currentVersion) { "已安全恢复到版本 $currentVersion" } else { '已安全恢复到原版本' }) $failureCode
            } else {
                Send-Result 'rollback_failed' '更新失败，自动恢复未完成' 'E_ROLLBACK_FAILED'
            }
        } else {
            Send-Result 'failed' '更新失败' $failureCode
        }
        try { Invoke-Docker @('ps') '失败后检查容器状态' } catch { Write-Detail "docker_status_failed error=$($_.Exception.ToString())" }
        Send-Message 'failed' '更新失败，请查看下方详细日志' 100 $detail
    }
}

if ($Headless) {
    # The C# updater owns the visible window. Headless mode must still perform
    # the signed check first. Calling the update worker directly with an empty
    # manifest leaves signature_verified=false and makes every signed release
    # fail before it can be applied.
    function Invoke-HeadlessWorker([string]$Mode, [string]$Data = '') {
        $failed = $false
        $availableData = ''
        $records = @(& $worker $ProjectRoot $Mode $Data 6>&1)
        foreach ($record in $records) {
            # A merged information stream (6>&1) unwraps Write-Information to
            # its MessageData payload; some hosts wrap it as an
            # InformationRecord instead. Normalize both shapes.
            if ($record -is [System.Management.Automation.InformationRecord]) { $record = $record.MessageData }
            if ($null -eq $record -or $null -eq $record.Kind) { continue }
            if ($record.Kind -eq 'uievent') { [Console]::Out.WriteLine('@@XIANYU_UI@@' + [string]$record.Json); continue }
            if ($record.Kind -eq 'log') { [Console]::Out.WriteLine([string]$record.Message); continue }
            if ($record.Kind -eq 'available') { $availableData = [string]$record.Data }
            if ($record.Kind -eq 'failed') {
                $failed = $true
                [Console]::Out.WriteLine([string]$record.Message)
                [Console]::Out.WriteLine([string]$record.Data)
            } else {
                [Console]::Out.WriteLine([string]$record.Message)
            }
        }
        [Console]::Out.Flush()
        return [pscustomobject]@{ Failed = $failed; AvailableData = $availableData }
    }

    $checkResult = Invoke-HeadlessWorker 'check'
    if ($checkResult.Failed) { exit 1 }
    # -CheckOnly: the C# updater shows its own confirmation page, so do not
    # apply anything until it re-invokes us without the switch.
    if ($CheckOnly) { exit 0 }
    if ($checkResult.AvailableData) {
        $updateResult = Invoke-HeadlessWorker 'update' $checkResult.AvailableData
        if ($updateResult.Failed) { exit 1 }
    }
    exit 0
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
$initialVersionText = '未知'
if (Test-Path -LiteralPath $VersionFile) { $initialVersionText = (Get-Content -LiteralPath $VersionFile -Raw).Trim() }
$initialBuildText = if (Test-Path -LiteralPath $BuildFile) { (Get-Content -LiteralPath $BuildFile -Raw).Trim() } else { '' }
if ($initialBuildText) { $version.Text = "当前版本：$initialVersionText  ($initialBuildText)" } else { $version.Text = "当前版本：$initialVersionText" }
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

function Finish-Check([string]$Kind, [string]$AvailableMessage = '') {
    if ($Kind -eq 'available') {
        $script:phase = 'available'
        $later.Visible = $true
        $now.Visible = $true
        $status.Text = if ($AvailableMessage) { $AvailableMessage } else { '发现新版本' }
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
        $later.Visible = $false
        $now.Visible = $true
        $status.Text = '当前已是最新版本'
        $status.ForeColor = $green
        $now.Text = '关闭'
        $now.Enabled = $true
        $later.Text = '关闭'
        $later.Enabled = $false
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
    $informationMessages = @()
    $messages = @(Receive-Job -Job $job -ErrorAction SilentlyContinue -InformationVariable informationMessages)
    foreach ($informationRecord in @($informationMessages)) {
        if ($null -ne $informationRecord.MessageData) {
            $messages += $informationRecord.MessageData
        }
    }
    foreach ($message in $messages) {
        if ($message.Kind -eq 'log') { Add-Log $message.Message; continue }
        if ($message.Percent -ge 0 -and $message.Percent -le 100) {
            $progress.Value = $message.Percent
            $progressText.Text = "$($message.Percent)%  $($message.Message)"
        }
        if ($message.Kind -eq 'available') {
            $manifestJson = $message.Data
            try {
                $payload = $manifestJson | ConvertFrom-Json
                $manifest = if ($null -ne $payload.manifest) { $payload.manifest } else { $payload }
                $curVersion = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
                $curBuildText = if (Test-Path -LiteralPath $BuildFile) { (Get-Content -LiteralPath $BuildFile -Raw).Trim() } else { '' }
                $latestText = "$($manifest.version)"
                if ($manifest.build_id) { $latestText += "  ($($manifest.build_id))" }
                $currentText = "$curVersion"
                if ($curBuildText) { $currentText += "  ($curBuildText)" }
                $version.Text = "当前版本：$currentText    →    最新版本：$latestText"
                $notes.Text = if ($manifest.notes) { $manifest.notes } else { '本次更新包含功能优化和稳定性修复。' }
            } catch { }
            Finish-Check 'available' $message.Message
        } elseif ($message.Kind -eq 'latest') {
            $curV = (Get-Content -LiteralPath $VersionFile -Raw).Trim()
            $curB = if (Test-Path -LiteralPath $BuildFile) { (Get-Content -LiteralPath $BuildFile -Raw).Trim() } else { '' }
            if ($curB) { $version.Text = "当前版本：$curV  ($curB)" } else { $version.Text = "当前版本：$curV" }
            $notes.Text = '当前已经是最新版本，无需更新。'
            Finish-Check 'latest'
        } elseif ($message.Kind -eq 'completed') {
            $script:phase = 'completed'
            $later.Visible = $false
            $status.Text = $message.Message
            $status.ForeColor = $green
            $now.Text = '关闭'
            $now.Enabled = $true
            $later.Text = '关闭'
            $later.Enabled = $false
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
