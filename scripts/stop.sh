#!/usr/bin/env bash
# Stop the backend and show the memory that came back.
set -euo pipefail
cd "$(dirname "$0")/.."

before="$(awk '/^MemAvailable:/ {printf "%.1f", $2/1048576}' /proc/meminfo)"
docker compose down
sleep 3
after="$(awk '/^MemAvailable:/ {printf "%.1f", $2/1048576}' /proc/meminfo)"

echo
echo "MemAvailable: ${before} GiB -> ${after} GiB"
echo "GPU processes still resident:"
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
