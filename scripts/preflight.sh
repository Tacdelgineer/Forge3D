#!/usr/bin/env bash
# Forge3D host preflight - READ ONLY.
#
# Inspects this machine and reports whether it matches the tested NVIDIA
# DGX Spark / GB10 configuration. It installs nothing, starts nothing, changes
# no configuration, and prints no secrets or private addresses.
#
#   ./scripts/preflight.sh              inspect and report
#   ./scripts/preflight.sh --gpu-test   additionally prove container GPU
#                                       passthrough, but only with a CUDA image
#                                       that is already present locally
#
# Exit status:
#   0   TESTED       matches the tested DGX Spark / GB10 configuration
#   10  UNTESTED     NVIDIA Linux GPU, but not the tested GB10 / sm_121 target
#   20  UNSUPPORTED  no NVIDIA GPU usable by this setup
#   30  ERROR        preflight could not run
#
# A non-zero status never means "install anyway". See AGENTS.md.
set -uo pipefail
cd "$(dirname "$0")/.."

GPU_TEST=0
case "${1:-}" in
  --gpu-test) GPU_TEST=1 ;;
  "") ;;
  -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "unknown argument: $1 (try --help)" >&2; exit 30 ;;
esac

BLOCKERS=()
NOTES=()
CLASS="UNTESTED"

section() { printf '\n%s\n' "[$1]"; }
row()     { printf '  %-24s %s\n' "$1" "$2"; }
block()   { BLOCKERS+=("$1"); }
note()    { NOTES+=("$1"); }
have()    { command -v "$1" >/dev/null 2>&1; }

# Read one key from .env. Callers decide what, if anything, to display; this
# never echoes a value on its own.
env_value() {
  local line
  [ -f .env ] || return 0
  line="$(grep -E "^[[:space:]]*$1=" .env 2>/dev/null | tail -1)" || return 0
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$(printf '%s' "$line" | tr -d '[:space:]')"
}

echo "======================================================================"
echo "Forge3D host preflight - inspection only, nothing is installed"
echo "======================================================================"

# --------------------------------------------------------------- host / OS --
section "host"
KERNEL_OS="$(uname -s)"
ARCH="$(uname -m)"
PRETTY="unknown"
if [ -r /etc/os-release ]; then
  PRETTY="$(. /etc/os-release && printf '%s' "${PRETTY_NAME:-$NAME}")"
fi
row "operating system" "$PRETTY"
row "kernel" "$(uname -r)"

if [ "$KERNEL_OS" != "Linux" ]; then
  row "kernel type" "$KERNEL_OS   [UNSUPPORTED - Linux only]"
  CLASS="UNSUPPORTED"
  block "Not Linux. Forge3D's Docker + NVIDIA runtime stack requires Linux."
else
  row "kernel type" "Linux"
fi

if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null; then
  row "environment" "WSL detected   [UNTESTED]"
  note "WSL has never been tested with this stack; GPU passthrough differs."
fi

case "$ARCH" in
  aarch64|arm64) row "architecture" "$ARCH   [matches tested target]" ;;
  *)             row "architecture" "$ARCH   [UNTESTED - tested target is aarch64]"
                 note "Images are built for ARM64; an x86_64 host needs its own build validation." ;;
esac

