param(
    [Parameter(Mandatory = $true)]
    [string]$StreamerUsername,
    [int]$Port = 9222,
    [int]$MaxFrames = 5000,
    [switch]$SaveRawFrames,
    [switch]$KeepBrowserCache,
    [ValidateRange(0, 1440)]
    [int]$DurationMinutes = 0
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

$StreamerUsername = $StreamerUsername.Trim().TrimStart('@')
if ($StreamerUsername -notmatch '^[A-Za-z0-9_.]+$') {
    throw 'Use a TikTok username containing only letters, numbers, underscores, or periods.'
}
if ($MaxFrames -lt 1) {
    throw 'MaxFrames must be at least 1.'
}

$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
$probeScript = Join-Path $repoRoot 'experiments\browser_ws_probe.py'
$probeRoot = Join-Path $repoRoot 'data\browser_ws_probe'
$ttsRoot = Join-Path $repoRoot 'data\tts'
$stopPath = Join-Path $ttsRoot 'stop.json'
$debugChromeLauncher = Join-Path $PSScriptRoot 'start_tiktok_debug_chrome.ps1'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Project Python environment not found: $python`nCreate .venv and install requirements.txt first."
}
if (-not (Test-Path -LiteralPath $probeScript -PathType Leaf)) {
    throw "Browser probe not found: $probeScript"
}

function Test-LoopbackPort {
    param([int]$TargetPort)

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $client.BeginConnect('127.0.0.1', $TargetPort, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(500)) {
            return $false
        }
        $client.EndConnect($pending)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function ConvertTo-ProcessArgument {
    param([string]$Value)
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Assert-DedicatedDebugChrome {
    param([int]$TargetPort)

    $portPattern = '(?:^|\s)--remote-debugging-port=' + [regex]::Escape([string]$TargetPort) + '(?:\s|$)'
    $matchingProcesses = @(
        Get-CimInstance -ClassName Win32_Process -Filter "Name = 'chrome.exe'" |
            Where-Object { $_.CommandLine -and $_.CommandLine -match $portPattern }
    )
    $expectedProfile = [System.IO.Path]::GetFullPath(
        (Join-Path $repoRoot 'data\chrome_tiktok_profile')
    ).TrimEnd('\')

    foreach ($chromeProcess in $matchingProcesses) {
        $profileMatch = [regex]::Match(
            $chromeProcess.CommandLine,
            '(?i)--user-data-dir=(?:"([^"]+)"|([^\s"]+))'
        )
        if (-not $profileMatch.Success) {
            continue
        }

        $profileValue = if ($profileMatch.Groups[1].Success) {
            $profileMatch.Groups[1].Value
        } else {
            $profileMatch.Groups[2].Value
        }
        $actualProfile = [System.IO.Path]::GetFullPath($profileValue).TrimEnd('\')
        if ([string]::Equals(
            $actualProfile,
            $expectedProfile,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
            return
        }
    }

    throw @"
CDP port $TargetPort is open, but its Chrome process is not using this project's dedicated profile:
$expectedProfile
Close that debugging Chrome and rerun this command so the isolated profile is used.
"@
}

$statePath = Join-Path $ttsRoot 'state.json'
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try {
        $existingTtsState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if (
            $existingTtsState.status -in @('watching', 'speaking') -and
            $existingTtsState.pid
        ) {
            $existingWorker = Get-Process -Id ([int]$existingTtsState.pid) -ErrorAction SilentlyContinue
            if ($existingWorker) {
                throw "A TTS worker is already running (PID $($existingTtsState.pid)). Stop it before starting this run."
            }
        }
    } catch {
        if ($_.Exception.Message -like 'A TTS worker is already running*') {
            throw
        }
        Write-Warning 'Could not inspect the previous TTS state; the worker lock will prevent a duplicate.'
    }
}

if (Test-LoopbackPort -TargetPort $Port) {
    Assert-DedicatedDebugChrome -TargetPort $Port
    Write-Host "CDP is responding on 127.0.0.1:$Port. Reusing the dedicated TikTok Chrome profile."
} else {
    Write-Host 'Starting dedicated debug Chrome with the persistent project profile...'
    & $debugChromeLauncher -ProfileDir 'data\chrome_tiktok_profile' -Port $Port

    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    while (-not (Test-LoopbackPort -TargetPort $Port) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Seconds 1
    }
    if (-not (Test-LoopbackPort -TargetPort $Port)) {
        throw "Chrome did not expose CDP on 127.0.0.1:$Port within 45 seconds."
    }
}
Assert-DedicatedDebugChrome -TargetPort $Port

Write-Host ''
Write-Host 'In the dedicated Chrome window:'
Write-Host '  1. Sign in to TikTok manually and complete any verification yourself.'
Write-Host "  2. Open https://www.tiktok.com/@$StreamerUsername/live"
Write-Host '  3. Return here and press Enter. The probe only observes browser traffic.'
if (-not $KeepBrowserCache) {
    Write-Host '  4. Probe startup clears only the dedicated Chrome HTTP cache; login and cookies stay.'
}
[void](Read-Host 'Press Enter when the LIVE page is open')

New-Item -ItemType Directory -Path $probeRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $ttsRoot 'logs') -Force | Out-Null
$previousSessionNames = @(
    Get-ChildItem -LiteralPath $probeRoot -Directory -Filter "*_$StreamerUsername" -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Name }
)
if (Test-Path -LiteralPath $stopPath) {
    Remove-Item -LiteralPath $stopPath -Force
}

$probeArguments = @(
    '-X', 'utf8',
    $probeScript,
    '--username', $StreamerUsername,
    '--browser-mode', 'cdp',
    '--cdp-url', "http://127.0.0.1:$Port",
    '--max-frames', [string]$MaxFrames
)
if ($SaveRawFrames) {
    $probeArguments += '--save-raw-frames'
}
if (-not $KeepBrowserCache) {
    $probeArguments += '--clear-browser-cache-on-start'
}
if ($DurationMinutes -gt 0) {
    $probeArguments += @('--duration-seconds', [string]($DurationMinutes * 60))
}
$probeArgumentLine = ($probeArguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' '
$probeProcess = Start-Process -FilePath $python -ArgumentList $probeArgumentLine -WorkingDirectory $repoRoot -PassThru
$ttsProcess = $null
$sessionDir = $null

try {
    Write-Host "Probe started in its own window (PID $($probeProcess.Id)). Waiting for its session folder..."
    $sessionDeadline = [DateTime]::UtcNow.AddSeconds(45)
    while ([DateTime]::UtcNow -lt $sessionDeadline) {
        $probeProcess.Refresh()
        if ($probeProcess.HasExited) {
            throw "The browser probe exited early with code $($probeProcess.ExitCode). Check the probe window."
        }

        $candidate = Get-ChildItem -LiteralPath $probeRoot -Directory -Filter "*_$StreamerUsername" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notin $previousSessionNames } |
            Sort-Object Name -Descending |
            Select-Object -First 1
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate.FullName 'session.json'))) {
            $sessionDir = $candidate
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $sessionDir) {
        throw 'The probe did not create a session folder within 45 seconds. Check the probe window.'
    }

    $sessionRelativePath = Join-Path 'data\browser_ws_probe' $sessionDir.Name
    $logStamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $logBase = Join-Path (Join-Path $ttsRoot 'logs') "browser_${StreamerUsername}_$logStamp"
    $ttsArguments = @(
        '-X', 'utf8',
        '-m', 'src.tts_worker',
        '--username', $StreamerUsername,
        '--browser-session-dir', $sessionRelativePath
    )
    $ttsArgumentLine = ($ttsArguments | ForEach-Object { ConvertTo-ProcessArgument $_ }) -join ' '
    $ttsProcess = Start-Process `
        -FilePath $python `
        -ArgumentList $ttsArgumentLine `
        -WorkingDirectory $repoRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput "$logBase.out.log" `
        -RedirectStandardError "$logBase.err.log" `
        -PassThru

    Start-Sleep -Seconds 2
    $ttsProcess.Refresh()
    if ($ttsProcess.HasExited -and $ttsProcess.ExitCode -ne 0) {
        throw "The TTS worker exited with code $($ttsProcess.ExitCode). See $logBase.err.log."
    }

    Write-Host ''
    Write-Host "[RUNNING] session=$($sessionDir.FullName)"
    Write-Host "[TTS] worker PID=$($ttsProcess.Id); audio plays through the default Windows audio device."
    Write-Host "[TTS LOG] $logBase.out.log"
    if ($DurationMinutes -gt 0) {
        Write-Host "[TIME LIMIT] $DurationMinutes minutes"
    } else {
        Write-Host 'Stop the probe with Ctrl+C in the probe window after the test or natural LIVE end.'
    }
    Write-Host 'Leave this launcher window open; it will gracefully stop the TTS worker when capture ends.'

    $noSocketWarningAt = [DateTime]::UtcNow.AddSeconds(45)
    $noSocketWarningShown = $false
    while ($true) {
        $probeProcess.Refresh()
        if ($probeProcess.HasExited) {
            break
        }

        if (-not $noSocketWarningShown -and [DateTime]::UtcNow -ge $noSocketWarningAt) {
            $currentSession = Get-Content -LiteralPath (Join-Path $sessionDir.FullName 'session.json') -Raw |
                ConvertFrom-Json
            if (-not $currentSession.target_live_websocket_open_count) {
                Write-Warning 'No TikTok LIVE WebSocket has opened yet. The page may be offline, verifying, or on a different tab; without LIVE events, TTS has nothing to speak.'
                Write-Host "Check the probe window for [PAGE SELECTED], [LIVE PREFLIGHT], and [WS DISCOVERED]. Dashboard: http://127.0.0.1:8502 (start with scripts\start_dashboard.bat)."
            }
            $noSocketWarningShown = $true
        }
        Start-Sleep -Seconds 5
    }
    Write-Host "[PROBE STOPPED] Review $($sessionDir.FullName)\session.json and events.ndjson"
} finally {
    if ($ttsProcess) {
        $ttsProcess.Refresh()
        if (-not $ttsProcess.HasExited) {
            $stopPayload = @{ requested_at_local = (Get-Date).ToString('o') } | ConvertTo-Json -Compress
            [System.IO.File]::WriteAllText($stopPath, $stopPayload, [System.Text.UTF8Encoding]::new($false))
            try {
                Wait-Process -Id $ttsProcess.Id -Timeout 20 -ErrorAction Stop
                Write-Host '[TTS STOPPED] Graceful stop completed.'
            } catch {
                Write-Warning "TTS worker has not exited yet; inspect $logBase.err.log and data\tts\state.json."
            }
        }
    }
    if ($sessionDir -and (Test-Path -LiteralPath (Join-Path $sessionDir.FullName 'session.json') -PathType Leaf)) {
        try {
            & $python -m src.live_summary --session-dir $sessionDir.FullName
            if ($LASTEXITCODE -ne 0) {
                throw "Summary generator exited with code $LASTEXITCODE."
            }
            Write-Host "[REPORT] $(Join-Path $sessionDir.FullName 'live_summary.md')"
        } catch {
            Write-Warning "Could not update the final LIVE report: $($_.Exception.Message)"
        }
    }
}
