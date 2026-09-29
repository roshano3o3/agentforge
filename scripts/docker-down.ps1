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

if ($Volumes) {
    docker compose down -v
} else {
    docker compose down
}
