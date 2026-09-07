param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$ComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'
$EnvFile = Join-Path $ProjectRoot '.env'

function Get-EnvMap([string]$Path) {
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $map[$Matches[1]] = $Matches[2]
        }
    }
    return $map
}

function Set-EnvValues([string]$Path, [hashtable]$Values) {
    $lines = @(Get-Content -LiteralPath $Path)
    $written = @{}
    $updated = foreach ($line in $lines) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
            $name = $Matches[1]
            if ($Values.ContainsKey($name)) {
                $written[$name] = $true
                "$name=$($Values[$name])"
                continue
            }
        }
        $line
    }
    foreach ($name in $Values.Keys) {
        if (-not $written.ContainsKey($name)) {
            $updated += "$name=$($Values[$name])"
        }
    }
    Set-Content -LiteralPath $Path -Value $updated -Encoding utf8
}

if (-not (Test-Path -LiteralPath $ComposeFile) -or -not (Test-Path -LiteralPath $EnvFile)) {
    exit 0
}

try {
    $mysqlId = (& docker compose --project-directory $ProjectRoot --env-file $EnvFile -f $ComposeFile ps -aq mysql 2>$null | Select-Object -First 1).Trim()
    if (-not $mysqlId) { exit 0 }

    $containerEnv = @(docker inspect $mysqlId --format '{{range .Config.Env}}{{println .}}{{end}}')
    if ($LASTEXITCODE -ne 0) { exit 0 }

    $stored = @{}
    foreach ($name in @('MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE')) {
        $line = $containerEnv | Where-Object { $_ -match "^$([regex]::Escape($name))=" } | Select-Object -First 1
        if ($line) {
            $stored[$name] = ($line -replace "^$([regex]::Escape($name))=", '')
        }
    }
    if (-not $stored.ContainsKey('MYSQL_PASSWORD') -or [string]::IsNullOrWhiteSpace($stored['MYSQL_PASSWORD'])) {
        exit 0
    }

    $current = Get-EnvMap $EnvFile
    $changes = @{}
    foreach ($name in $stored.Keys) {
        if (-not $current.ContainsKey($name) -or $current[$name] -ne $stored[$name]) {
            $changes[$name] = $stored[$name]
        }
    }
    if ($changes.Count -gt 0) {
        Set-EnvValues $EnvFile $changes
        Write-Host '[xianyu] Existing MySQL credentials synchronized.' -ForegroundColor Cyan
    }
} catch {
    # Credential synchronization is best effort. The normal startup path still
    # reports the original database error if the Docker metadata is unavailable.
    exit 0
}

exit 0
