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
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) { $failures.Add("PowerShell parse failed: $Path") }
}

function Check-BatAscii([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    if (($bytes | Where-Object { $_ -gt 127 }).Count -gt 0) { $failures.Add("BAT contains non-ASCII bytes: $Path") }
}

Write-Host '[xianyu] Checking commercial delivery source.' -ForegroundColor Cyan
foreach ($path in @(
    'docker-compose.yml', 'VERSION.txt', 'deploy\update-xianyu-gui.ps1',
    'deploy\update-signing-public-key.xml', 'tools\build-windows-installer.ps1',
    'tools\build-offline-image-bundle.ps1', 'tools\import-offline-image-bundle.ps1',
    'tools\windows-installer-apply-client-update.ps1', 'tools\windows-reset-xianyu-docker.ps1',
    '.github\workflows\build-and-publish.yml'
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
    & python -m py_compile (Join-Path $ProjectRoot 'tools\build_xianyu_release_manifest.py'), (Join-Path $ProjectRoot 'tools\generate-update-signing-key.py')
    if ($LASTEXITCODE -ne 0) { $failures.Add('Python syntax check failed.') }
    Push-Location $ProjectRoot
    try {
        & python -c "import pathlib,yaml; yaml.safe_load(pathlib.Path('.github/workflows/build-and-publish.yml').read_text(encoding='utf-8')); print('workflow yaml ok')" 2>$null
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
        'package-manifest.json', 'README.txt', 'resources\images\offline-manifest.json',
        'app\deploy\update-signing-public-key.xml', 'scripts\apply-client-update.ps1',
        'resources\reset-xianyu-docker.ps1'
    )) { Require-Path (Join-Path $package $path) "package\$path" }
    $imageCount = @(Get-ChildItem -LiteralPath (Join-Path $package 'resources\images') -Filter '*.tar.gz' -File -ErrorAction SilentlyContinue).Count
    if ($imageCount -ne 6) { $failures.Add("Package must contain 6 image archives; found $imageCount") }
    $guiCount = @(Get-ChildItem -LiteralPath $package -Filter '*.exe' -File -ErrorAction SilentlyContinue).Count
    if ($guiCount -ne 5) { $failures.Add("Package must contain 5 GUI executables; found $guiCount") }
    if (Test-Path -LiteralPath (Join-Path $package 'README.txt')) {
        $readme = Get-Content -LiteralPath (Join-Path $package 'README.txt') -Raw
        if ($readme -match '[\u95c2\u7e60\u7487\u7f01\u951f]') { $failures.Add('Package README contains mojibake.') }
    }
}

if ($failures.Count -gt 0) {
    Write-Host '[xianyu] Commercial delivery validation failed:' -ForegroundColor Red
    $failures | ForEach-Object { Write-Host " - $_" -ForegroundColor Red }
    exit 1
}
Write-Host '[xianyu] Commercial delivery validation passed.' -ForegroundColor Green
