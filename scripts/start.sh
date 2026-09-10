#!/usr/bin/env bash
# Start the TRELLIS.2 backend, refusing if the machine is short on memory.
set -euo pipefail
cd "$(dirname "$0")/.."

MIN_GB="$(grep -E '^TRELLIS_MIN_AVAILABLE_GB=' .env 2>/dev/null | cut -d= -f2 || echo 65)"
MIN_GB="${MIN_GB:-65}"
AVAIL_GB="$(awk '/^MemAvailable:/ {printf "%.1f", $2/1048576}' /proc/meminfo)"

echo "MemAvailable: ${AVAIL_GB} GiB   (floor for generation: ${MIN_GB} GiB)"

if awk "BEGIN{exit !($AVAIL_GB < $MIN_GB)}"; then
  echo
  echo "REFUSING TO START: only ${AVAIL_GB} GiB available, need ${MIN_GB} GiB."
  echo "Something else is holding unified memory. Check with:"
  echo "    nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv"
  echo
  echo "Nothing has been unloaded automatically - that is your call."
  exit 1
fi

docker compose up -d
echo
echo "Started. The model is NOT loaded yet (lazy load on first /generate)."
echo "    ./scripts/status.sh        # state"
echo "    docker compose logs -f     # logs"
