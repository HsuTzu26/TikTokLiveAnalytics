param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8502
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$logDir = Join-Path $repoRoot 'data\system'
$stdoutPath = Join-Path $logDir 'dashboard.stdout.log'
$stderrPath = Join-Path $logDir 'dashboard.stderr.log'
$url = "http://127.0.0.1:$Port"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python environment not found: $python"
}

function Test-DashboardHealth {
    try {
        return (Invoke-WebRequest -Uri "$url/_stcore/health" -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200
    } catch {
        return $false
    }
}

if (-not (Test-DashboardHealth)) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        throw "Port $Port is in use by another service. Choose another port with -Port."
    }

    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $arguments = @(
        '-m', 'streamlit', 'run', 'app\dashboard.py',
        '--server.address', '127.0.0.1',
        '--server.port', [string]$Port,
        '--server.headless', 'true'
    )
    Start-Process `
        -FilePath $python `
        -ArgumentList $arguments `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath | Out-Null

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline -and -not (Test-DashboardHealth)) {
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-DashboardHealth)) {
        throw "Dashboard did not start. Check $stderrPath"
    }
}

Write-Host "Dashboard: $url"
Write-Host 'This starts the local web UI only; it does not start the Watcher or a TikTok collector.'
Start-Process $url
