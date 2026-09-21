# AGENTS.md — installing Forge3D with a coding agent

This file is the primary instruction set for an autonomous coding or install
agent (Codex, Claude Code, Hermes, or any capable shell agent) that has been
asked to set up Forge3D on a machine.

Read it fully before running anything that changes the host.

---

## 0. Directive

**Before changing anything:**

1. **Inspect the host.** Run `./scripts/preflight.sh` and read its output.
2. **Read [README.md](README.md)** — hardware support, install flow, memory model.
3. **Read [docs/SETUP.md](docs/SETUP.md)** — clone-to-first-run audit, environment
   forwarding, the optional host helper, and the existing check commands.
4. **Verify that hardware and OS match a supported or known configuration** using
   the classification in §3. Do not skip this because the build "looks generic".
5. **Preserve the tested dependency pins and the GB10 / sm_121 patches.** They are
   load-bearing, not leftovers.

**Your job during installation is not to redesign Forge3D.**

Forge3D is a working, hardware-specific tool. The Dockerfiles, pins, memory gate,
bind address and security restrictions are the result of measured debugging on
real hardware. An install agent's job is to *reproduce* that state on a matching
host, or to *report honestly* that the host does not match. It is not to
refactor, modernise, "simplify", or make the stack look portable.

If something does not work, the correct response is to diagnose against §10,
not to change the recipe.

### Hard rules

Never do any of the following without the human explicitly asking for it:

| Do not | Why |
| --- | --- |
| Change `TORCH_CUDA_ARCH_LIST`, `FLASH_ATTN_CUDA_ARCHS`, or the sm_121 build assertions | These make the extensions runnable on GB10 at all |
| Replace the source-built torchvision with a stock wheel | The wheel has no sm_121 cubin; RMBG-2.0's `deform_conv2d` dies on GB10 |
| Loosen or bump pinned versions to resolve a build error | Pins are tested pairings (`torch 2.9.1` ↔ `torchvision 0.24.1`, `flash-attn 2.7.4.post1`, `transformers`, `diffusers`, `setuptools<81`, `Open3D 0.18.0`) |
| Lower `TRELLIS_MIN_AVAILABLE_GB` or `TRELLIS_PROTECTED_FLOOR_GB` | They stop a run from starving other resident workloads. A refused mode is the gate working |
| Set `FORGE3D_BIND=0.0.0.0`, publish 8189, or edit firewall/router config | The UI has no authentication and Docker bypasses ufw |
| Change `restart: "no"` | Prevents a reboot silently reclaiming tens of GB of unified memory |
| Stop, unload or kill unrelated user processes | Other models on the box are not yours to evict |
| Print, echo, log or paste a token value | See §4 |
| Run a full 3D generation to "check it works" | Expensive; see §7 step G |

---

## 1. What Forge3D is

A self-hosted image-to-3D asset generator. A FastAPI service (`trellis`, port
8189) serves a browser dashboard and runs Microsoft TRELLIS.2 in-process; an
optional second container (`hunyuan`, port 8190) serves Hunyuan3D 2.1 and
Hunyuan3D Multi-View. Output is GLB files in a local library.

Developed and tested **only** on NVIDIA DGX Spark / GB10 / Linux ARM64.
See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

**Forge3D produces GLB meshes. It does not produce finished, game-ready assets.**
See §12.

---

## 2. Host preflight

The repository ships a read-only inspection script. It installs nothing, starts
nothing, and prints no secrets or private addresses.

```bash
./scripts/preflight.sh              # inspect and report
./scripts/preflight.sh --gpu-test   # also prove container GPU passthrough,
                                    # using only a CUDA image already present
```

Exit status: `0` TESTED · `10` UNTESTED · `20` UNSUPPORTED · `30` preflight error.
A non-zero status never means "install anyway."

It reports OS and architecture, RAM (total and available), GPU / compute
capability / driver, Docker, Compose, the `nvidia` runtime and Container
Toolkit, free disk on both the repo and Docker filesystems, host tools, the
optional Exclusive-mode prerequisites, other workloads holding unified memory,
and the repo's own `.env` / directory state.