# ---------------------------------------------------------------- memory ----
section "memory"
if [ -r /proc/meminfo ]; then
  MEM_TOTAL="$(awk '/^MemTotal:/ {printf "%.1f", $2/1048576}' /proc/meminfo)"
  MEM_AVAIL="$(awk '/^MemAvailable:/ {printf "%.1f", $2/1048576}' /proc/meminfo)"
  row "MemTotal" "${MEM_TOTAL} GiB   (tested host reports ~121.7 GiB, 128 GB unified)"
  row "MemAvailable" "${MEM_AVAIL} GiB"

  MIN_GB="$(env_value TRELLIS_MIN_AVAILABLE_GB)"; MIN_GB="${MIN_GB:-65}"
  FLOOR_GB="$(env_value TRELLIS_PROTECTED_FLOOR_GB)"; FLOOR_GB="${FLOOR_GB:-40}"
  row "configured gate" "min headroom ${MIN_GB} GiB, protected floor ${FLOOR_GB} GiB"

  if awk "BEGIN{exit !($MEM_TOTAL < 100)}"; then
    block "MemTotal ${MEM_TOTAL} GiB cannot satisfy the ${MIN_GB} GiB generation gate. \
Forge3D will install and start, but every quality mode will refuse to run. \
Do not lower the gate to work around this."
  elif awk "BEGIN{exit !($MEM_AVAIL < $MIN_GB)}"; then
    note "MemAvailable ${MEM_AVAIL} GiB is currently below the ${MIN_GB} GiB gate. \
Install and startup are fine; generation will be refused until other resident \
models release memory. This is expected on a shared box and is not a blocker."
  fi
else
  row "/proc/meminfo" "unreadable   [ERROR]"
  block "Cannot read /proc/meminfo; the unified-memory gate cannot be evaluated."
fi

# ------------------------------------------------------------------- GPU ----
section "gpu"
GPU_OK=0
if have nvidia-smi; then
  GPU_LINE="$(nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv,noheader 2>/dev/null | head -1)"
  if [ -z "$GPU_LINE" ]; then
    row "nvidia-smi" "present but reported no GPU   [UNSUPPORTED]"
    CLASS="UNSUPPORTED"
    block "nvidia-smi found no GPU. Forge3D has no CPU-only inference path."
  else
    GPU_NAME="$(printf '%s' "$GPU_LINE" | awk -F', *' '{print $1}')"
    DRIVER="$(printf '%s' "$GPU_LINE"  | awk -F', *' '{print $2}')"
    CCAP="$(printf '%s' "$GPU_LINE"    | awk -F', *' '{print $3}')"
    GPU_OK=1

    case "$GPU_NAME" in
      *GB10*) row "gpu" "$GPU_NAME   [matches tested target]" ;;
      *)      row "gpu" "$GPU_NAME   [UNTESTED - tested target is GB10]" ;;
    esac

    if [ "$CCAP" = "12.1" ]; then
      row "compute capability" "$CCAP (sm_121)   [matches tested target]"
    else
      row "compute capability" "$CCAP   [UNTESTED - images compile for sm_121]"
      note "Dockerfiles set TORCH_CUDA_ARCH_LIST=12.1+PTX and the TRELLIS build \
asserts an sm_121 cubin in torchvision. On other capabilities that build fails \
by design. Changing the arch list is a porting project, not an install step."
    fi

    row "driver" "$DRIVER   (tested host: 580.173.02)"
    DRIVER_MAJOR="${DRIVER%%.*}"
    if [ -n "$DRIVER_MAJOR" ] && [ "$DRIVER_MAJOR" -lt 580 ] 2>/dev/null; then
      note "Driver major ${DRIVER_MAJOR} is older than the tested 580 series."
    fi

    # Unified memory: nvidia-smi reports no framebuffer total on GB10, which is
    # itself a signal, so this is informational rather than a check.
    row "resident GPU processes" "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null | grep -c . || echo 0) \
(see [other workloads] below)"
  fi
else
  row "nvidia-smi" "NOT FOUND   [UNSUPPORTED]"
  CLASS="UNSUPPORTED"
  block "No NVIDIA driver / nvidia-smi. Forge3D is GPU-only: there is no \
CPU-only, Windows-native or macOS inference path in this setup."
fi

