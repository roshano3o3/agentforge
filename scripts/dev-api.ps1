<#
.SYNOPSIS
  Run the AgentForge API natively on Windows against local SQLite
  (apps/api/agentforge_dev.db), bound to 127.0.0.1 only.
#>
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

Push-Location "$RepoRoot\apps\api"
try {
    & $venvPython -m uvicorn agentforge_api.main:app --host 127.0.0.1 --port 8000
} finally {
    Pop-Location
}
