<#
.SYNOPSIS
  Publish the example dataset and run the 5-case evaluation against the
  example RAG app. Assumes the API is already running (dev-api.ps1 or
  docker compose) at the given URL.
#>
param(
    [string]$ApiUrl = "http://127.0.0.1:8000"
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$agentforge = Join-Path $RepoRoot ".venv\Scripts\agentforge.exe"

Push-Location $RepoRoot
try {
    & $agentforge dataset publish "datasets\rag_support_v1.yaml" --api-url $ApiUrl
    & $agentforge evaluate `
        --app rag-assistant `
        --app-version v1 `
        --dataset rag-support `
        --dataset-version latest `
        --adapter rag_app.adapter:answer `
        --api-url $ApiUrl
} finally {
    Pop-Location
}
