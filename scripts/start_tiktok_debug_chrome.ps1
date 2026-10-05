param(
    [string]$ChromePath,
    [string]$ProfileDir,
    [int]$Port = 9222
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($ChromePath)) {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
        (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    $ChromePath = $candidates | Select-Object -First 1
}

if (-not $ChromePath -or -not (Test-Path -LiteralPath $ChromePath)) {
    throw 'Chrome was not found. Pass -ChromePath with the full path to chrome.exe.'
}

if ([string]::IsNullOrWhiteSpace($ProfileDir)) {
    $ProfileDir = Join-Path $repoRoot 'data\chrome_tiktok_profile'
} elseif (-not [System.IO.Path]::IsPathRooted($ProfileDir)) {
    $ProfileDir = Join-Path $repoRoot $ProfileDir
}

$ProfileDir = [System.IO.Path]::GetFullPath($ProfileDir)
$defaultChromeProfile = Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data'
if (
    $ProfileDir.TrimEnd('\') -ieq $defaultChromeProfile.TrimEnd('\') -or
    $ProfileDir.StartsWith(
        $defaultChromeProfile.TrimEnd('\') + '\',
        [System.StringComparison]::OrdinalIgnoreCase
    )
) {
    throw 'The everyday Chrome profile is not allowed. Use the dedicated TikTok profile directory.'
}
New-Item -ItemType Directory -Path $ProfileDir -Force | Out-Null

$arguments = @(
    "--remote-debugging-port=$Port",
    '--remote-debugging-address=127.0.0.1',
    "--user-data-dir=`"$ProfileDir`"",
    '--no-first-run',
    '--no-default-browser-check',
    'https://www.tiktok.com/'
)
Start-Process -FilePath $ChromePath -ArgumentList $arguments

Write-Host "Started dedicated debug Chrome."
Write-Host "Profile: $ProfileDir"
Write-Host "CDP endpoint: http://127.0.0.1:$Port"
Write-Host 'Log into TikTok manually and complete any verification in Chrome.'
