<#
.SYNOPSIS
  Apply Alembic migrations to whatever AGENTFORGE_DATABASE_URL currently
  points at (default: local SQLite file apps/api/agentforge_dev.db).

  For the Postgres/Docker path, migrations run automatically in the api
  container's entrypoint -- you don't need this script there.
#>
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

Push-Location "$RepoRoot\apps\api"
try {
    & $venvPython -m alembic upgrade head
} finally {
    Pop-Location
}
