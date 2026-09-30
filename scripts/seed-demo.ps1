<#
.SYNOPSIS
  Publish the example dataset and submit a 5-case evaluation of the example
  RAG app, waiting for the worker to finish it. Needs the Docker stack
  (docker-up.ps1): the API at the given URL plus Redis and the worker --
  the run is executed by the worker container, not by this script.
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
