#!/usr/bin/env bash
# One-shot generation helper:  ./scripts/generate.sh <image> [pipeline_type] [seed]
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${1:?usage: generate.sh <image> [pipeline_type] [seed]}"
PTYPE="${2:-}"
SEED="${3:-42}"

args=(-F "image=@${IMAGE}" -F "seed=${SEED}")
[ -n "$PTYPE" ] && args+=(-F "pipeline_type=${PTYPE}")

echo "Generating from ${IMAGE} (this can take several minutes)..."
start=$(date +%s)
curl -sS --max-time 7200 "${args[@]}" http://127.0.0.1:8189/generate | python3 -m json.tool
echo "wall clock: $(( $(date +%s) - start ))s"