### Equivalent individual commands

Use these if you need a single value, or if the script cannot run:

```bash
uname -sm                                   # OS and architecture
. /etc/os-release && echo "$PRETTY_NAME"    # distribution
awk '/^MemTotal:|^MemAvailable:/ {printf "%-14s %.1f GiB\n", $1, $2/1048576}' /proc/meminfo
nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv
docker --version && docker compose version
docker info --format '{{json .Runtimes}}' | grep -q '"nvidia"' && echo "nvidia runtime OK"
nvidia-ctk --version
df -h . && df -h "$(docker info --format '{{.DockerRootDir}}')"
git --version
[ -d /run/systemd/system ] && echo systemd; command -v sudo
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
```

`nvidia-smi` reports **no framebuffer total** on GB10 — memory is unified, so read
`/proc/meminfo`, not `nvidia-smi`, for capacity. `scripts/status.sh` and
`app/memory.py` already do this.

### Reference: the tested host

| Component | Tested value |
| --- | --- |
| Hardware | NVIDIA DGX Spark class / GB10 (validation host: GIGABYTE AI TOP ATOM) |
| OS | Ubuntu 24.04 LTS |
| Architecture | `aarch64` |
| GPU | NVIDIA GB10, compute capability **12.1** (sm_121) |
| Unified memory | 128 GB nominal, ~121.7 GiB `MemTotal` |
| Driver | 580.173.02 |
| Docker | 29.2.1 |
| Compose | 5.0.2 |
| NVIDIA Container Toolkit | 1.20.0, `nvidia` runtime registered |

---

## 3. Support classification

Classify the host before doing anything else, and **say which class it is in your
report**. Do not silently proceed as though untested hardware were supported.

### TESTED — proceed with the documented install

- NVIDIA DGX Spark / GB10
- Linux, `aarch64`
- Compute capability 12.1 (sm_121)
- NVIDIA driver, Docker + Compose, and the `nvidia` container runtime present

Follow §5 as written.

### UNTESTED — inspect and report; do not rewrite the stack

Other NVIDIA Linux hardware (x86_64, other Blackwell/Hopper/Ada parts, other
compute capabilities).

You **may**: run the preflight, read the Dockerfiles, and report the specific
blockers you expect. You **must not**: casually rewrite the tested Docker stack,
change the arch list, swap base images, or relax pins to force a build.

Known, concrete blockers to report for non-GB10 NVIDIA hosts:

- Both Dockerfiles set `TORCH_CUDA_ARCH_LIST="12.1+PTX"`; extensions are compiled
  for sm_121 only.
- `FLASH_ATTN_CUDA_ARCHS=121` deliberately defeats flash-attn's hardcoded arch
  branches. Another GPU needs a different value.
- The TRELLIS build **asserts** an `sm_121` cubin in `torchvision/_C.so` with
  `cuobjdump` and fails the build if it is absent. This is intentional.
- Memory defaults (65 GiB headroom, 40 GiB floor, mode peaks up to 58 GiB) assume
  a ~128 GB unified pool. A 24–80 GB discrete-VRAM card is a different memory
  model entirely, not a config tweak.
- Images are built ARM64; an x86_64 host rebuilds everything from source.

Porting is a project a human should decide to start. Report, then stop unless
told otherwise.

### UNSUPPORTED BY CURRENT SETUP — report and stop

- **CPU-only** hosts — there is no CPU inference path.
- **Native Windows** inference — no support. (WSL2 is untested; treat as UNTESTED
  at best and say so.)
- **macOS** inference — no support, including Apple Silicon.

Report clearly that Forge3D cannot be installed here and why. Do not offer to
build a CPU fallback: none exists.

---

## 4. Human-only blockers

These require the human. Stop at them, report precisely, and resume afterwards.

### 4.1 Hugging Face gated access — required for TRELLIS

TRELLIS.2 constructs **both** of these when loading its pipeline, even for an
already-transparent input image:

