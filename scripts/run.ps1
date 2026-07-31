$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$VenvPython = Join-Path $PWD ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Среда не установлена. Сначала выполните .\scripts\setup.ps1"
}

& $VenvPython -m app serve
