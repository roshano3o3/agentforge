<#
.SYNOPSIS
  Stop and remove the Docker Compose services (Postgres data volume is
  preserved; pass -Volumes to wipe it too).
#>
param(
    [switch]$Volumes
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# See docker-up.ps1: docker's stderr progress must not abort the script;
# the exit code is what signals failure.
$ErrorActionPreference = "Continue"
if ($Volumes) {
    docker compose down -v
} else {
    docker compose down
}
if ($LASTEXITCODE -ne 0) { throw "docker compose down failed (exit $LASTEXITCODE)" }
