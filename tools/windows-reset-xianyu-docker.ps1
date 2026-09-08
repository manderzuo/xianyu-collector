param(
    [string]$AppRoot = '',
    [switch]$AllDockerData,
    [switch]$KeepImages,
    [switch]$Preview,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function Fail([string]$Message) {
    throw "Docker reset failed: $Message"
}

function Invoke-Docker([string[]]$Arguments, [string]$Label) {
    Write-Host "[xianyu] $Label" -ForegroundColor Cyan
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & docker @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    foreach ($line in $output) { Write-Host ([string]$line) }
    if ($exitCode -ne 0) { Fail "$Label (exit code $exitCode)" }
}

function Get-DockerLines([string[]]$Arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & docker @Arguments 2>$null
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0) { Fail "Docker query failed (exit code $exitCode)" }
    return @($output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker CLI was not found.' }
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Docker Desktop is not running.' }

if ($AllDockerData) {
    if ($Preview) {
        Write-Host '[xianyu] PREVIEW: would remove every Docker container, volume, user network, image and build cache.' -ForegroundColor Yellow
        exit 0
    }
    if (-not $Force) {
        Fail 'Global cleanup is destructive. Re-run with -AllDockerData -Force only after confirming that every Docker project may be deleted.'
    }

    Write-Host '[xianyu] WARNING: removing ALL Docker data on this computer.' -ForegroundColor Red
    $containers = Get-DockerLines @('ps', '-aq')
    if ($containers.Count -gt 0) { Invoke-Docker (@('rm', '-f') + $containers) 'Removing all containers' }

    $volumes = Get-DockerLines @('volume', 'ls', '-q')
    if ($volumes.Count -gt 0) { Invoke-Docker (@('volume', 'rm', '-f') + $volumes) 'Removing all volumes' }

    $networks = @(Get-DockerLines @('network', 'ls', '--format', '{{.Name}}') | Where-Object { $_ -notin @('bridge', 'host', 'none') })
    if ($networks.Count -gt 0) { Invoke-Docker (@('network', 'rm') + $networks) 'Removing user networks' }

    $images = @(Get-DockerLines @('image', 'ls', '-aq') | Sort-Object -Unique)
    if ($images.Count -gt 0) { Invoke-Docker (@('image', 'rm', '-f') + $images) 'Removing all images' }
    Invoke-Docker @('builder', 'prune', '-af') 'Removing build cache'
    Invoke-Docker @('buildx', 'prune', '-af') 'Removing BuildKit cache'
    Invoke-Docker @('system', 'prune', '-af', '--volumes') 'Final Docker cleanup'
    Write-Host '[xianyu] Global Docker cleanup completed.' -ForegroundColor Green
    exit 0
}

if ([string]::IsNullOrWhiteSpace($AppRoot)) {
    $candidates = @(
        (Join-Path $PSScriptRoot '..\app'),
        (Join-Path $PSScriptRoot '..\xianyu-one-click-installer\app'),
        (Join-Path $PSScriptRoot '..\docker-compose.yml')
    )
    foreach ($candidate in $candidates) {
        if ((Test-Path -LiteralPath $candidate -PathType Container) -and (Test-Path -LiteralPath (Join-Path $candidate 'docker-compose.yml'))) {
            $AppRoot = $candidate
            break
        }
        if ((Test-Path -LiteralPath $candidate -PathType Leaf) -and ([IO.Path]::GetFileName($candidate) -eq 'docker-compose.yml')) {
            $AppRoot = Split-Path -Parent $candidate
            break
        }
    }
}
if ([string]::IsNullOrWhiteSpace($AppRoot)) { Fail 'Project app root was not found. Pass -AppRoot PATH.' }
$AppRoot = (Resolve-Path -LiteralPath $AppRoot).Path.TrimEnd('\')
$compose = Join-Path $AppRoot 'docker-compose.yml'
if (-not (Test-Path -LiteralPath $compose)) { Fail "docker-compose.yml was not found under $AppRoot" }
$envFile = if (Test-Path -LiteralPath (Join-Path $AppRoot '.env')) { Join-Path $AppRoot '.env' } else { Join-Path $AppRoot '.env.example' }
if (-not (Test-Path -LiteralPath $envFile)) { Fail "No .env or .env.example was found under $AppRoot" }

if ($Preview) {
    Write-Host "[xianyu] PREVIEW: would stop and remove the Compose project under $AppRoot, including its volumes and $(if ($KeepImages) { 'not its images' } else { 'service images' })." -ForegroundColor Yellow
    exit 0
}

$composeArgs = @('--project-directory', $AppRoot, '--env-file', $envFile, '-f', $compose, 'down', '--volumes', '--remove-orphans')
if (-not $KeepImages) { $composeArgs += '--rmi'; $composeArgs += 'all' }
Invoke-Docker $composeArgs 'Removing the Xianyu Compose project'
Write-Host "[xianyu] Xianyu project cleanup completed: $AppRoot" -ForegroundColor Green
