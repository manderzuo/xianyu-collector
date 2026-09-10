param(
    [string]$OutputDirectory = '',
    [switch]$Force,
    [switch]$IncludeDockerImages,
    [string]$ImageSourceRegistry = 'www.gemstory.cn',
    [string]$ImageSourceNamespace = 'xianyu',
    [string]$ImageSourceTag = '',
    [string]$OfflineTempDirectory = '',
    [string]$BundledWslMsiPath = '',
    [string]$VersionOverride = '',
    [string]$BuildIdOverride = '',
    [switch]$DeferRuntimeImageUpdate,
    [switch]$ApplicationImagesOnly
)

$ErrorActionPreference = 'Stop'
$SourceRoot = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = "$env:XIANYU_PACKAGE_OUTPUT_DIRECTORY".Trim()
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    throw 'OutputDirectory is required. Choose a destination outside the source tree, for example D:\xianyu-release.'
}
$resolvedSource = (Resolve-Path -LiteralPath $SourceRoot).Path.TrimEnd('\')
$resolvedOutputCandidate = [IO.Path]::GetFullPath($OutputDirectory).TrimEnd('\')
if ($resolvedOutputCandidate -eq $resolvedSource -or
    $resolvedOutputCandidate.StartsWith($resolvedSource + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $resolvedSource.StartsWith($resolvedOutputCandidate + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'OutputDirectory must be outside the source tree.'
}

if ((Test-Path -LiteralPath $OutputDirectory) -and -not $Force) {
    throw "Output directory already exists: $OutputDirectory. Use -Force only when replacing this package."
}

if (Test-Path -LiteralPath $OutputDirectory) {
    $resolvedOutput = (Resolve-Path -LiteralPath $OutputDirectory).Path
    if ($resolvedOutput -eq $resolvedSource) {
        throw 'Output directory must not be the project root.'
    }
    Remove-Item -LiteralPath $OutputDirectory -Recurse -Force
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$AppRoot = Join-Path $OutputDirectory 'app'
$ScriptsRoot = Join-Path $OutputDirectory 'scripts'
$ResourcesRoot = Join-Path $OutputDirectory 'resources'
New-Item -ItemType Directory -Path $AppRoot, $ScriptsRoot, $ResourcesRoot -Force | Out-Null

$excludedDirectoryNames = @(
    '.git', '.pytest_cache', '__pycache__', '.mypy_cache', '.ruff_cache', '.venv', 'venv', 'node_modules', 'dist', 'build',
    'static', 'backups', 'browser_data', 'logs', 'release', 'xianyu-one-click-installer'
)
$excludedFileNames = @('.env', '.env.local')

function Copy-ProjectTree {
    param([string]$Source, [string]$Destination)

    $sourceFull = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
    Get-ChildItem -LiteralPath $sourceFull -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($sourceFull.Length).TrimStart('\')
        $parts = $relative -split '[\\/]'
        if ($parts | Where-Object { $excludedDirectoryNames -contains $_ }) { return }
        if ($excludedFileNames -contains $_.Name) { return }
        $target = Join-Path $Destination $relative
        $targetParent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetParent)) {
            New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
}

Copy-ProjectTree -Source $SourceRoot -Destination $AppRoot

# frontend/dist is intentionally included when present. The commercial
# updater bind-mounts these compiled assets over the stable nginx runtime so
# ordinary UI releases remain a small client package instead of a full image.
$frontendDist = Join-Path $SourceRoot 'frontend\dist'
if (Test-Path -LiteralPath $frontendDist) {
    Copy-Item -LiteralPath $frontendDist -Destination (Join-Path $AppRoot 'frontend\dist') -Recurse -Force
}

# The package entry points live at the package root. The source tree is kept under app.
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-install.ps1') -Destination (Join-Path $ScriptsRoot 'install.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-start.ps1') -Destination (Join-Path $ScriptsRoot 'start.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-stop.ps1') -Destination (Join-Path $ScriptsRoot 'stop.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-diagnostics.ps1') -Destination (Join-Path $ScriptsRoot 'diagnostics.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-upload-diagnostics.ps1') -Destination (Join-Path $ScriptsRoot 'upload-diagnostics.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-update.ps1') -Destination (Join-Path $ScriptsRoot 'update.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-apply-client-update.ps1') -Destination (Join-Path $ScriptsRoot 'apply-client-update.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-docker.ps1') -Destination (Join-Path $ResourcesRoot 'docker-bootstrap.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-wsl.ps1') -Destination (Join-Path $ResourcesRoot 'prepare-wsl.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-cleanup-rdp.ps1') -Destination (Join-Path $ResourcesRoot 'cleanup-rdp.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'import-offline-image-bundle.ps1') -Destination (Join-Path $ResourcesRoot 'import-offline-image-bundle.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-repair-offline-install.ps1') -Destination (Join-Path $ResourcesRoot 'repair-offline-install.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-repair-legacy-update.ps1') -Destination (Join-Path $ResourcesRoot 'repair-legacy-update.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-repair-offline-runtime.ps1') -Destination (Join-Path $ResourcesRoot 'repair-offline-runtime.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'add-offline-infrastructure-bundle.ps1') -Destination (Join-Path $ResourcesRoot 'add-offline-infrastructure-bundle.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-reset-xianyu-docker.ps1') -Destination (Join-Path $ResourcesRoot 'reset-xianyu-docker.ps1') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-install.bat') -Destination (Join-Path $OutputDirectory 'install.bat') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-start.bat') -Destination (Join-Path $OutputDirectory 'start.bat') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-stop.bat') -Destination (Join-Path $OutputDirectory 'stop.bat') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-diagnostics.bat') -Destination (Join-Path $OutputDirectory 'diagnostics.bat') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-update.bat') -Destination (Join-Path $OutputDirectory 'update.bat') -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows-installer-inject-offline-images.bat') -Destination (Join-Path $OutputDirectory 'inject-offline-images.bat') -Force
$bundledWslMsi = ''
$bundledWslMsiSha256 = ''
$bundledWslMsiBytes = 0
if (-not [string]::IsNullOrWhiteSpace($BundledWslMsiPath)) {
    $resolvedWslMsi = (Resolve-Path -LiteralPath $BundledWslMsiPath -ErrorAction Stop).Path
    if ([IO.Path]::GetExtension($resolvedWslMsi) -ine '.msi') {
        throw "BundledWslMsiPath must point to an MSI file: $resolvedWslMsi"
    }
    $wslResourceDirectory = Join-Path $ResourcesRoot 'wsl'
    New-Item -ItemType Directory -Path $wslResourceDirectory -Force | Out-Null
    Copy-Item -LiteralPath $resolvedWslMsi -Destination (Join-Path $wslResourceDirectory 'wsl-update-x64.msi') -Force
    $bundledWslMsi = 'resources/wsl/wsl-update-x64.msi'
    $bundledWslMsiSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $resolvedWslMsi).Hash.ToLowerInvariant()
    $bundledWslMsiBytes = (Get-Item -LiteralPath $resolvedWslMsi).Length
    Write-Host "[xianyu] Bundled WSL MSI: $resolvedWslMsi" -ForegroundColor Cyan
}
$iconSource = Join-Path $SourceRoot 'assets\xianyu-launcher.ico'
if (Test-Path -LiteralPath $iconSource) {
    Copy-Item -LiteralPath $iconSource -Destination (Join-Path $OutputDirectory 'xianyu-launcher.ico') -Force
}

$version = '0.0.0'
$versionPath = Join-Path $SourceRoot 'VERSION.txt'
if (-not [string]::IsNullOrWhiteSpace($VersionOverride)) {
    $version = $VersionOverride.Trim()
} elseif (Test-Path -LiteralPath $versionPath) {
    $candidate = (Get-Content -LiteralPath $versionPath -Raw).Trim()
    if ($candidate) { $version = $candidate }
}
if ($version -notmatch '^\d+(?:\.\d+){1,3}$') {
    throw "Invalid release version: $version"
}

# A code-only release still needs the compiled frontend because the compose
# project bind-mounts frontend/dist over the stable nginx image. Fail the build
# instead of silently shipping a package that keeps the old UI.
$compiledFrontendIndex = Join-Path $frontendDist 'index.html'
if (-not (Test-Path -LiteralPath $compiledFrontendIndex -PathType Leaf)) {
    throw "Compiled frontend is missing: $compiledFrontendIndex. Run npm ci and npm run build in frontend before packaging."
}
$compiledFrontendScripts = @(Get-ChildItem -LiteralPath (Join-Path $frontendDist 'assets') -Filter '*.js' -File -Force -ErrorAction SilentlyContinue)
if ($compiledFrontendScripts.Count -eq 0 -or -not (Select-String -Path $compiledFrontendScripts.FullName -SimpleMatch $version -Quiet)) {
    throw "Compiled frontend does not contain release version $version. Rebuild frontend with APP_VERSION=$version before packaging."
}
$buildIdPath = Join-Path $SourceRoot 'BUILD_ID.txt'
$buildId = ''
if (-not [string]::IsNullOrWhiteSpace($BuildIdOverride)) {
    $buildId = $BuildIdOverride.Trim()
} elseif (Test-Path -LiteralPath $buildIdPath) {
    $buildId = (Get-Content -LiteralPath $buildIdPath -Raw).Trim()
}
if ($buildId -and $buildId -notmatch '^[A-Za-z0-9._-]+$') {
    throw "Invalid release build ID: $buildId"
}
# A release override must be reflected in the copied application tree as well
# as in package-manifest.json. Otherwise a tag such as v1.0.11 could produce a
# package that reports the older source VERSION.txt after installation.
Set-Content -LiteralPath (Join-Path $AppRoot 'VERSION.txt') -Value $version -Encoding UTF8
if ($buildId) {
    Set-Content -LiteralPath (Join-Path $AppRoot 'BUILD_ID.txt') -Value $buildId -Encoding UTF8
}
if ($DeferRuntimeImageUpdate) {
    $runtimeMarker = [ordered]@{
        protocol = 1
        version = $version
        build_id = $buildId
        reason = 'client-first-runtime-sync'
        created_at = [DateTime]::UtcNow.ToString('o')
    }
    $runtimeMarker | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $AppRoot 'runtime-sync.pending.json') -Encoding UTF8
}
$launcherBuilder = Join-Path $PSScriptRoot 'build-launcher.ps1'
if (Test-Path -LiteralPath $launcherBuilder) {
    & $launcherBuilder -OutputDirectory $OutputDirectory -Force
    if ($LASTEXITCODE -ne 0) { throw "Launcher build failed with exit code $LASTEXITCODE." }
}
$launcherSha256 = $null
$launcherBinary = Join-Path $OutputDirectory 'xianyu-launcher.exe'
if (Test-Path -LiteralPath $launcherBinary -PathType Leaf) {
    $launcherSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $launcherBinary).Hash.ToLowerInvariant()
}
$frontendIndexSha256 = $null
$frontendIndex = Join-Path $AppRoot 'frontend\dist\index.html'
if (Test-Path -LiteralPath $frontendIndex -PathType Leaf) {
    $frontendIndexSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $frontendIndex).Hash.ToLowerInvariant()
}
$manifest = [ordered]@{
    product = 'xianyu-rewrite'
    package_type = 'windows-portable-docker'
    version = $version
    created_at = [DateTime]::UtcNow.ToString('o')
    source_directory = 'app'
    install_entry = 'install.bat'
    start_entry = 'start.bat'
    stop_entry = 'stop.bat'
    launcher_entry = ((-join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe')
    launcher_sha256 = $launcherSha256
    frontend_index_sha256 = $frontendIndexSha256
    installer_entry = ((-join ([char[]](0x5b89, 0x88c5, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe')
    launcher_aliases = @('xianyu-launcher.exe', 'xianyu-installer.exe', 'xianyu-updater.exe', 'xianyu-stopper.exe', 'xianyu-diagnostics.exe')
    maintenance_entries = @(
        ((-join ([char[]](0x66f4, 0x65b0, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'),
        ((-join ([char[]](0x505c, 0x6b62, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'),
        ((-join ([char[]](0x8bca, 0x65ad, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe')
    )
    data_location = 'Docker named volumes managed by the generated compose project'
    offline_images = [bool]$IncludeDockerImages
    offline_image_manifest = if ($IncludeDockerImages) { 'resources/images/offline-manifest.json' } else { $null }
    offline_application_images_only = [bool]$ApplicationImagesOnly
    runtime_images_deferred = [bool]$DeferRuntimeImageUpdate
    bundled_wsl_msi = if ($bundledWslMsi) { $bundledWslMsi } else { $null }
    bundled_wsl_msi_sha256 = if ($bundledWslMsiSha256) { $bundledWslMsiSha256 } else { $null }
    bundled_wsl_msi_bytes = if ($bundledWslMsi) { $bundledWslMsiBytes } else { $null }
    requires = if ($IncludeDockerImages) {
        @('Windows 10 or later', 'Docker Desktop with Linux containers', 'At least 5 GB of free disk space for image import and Docker volumes')
    } else {
        @('Windows 10 or later', 'Docker Desktop with Linux containers', 'Internet access for the first image build unless images are preloaded')
    }
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'package-manifest.json') -Encoding UTF8

if ($IncludeDockerImages) {
    # Build application archives from the registry manifest so Docker's
    # containerd snapshotter cannot produce a metadata-only docker save.
    # Registry blobs retain their original layer digests and remain reusable
    # by future incremental pulls.
    $offlineBuilder = Join-Path $PSScriptRoot 'build-offline-registry-bundle.ps1'
    if (-not (Test-Path -LiteralPath $offlineBuilder)) { throw "Offline registry image builder not found: $offlineBuilder" }
    $offlineArguments = @{
        PackageRoot = $OutputDirectory
        Version = $version
        SourceRegistry = $ImageSourceRegistry
        SourceNamespace = $ImageSourceNamespace
        SourceTag = $ImageSourceTag
    }
    if (-not [string]::IsNullOrWhiteSpace($OfflineTempDirectory)) {
        $offlineArguments.TempDirectory = $OfflineTempDirectory
    }
    & $offlineBuilder @offlineArguments
    if ($LASTEXITCODE -ne 0) { throw "Offline image bundle creation failed with exit code $LASTEXITCODE." }
    if (-not $ApplicationImagesOnly) {
        $infrastructureBuilder = Join-Path $PSScriptRoot 'add-offline-infrastructure-bundle.ps1'
        if (-not (Test-Path -LiteralPath $infrastructureBuilder)) { throw "Offline infrastructure image builder not found: $infrastructureBuilder" }
        & $infrastructureBuilder -PackageRoot $OutputDirectory -Version $version
        if ($LASTEXITCODE -ne 0) { throw "Offline infrastructure bundle creation failed with exit code $LASTEXITCODE." }
    }
}

$launcherDisplayName = (-join ([char[]](0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$installerDisplayName = (-join ([char[]](0x5b89, 0x88c5, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$updaterDisplayName = (-join ([char[]](0x66f4, 0x65b0, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$stopperDisplayName = (-join ([char[]](0x505c, 0x6b62, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$diagnosticsDisplayName = (-join ([char[]](0x8bca, 0x65ad, 0x95f2, 0x9c7c, 0x7ba1, 0x7406, 0x7cfb, 0x7edf))) + '.exe'
$centralDeploymentDoc = -join ([char[]](0x817e, 0x8baf, 0x4e91, 0x4e2d, 0x5fc3, 0x5316, 0x90e8, 0x7f72, 0x8bf4, 0x660e))
$readme = @'
Xianyu One-Click Installer

Copy this whole folder to another Windows computer. Do not move only one file.

1. Install or start Docker Desktop.
2. Double-click __INSTALLER__ (install.bat remains available as a compatibility fallback).
3. Open the desktop shortcut named __LAUNCHER__.

On the first run, the GUI checks and configures WSL 2 prerequisites. It also disables WSLg GUI applications for the current Windows user, because Docker Desktop does not require Linux GUI applications and WSLg can trigger a broken RDP ActiveX popup on affected systems. The original .wslconfig is backed up before changes. Windows may ask for administrator permission and a restart. Run the installer again after the restart. PowerShell runs in the background; progress, errors and logs are shown in the GUI.

The installer uses its own folder as the project root. It does not depend on a fixed drive or user path.
The installer chooses free ports beginning at 20000 and never uses the old 19000 deployment.
The app source, compose file, environment template, scripts and Docker bootstrap helper are all under this folder.

This package is for standalone deployment. If multiple computers must share users and registration approvals, deploy one central copy on your Tencent Cloud server and let all computers access that same URL. Do not run separate local databases for that scenario. See app\docs\__CENTRAL_DOC__.md.

Docker Desktop is not included in this package. Install Docker Desktop separately, then run install.bat.
You may also place your installer at resources\DockerDesktopInstaller.exe before copying the package.
If Docker Desktop is missing, the GUI reports that it must be installed before continuing. The target computer needs administrator permission for Docker Desktop installation.

This package can include all four application images plus MySQL and Redis in resources\images. When resources\images\offline-manifest.json is present, install.bat imports all listed images locally and does not download Docker images. inject-offline-images.bat can re-import the same archives on another Docker Desktop installation; it never performs a network pull.
When resources\wsl\wsl-update-x64.msi is present, the installer silently installs the bundled Microsoft WSL package before Docker Desktop starts; this avoids Docker's own WSL update dialog on a new computer.
Without an offline image bundle, first startup builds the four application images and downloads base images. This can take several minutes.
After installation, __LAUNCHER__ starts the existing containers without deleting data.
__UPDATER__, __STOPPER__ and __DIAGNOSTICS__ are GUI
maintenance shortcuts; the original update.bat, stop.bat and diagnostics.bat remain available
as compatibility fallbacks.
Each launcher start checks the Tencent-hosted release
manifest. If a newer image release is available, the window shows the release notes and
asks for confirmation, then compares each service's remote Docker digest with the local
image and pulls only changed services. Docker reuses existing layers, and the updater
restarts services only after verifying the pulled image IDs and the running containers.
The detailed update log is saved at app\logs\update.log, including the manifest response,
digest comparison, pull output, exit codes and post-failure container status.
If the release also contains a signed client maintenance package, the updater verifies its
SHA-256 value and stages it for safe replacement at the next launcher start. This updates
the GUI and maintenance scripts without replacing files in the middle of a running update.
A temporary network or registry failure is logged and the current installation still starts.
The installer also synchronizes the application database credentials with an existing
MySQL container before the services start, so an update does not break an existing database.
If an older package left a garbled desktop shortcut, copy this package over the same
installation folder and run start.bat once. The launcher will remove the stale Xianyu
shortcut and recreate it with the correct Chinese name.
The launcher can stop containers, check updates and open diagnostics without a console window.
The original stop.bat, diagnostics.bat and update.bat remain available for recovery.
The installer also performs a restricted cleanup of stale RDP ActiveX client
processes and orphaned startup entries. It never removes Windows system DLLs or
disables the Windows remote desktop service. WSLg is disabled through the
documented per-user .wslconfig setting; the previous file is retained as a
timestamped .xianyu-backup-*.bak file if it existed.
Launcher failures are also
recorded in app\logs\updater-launch.log and shown in a visible error dialog.
diagnostics.bat also creates app\logs\diagnostics-latest.txt, opens it in
Notepad, and includes Docker status, recent logs from every service, and cloud
connectivity checks. It does not dump app\.env or intentionally collect secrets;
review service logs before sharing because they may contain application data.
Startup, installation, shutdown and update-check logs are saved as startup.log,
client-update.log, install.log, shutdown.log and update-check.log in app\logs. If a PowerShell
operation fails, a persistent window displays the full error and provides
buttons to copy it or open the log folder. The window closes only when you
click Close.

Default first-login credentials are created by the application. Change them after first login.
Never share app\.env: it contains database passwords, JWT secrets and external API keys.
'@
$readme = $readme.Replace('__INSTALLER__', $installerDisplayName)
$readme = $readme.Replace('__LAUNCHER__', $launcherDisplayName)
$readme = $readme.Replace('__UPDATER__', $updaterDisplayName)
$readme = $readme.Replace('__STOPPER__', $stopperDisplayName)
$readme = $readme.Replace('__DIAGNOSTICS__', $diagnosticsDisplayName)
$readme = $readme.Replace('__CENTRAL_DOC__', $centralDeploymentDoc)
$readme | Set-Content -LiteralPath (Join-Path $OutputDirectory 'README.txt') -Encoding UTF8

Write-Host "Package created: $OutputDirectory" -ForegroundColor Green
Write-Host "Run: $OutputDirectory\install.bat" -ForegroundColor Cyan