- [`facebook/dinov3-vitl16-pretrain-lvd1689m`](https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m) — gated; access must be requested and **approved**.
- [`briaai/RMBG-2.0`](https://huggingface.co/briaai/RMBG-2.0) — gated; terms must be accepted, including a non-commercial restriction.

The human must:

1. Have a Hugging Face account.
2. Request/accept access for **both** repositories **with that same account**.
3. Create a **read** token.
4. Put it into `.env` themselves: the line `HF_TOKEN=hf_...` in the repo's `.env`.

**An installation agent must NEVER ask the human to paste a token into chat.**
Do not ask for it, do not accept it if offered, do not echo it, do not write it
into a Dockerfile, a commit, a log, a screenshot or an issue. If a human pastes
one anyway, tell them to treat it as compromised and rotate it.

Instruct them like this:

> Open `.env` in the Forge3D directory in your own editor and set
> `HF_TOKEN=` to your Hugging Face read token. Save the file. Tell me when it is
> done — do not paste the token to me.

You **may** check that the variable appears configured, without revealing it:

```bash
# Prints only "configured" or "not set".
grep -q '^[[:space:]]*HF_TOKEN=.\+' .env && echo configured || echo "not set"
```

`./scripts/preflight.sh` already reports this as `HF_TOKEN  configured (value not printed)`.

**Having a token does not imply gated access has been approved.** They are
independent. A configured token with unapproved repositories fails at first
generation with a 401/403, not at startup. Starting the dashboard successfully
proves nothing about gated access. Only a real model load does — which is why
this is the one place the "no expensive generation" rule may need the human's
explicit permission to test.

The Hunyuan weight repositories are currently **ungated** and need no token.

### 4.2 Other human-only actions

| Action | Why it is human-only |
| --- | --- |
| Installing Docker, the NVIDIA driver, or the Container Toolkit | Needs sudo and changes the machine globally |
| `sudo nvidia-ctk runtime configure --runtime=docker` + daemon restart | Needs sudo; restarts Docker |
| Adding the user to the `docker` group | Needs sudo; requires re-login |
| Installing the optional Exclusive helper (`sudo host/install.sh`) | Needs root, installs a systemd unit; opt-in only (§9) |
| Choosing a non-loopback `FORGE3D_BIND` | A deliberate security decision (§8) |
| Deciding to evict another resident model to free memory | Someone else's workload |
| Accepting upstream model licenses (Hunyuan territory limits, RMBG non-commercial) | A legal decision — see [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md) |

---

## 5. Canonical install flow (TESTED hosts)

Run from the machine that will host Forge3D. Each step is checked before the next.

### Step 1 — Clone and inspect

```bash
git clone https://github.com/Tacdelgineer/Forge3D.git
cd Forge3D
./scripts/preflight.sh
```

Read README.md, AGENTS.md and docs/SETUP.md. Classify the host (§3). Stop here if
the classification is UNSUPPORTED, or if preflight reported blockers.

### Step 2 — Configure

```bash
cp .env.example .env
mkdir -p data/uploads data/outputs models/hunyuan
printf '\nUID=%s\nGID=%s\n' "$(id -u)" "$(id -g)" >> .env
```

`UID`/`GID` must match the owner of the bind mounts, or the container cannot
write to `data/` and `models/`. Do not `source` `.env` in bash — `UID` is a
read-only bash variable there.

Leave `FORGE3D_BIND=127.0.0.1` (§8). Leave the memory gate values alone (§9).

**Now stop for the human-only Hugging Face step (§4.1)** if `HF_TOKEN` is not
already configured. TRELLIS cannot generate without it.

### Step 3 — Validate configuration

```bash
docker compose config --quiet          # silent + exit 0 == valid
```

Use `--quiet`. A bare `docker compose config` prints the **resolved** configuration,
including `HF_TOKEN`. Never run it unredacted, and never paste its output.

### Step 4 — Build

Choose backends per §6 first.

```bash
docker compose build trellis                        # required
docker compose --profile hunyuan build hunyuan      # optional
```

Builds compile ARM64 CUDA extensions and **can take tens of minutes** each. This
is normal; do not interrupt and retry with different flags. `MAX_JOBS` in `.env`
(example: 4) caps compilation parallelism — lower it if the build competes with
other workloads on the box, but do not raise it to "speed things up" on a machine
that is also serving other models.

### Step 5 — Start

```bash
docker compose up -d trellis
docker compose --profile hunyuan up -d hunyuan      # only if built
```

`./scripts/start.sh` is an alternative for TRELLIS that refuses to start when raw
host `MemAvailable` is below the configured floor.

### Step 6 — Verify

Run the ladder in §7, stopping at step F. Do not proceed to G.

### Step 7 — Report

Use the template in §11.

### Useful operational commands

```bash
./scripts/status.sh                                 # containers, memory, GPU, /health, /system
docker compose logs -f trellis
docker compose --profile hunyuan logs -f hunyuan
docker compose --profile hunyuan down               # stop; caches and outputs persist
```

`scripts/status.sh` queries `http://127.0.0.1:8189` and therefore assumes the
default loopback bind. On a host with a non-loopback `FORGE3D_BIND`, use the
bind-independent probe in §7 step E instead.

---

## 6. Backend install choices

You do not need to build every backend immediately. Build what the human asked
for; default to TRELLIS only.

| Choice | Builds | Gets you |
| --- | --- | --- |
| **MINIMUM / TRELLIS** *(default)* | `trellis` | TRELLIS.2 image-to-3D, the dashboard, the asset library, the GLB viewer. Highest-quality textured results in the current workflow. |
| **OPTIONAL HUNYUAN** | `trellis` + `hunyuan` | Adds Hunyuan3D 2.1 (faster geometry, optional baked texture) and Hunyuan3D Multi-View (up to four named reference views). |
| **FULL** | both worker stacks | Everything above. |

The Hunyuan container is behind a Compose profile, so plain `docker compose up -d`
never starts it. If it is not running, the Hunyuan generators simply show as
unavailable in the UI; nothing else is affected.

### Storage, from what this repository actually records

| Item | Recorded size |
| --- | --- |
| `3d-generator-trellis:latest` image | 19.5 GB |
| `3d-generator-hunyuan:latest` image | 21.5 GB |
| TRELLIS model cache (`models/hf/`) | ~16 GiB |
| Hunyuan model cache (`models/hunyuan/`) | ~29 GiB |

Plus temporary build layers and generated assets. Practical guidance: **~60 GB
free for TRELLIS only, >100 GB free for both backends** — sensible floors, not
guaranteed minimums. Model weights download lazily on first generation, not at
startup, so disk use grows after the first run. Real-ESRGAN is fetched into the
Hunyuan image at build time.

Do not invent other download figures. If a number is not recorded here, in
README.md or in docs/SETUP.md, say it is unknown.

---

## 7. Verification ladder

Climb in order. Stop at the first failure and diagnose with §10. **Do not skip to
the expensive step.** Use the existing scripts — do not write redundant ones.

**A. Compose configuration validates**

```bash
docker compose config --quiet && echo "A OK"
```

**B. Containers build and start**

```bash
docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
```

**C. GPU is visible inside the required container**

```bash
docker compose exec -T trellis nvidia-smi -L
```

**D. Imports and small CUDA checks pass**

```bash
# TRELLIS: sm_121, a real bf16 matmul, flash-attn, nvdiffrast, o-voxel, EGL.
docker compose exec -T trellis python /app/scripts/verify_stack.py

# Hunyuan, only if built. This script is not copied into the image; pipe it in.
docker compose --profile hunyuan exec -T hunyuan python - < scripts/verify_hunyuan.py
```

`verify_stack.py` exits non-zero if a required component fails. Its EGL checks are
marked informational — TRELLIS.2 uses the CUDA rasterizer — so `WARN` lines there
are not install failures. `libEGL warning: ...` and `pci id for fd ...` noise on
stderr, and a `RasterizeGLContext has been deprecated` warning, are expected even
on a fully healthy host; read the `RESULT:` line, not the noise.

**E. Forge3D status endpoint and UI respond**

```bash
./scripts/status.sh          # loopback bind only
```

Bind-independent alternative (works whatever `FORGE3D_BIND` is, because the app
listens on `0.0.0.0` *inside* the container):

```bash
docker compose exec -T trellis python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8189/health').read().decode())"
```

Expect `{"status":"ok","model":"TRELLIS.2-4B","gpu_available":true}`.

`GET /system` additionally reports memory, per-generator availability and
per-mode gate reasons — the fastest way to see *why* a quality mode is disabled.

Confirm the UI is reachable at **http://127.0.0.1:8189/** on the host (§8).

**F. Optional backend verification scripts pass**

```bash
docker compose exec -T trellis python - < scripts/test_step5.py
# Only meaningful where the host helper and releasable workloads exist:
docker compose exec -T trellis python - < scripts/test_step6.py
```

These do not generate an asset. `test_step6.py` expects the optional helper; on a
host without it, a failure there is expected and is **not** an install failure.

One `test_step6.py` case is conditional and prints `[SKIP]`: it asserts that the
normal memory gate *refuses* Ultra, which only holds while memory is tight. On a
host with plenty free the gate correctly allows Ultra, and posting that job would
start a real multi-minute, ~48 GiB generation — so the script checks `/system`
first and skips. **Do not remove that guard**, and do not "fix" the skip by
making it post the job anyway.

**G. Actual asset generation — only when the human explicitly asks**

A real run costs minutes of GPU time and tens of GiB of unified memory. Do not do
it as a smoke test. When explicitly requested, prefer TRELLIS **Standard** (`512`)
or Hunyuan **Shape** — the cheapest modes — via the UI, or:

```bash
./scripts/generate.sh <image> [pipeline_type] [seed]
```

Note that the **first** generation is also the first real test of Hugging Face
gated access (§4.1), and of the lazy weight download. If the human needs that
proven, this is the step that proves it — ask first.

---

## 8. Network security

**Forge3D has no authentication.** Anyone who can reach port 8189 can use the GPU,
read the asset library and delete assets.

The safe default is `FORGE3D_BIND=127.0.0.1`. **Preserve it.**

An agent must **NOT**:

- bind to `0.0.0.0` automatically,
- publish or forward port 8189 publicly,
- modify firewall, ufw, iptables, router or cloud security-group configuration,
- add a reverse proxy or tunnel to the public internet.

Docker publishes ports through its own iptables chain and **bypasses ufw**, so a
host firewall rule is not the safety net it looks like. That is precisely why the
default is loopback.

For remote access, use the documented SSH tunnel:

```bash
ssh -N -L 8189:127.0.0.1:8189 your-dgx-host
```

Then open `http://127.0.0.1:8189/` on the local machine. Alternatively the human
may deliberately choose a trusted private interface address (for example a
Tailscale or VPN address) and set `FORGE3D_BIND` to it. That is **their** decision
to make and state — never yours, and never a default you pick to make the UI
easier to reach.

The Hunyuan worker's host port **8190** is loopback-only by design; Forge3D
reaches it over the Compose network as `hunyuan:8190`. Do not republish it.

Never record a private address in documentation, a commit, an issue or a report.

---

## 9. Memory and other AI services

GB10 has **one unified memory pool shared by CPU and GPU** — there is no separate
VRAM. Everything else resident on the machine (Ollama, ComfyUI, other inference
services, another Forge3D backend) consumes the same pool and can leave too
little for a heavy generation.

This is why Forge3D has a pre-flight gate, and why it sometimes refuses.

**Preserve all of it:**

- **Memory headroom checks** — `app/memory.py` reads `/proc/meminfo`, not
  `nvidia-smi` (which reports no framebuffer total here) and not `docker stats`
  (which counts reclaimable page cache and badly overstates the footprint).
- **`TRELLIS_MIN_AVAILABLE_GB=65`** — minimum headroom for any job.
- **`TRELLIS_PROTECTED_FLOOR_GB=40`** — memory a run must leave behind for
  everything else. Required headroom is `max(65, mode peak + 40)` GiB.
- **The Exclusive / Max Quality mechanism** — see below.

A mode showing as unavailable with a stated reason is **the gate working
correctly**, not a bug to fix. Report the reason from `GET /system`; do not lower
the floor to force a job through. On a box with other models resident, the
high-memory modes being refused is the expected, designed outcome.

**Do not automatically kill or unload unrelated user processes** to free memory.
Report what is resident and let the human decide.

### Exclusive / Max Quality

An **optional, host-specific** helper (`host/forge3d-resctl`, a systemd unit
reached over a UID-checked Unix socket). When present and configured, it can
temporarily release configured Ollama residency, unload Hunyuan pipelines, and
pause the fixed `pipeline-worker.service`, then restore the prior service state
and rewarm previously pinned Ollama models.

Rules for agents:

- It is **opt-in**. Do not install it to make quality options appear enabled on a
  machine that does not need it. Installing it requires root.
- If it is already configured, use it **only** according to
  [docs/SETUP.md](docs/SETUP.md).
- It **does not lower the memory floor** and is not a way around the memory gate above.
- It controls a **fixed** service name only. It exposes no arbitrary command
  interface, and the containers get no Docker socket. Keep it that way.
- Without it, the socket directory is simply empty and Exclusive reports itself
  unavailable. Everything else works normally.

Recovery, if a workload is left paused:

```bash
sudo forge3d-resctl recover
sudo journalctl -u forge3d-resctl.service -n 50 --no-pager
```

---

## 10. Troubleshooting map

Symptom → cause → action. Prefer these known fixes.

**Do not respond to a dependency problem by upgrading everything.** The pins are
tested pairings; `pip install --upgrade` is how you break a working image.

| Symptom | Likely cause | Action |
| --- | --- | --- |
| **HF 401 / 403, or a gated-model load failure at first generation** | Token missing, wrong account, or gated access not approved | Confirm `HF_TOKEN` is configured (§4.1) **and** that the same account was approved for `facebook/dinov3-vitl16-pretrain-lvd1689m` **and** `briaai/RMBG-2.0`. Recreate the container after editing `.env`: `docker compose up -d trellis`. Human-only step — do not ask for the token. |
| **`nvidia` runtime missing / `unknown runtime: nvidia`** | NVIDIA Container Toolkit not installed or not registered with Docker | Human + sudo: install the Toolkit, then `sudo nvidia-ctk runtime configure --runtime=docker` and restart Docker. Verify with `docker info --format '{{json .Runtimes}}' \| grep nvidia`. Do not switch the compose file to `deploy.resources` — `runtime: nvidia` is used deliberately because it honours `NVIDIA_DRIVER_CAPABILITIES=graphics`. |
| **Docker GPU passthrough fails inside the container** | Toolkit/driver mismatch, or the container was created before the runtime was registered | `./scripts/preflight.sh --gpu-test`, then ladder step C. Recreate containers (`docker compose up -d --force-recreate trellis`) rather than rebuilding the image. |
| **`no kernel image is available for execution` / torchvision CUDA failure** | An sm_121-incompatible binary — usually a stock torchvision wheel | Keep the source-built torchvision and the sm_121 build flags. The TRELLIS Dockerfile asserts an `sm_121` cubin with `cuobjdump` and fails the build deliberately. Rebuild with the current Dockerfile; do not substitute a wheel. The op that needs it is `torchvision.ops.deform_conv2d`, reached through RMBG-2.0's runtime-downloaded `birefnet.py` — grepping the TRELLIS source will not reveal it. |
| **Insufficient memory / a quality mode is disabled** | Unified-memory gate refusing, correctly | Read the per-mode `reason` from `GET /system`. Other resident models are consuming the shared pool. Prefer Standard (`512`) or Hunyuan Shape. Do **not** lower `TRELLIS_MIN_AVAILABLE_GB` or `TRELLIS_PROTECTED_FLOOR_GB`, and do not kill other processes. |
| **Hunyuan not offered in the generator selector** | The `hunyuan` profile was never built or is not running | `docker compose --profile hunyuan build hunyuan` then `... up -d hunyuan`. Check `docker compose --profile hunyuan logs hunyuan` and host port 8190. A missing Hunyuan backend is reported as a backend problem, not a memory problem. |
| **Hunyuan still holds GiB after unloading** | Allocator retains memory after the pipeline is released | Only when the worker is idle: `docker compose --profile hunyuan restart hunyuan`. Models reload lazily on the next generation. |
| **Permission denied, or cache write errors** | `.env` `UID`/`GID` do not match the bind-mount owner | Set them to `id -u` / `id -g` and recreate the containers. Hunyuan needs its writable `/tmp` caches and `/models`; keep those image settings. |
| **Hunyuan `pkg_resources`, `libharfbuzz`, or remesh failure** | Building against an older recipe | Rebuild with the current `hunyuan/Dockerfile` — it pins `setuptools<81`, installs the required GL/font libraries and Open3D 0.18.0. |
| **Exclusive unavailable, or a workload left paused** | Helper not installed, socket ownership wrong, or a lease not released | Follow [docs/SETUP.md](docs/SETUP.md), including socket ownership and UID/GID. Recover with `sudo forge3d-resctl recover`; inspect with `sudo journalctl -u forge3d-resctl.service -n 50 --no-pager`. An inaccessible socket leaves Exclusive *unavailable* rather than bypassing it — that is correct. |
| **Build runs for tens of minutes** | Expected — ARM64 CUDA extensions compile from source | Let it finish. Lower `MAX_JOBS` if it is competing with other workloads. |
| **UI unreachable at 127.0.0.1:8189** | `FORGE3D_BIND` is set to a non-loopback address, or the container is not up | Use the bind-independent probe in §7 step E. Do **not** "fix" this by setting `0.0.0.0`. |

---

## 11. Reporting template

Finish by reporting exactly this, and nothing secret:

```text
Detected hardware
  - Classification: TESTED | UNTESTED | UNSUPPORTED
  - GPU / compute capability / driver
  - OS / architecture / MemTotal
  - Docker, Compose, NVIDIA Container Toolkit versions

Installed backends
  - TRELLIS: built / started / not installed
  - Hunyuan (2.1 + Multi-View): built / started / not installed

Local access URL
  - http://127.0.0.1:8189/  (loopback; SSH tunnel for remote access)

Validation results
  - A Compose config validates        pass/fail
  - B containers built and started    pass/fail
  - C GPU visible in container        pass/fail
  - D verify_stack.py / verify_hunyuan.py   pass/fail (+ any WARN lines)
  - E /health + /system respond, UI reachable   pass/fail
  - F test_step5.py / test_step6.py   pass/fail/not applicable
  - G generation                      NOT RUN unless explicitly requested

Still requiring manual action
  - e.g. Hugging Face gated approval for dinov3 / RMBG-2.0 (never the token itself)
  - e.g. install the optional Exclusive helper (needs root)
  - e.g. free unified memory before high-quality modes become available
```

Never include: token values, private IP addresses, private hostnames, private
filesystem paths belonging to the human, or resolved `docker compose config` output.

---

## 12. What Forge3D does not do

**Forge3D currently makes generated GLBs. It does not automatically make every
output game-ready.**

A generated GLB is a mesh with baked textures. Rigging, retopology and
optimisation, LODs, collision, material/shader setup for a specific engine,
animation, and engine import conventions are **not** part of Forge3D. Generated
assets commonly need cleanup before game use. TRELLIS texture maps are baked PBR;
the verified Hunyuan 2.1 GLB contained baked albedo rather than a complete
metallic/roughness/normal set, and Multi-View texturing remains unverified.

That downstream work belongs to a separate Blender / game asset pipeline — the
`game-3d-asset-pipeline` Skill repository is the intended companion for it. It is
**not** a dependency of Forge3D: Forge3D installs, runs and produces GLBs without
it, and an install agent should not fetch, install or configure it as part of
setting up Forge3D.

Do not describe Forge3D's output as "game-ready" in any report.

---

## 13. Future work — packaged installers

A one-click **Pinokio** package is **future work**, not something to build during
an installation. The priority is making the Git clone + agent-driven Docker
installation reliable. If asked about it, say it is planned and not yet available.

---

## Related documents

[README.md](README.md) · [docs/SETUP.md](docs/SETUP.md) ·
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md)

The `docs/step-*.md` files are historical milestone records — measurements and
reasoning, not the current installation guide. Read them for *why* a value is what
it is, never as install instructions.
