<#
.SYNOPSIS
  Prints the checkout's git commit for the worker image's code version
  (AGENTFORGE_GIT_SHA build arg), with "-dirty" when there are uncommitted
  changes. Prints nothing outside a git checkout. The worker also hashes the
  source it runs, so a dirty build is still told apart from a clean one.
#>
$ErrorActionPreference = "Continue"
$sha = git rev-parse HEAD 2>$null
if ($LASTEXITCODE -ne 0 -or -not $sha) { return "" }
$sha = "$sha".Trim()
if (git status --porcelain 2>$null) { $sha += "-dirty" }
return $sha
