#!/usr/bin/env bash
# Assemble the landing page into a deployable folder (default site/_dist): the page, its stylesheet, and the images
# it shows, copied from docs/ at build time so they're never duplicated in git. The demo GIF (8.3 MB) is also
# converted to an MP4 for the page's <video>, which keeps the page light; the GIF stays as the fallback and a link.
# ffmpeg: the local one if present, else the pinned mwader/static-ffmpeg:7.1 image (the same one demo-capture uses).
#
#   bash site/build.sh [out-dir]
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:-site/_dist}
SHOTS=(runs regression-v1-vs-v2 safety-v1-vs-v2 trace-explorer replay-diff trajectory-v2-failing run-detail-completed baselines)

rm -rf "$OUT"
mkdir -p "$OUT/assets/screenshots"
cp site/index.html site/style.css "$OUT/"
touch "$OUT/.nojekyll"
cp docs/demo.gif "$OUT/assets/demo.gif"
for s in "${SHOTS[@]}"; do cp "docs/screenshots/$s.png" "$OUT/assets/screenshots/"; done

# H.264, yuv420p and even dimensions so every browser plays it; no audio; faststart so it starts while loading.
ARGS=(-y -loglevel error -i demo.gif -movflags +faststart -pix_fmt yuv420p -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" -c:v libx264 -crf 26 -preset slow -an demo.mp4)
if command -v ffmpeg >/dev/null 2>&1; then
    (cd "$OUT/assets" && ffmpeg "${ARGS[@]}")
else
    dir=$(cd "$OUT/assets" && pwd -W 2>/dev/null || pwd)
    MSYS_NO_PATHCONV=1 docker run --rm -v "$dir:/w" -w /w mwader/static-ffmpeg:7.1 "${ARGS[@]}"
fi
du -sh "$OUT" "$OUT/assets/demo.mp4" "$OUT/assets/demo.gif"
