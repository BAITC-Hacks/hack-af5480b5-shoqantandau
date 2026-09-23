$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        throw 'Install Python 3.12, then run this script again.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python virtual environment.' }
}
$demoPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
& $demoPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $demoPython manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Database setup failed.' }
Write-Host 'Open http://127.0.0.1:8000/ - demo login: manager / manager12345'
& $demoPython manage.py runserver 127.0.0.1:8000
