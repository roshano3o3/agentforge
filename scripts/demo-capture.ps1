<#
.SYNOPSIS
  Re-capture docs/screenshots/*.png and docs/demo.gif from a running, seeded demo stack.

.DESCRIPTION
  1. Start the stack in an EMPTY database: `docker compose up` (its `seed` service runs `agentforge demo seed`).
  2. Run this script. It drives the real dashboard with Playwright (apps/web/demo/capture.spec.ts, light theme,
     1440x900 for screenshots, 1280x800 for the video), then converts the video to docs/demo.gif with ffmpeg
     (the local ffmpeg if there is one, else the pinned mwader/static-ffmpeg:7.1 image).

  The capture changes the stack's data: it adds a draft v2 of rag-support (for the two dataset-draft
  screenshots) and a second replay of PR #4's case (recorded live in the video). Run it on a throwaway stack.
#>
param(
    [string]$WebUrl = "http://127.0.0.1:3000",
    [string]$ApiUrl = "http://127.0.0.1:8000",
    [int]$Fps = 8,
    [int]$Width = 960,
    [int]$Colors = 64   # 256 gave 11.8 MB for a 27 s clip; 64 keeps it under 10 MB and the text readable
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Web = Join-Path $RepoRoot "apps\web"
$Video = Join-Path $Web "test-results\demo-video\demo.webm"
$Gif = Join-Path $RepoRoot "docs\demo.gif"

$env:AGENTFORGE_DEMO_URL = $WebUrl
$env:AGENTFORGE_DEMO_API_URL = $ApiUrl
Push-Location $Web
try {
    $ErrorActionPreference = "Continue"
    npx playwright test -c demo/playwright.config.ts
    if ($LASTEXITCODE -ne 0) { throw "capture failed (exit $LASTEXITCODE)" }
} finally {
    $ErrorActionPreference = "Stop"
    Pop-Location
}

# Two-pass palette: one palette for the whole clip (the UI is mostly flat colour), frames diffed against it.
$filter = "fps=$Fps,scale=${Width}:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=${Colors}:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
$ErrorActionPreference = "Continue"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    ffmpeg -y -loglevel error -i $Video -vf $filter -loop 0 $Gif
} else {
    $videoDir = Split-Path $Video
    $docs = Join-Path $RepoRoot "docs"
    docker run --rm -v "${videoDir}:/in" -v "${docs}:/out" mwader/static-ffmpeg:7.1 `
        -y -loglevel error -i /in/demo.webm -vf $filter -loop 0 /out/demo.gif
}
if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed (exit $LASTEXITCODE)" }
$ErrorActionPreference = "Stop"
$mb = (Get-Item $Gif).Length / 1MB
Write-Host ("Wrote {0} ({1:N1} MB)" -f $Gif, $mb)
