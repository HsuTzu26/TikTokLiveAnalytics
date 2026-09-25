param(
    [Parameter(Mandatory = $true)]
    [int]$ProcessId,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath,

    [ValidateRange(5, 3600)]
    [int]$IntervalSeconds = 60,

    [ValidateRange(1, 1440)]
    [int]$DurationMinutes = 120
)

$outputFile = [System.IO.Path]::GetFullPath($OutputPath)
$outputDirectory = [System.IO.Path]::GetDirectoryName($outputFile)
if (-not [string]::IsNullOrWhiteSpace($outputDirectory)) {
    New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
}

$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText(
    $outputFile,
    "timestamp_utc,process_id,cpu_seconds,rss_bytes`n",
    $utf8
)

$stopAt = [DateTime]::UtcNow.AddMinutes($DurationMinutes)
$sampleCount = 0
$stopReason = "duration_elapsed"

while ([DateTime]::UtcNow -lt $stopAt) {
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        $stopReason = "process_exited"
        break
    }

    $timestamp = [DateTime]::UtcNow.ToString("o")
    $cpuSeconds = if ($null -eq $process.CPU) { "" } else { $process.CPU.ToString("0.000", [Globalization.CultureInfo]::InvariantCulture) }
    $rssBytes = $process.WorkingSet64
    $row = "$timestamp,$ProcessId,$cpuSeconds,$rssBytes`n"
    [System.IO.File]::AppendAllText($outputFile, $row, $utf8)
    $sampleCount += 1
    Start-Sleep -Seconds $IntervalSeconds
}

Write-Output "samples=$sampleCount; stop_reason=$stopReason; output=$outputFile"
