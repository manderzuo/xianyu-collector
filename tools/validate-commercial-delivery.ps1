param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PackageRoot = '',
    [switch]$SkipDocker
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path.TrimEnd('\')
$failures = New-Object System.Collections.Generic.List[string]

function Require-Path([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) { $failures.Add("Missing ${Label}: $Path") }
}

function Check-PowerShell([string]$Path) {
    $parseTokens = $null
    $parseErrors = $null
    $source = [IO.File]::ReadAllText($Path)
    [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$parseTokens, [ref]$parseErrors) | Out-Null
    $parseErrorItems = @($parseErrors)
    if ($parseErrorItems.Count -gt 0) {
        $failures.Add("PowerShell parse failed: $Path ($($parseErrorItems[0].Message))")
    }
}

function Check-BatAscii([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    if (($bytes | Where-Object { $_ -gt 127 }).Count -gt 0) { $failures.Add("BAT contains non-ASCII bytes: $Path") }
}

Write-Host '[xianyu] Checking commercial delivery source.' -ForegroundColor Cyan
foreach ($path in @(
    'docker-compose.yml', 'VERSION.txt', 'deploy\update-xianyu-gui.ps1',
    'deploy\update-signing-public-key.xml', 'tools\build-windows-installer.ps1',
    'tools\build-offline-image-bundle.ps1', 'tools\build-offline-registry-bundle.ps1', 'tools\add-offline-infrastructure-bundle.ps1', 'tools\import-offline-image-bundle.ps1',
    'tools\windows-installer-apply-client-update.ps1', 'tools\windows-installer-cleanup-rdp.ps1', 'tools\windows-installer-repair-offline-install.ps1', 'tools\windows-installer-repair-offline-runtime.ps1', 'tools\windows-reset-xianyu-docker.ps1',
    '.github\workflows\build-and-publish.yml', '.github\workflows\deploy-cloud-auth.yml'
)) { Require-Path (Join-Path $ProjectRoot $path) $path }

Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'deploy') -Filter '*.ps1' -File -Recurse | ForEach-Object { Check-PowerShell $_.FullName }
Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'tools') -Filter '*.ps1' -File -Recurse | ForEach-Object { Check-PowerShell $_.FullName }

$batRoot = Join-Path $ProjectRoot 'tools'
Get-ChildItem -LiteralPath $batRoot -Filter '*.bat' -File | ForEach-Object { Check-BatAscii $_.FullName }

