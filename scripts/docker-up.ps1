<#
.SYNOPSIS
  Bring up Postgres + API via Docker Compose (the "real" Postgres path,
  as opposed to the no-Docker SQLite dev path). Migrations run
  automatically inside the api container on startup.

.NOTES
  Requires Docker Desktop (with WSL2) installed and running.
#>
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
}

docker compose up -d --build
Write-Host ""
Write-Host "API:      http://127.0.0.1:8000/health"
Write-Host "Postgres: 127.0.0.1:5432 (localhost only)"
Write-Host "Then run: .\scripts\seed-demo.ps1   and   .\scripts\dev-web.ps1"
