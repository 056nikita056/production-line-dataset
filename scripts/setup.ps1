$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$PythonCommand = $null
$PythonArguments = @()

if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($Version in @("-3.12", "-3.11")) {
        try {
            & py $Version --version *> $null
            if ($LASTEXITCODE -eq 0) {
                $PythonCommand = "py"
                $PythonArguments = @($Version)
                break
            }
        }
        catch {
            continue
        }
    }
}

if (-not $PythonCommand) {
    foreach ($Candidate in @("python3.12", "python3.11", "python")) {
        if (Get-Command $Candidate -ErrorAction SilentlyContinue) {
            $PythonCommand = $Candidate
            break
        }
    }
}

if (-not $PythonCommand) {
    throw "Требуется Python 3.11 или 3.12."
}

& $PythonCommand @PythonArguments -c "import sys; raise SystemExit(0 if sys.version_info[:2] in ((3, 11), (3, 12)) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Требуется Python 3.11 или 3.12."
}

& $PythonCommand @PythonArguments -m venv .venv
$VenvPython = Join-Path $PWD ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e .

Write-Host "Установка завершена."
Write-Host "Выполните: codex login"
Write-Host "Затем: .venv\Scripts\python.exe -m app doctor"
Write-Host "Запуск: .\scripts\run.ps1"