# ---------------------------------------------------------------- docker ----
section "docker"
if have docker; then
  row "docker" "$(docker --version 2>/dev/null)   (tested host: 29.2.1)"
  if docker info >/dev/null 2>&1; then
    row "docker daemon" "reachable"
  else
    row "docker daemon" "NOT reachable   [BLOCKER]"
    block "The Docker daemon is not reachable by this user. Start Docker, or \
add the user to the docker group. This needs a human with sudo."
  fi

  if docker compose version >/dev/null 2>&1; then
    row "compose plugin" "$(docker compose version --short 2>/dev/null)   (tested host: 5.0.2)"
  else
    row "compose plugin" "NOT FOUND   [BLOCKER]"
    block "Docker Compose v2+ plugin missing. 'docker compose' is required; \
the legacy docker-compose script is not a substitute here."
  fi

  if docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"'; then
    row "nvidia runtime" "registered"
  else
    row "nvidia runtime" "NOT registered   [BLOCKER]"
    block "The 'nvidia' Docker runtime is not registered. docker-compose.yml \
uses 'runtime: nvidia' deliberately, because it honours \
NVIDIA_DRIVER_CAPABILITIES=graphics (EGL). Install the NVIDIA Container \
Toolkit and run 'sudo nvidia-ctk runtime configure --runtime=docker'. \
Human action: needs sudo."
  fi

  if have nvidia-ctk; then
    row "container toolkit" "$(nvidia-ctk --version 2>/dev/null | head -1)   (tested host: 1.20.0)"
  else
    row "container toolkit" "nvidia-ctk not on PATH   [check]"
    note "nvidia-ctk was not found. The runtime may still be registered; treat \
the 'nvidia runtime' row above as authoritative."
  fi
else
  row "docker" "NOT FOUND   [BLOCKER]"
  block "Docker Engine is not installed. Forge3D ships only a Docker setup. \
Installing Docker is a human/sudo action: https://docs.docker.com/engine/install/ubuntu/"
fi

# ------------------------------------------------- container GPU passthrough --
section "container gpu passthrough"
if [ "$GPU_TEST" != "1" ]; then
  row "status" "not run (pass --gpu-test)"
  row "note" "verified later at ladder step C against the built image"
elif ! have docker || ! docker info >/dev/null 2>&1; then
  row "status" "skipped - no usable Docker"
else
  TEST_IMAGE=""
  for candidate in 3d-generator-trellis:latest 3d-generator-hunyuan:latest; do
    if docker image inspect "$candidate" >/dev/null 2>&1; then TEST_IMAGE="$candidate"; break; fi
  done
  if [ -z "$TEST_IMAGE" ]; then
    TEST_IMAGE="$(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
                  | grep -E '(^|/)(nvidia/)?cuda:' | head -1)"
  fi
  if [ -z "$TEST_IMAGE" ]; then
    row "status" "skipped - no local CUDA image"
    row "note" "this check never pulls; step C covers it after the build"
  else
    row "image" "$TEST_IMAGE (already local)"
    if OUT="$(docker run --rm --runtime nvidia "$TEST_IMAGE" nvidia-smi -L 2>&1)"; then
      row "result" "GPU visible in container"
      # NGC base images print an entrypoint banner first, so pick the GPU line.
      # The device UUID is dropped: it identifies the host and adds nothing here.
      printf '  %-24s %s\n' "" "$(printf '%s\n' "$OUT" | grep -m1 '^GPU' | sed 's/ *(UUID:.*//' \
        || printf '%s\n' "$OUT" | grep -m1 . )"
    else
      row "result" "FAILED   [BLOCKER]"
      printf '  %-24s %s\n' "" "$(printf '%s' "$OUT" | tail -3 | tr '\n' ' ')"
      block "Container GPU passthrough failed. Fix the NVIDIA Container Toolkit \
before building; the build itself compiles CUDA extensions and will waste time."
    fi
  fi
fi

# ------------------------------------------------------------------ disk ----
section "disk"
disk_free_gb() { df -PBG "$1" 2>/dev/null | awk 'NR==2 {gsub("G","",$4); print $4}'; }
REPO_FREE="$(disk_free_gb .)"
row "free on repo filesystem" "${REPO_FREE:-?} GB   ($(pwd))"

DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
if [ -n "$DOCKER_ROOT" ] && [ -d "$DOCKER_ROOT" ]; then
  DOCKER_FREE="$(disk_free_gb "$DOCKER_ROOT")"
  row "free on docker root" "${DOCKER_FREE:-?} GB   ($DOCKER_ROOT)"
else
  DOCKER_FREE="$REPO_FREE"
fi
row "recorded usage" "images 19.5 GB + 21.5 GB; model caches ~16 GiB (TRELLIS) + ~29 GiB (Hunyuan)"
row "guidance" ">100 GB free for both backends, plus build layers and assets"

SMALLEST="${REPO_FREE:-0}"
[ -n "${DOCKER_FREE:-}" ] && [ "${DOCKER_FREE:-0}" -lt "$SMALLEST" ] 2>/dev/null && SMALLEST="$DOCKER_FREE"
if [ "${SMALLEST:-0}" -lt 60 ] 2>/dev/null; then
  block "Only ${SMALLEST} GB free. TRELLIS alone needs roughly 40 GB of image \
plus weights before build layers; this will fail mid-build."
elif [ "${SMALLEST:-0}" -lt 100 ] 2>/dev/null; then
  note "${SMALLEST} GB free is enough for TRELLIS only. Adding the Hunyuan \
backend as well is likely to run the filesystem out of space."
fi

# ------------------------------------------------------------------ tools ----
section "host tools"
for t in git curl awk python3; do
  if have "$t"; then
    row "$t" "$( { "$t" --version 2>/dev/null || echo present; } | head -1 | cut -c1-58)"
  else
    row "$t" "NOT FOUND"
    [ "$t" = "git" ] && block "git is required to clone the repository."
    [ "$t" = "curl" ] && note "curl is used by scripts/status.sh and the health checks."
    [ "$t" = "python3" ] && note "python3 is used to pretty-print status output."
  fi
done

# --------------------------------------------- exclusive-mode prerequisites --
section "exclusive mode prerequisites (optional)"
if [ -d /run/systemd/system ]; then
  row "systemd" "running"
else
  row "systemd" "not the active init   [Exclusive mode unavailable]"
  note "The optional Exclusive / Max Quality helper is a systemd unit. Without \
systemd, skip it: Forge3D works normally, Exclusive simply reports unavailable."
fi
if have sudo; then row "sudo" "present"; else
  row "sudo" "NOT FOUND"
  note "Installing the optional helper needs root. Skip it otherwise."
fi
RESCTL_DIR="$(env_value FORGE3D_RESCTL_DIR)"; RESCTL_DIR="${RESCTL_DIR:-/run/forge3d-resctl}"
if [ -S "${RESCTL_DIR}/resctl.sock" ]; then
  row "helper socket" "present at ${RESCTL_DIR}/resctl.sock"
else
  row "helper socket" "absent (Exclusive mode will report unavailable)"
fi

# --------------------------------------------------------- other workloads --
section "other workloads holding unified memory"
if have nvidia-smi; then
  APPS="$(nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader 2>/dev/null)"
  if [ -n "$APPS" ]; then
    printf '%s\n' "$APPS" | while IFS= read -r line; do row "" "$line"; done
    note "Other resident models share this unified memory pool. Report them; \
do NOT stop or unload anyone else's processes to make room."
  else
    row "" "none reported by nvidia-smi"
  fi
fi
for svc in ollama; do
  if have "$svc"; then row "$svc" "installed (may hold unified memory when resident)"; fi
done

# ------------------------------------------------------------- repo state ---
section "repository state"
if [ -f docker-compose.yml ] && [ -f app/main.py ]; then
  row "checkout" "looks like a Forge3D checkout"
else
  row "checkout" "NOT a Forge3D checkout   [ERROR]"
  block "Run this from the root of a Forge3D clone."
