<#
.SYNOPSIS
  Run the Playwright browser end-to-end test. Spins up its own API (fresh
  temp SQLite DB, port 8010) and web dev server (port 3010) automatically --
  does not require dev-api.ps1 / dev-web.ps1 to be running, and does not
  touch the shared dev DB. Screenshots are written to docs/screenshots/.
#>
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Push-Location "$RepoRoot\apps\web"
try {
    npm run test:e2e
} finally {
    Pop-Location
}
