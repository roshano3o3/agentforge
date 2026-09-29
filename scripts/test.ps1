<#
.SYNOPSIS
  Run the Python test suite (unit + integration + e2e). No Docker required
  -- these use an in-memory/temp-file SQLite database, not the dev DB.
#>
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

Push-Location $RepoRoot
try {
    & $venvPython -m pytest tests -v
} finally {
    Pop-Location
}
