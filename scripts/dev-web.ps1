<#
.SYNOPSIS
  Run the Next.js dashboard dev server on http://127.0.0.1:3000.
#>
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

Push-Location "$RepoRoot\apps\web"
try {
    npm run dev
} finally {
    Pop-Location
}
