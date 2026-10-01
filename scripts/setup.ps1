<#
.SYNOPSIS
  One-time (or repeatable) local setup: Python venv + all local packages,
  and the Next.js frontend's node_modules.

  Does NOT touch Docker / Postgres -- this is for the no-Docker SQLite dev
  path. See README "Start the services (Docker + Postgres)" for the other
  path.
#>
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment at .venv ..."
    python -m venv .venv
}

$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

Write-Host "Upgrading pip ..."
& $venvPython -m pip install --quiet --upgrade pip

Write-Host "Installing local packages (editable, in dependency order) ..."
& $venvPython -m pip install --quiet -e "$RepoRoot\packages\core"
& $venvPython -m pip install --quiet -e "$RepoRoot\packages\evaluators"
& $venvPython -m pip install --quiet -e "$RepoRoot\packages\sdk"
& $venvPython -m pip install --quiet -e "$RepoRoot\apps\api[dev]"
# Installed on the host only so the integration tests can call the worker's
# run executor in-process. The worker *service* runs only in Docker.
& $venvPython -m pip install --quiet -e "$RepoRoot\apps\worker"
& $venvPython -m pip install --quiet -e "$RepoRoot\cli"
& $venvPython -m pip install --quiet -e "$RepoRoot\examples\rag_app"
& $venvPython -m pip install --quiet -e "$RepoRoot\examples\invoice_agent"

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Wrote .env from .env.example (Postgres/docker-compose credentials, local dev only)."
}

Write-Host "Installing frontend dependencies (apps/web) ..."
Push-Location "$RepoRoot\apps\web"
npm install
Pop-Location

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next: .\scripts\db-migrate.ps1   (creates the local SQLite dev DB)"
Write-Host "Then: .\scripts\dev-api.ps1      (in one terminal)"
Write-Host "  and: .\scripts\dev-web.ps1     (in another terminal)"
