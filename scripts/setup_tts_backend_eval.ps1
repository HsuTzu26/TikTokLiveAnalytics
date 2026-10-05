param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('kokoro', 'melo')]
    [string]$Engine
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$basePython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$venvPath = Join-Path $repoRoot ".venv_tts_eval_$Engine"
$python = Join-Path $venvPath 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $basePython)) {
    throw "Project Python 3.12 was not found: $basePython"
}

if (-not (Test-Path -LiteralPath $python)) {
    & $basePython -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Could not create isolated $Engine environment" }
}

if ($Engine -eq 'kokoro') {
    & $python -m pip install 'sherpa-onnx==1.13.8' 'sherpa-onnx-bin==1.13.8' soundfile
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the Kokoro evaluation dependencies' }
    Write-Host 'Next: run scripts/download_kokoro_eval_model.py, then experiments/tts_backend_benchmark.py --engine kokoro.'
    exit 0
}

# Upstream documents Docker as its recommended Windows route. A Docker runtime
# is not installed on this machine, so this isolated native setup is a
# compatibility trial and does not alter the production environment.
& $python -m pip install 'git+https://github.com/myshell-ai/MeloTTS.git'
if ($LASTEXITCODE -ne 0) {
    throw 'MeloTTS native Windows install failed. Production dependencies remain untouched; use the upstream Docker route or review the isolated install log.'
}
& $python -m unidic download
if ($LASTEXITCODE -ne 0) { throw 'MeloTTS installed but its required UniDic data download failed' }
Write-Host 'Next: run experiments/tts_backend_benchmark.py --engine melo --device cpu.'
