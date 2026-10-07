<#
.SYNOPSIS
  Run the Python test suite (unit + integration + e2e).

  Default: no Docker -- an in-memory/temp-file SQLite database, not the dev DB.
  The integration tests call the worker's run executor in-process; the e2e
  tests that need the real worker are skipped.

  -Postgres: the same suite against a dedicated `agentforge_test` database in
  the Docker Compose Postgres (start the stack with docker-up.ps1 first),
  plus a dedicated worker container on Redis DB 1, so the e2e tests go
  CLI -> API -> Redis -> Docker worker -> Postgres for real. The schema is
  built by the Alembic migrations; the dev `agentforge` database and the dev
  queue (Redis DB 0) are never touched.
#>
param(
    [switch]$Postgres
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$TestWorker = "agentforge-test-worker"

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
        docker compose up -d --wait redis
        if ($LASTEXITCODE -ne 0) { throw "could not start redis" }
        # Rebuild so the test worker runs the code in this checkout (cached layers make this quick).
        $env:AGENTFORGE_GIT_SHA = & (Join-Path $PSScriptRoot "git-sha.ps1")  # the worker image records it
        docker compose build worker
        if ($LASTEXITCODE -ne 0) { throw "worker image build failed" }
        docker rm -f $TestWorker 2>$null | Out-Null
        docker compose run -d --no-deps --name $TestWorker `
            -e "AGENTFORGE_DATABASE_URL=postgresql+asyncpg://${user}:${password}@postgres:5432/agentforge_test" `
            -e "AGENTFORGE_REDIS_URL=redis://redis:6379/1" `
            worker | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "could not start the test worker container" }
        $ErrorActionPreference = "Stop"
        $env:AGENTFORGE_TEST_DATABASE_URL = "postgresql+asyncpg://${user}:${password}@127.0.0.1:5432/agentforge_test"
        $env:AGENTFORGE_TEST_REDIS_URL = "redis://127.0.0.1:6379/1"
        Write-Host "Running against Postgres (agentforge_test) + Redis DB 1 + worker container $TestWorker"
    }
    & $venvPython -m pytest tests -v
    $code = $LASTEXITCODE
    if ($Postgres -and $code -ne 0) {
        Write-Host "--- test worker logs (last 40 lines) ---"
        $ErrorActionPreference = "Continue"
        docker logs --tail 40 $TestWorker
    }
    if ($code -ne 0) { exit $code }
} finally {
    if ($Postgres) {
        $ErrorActionPreference = "Continue"
        docker rm -f $TestWorker 2>$null | Out-Null
    }
    Remove-Item Env:AGENTFORGE_TEST_DATABASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:AGENTFORGE_TEST_REDIS_URL -ErrorAction SilentlyContinue
    Pop-Location
}
