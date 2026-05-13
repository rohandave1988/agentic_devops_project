#!/usr/bin/env bash
# Combines 4 scenario MP4s into one LinkedIn demo video.
# Uses stream-copy concatenation — no re-encode, source quality preserved exactly.
#
# Usage (from project root, after recording all 4 scenarios):
#   ./scripts/combine_demo.sh
#
# Output: docs/demo_linkedin_final.mp4
set -euo pipefail

DOCS="$(git rev-parse --show-toplevel)/docs"
OUT="${DOCS}/demo_linkedin_final.mp4"

echo "━━━  Combining demo scenarios  ━━━"

# ── Check all 4 clips exist ────────────────────────────────────────────────────
for f in demo_s1.mp4 demo_s2.mp4 demo_s3.mp4 demo_s4.mp4; do
    if [[ ! -f "${DOCS}/${f}" ]]; then
        echo "ERROR: ${DOCS}/${f} not found."
        echo "Record it first: ./scripts/pre_scenario.sh <scenario> && vhs docs/${f%.mp4}.tape"
        exit 1
    fi
done

# ── Detect source resolution and framerate from first clip ────────────────────
read W H FPS < <(ffprobe -v quiet \
    -select_streams v:0 \
    -show_entries stream=width,height,r_frame_rate \
    -of csv=p=0 "${DOCS}/demo_s1.mp4" 2>/dev/null | tr ',' ' ' | awk '{
        split($3, f, "/")
        fps = (f[2] > 0) ? f[1]/f[2] : f[1]
        printf "%s %s %.0f\n", $1, $2, fps
    }')

echo "  Source: ${W}x${H} @ ${FPS}fps — matching title cards to source"

# ── Create title cards at source resolution/framerate ─────────────────────────
echo "► Creating title cards..."
SOURCE_W="${W}" SOURCE_H="${H}" SOURCE_FPS="${FPS}" \
    python3 "$(git rev-parse --show-toplevel)/scripts/make_titles.py"

# ── Concatenate with stream copy (no re-encode) ───────────────────────────────
echo "► Concatenating (stream copy — no quality loss)..."

CONCAT="${DOCS}/concat.txt"
cat > "${CONCAT}" << EOF
file '${DOCS}/title_intro.mp4'
file '${DOCS}/title_s1.mp4'
file '${DOCS}/demo_s1.mp4'
file '${DOCS}/title_s2.mp4'
file '${DOCS}/demo_s2.mp4'
file '${DOCS}/title_s3.mp4'
file '${DOCS}/demo_s3.mp4'
file '${DOCS}/title_s4.mp4'
file '${DOCS}/demo_s4.mp4'
file '${DOCS}/title_outro.mp4'
EOF

ffmpeg -y -f concat -safe 0 -i "${CONCAT}" \
    -c copy \
    -movflags +faststart \
    "${OUT}"

rm -f "${DOCS}"/title_*.mp4 "${CONCAT}"

SIZE=$(du -h "${OUT}" | cut -f1)
DURATION=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "${OUT}" 2>/dev/null | xargs printf "%.0f")
MINS=$((DURATION / 60))
SECS=$((DURATION % 60))

echo ""
echo "✓ Done: ${OUT}"
echo "  Size:     ${SIZE}"
echo "  Duration: ${MINS}m ${SECS}s"
echo "  Quality:  stream copy — no re-encode"
echo ""
echo "Upload directly to LinkedIn as native video for best reach."
