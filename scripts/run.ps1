$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    throw "Repository-local Python executable is unavailable."
}

$logsDirectory = Join-Path $repoRoot "logs"
New-Item -ItemType Directory -Force -LiteralPath $logsDirectory | Out-Null

& $pythonExe -m uvicorn ticket_reviewer.main:app --host 127.0.0.1 --port 8765
exit $LASTEXITCODE
