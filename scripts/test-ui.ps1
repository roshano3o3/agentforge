<#
.SYNOPSIS
  Run the Playwright browser end-to-end test. Spins up its own API (fresh
  temp SQLite DB, port 8010) and web dev server (port 3010) automatically --
  does not require dev-api.ps1 / dev-web.ps1 to be running, and does not
  touch the shared dev DB. Screenshots are written to docs/screenshots/.

  -Postgres: the API uses a freshly (re)created `agentforge_e2e_test`
  database in the Docker Compose Postgres instead (start it with
  docker-up.ps1 first), with the schema built by the Alembic migrations.
#>
param(
    [switch]$Postgres
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Push-Location $RepoRoot
try {
    if ($Postgres) {
        $user = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "agentforge" }
        $password = if ($env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD } else { "agentforge" }
        # docker writes to stderr; see docker-up.ps1 for why this is relaxed.
        $ErrorActionPreference = "Continue"
        docker compose exec -T postgres dropdb -U $user --if-exists agentforge_e2e_test
        if ($LASTEXITCODE -ne 0) { throw "Postgres isn't reachable -- run .\scripts\docker-up.ps1 first" }
        docker compose exec -T postgres createdb -U $user agentforge_e2e_test
        if ($LASTEXITCODE -ne 0) { throw "createdb agentforge_e2e_test failed" }
        $ErrorActionPreference = "Stop"
        $env:AGENTFORGE_E2E_DATABASE_URL = "postgresql+asyncpg://${user}:${password}@127.0.0.1:5432/agentforge_e2e_test"
        Write-Host "Running against Postgres: agentforge_e2e_test"
    }
    Set-Location "$RepoRoot\apps\web"
    # Playwright's webServer logs go to stderr; the exit code is what signals failure.
    $ErrorActionPreference = "Continue"
    npm run test:e2e
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Remove-Item Env:AGENTFORGE_E2E_DATABASE_URL -ErrorAction SilentlyContinue
    Pop-Location
}
