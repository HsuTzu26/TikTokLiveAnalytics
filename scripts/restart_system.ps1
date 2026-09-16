param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$pidFile = Join-Path $root 'data\watcher\watcher.pid'
$stopFile = Join-Path $root 'data\watcher\watcher.stop'
$logDir = Join-Path $root 'data\system'
Set-Location -LiteralPath $root

function Get-ProjectProcesses {
    $all = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'")
    foreach ($process in $all) {
        $current = $process
        $seen = @{}
        while ($current -and -not $seen.ContainsKey([int]$current.ProcessId)) {
            $seen[[int]$current.ProcessId] = $true
            if ($current.CommandLine -and $current.CommandLine.Contains($python)) {
                $process
                break
            }
            $parentId = $current.ParentProcessId
            $current = $all | Where-Object ProcessId -eq $parentId | Select-Object -First 1
        }
    }
}

foreach ($relative in @('.venv\Scripts\python.exe','src\watcher.py','app\dashboard.py','watchlist.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $relative))) { throw "Missing required file: $relative" }
}
$processes = @(Get-ProjectProcesses)
if ($CheckOnly) {
    Write-Host "Checks passed. Project Python processes: $($processes.Count). No services changed."
    exit 0
}

# Load updated Windows environment settings without displaying credentials.
foreach ($name in @('TIKTOOL_API_KEY','TIKTOK_COOKIE_HEADER')) {
    $value = [Environment]::GetEnvironmentVariable($name, 'User')
    if ([string]::IsNullOrWhiteSpace($value)) { $value = [Environment]::GetEnvironmentVariable($name, 'Machine') }
    if (-not [string]::IsNullOrWhiteSpace($value)) { [Environment]::SetEnvironmentVariable($name, $value, 'Process') }
}

$watchers = @($processes | Where-Object { $_.CommandLine -match 'watcher\.py' })
if (Test-Path -LiteralPath $pidFile) {
    $watcherId = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
    $existing = Get-Process -Id $watcherId -ErrorAction SilentlyContinue
    if ($existing -and -not ($watchers | Where-Object ProcessId -eq $watcherId)) {
        throw 'Watcher PID cannot be verified as belonging to this project; restart aborted.'
    }
}
if ($watchers.Count) {
    Write-Host 'Requesting graceful Watcher shutdown (collectors flush their data)...'
    Set-Content -LiteralPath $stopFile -Value 'requested' -Encoding ascii
    $deadline = (Get-Date).AddSeconds(45)
    do {
        Start-Sleep -Milliseconds 500
        $remaining = @(Get-ProjectProcesses | Where-Object { $_.CommandLine -match 'watcher\.py|collector\.py' })
    } while ($remaining.Count -and (Get-Date) -lt $deadline)
    if ($remaining.Count) { throw 'Watcher/collectors did not stop within 45 seconds. No forced shutdown was performed.' }
} elseif ($processes | Where-Object { $_.CommandLine -match 'collector\.py' }) {
    throw 'Standalone collector detected. Stop it gracefully before restarting.'
}

$dashboards = @(Get-ProjectProcesses | Where-Object { $_.CommandLine -match 'streamlit\s+run.*dashboard\.py' })
foreach ($process in ($dashboards | Sort-Object ParentProcessId -Descending)) {
    Stop-Process -Id $process.ProcessId -ErrorAction SilentlyContinue
}
Start-Sleep -Seconds 1
if (Get-NetTCPConnection -LocalPort 8501 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8501 is still occupied. No unrelated process was stopped.'
}
foreach ($path in @($pidFile,$stopFile)) {
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
}
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
Start-Process -FilePath $python -ArgumentList @('src\watcher.py','--config','watchlist.json') -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'watcher.stdout.log') -RedirectStandardError (Join-Path $logDir 'watcher.stderr.log') | Out-Null
$deadline = (Get-Date).AddSeconds(15)
do { Start-Sleep -Milliseconds 500 } while (-not (Test-Path -LiteralPath $pidFile) -and (Get-Date) -lt $deadline)
if (-not (Test-Path -LiteralPath $pidFile)) { throw 'Watcher failed to start; inspect data/system/watcher.stderr.log.' }
$newWatcherId = [int](Get-Content -LiteralPath $pidFile -Raw).Trim()
if (-not (Get-Process -Id $newWatcherId -ErrorAction SilentlyContinue)) { throw 'New Watcher process exited.' }
Start-Process -FilePath $python -ArgumentList @('-m','streamlit','run','app\dashboard.py','--server.headless','true','--server.address','127.0.0.1','--server.port','8501') -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'streamlit.stdout.log') -RedirectStandardError (Join-Path $logDir 'streamlit.stderr.log') | Out-Null
$deadline = (Get-Date).AddSeconds(20)
$healthy = $false
do {
    Start-Sleep -Milliseconds 500
    try { $healthy = (Invoke-WebRequest -Uri 'http://127.0.0.1:8501/_stcore/health' -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch { }
} while (-not $healthy -and (Get-Date) -lt $deadline)
if (-not $healthy) { throw 'Streamlit failed its health check; inspect data/system/streamlit.stderr.log. Watcher remains running.' }
Write-Host "Restart complete. Watcher PID: $newWatcherId"
Write-Host 'Dashboard: http://127.0.0.1:8501'
Write-Host 'Startup logs: data/system; collection logs: data/watcher/watcher.log'
