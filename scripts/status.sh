#!/usr/bin/env bash
# Show container state, API health, memory, and whether TRELLIS is resident.
set -uo pipefail
cd "$(dirname "$0")/.."

echo "=== container ==="
if docker compose ps --status running --quiet 2>/dev/null | grep -q .; then
  docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
  running=1
else
  echo "stopped"
  running=0
fi

echo
echo "=== host memory ==="
awk '/^MemTotal:|^MemAvailable:/ {printf "%-14s %.1f GiB\n", $1, $2/1048576}' /proc/meminfo

echo
echo "=== GPU ==="
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv

if [ "$running" = "1" ]; then
  echo
  echo "=== /health ==="
  curl -fsS --max-time 10 http://127.0.0.1:8189/health | python3 -m json.tool 2>/dev/null || echo "(not responding yet)"
  echo
  echo "=== /system ==="
  curl -fsS --max-time 10 http://127.0.0.1:8189/system | python3 -m json.tool 2>/dev/null || echo "(not responding yet)"
fi
