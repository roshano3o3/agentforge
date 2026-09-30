<#
.SYNOPSIS
  Run the Python test suite (unit + integration + e2e).

  Default: no Docker -- an in-memory/temp-file SQLite database, not the dev DB.

  -Postgres: run the same suite against a dedicated `agentforge_test`
  database in the Docker Compose Postgres (start it with docker-up.ps1
  first). The schema is built by the real Alembic migrations, and tables are
  emptied before each test; the dev `agentforge` database is never touched.
#>
param(
    [switch]$Postgres
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

Push-Location $RepoRoot
try {
    if ($Postgres) {
        $user = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "agentforge" }
        $password = if ($env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD } else { "agentforge" }
        # docker writes to stderr; see docker-up.ps1 for why this is relaxed.
        $ErrorActionPreference = "Continue"
        $exists = docker compose exec -T postgres psql -U $user -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'agentforge_test'"
        if ($LASTEXITCODE -ne 0) { throw "Postgres isn't reachable -- run .\scripts\docker-up.ps1 first" }
        if ("$exists".Trim() -ne "1") {
            docker compose exec -T postgres createdb -U $user agentforge_test
            if ($LASTEXITCODE -ne 0) { throw "createdb agentforge_test failed" }
        }
        $ErrorActionPreference = "Stop"
        $env:AGENTFORGE_TEST_DATABASE_URL = "postgresql+asyncpg://${user}:${password}@127.0.0.1:5432/agentforge_test"
        Write-Host "Running against Postgres: agentforge_test"
    }
    & $venvPython -m pytest tests -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Remove-Item Env:AGENTFORGE_TEST_DATABASE_URL -ErrorAction SilentlyContinue
    Pop-Location
}
