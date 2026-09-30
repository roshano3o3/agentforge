<#
.SYNOPSIS
  Run the Playwright browser end-to-end tests against the real stack.

  Needs the Docker Compose Postgres + Redis (start with docker-up.ps1). This
  script (re)creates a disposable `agentforge_e2e_test` database, starts a
  dedicated worker container on Redis DB 2 pointed at it, and runs Playwright,
  which starts its own API (port 8010, Alembic-migrated) and web dev server
  (port 3010). The worker is removed afterwards; the dev DB and dev queue are
  never touched. Screenshots are written to docs/screenshots/.

  There is no SQLite mode any more: the UI now starts runs, and runs are
  executed only by the worker, which runs only in Docker.
#>
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$E2EWorker = "agentforge-e2e-worker"

Push-Location $RepoRoot
try {
    $user = if ($env:POSTGRES_USER) { $env:POSTGRES_USER } else { "agentforge" }
    $password = if ($env:POSTGRES_PASSWORD) { $env:POSTGRES_PASSWORD } else { "agentforge" }
    # docker writes to stderr; see docker-up.ps1 for why this is relaxed.
    $ErrorActionPreference = "Continue"
    docker compose exec -T postgres dropdb -U $user --if-exists agentforge_e2e_test
    if ($LASTEXITCODE -ne 0) { throw "Postgres isn't reachable -- run .\scripts\docker-up.ps1 first" }
    docker compose exec -T postgres createdb -U $user agentforge_e2e_test
    if ($LASTEXITCODE -ne 0) { throw "createdb agentforge_e2e_test failed" }
    docker compose up -d --wait redis
    if ($LASTEXITCODE -ne 0) { throw "could not start redis" }
    docker compose exec -T redis redis-cli -n 2 FLUSHDB | Out-Null
    docker compose build worker
    if ($LASTEXITCODE -ne 0) { throw "worker image build failed" }
    docker rm -f $E2EWorker 2>$null | Out-Null
    docker compose run -d --no-deps --name $E2EWorker `
        -e "AGENTFORGE_DATABASE_URL=postgresql+asyncpg://${user}:${password}@postgres:5432/agentforge_e2e_test" `
        -e "AGENTFORGE_REDIS_URL=redis://redis:6379/2" `
        worker | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not start the e2e worker container" }

    $env:AGENTFORGE_E2E_DATABASE_URL = "postgresql+asyncpg://${user}:${password}@127.0.0.1:5432/agentforge_e2e_test"
    $env:AGENTFORGE_E2E_REDIS_URL = "redis://127.0.0.1:6379/2"
    Write-Host "Running against Postgres (agentforge_e2e_test) + Redis DB 2 + worker container $E2EWorker"

    Set-Location "$RepoRoot\apps\web"
    # Playwright's webServer logs go to stderr; the exit code is what signals failure.
    npm run test:e2e
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Write-Host "--- e2e worker logs (last 40 lines) ---"
        docker logs --tail 40 $E2EWorker
        exit $code
    }
} finally {
    $ErrorActionPreference = "Continue"
    docker rm -f $E2EWorker 2>$null | Out-Null
    Remove-Item Env:AGENTFORGE_E2E_DATABASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_E2E_REDIS_URL -ErrorAction SilentlyContinue
    Pop-Location
}