$bashRoot = Join-Path $ProjectRoot 'deploy\server-migration'
if (Get-Command bash -ErrorAction SilentlyContinue) {
    Push-Location $ProjectRoot
    try {
        bash -n deploy/server-migration/*.sh
        if ($LASTEXITCODE -ne 0) { $failures.Add('Bash syntax check failed.') }
    } finally { Pop-Location }
} else { Write-Host '[xianyu] bash not found; migration syntax check skipped.' -ForegroundColor Yellow }

if (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonFiles = @(
        (Join-Path $ProjectRoot 'tools\build_xianyu_release_manifest.py'),
        (Join-Path $ProjectRoot 'tools\generate-update-signing-key.py')
    ) + @(Get-ChildItem -LiteralPath (Join-Path $ProjectRoot 'deploy\cloud_auth') -Filter '*.py' -File | Select-Object -ExpandProperty FullName)
    & python -m py_compile $pythonFiles
    if ($LASTEXITCODE -ne 0) { $failures.Add('Python syntax check failed.') }
    Push-Location $ProjectRoot
    try {
        & python -c "import pathlib,yaml; [yaml.safe_load(p.read_text(encoding='utf-8')) for p in pathlib.Path('.github/workflows').glob('*.yml')]; print('workflow yaml ok')" 2>$null
        if ($LASTEXITCODE -ne 0) { $failures.Add('Workflow YAML check failed.') }
    } finally { Pop-Location }
} else { Write-Host '[xianyu] python not found; Python checks skipped.' -ForegroundColor Yellow }

if (-not $SkipDocker -and (Get-Command docker -ErrorAction SilentlyContinue)) {
    Push-Location $ProjectRoot
    try {
        docker compose --env-file .env.example -f docker-compose.yml config --quiet
        if ($LASTEXITCODE -ne 0) { $failures.Add('Docker Compose source configuration is invalid.') }
        docker compose --env-file .env.example -f docker-compose.yml -f deploy/server-migration/docker-compose.api-scale.yml config --quiet
        if ($LASTEXITCODE -ne 0) { $failures.Add('Docker Compose API scale configuration is invalid.') }
    } finally { Pop-Location }
} else { Write-Host '[xianyu] Docker check skipped.' -ForegroundColor Yellow }

if ($PackageRoot) {
    $package = (Resolve-Path -LiteralPath $PackageRoot).Path.TrimEnd('\')
    Write-Host "[xianyu] Checking package: $package" -ForegroundColor Cyan
    foreach ($path in @(
        'package-manifest.json', 'README.txt', 'resources\images\offline-manifest.json', 'resources\import-offline-image-bundle.ps1', 'resources\repair-offline-runtime.ps1', 'resources\add-offline-infrastructure-bundle.ps1', 'inject-offline-images.bat',
        'app\deploy\update-signing-public-key.xml', 'scripts\apply-client-update.ps1',
        'resources\reset-xianyu-docker.ps1', 'resources\cleanup-rdp.ps1', 'xianyu-installer.exe', 'xianyu-launcher.exe',
        'xianyu-updater.exe', 'xianyu-stopper.exe', 'xianyu-diagnostics.exe'
    )) { Require-Path (Join-Path $package $path) "package\$path" }
    $packageManifest = Get-Content -LiteralPath (Join-Path $package 'package-manifest.json') -Raw | ConvertFrom-Json
    $expectedImageCount = if ([bool]$packageManifest.offline_application_images_only) { 4 } else { 6 }
    $imageRoot = Join-Path $package 'resources\images'
    $imageCount = @(Get-ChildItem -LiteralPath $imageRoot -Filter '*.tar.gz' -File -ErrorAction SilentlyContinue).Count
    if ($imageCount -ne $expectedImageCount) { $failures.Add("Package must contain $expectedImageCount image archives; found $imageCount") }
    $offlineManifest = Get-Content -LiteralPath (Join-Path $imageRoot 'offline-manifest.json') -Raw | ConvertFrom-Json
    foreach ($entry in @($offlineManifest.images)) {
        $archiveName = [string]$entry.archive
        if ([string]::IsNullOrWhiteSpace($archiveName) -or $archiveName -match '[\\/:]' -or $archiveName -in @('.', '..')) {
            $failures.Add("Offline manifest contains an unsafe archive name: $archiveName")
            continue
        }
        $archivePath = Join-Path $imageRoot $archiveName
        if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
            $failures.Add("Offline manifest archive is missing: $archiveName")
            continue
        }
        $actualHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne ([string]$entry.sha256).Trim().ToLowerInvariant()) { $failures.Add("Offline archive hash mismatch: $archiveName") }
    }
    $guiCount = @(Get-ChildItem -LiteralPath $package -Filter '*.exe' -File -ErrorAction SilentlyContinue).Count
    if ($guiCount -ne 10) { $failures.Add("Package must contain 10 GUI executables; found $guiCount") }
    if (Test-Path -LiteralPath (Join-Path $package 'README.txt')) {
        $readme = [IO.File]::ReadAllText((Join-Path $package 'README.txt'), [Text.Encoding]::UTF8)
        if ($readme -match '[\u95c2\u7e60\u7487\u7f01\u951f]') { $failures.Add('Package README contains mojibake.') }
    }
}

if ($failures.Count -gt 0) {
    Write-Host '[xianyu] Commercial delivery validation failed:' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" -ForegroundColor Red }
    exit 1
}
Write-Host '[xianyu] Commercial delivery validation passed.' -ForegroundColor Green