fi

if [ -f .env ]; then
  row ".env" "present"
  TOKEN="$(env_value HF_TOKEN)"
  if [ -n "$TOKEN" ]; then
    row "HF_TOKEN" "configured (value not printed)"
    note "A configured token does NOT prove gated access was approved. TRELLIS \
needs approvals on facebook/dinov3-vitl16-pretrain-lvd1689m and briaai/RMBG-2.0 \
for the same account. Only a real load proves it."
  else
    row "HF_TOKEN" "not set   [human action required for TRELLIS]"
  fi
  BIND="$(env_value FORGE3D_BIND)"; BIND="${BIND:-127.0.0.1}"
  case "$BIND" in
    127.0.0.1|localhost|::1) row "FORGE3D_BIND" "loopback (safe default)" ;;
    0.0.0.0)                 row "FORGE3D_BIND" "0.0.0.0   [WARNING]"
                             note "FORGE3D_BIND=0.0.0.0 publishes an unauthenticated UI on every \
interface, and Docker's port publishing bypasses ufw. Do not leave this set \
unless the human chose it deliberately." ;;
    *)                       row "FORGE3D_BIND" "set to a non-loopback address (value not printed)" ;;
  esac
  UID_V="$(env_value UID)"; GID_V="$(env_value GID)"
  row "UID/GID in .env" "${UID_V:-unset}/${GID_V:-unset}   (this user: $(id -u)/$(id -g))"
  if [ -n "$UID_V" ] && [ "$UID_V" != "$(id -u)" ]; then
    note "UID in .env does not match this user; bind-mount writes may fail."
  fi
else
  row ".env" "absent   (cp .env.example .env)"
fi
for d in data/uploads data/outputs models/hunyuan; do
  [ -d "$d" ] && row "$d" "exists" || row "$d" "missing (create before first start)"
done

# ------------------------------------------------------------- conclusion ---
if [ "$CLASS" != "UNSUPPORTED" ]; then
  if [ "$GPU_OK" = "1" ] && [ "$KERNEL_OS" = "Linux" ] \
     && { [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; } \
     && [ "${CCAP:-}" = "12.1" ] && case "${GPU_NAME:-}" in *GB10*) true ;; *) false ;; esac; then
    CLASS="TESTED"
  else
    CLASS="UNTESTED"
  fi
fi

echo
echo "======================================================================"
echo "CLASSIFICATION: $CLASS"
case "$CLASS" in
  TESTED)      echo "  Matches the tested DGX Spark / GB10 target." ;;
  UNTESTED)    echo "  NVIDIA Linux hardware, but NOT the tested GB10 / sm_121 target."
               echo "  Report likely blockers. Do not rewrite the Docker stack, the"
               echo "  dependency pins or the sm_121 build flags to force a build." ;;
  UNSUPPORTED) echo "  This setup cannot run here. There is no CPU-only, Windows-native"
               echo "  or macOS inference path. Report and stop." ;;
esac

if [ "${#BLOCKERS[@]}" -gt 0 ]; then
  echo
  echo "BLOCKERS (${#BLOCKERS[@]}) - resolve before building:"
  for b in "${BLOCKERS[@]}"; do printf '  * %s\n' "$b" | fold -s -w 70 | sed '2,$s/^/    /'; done
fi
if [ "${#NOTES[@]}" -gt 0 ]; then
  echo
  echo "NOTES (${#NOTES[@]}):"
  for n in "${NOTES[@]}"; do printf '  - %s\n' "$n" | fold -s -w 70 | sed '2,$s/^/    /'; done
fi
echo
echo "Next: read AGENTS.md before changing anything."
echo "======================================================================"

case "$CLASS" in
  TESTED)      exit 0 ;;
  UNTESTED)    exit 10 ;;
  UNSUPPORTED) exit 20 ;;
  *)           exit 30 ;;
esac
