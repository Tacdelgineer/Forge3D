# Step 01 — DGX Spark Audit (Diagnosis Only)

**Date:** 2026-09-09 20:39–20:50 PDT
**Host:** `aitopatom-c85a`
**Scope:** Read-only diagnosis. Nothing was installed, stopped, modified, or deleted.

> **Note on project location.** The brief proposed `~/Projects/3d-generator/`. `~/Projects` does not
> exist on this host — every existing project lives directly at `~/<name>` (`~/content-factory`,
> `~/parameter-golf`, `~/petri`, `~/flow88-enhancer-engine`, …). This audit is therefore filed at
> `~/3d-generator/docs/`. If you prefer the `~/Projects/` prefix, it is a one-line move before Step 2.

---

## 1. Current system

### Hardware / platform

| Property | Value |
| --- | --- |
| Machine | GIGABYTE **AI TOP ATOM** (GB10 Grace-Blackwell, DGX Spark class) |
| Architecture | `aarch64` (ARM64) |
| Kernel | `6.17.0-1031-nvidia` |
| OS | Ubuntu 24.04.4 LTS (Noble) |
| Uptime | 8 days, 9:52 |
| CPU | 20 cores — 10× ARM Cortex-X925 @ 3.9 GHz + 10× Cortex-A725 @ 2.8 GHz (big.LITTLE, 1 NUMA node) |
| CPU features | SVE2, BF16, I8MM, SHA3/SM4 |
| Cache | L2 25 MiB, L3 24 MiB |
| RAM | 121.7 GiB unified (127,600,788 kB), 16 GiB swap (unused) |
| GPU | **NVIDIA GB10**, Blackwell, **compute capability 12.1 (sm_121)** |
| GPU addressing | **ATS** — coherent unified memory, no discrete framebuffer |
| Driver | 580.173.02 (NVIDIA Open Kernel Module for aarch64) |
| CUDA (driver) | 13.0 |
| CUDA toolkit | 13.0, V13.0.88 at `/usr/local/cuda-13.0` |
| Container toolkit | NVIDIA Container Toolkit 1.20.0, `nvidia` runtime registered in Docker |
| Docker | dockerd + containerd, overlay2, Docker Root Dir `/var/lib/docker` |
| Disk | 1× NVMe `nvme0n1` 3.7 TB, ext4 on `/`, **2.5 TB free (31% used)** |
| Python | 3.12.3; `uv` 0.10.9 available |

### PyTorch situation (critical for Step 2)

The PyTorch present in `~/jupyterlab/.venv` reports:

```
torch 2.9.0+cu130 / cuda 13.0 / device NVIDIA GB10 / capability (12, 1)
arch_list ['sm_80', 'sm_90', 'sm_100', 'sm_110', 'sm_120', 'compute_120']

UserWarning: Found GPU0 NVIDIA GB10 which is of cuda capability 12.1.
Minimum and Maximum cuda capability supported by this version of PyTorch is (8.0) - (12.0)
```

**The stock pip wheel contains no `sm_121` cubins.** It runs only because `compute_120` PTX is
JIT-compiled to sm_121 at load time. This is exactly the constraint recorded in prior project notes,
and it is the single most important fact governing the TRELLIS build strategy: any CUDA extension we
compile must be built with `TORCH_CUDA_ARCH_LIST` including `12.1`, not left at the wheel default.

---

## 2. RAM diagnosis

### Headline numbers (sample at 20:39)

```
               total        used        free      shared  buff/cache   available
Mem:           121Gi        41Gi        53Gi        93Mi        29Gi        80Gi
Swap:           15Gi       328Ki        15Gi
```

**80 GiB is genuinely available. Swap is untouched. The machine is not under memory pressure.**

### Where the "used" memory actually is

`free`'s *used* bucket = `MemTotal − MemFree − Buffers − Cached − SReclaimable` = **39.2 GiB**.
Breaking that down from `/proc/meminfo`:

| Component | Size | Notes |
| --- | ---: | --- |
| **NVIDIA driver-pinned unified memory** | **~33.6 GiB** | Not attributable to any process RSS — see below |
| `AnonPages` (all process anonymous memory, combined) | 4.86 GiB | Every userspace process on the box, added up |
| `SUnreclaim` (kernel slab, non-reclaimable) | 0.56 GiB | |
| `PageTables` + `SecPageTables` | 0.13 GiB | |
| `KernelStack` + `ShadowCallStack` | 0.02 GiB | |
| `Mlocked` / `Unevictable` | 0.02 GiB | |
| **Total** | **~39.2 GiB** | |

Separately, **29.2 GiB is `buff/cache`** (`Buffers` 1.06 + `Cached` 26.4 + `SReclaimable` 1.75 GiB).
This is filesystem page cache from reading model files — it is **not** consumed memory; the kernel
reclaims it on demand. It is included in the 80 GiB `available` figure.

### The ~33.6 GiB: Ollama

`nvidia-smi` process table:

```
|    0   N/A  N/A       4379      C   /usr/local/bin/ollama          32888MiB |
|    0   N/A  N/A     913941      C   python (ComfyUI)                 170MiB |
```

```
$ ollama ps
NAME               ID              SIZE     PROCESSOR    CONTEXT    UNTIL
qwen3.6:35b-a3b    07d35212591f    34 GB    100% GPU     262144     Forever
```

The `ollama runner` process (PID 4379) shows an RSS of only **1.15 GiB** in `ps`, yet holds
**32,888 MiB (32.1 GiB)** of GPU memory. On GB10 there is no discrete VRAM: the GPU allocation is
carved out of the same 128 GB pool, is pinned by the NVIDIA driver, and therefore appears in
`free` as *used* while being invisible to `ps`/`top`. **This is the whole mystery.**

It is pinned deliberately. `/etc/systemd/system/ollama-warm-qwen3.service` runs
`/usr/local/libexec/ollama-warm-qwen3`, which POSTs:

```json
{"model":"qwen3.6:35b-a3b", ..., "keep_alive":-1, "options":{"num_predict":2}}
```

`keep_alive: -1` means **never unload**. Combined with a 262,144-token context window, that is the
34 GB residency. The unit is `enabled`, so it re-pins on every boot.

> The service is described as "Keep qwen3:8b resident" but the script actually warms
> `qwen3.6:35b-a3b` — the description is stale relative to the payload. Cosmetic, but worth knowing.

### Why you may have seen ~50 GB

At 20:36–20:37, roughly two minutes before this audit began, a batch of `dgx-ai-stack` containers was
stopped (`ai-whisper`, `ai-voxcpm2`, `ai-supervisor`, `ai-redis`, `ai-wan-ui`, `ai-gateway`,
`ai-frontend`, `ai-misotts`, `ai-lipsync`, `ai-fish-speech`, `ai-f5tts`, `ai-liveportrait`,
`flow88-mix-engine`, `murch-server`). Those services would plausibly account for the extra ~9 GiB
between a ~50 GB reading and the 41 GiB measured here. See §5 for what triggered that.

### Top memory consumers (`ps aux --sort=-%mem`)

| PID | RSS | Process | Belongs to |
| ---: | ---: | --- | --- |
| 913941 | 1.35 GB | `python main.py --port 8188` (ComfyUI, in container) | content-factory / dgx-ai-stack |
| 4379 | 1.15 GB **+ 32.1 GiB GPU** | `ollama runner` (qwen3.6:35b-a3b) | Ollama (shared LLM backend) |
| 909424 | 1.02 GB | VS Code Pylance language server | dev tooling |
| 907369 | 0.84 GB | VS Code extension host | dev tooling |
| 907688 | 0.39 GB | VS Code file watcher | dev tooling |
| 914571 | 0.34 GB | `claude` (this session) | dev tooling |
| 911093 | 0.23 GB | `claude` (second session) | dev tooling |
| 906995 | 0.16 GB | VS Code server main | dev tooling |
| 2153 | 0.15 GB | NoMachine `nxserver.bin` | remote desktop |
| 907937 | 0.14 GB | Codex app-server | dev tooling |
| 2390 | 0.12 GB | `dockerd` | infra |
| 2163 | 0.08 GB | `tailscaled` | networking |

**Observation:** the VS Code server plus its extensions and the two `claude` sessions total roughly
**2.9 GB** — more real process memory than everything AI-related combined. All actual AI weight is in
the GPU-pinned pool, not in RSS.

---

## 3. GPU diagnosis

```
+-----------------------------------------+------------------------+----------------------+
|   0  NVIDIA GB10                    On  | 0000000F:01:00.0   Off |                  N/A |
| N/A   43C    P0              9W /  N/A  |     Not Supported      |      0%      Default |
+-----------------------------------------+------------------------+----------------------+
```

| Metric | Value |
| --- | --- |
| GPU utilization | **0 %** (71 samples over 14 s: max 0 %, avg 0 %) |
| Memory utilization | 0 % |
| Encoder / Decoder / JPEG / OFA | 0 % |
| Power draw | ~10 W (idle) |
| Temperature | 43 °C |
| Performance state | P0 |
| Persistence mode | Enabled (`nvidia-persistenced` running) |
| MIG | Not available |
| Addressing mode | **ATS** |

### Processes holding GPU memory

| PID | Process | GPU memory | Note |
| ---: | --- | ---: | --- |
| 4379 | `ollama` runner | **32,888 MiB** | `qwen3.6:35b-a3b`, pinned `keep_alive=-1` |
| 913941 | `python` (ComfyUI) | 170 MiB | Idle CUDA context, no model loaded |

**Total GPU memory committed: ~33.1 GiB. Compute is completely idle.**

### How `nvidia-smi` is misleading on this box

This matters more than it sounds, and it will bite during Step 2:

1. **`Memory-Usage` reads `Not Supported`.** `nvidia-smi -q -d MEMORY` returns `N/A` for FB Memory
   Total / Reserved / Used / Free, and `--query-gpu=memory.total,memory.used,memory.free` returns
   `[N/A]`. There is no framebuffer to report — memory is the system's 128 GB pool.
2. **Anything that autodetects VRAM will get nothing or zero.** vLLM's `gpu_memory_utilization`,
   many ComfyUI/Gradio memory probes, and most "how much VRAM do I have" helper scripts read exactly
   those fields. Expect them to misbehave, report 0 GB, or divide-by-zero. TRELLIS/Gradio memory
   management is in this category and may need a manual override.
3. **The per-process table is still accurate** and is the *only* place GPU allocations are visible.
4. **The authority on real memory is `/proc/meminfo`**, not `nvidia-smi`. GPU allocations show up
   there as `MemFree` shrinking with no matching process RSS.
5. `tegrastats` is **not** installed on this host, so the usual Jetson fallback is unavailable.

---

## 4. Running services

| Service | Type | RAM | GPU | Port | Likely project |
| --- | --- | ---: | ---: | ---: | --- |
| **Ollama** (`ollama serve` + runner) | systemd | 1.2 GB RSS | **32,888 MiB** | `11434` (Tailscale IP only), `34195` (loopback) | Shared LLM backend for content-factory |
| **ComfyUI** (`content-factory-comfyui`) | systemd → docker | 941 MiB | 170 MiB | `127.0.0.1:8188` | content-factory / dgx-ai-stack |
| **pipeline-worker** (`pipeline_worker.py`) | systemd | 26 MB | — | `8788` (Tailscale IP) | content-factory — "DGX Hermes" media pipeline |
| **spark-status** (`spark_status.py`) | systemd | 19 MB | — | `8765` (Tailscale IP) | DGX Spark status server |
| **Portainer** | docker | 25 MiB | — | `127.0.0.1:9000`, `9443` | Container management UI |
| **dgx-trivy-watchdog** | docker | 163 MiB | — | — | dgx-security-watchdog (image CVE scanning) |
| **NVIDIA DGX Dashboard** | systemd | 26 MB | — | `127.0.0.1:11000` | DGX OS built-in |
| **dockerd + containerd** | systemd | 173 MB | — | — | Infra |
| **tailscaled** | systemd | 78 MB | — | `41641/udp`, `40293` | Networking (all AI services bind Tailscale-only) |
| **NoMachine** (`nxserver`/`nxd`) | systemd | 149 MB | — | `4000`, `24825`, `29617` | Remote desktop |
| **Samba** (`smbd`) | systemd | 23 MB | — | `139`, `445` | File sharing |
| **sshd** | systemd | — | — | `22` | Remote access |
| **CUPS** | systemd | 24 MB | — | `631`, `5353/udp` | Printing (DGX OS default) |
| **VS Code Server + extensions + 2× `claude`** | user session | ~2.9 GB | — | loopback ephemeral | Dev tooling (this session) |

**Not running:** vLLM, llama.cpp, Jupyter, Open WebUI, Redis, Traefik — all present on disk but stopped.

### Notable service internals

`pipeline-worker.service` reads `/etc/pipeline-worker.env`, which contains:

```
VISUAL_MIN_AVAILABLE_GB=40
```

`pipeline_worker.py:872` enforces a **hard gate**: any visual job first reads `MemAvailable` from
`/proc/meminfo`, and if it is below 40 GiB it asks ComfyUI to unload cached models, waits, re-checks,
and **fails the job** if still below:

```
Visual job requires 40.0 GiB MemAvailable; only X GiB is available after ComfyUI cleanup
```

This is the most important constraint for coexistence — see §8.

---

## 5. Docker state

### Running (3 of 33 containers)

| Container | Image | CPU | RAM | GPU | Ports | Compose project |
| --- | --- | ---: | ---: | ---: | --- | --- |
| `content-factory-comfyui` | `dgx-ai-stack-comfyui:latest` | 0.08 % | 940.9 MiB | 170 MiB | `127.0.0.1:8188` | dgx-ai-stack |
| `portainer` | `portainer/portainer-ce:latest` | 0.00 % | 25.1 MiB | — | `127.0.0.1:9000`, `9443` | — |
| `dgx-trivy-watchdog` | `aquasec/trivy:latest` | 0.00 % | 162.7 MiB | — | — | dgx-security-watchdog |

Combined running-container footprint: **~1.13 GiB RAM, 170 MiB GPU, effectively 0 % CPU.**

### Stopped (30 containers)

Attributed by Compose label:

| Compose project | Containers | Working dir |
| --- | --- | --- |
| `dgx-ai-stack` | `ai-wan-ui`, `ai-frontend`, `ai-gateway`, `ai-whisper`, `ai-misotts`, `ai-cosyvoice3`, `ai-qwen3tts`, `ai-redis`, `ai-lipsync`, `ai-fish-speech`, `ai-f5tts`, `ai-liveportrait`, `ai-voxcpm2`, `ai-supervisor`, `ai-wan21`, `ai-comfyui` | `~/neonforge` |
| `dgx-vllm-stack` | `vllm-gemma-1`, `vllm-nemotron-1`, `open-webui-1` | `~/dgx-vllm-stack` |
| `roughcut` | `roughcut-frontend-1`, `roughcut-backend-1`, `roughcut-worker-1` | `~/video-agent` |
| `flow88-mix-engine` | `flow88-mix-engine` | `~/Flow88-Mix-Engine` |
| `flow88-enhancer-engine` | `flow88-enhancer` | `~/flow88-enhancer-engine` |
| `attention-worker` | `attention-worker` | `~/attention-worker` |
| `murch` | `murch-server` | `/srv/tacdel/studio/murch` |
| `parameter-golf` | `parameter-golf` | `~/parameter-golf` |
| (no label) | `petri-web`, `openshell-cluster-nemoclaw`, `workbench-proxy` | — |

**Stopped containers consume zero RAM, zero GPU, and zero CPU.** They occupy disk only. Nothing was
stopped or removed during this audit.

### The 20:36–20:37 container teardown

`docker events` over the last 24 h shows exactly one burst of activity, immediately before this audit:

```
20:36:05  stop  ai-frontend, ai-gateway, ai-misotts, ai-lipsync, ai-fish-speech,
                ai-f5tts, ai-liveportrait
20:36:25  stop  flow88-mix-engine
20:36:32  stop  murch-server
20:36:59  stop  ai-wan-ui, content-factory-comfyui
20:37:05  start content-factory-comfyui
20:37:12  stop  ai-whisper, ai-redis
20:37:23  stop  ai-voxcpm2, ai-supervisor
20:37:26  stop  content-factory-comfyui
20:37:32  start content-factory-comfyui
```

Findings:

- **This is not a recurring job.** There is no matching cron entry, no `/etc/cron.d` job, and no
  systemd timer. `docker events --since 24h` contains no other start/stop activity at all.
- **It was not the pipeline worker's memory gate.** `_free_comfyui_memory()` (`pipeline_worker.py:674`)
  only calls ComfyUI's unload endpoint; it does not stop containers.
- The two `content-factory-comfyui` restarts are explained by `comfyui.service`
  (`ExecStartPre=-/usr/bin/docker rm -f content-factory-comfyui`) being restarted twice.
- The most likely explanation is a **manual or external teardown** — someone running
  `docker compose stop` in `~/neonforge` plus a `systemctl restart comfyui`, shortly before this
  audit. A second `claude` process (PID 911093) started at 20:34, two minutes before the burst.

Nothing here is broken; flagging it so you can confirm it was intentional when you verify the box.

### Docker disk usage

```
TYPE            TOTAL     ACTIVE    SIZE      RECLAIMABLE
Images          215       31        258GB     207GB (80%)
Containers      33        3         5.783GB   5.77GB (99%)
Local Volumes   5         5         8.631GB   0B (0%)
Build Cache     1326      0         93.44GB   19.51GB
```

Actual on-disk: `du -sh /var/lib/docker` → **309 GB**.

---

## 6. Storage

### Free space

| Filesystem | Type | Size | Used | Avail | Use% | Mount |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| `/dev/nvme0n1p2` | ext4 | 3.7 T | 1.1 T | **2.5 T** | 31 % | `/` |
| `/dev/nvme0n1p1` | vfat | 511 M | 6.4 M | 505 M | 2 % | `/boot/efi` |
| `tmpfs` | tmpfs | 61 G | 0 | 61 G | 0 % | `/dev/shm` |

Single NVMe device, one big ext4 root. There is no separate data volume — everything shares the
same 3.7 TB pool.

### Major consumers

| Path | Size | What it is |
| --- | ---: | --- |
| `/var/lib/docker` | **309 GB** | Docker images + build cache (~227 GB reclaimable) |
| `~/ai_models` | 124 GB | Local model store |
| `/srv/ai` | 143 GB | Shared AI data root |
| ├ `/srv/ai/models` | 109 GB | `comfyui` 77 G, `voice` 16 G, `fish_speech` 11 G, `voxcpm2` 4.7 G, `whisper` 1.5 G |
| ├ `/srv/ai/cache/hf` | 34 GB | Hugging Face hub cache (shared into containers) |
| ├ `/srv/ai/outputs` | 657 MB | `comfyui` 393 M, `tts` 203 M, `uploads` 42 M |
| └ `/srv/ai/assets` | 79 MB | pipeline-worker assets |
| `~/.cache/huggingface` | 90 GB | Second, user-level HF cache |
| `~/pinokio` | 77 GB | Pinokio app environments |
| `~/vllm-spark` | 20 GB | vLLM source build tree |
| `~/dgx-security-watchdog` | 16 GB | Trivy DB + scan artifacts |
| `~/jupyterlab` | 5.8 GB | Jupyter venv (contains the torch 2.9.0+cu130 install) |
| `~/petri` | 3.3 GB | Petri project |
| `~/parameter-golf` | 3.2 GB | Parameter Golf project |

### Recommended locations for this project

Storage is not a constraint — 2.5 TB free against an estimated ~70 GB total need.

| Purpose | Recommended path | Est. size | Rationale |
| --- | --- | ---: | --- |
| Project root | `~/3d-generator/` | small | Matches host convention (`~/<project>`) |
| TRELLIS weights | `/srv/ai/models/trellis2/` | ~16–20 GB | Sits with `comfyui`/`whisper`/`voice`; survives container rebuilds; mount **read-only** |
| HF cache | `/srv/ai/cache/hf/` (reuse) | +~5 GB | Already mounted into the ComfyUI container; avoids a third HF cache. DINOv3/RMBG-2.0 land here |
| Uploads | `~/3d-generator/data/uploads/` | small | Project-local, trivial to back up |
| Outputs (`.glb`) | `~/3d-generator/data/outputs/` | grows | Project-local, trivial to back up |
| Docker image | `/var/lib/docker` | ~30–45 GB | The CUDA devel base + compiled extensions is a large image |

> House style would put outputs under `/srv/ai/outputs/3d-generator/`. I recommend project-local
> `data/` instead — it keeps everything the dashboard manages in one directory that you can move,
> back up, or delete as a unit, which is exactly what the rename/delete/manage-old-generations
> requirements call for. Either works; say the word and I will use `/srv/ai/outputs/`.

---

## 7. TRELLIS.2 compatibility

### Verdict: **Viable.** Recommended strategy: **Option C — custom Dockerfile with patched dependencies**, seeded from the existing DGX Spark fork.

### What TRELLIS.2 actually requires

From the [official README](https://github.com/microsoft/TRELLIS.2):

- Model: **TRELLIS.2-4B** (4 B params), [`microsoft/TRELLIS.2-4B`](https://huggingface.co/microsoft/TRELLIS.2-4B) — **16.2 GB** download
- Output resolutions 512³ → 1536³, PBR materials, `.glb` export
- **Minimum 24 GB GPU memory**; tested on A100 / H100 only
- Linux only; PyTorch 2.6.0 + CUDA 12.4 by default
- Attention backend: `flash-attn` (default) or `xformers` (fallback)
- CUDA extensions via `setup.sh` flags: `--flash-attn --nvdiffrast --nvdiffrec --cumesh --o-voxel --flexgemm`
- Gated Hugging Face models required: **DINOv3** and **RMBG-2.0**

**Important:** TRELLIS.2 is *not* TRELLIS 1. It **does not need Kaolin, spconv, diffoctreerast,
mip-splatting, or vox2seq** — those were replaced by `CuMesh`, `FlexGEMM`, and `o-voxel`. This
removes the single worst ARM64 dependency (Kaolin has no aarch64 wheels). `xformers` is also not
needed, since flash-attn builds fine here.

### Dependency-by-dependency assessment for this host

| Dependency | ARM64 wheel? | sm_121? | Assessment |
| --- | --- | --- | --- |
| **PyTorch** | Yes (cu128/cu129/cu130 aarch64 wheels exist) | **No — max `sm_120` + `compute_120` PTX** | ⚠️ Works via PTX JIT. Verified live on this box. Adds one-time JIT latency per kernel. |
| **torchvision** | Yes | No | ⚠️ **Must be rebuilt from source** with `TORCH_CUDA_ARCH_LIST=12.1`; the fork does exactly this |
| **flash-attn** | **No aarch64 wheel** | Buildable | ⚠️ **Source build required, 30–60+ min, RAM-hungry.** sm_121 is binary-compatible with sm_120 codegen, so FA2 compiled for 12.0/12.1 runs natively on GB10 |
| **nvdiffrast** | Source-only anyway | Yes | ⚠️ **Needs EGL/OpenGL** — see the gotcha below |
| **nvdiffrec** (renderutils) | Source-only | Yes | ✅ Compiles with arch 12.1 |
| **CuMesh** | Source-only | Yes | ✅ Compiles with arch 12.1 |
| **FlexGEMM** | Source-only (Triton) | Yes | ✅ Triton JITs to sm_121; needs `TRITON_PTXAS_PATH` set |
| **o-voxel** | Source-only (in TRELLIS.2 repo) | Yes | ✅ Compiles with arch 12.1 |
| **xformers** | n/a | n/a | ✅ **Not needed** |
| **Kaolin** | **No aarch64 wheels** | n/a | ✅ **Not needed by TRELLIS.2** |
| **Memory (24 GB min)** | — | — | ✅ 80 GiB available today |

### The reference implementation

[`dr-vij/Trellis2-DGX-Spark-Docker`](https://github.com/dr-vij/Trellis2-DGX-Spark-Docker) is a
working, Docker-based DGX Spark port. Reading its actual `Dockerfile` (not just the README), it:

- Bases on `nvcr.io/nvidia/cuda:12.9.1-cudnn-devel-ubuntu24.04` (multi-arch, arm64 available)
- Sets **`ENV TORCH_CUDA_ARCH_LIST="12.1+PTX"`** — the correct sm_121 target
- Installs PyTorch nightly cu129, then **uninstalls and rebuilds torchvision from source** with
  `FORCE_CUDA=1 TORCH_CUDA_ARCH_LIST="12.1"`
- Builds `flash_attn==2.7.4.post1` from source (`--no-build-isolation`), noting 30+ minutes
- Builds nvdiffrast v0.4.0, nvdiffrec (`renderutils` branch), CuMesh, FlexGEMM, and o-voxel from source
- Sets `TRITON_PTXAS_PATH=$CUDA_HOME/bin/ptxas` for FlexGEMM
- Ships a `docker-compose.yml` with GPU reservation, Gradio on `7860`, and bind-mounted HF/Triton/torch caches

A second, non-Docker conda-based port exists at
[`raziel2001au/dgx-trellis2`](https://github.com/raziel2001au/dgx-trellis2), announced on the
[NVIDIA DGX Spark forum](https://forums.developer.nvidia.com/t/trellis-2-on-dgx-spark/355816).
An independent [hands-on writeup](https://note.com/npaka/n/n344aae232ac0?hl=en) reports the Docker
route working end-to-end on a DGX Spark with GLB export, ~2 hours for the first build including
model download.

Note also [microsoft/TRELLIS.2#32](https://github.com/microsoft/TRELLIS.2/issues/32) — a DGX Spark
user hitting `The requested image's platform (linux/amd64) does not match linux/arm64/v8`, still
unresolved upstream. **Upstream has no ARM64 story. We must build our own image.**

### Why Option C, not A / B / D

| Option | Verdict |
| --- | --- |
| **A. Official Microsoft TRELLIS.2** | ❌ No ARM64 support, no Docker image, defaults to CUDA 12.4 / torch 2.6, and its `setup.sh` will not target sm_121. Would fail as-is. |
| **B. Use a fork directly** | ⚠️ Close, but `dr-vij`'s image is a **Gradio demo**, not a service. It bind-mounts the TRELLIS source as a git submodule, exposes no REST API, and sets `NVIDIA_DRIVER_CAPABILITIES: compute,utility` (a likely nvdiffrast problem — see below). Good seed, wrong shape for our dashboard. |
| **C. Custom Dockerfile with patched dependencies** | ✅ **Recommended.** Take `dr-vij`'s Dockerfile as the proven build recipe, fix the EGL/capabilities issue, pin versions, drop Gradio, and expose our own FastAPI service. Self-contained, reproducible, `docker compose up -d` / `down`. |
| **D. Something else** (native conda, Pinokio, ComfyUI node) | ❌ Native conda contaminates the host and conflicts with the "everything in Docker" requirement. A ComfyUI 3D node would entangle us with the running content-factory ComfyUI — explicitly undesirable. |

### Two concrete risks to plan for in Step 2

1. **nvdiffrast + EGL.** nvdiffrast's OpenGL rasterizer needs a working EGL stack inside the
   container. Prior work on this exact host established that **NGC `cuda:*-devel` images need a
   manually written NVIDIA EGL ICD JSON**, or EGL silently falls back to llvmpipe (software
   rendering — correct output, catastrophically slow). The host has `10_nvidia.json` and
   `50_mesa.json` in `/usr/share/glvnd/egl_vendor.d/`, but the container will not. Additionally,
   `dr-vij`'s compose sets `NVIDIA_DRIVER_CAPABILITIES: compute,utility` — **`graphics` and
   `display` are missing**, which is required for EGL passthrough. Plan: set
   `NVIDIA_DRIVER_CAPABILITIES=compute,utility,graphics` and write the ICD JSON in the Dockerfile.
   Fallback if it resists: force nvdiffrast's `RasterizeCudaContext` instead of `RasterizeGLContext`.

2. **Gated Hugging Face models.** DINOv3 and RMBG-2.0 both require **manually approved access
   requests** on huggingface.co, plus an `HF_TOKEN`. This is a human action that cannot be
   automated and will block the first run. **Worth doing before Step 2 starts** — approvals can
   take hours.

Lesser risks: the CUDA 12.9 base image against a CUDA 13.0 / 580 driver is fine (minor-version
forward compatibility), but pinning to a CUDA 13.0 base would match the host better and is worth
testing. `flash-attn` source builds are memory-hungry — cap `MAX_JOBS` (≈8) so the build does not
starve the running ComfyUI service.

### Expected performance (estimate)

Microsoft's published H100 timings are 512³ ≈ 3 s, 1024³ ≈ 17 s, 1536³ ≈ 60 s. GB10 has roughly an
order of magnitude less compute and far lower memory bandwidth (~273 GB/s vs ~3.35 TB/s), so a
reasonable expectation is **~30 s–3 min at 512³** and **several minutes at 1024³+**, plus a one-time
PTX JIT warm-up on the first generation after each container start. These are extrapolations, not
measurements — we will benchmark for real in a later step.

---

## 8. Proposed architecture

```
Browser
   │
   ▼
┌──────────────────────────────────────────────┐
│ container: 3d-generator   (port 8189)        │
│                                              │
│  FastAPI  ── serves static HTML/JS dashboard │
│     │      ── REST: upload / jobs / files    │
│     ▼                                        │
│  SQLite job table  (data/jobs.db)            │
│     │                                        │
│     ▼                                        │
│  single background worker thread             │
│     │                                        │
│     ▼                                        │
│  TRELLIS.2 pipeline (in-process, 4B)         │
└──────────────────────────────────────────────┘
   │                        │
   ▼                        ▼
bind mounts:          /srv/ai/models/trellis2  (ro)
  data/uploads/       /srv/ai/cache/hf         (rw)
  data/outputs/
```

### Layout

```
~/3d-generator/
    docker-compose.yml
    Dockerfile
    .env                    # HF_TOKEN, UID/GID, port
    app/
        main.py             # FastAPI: REST + static mount
        worker.py           # single-threaded job queue
        trellis_backend.py  # thin wrapper; Hunyuan3D slots in beside it later
        db.py               # SQLite
        static/             # index.html + one JS file + <model-viewer>
    data/
        uploads/            # input images
        outputs/            # .glb + previews
        jobs.db             # SQLite
    docs/
        step-01-dgx-audit.md
```

Models live at `/srv/ai/models/trellis2` and the HF cache at `/srv/ai/cache/hf` — both **outside** the
project, bind-mounted in, so `docker compose down`, image rebuilds, and even deleting the project
directory never touch the 16 GB of weights.

### Deliberate simplicity choices

- **One container, not two.** The dashboard and the model share a process, so the 4B model is loaded
  once and there is no IPC layer. Splitting them would mean either loading the model twice or adding
  a message broker — neither is justified for a single-user, single-GPU box.
- **SQLite, not Redis/Postgres.** Job state is a handful of rows. A file in `data/` is durable across
  restarts, needs no extra service, and is directly inspectable.
- **A worker thread, not Celery/RQ.** There is exactly one GPU. Concurrency is 1 by definition; a
  thread with a queue is the correct size of solution and gives us serialization for free.
- **No auth, no reverse proxy.** Bind to `127.0.0.1:8189` and reach it over Tailscale, matching how
  every other service on this box is already exposed.
- **`<model-viewer>` for preview.** One `<script>` tag renders `.glb` in the browser. No server-side
  rendering, no extra dependency.
- **Second backend later.** `trellis_backend.py` behind a small interface means Hunyuan3D becomes an
  additional module and a dropdown, not a rewrite.

### Control surface

```bash
cd ~/3d-generator
docker compose up -d     # start
docker compose down      # stop — releases all GPU and RAM
docker compose logs -f   # watch
```

### Idle cost when stopped

| Resource | While stopped |
| --- | ---: |
| GPU memory | **0 MiB** |
| GPU utilization | **0 %** |
| RAM | **0 GiB** |
| CPU | **0 %** |
| Disk (image) | ~30–45 GB |
| Disk (weights, persistent) | ~16–20 GB |

`docker compose down` removes the container and the CUDA context with it; the driver reclaims the
unified memory immediately. Verified precedent: the 30 stopped containers on this box right now
consume exactly zero RAM and zero GPU. Generated assets persist because they are on bind mounts,
not in the container filesystem.

### Foreseen conflicts with existing services

| # | Conflict | Severity | Mitigation |
| --- | --- | --- | --- |
| 1 | **Ollama pins 32.1 GiB forever** (`keep_alive:-1`). TRELLIS needs ≥24 GB. Together: ~55–65 GiB, leaving ~50 GiB `MemAvailable`. | **Medium** | Fits today. But if a big ComfyUI video job runs concurrently, we approach the limit. Escape hatch: `curl -d '{"model":"qwen3.6:35b-a3b","keep_alive":0}' .../api/generate` frees 32 GiB in seconds without touching config. Do **not** change the systemd unit. |
| 2 | **pipeline-worker's 40 GiB `MemAvailable` gate.** If TRELLIS drives `MemAvailable` below 40 GiB, content-factory **visual jobs fail outright**, not just slow down. | **High — the main risk** | Never run a large TRELLIS generation concurrently with a content-factory visual job. Long term, either raise the TRELLIS resolution ceiling deliberately, or add a `mem_limit` to our compose service. Worth a decision in Step 2. |
| 3 | **Single GB10, no MIG.** TRELLIS and ComfyUI would time-slice the same GPU. | Medium | Our single-worker queue serializes our own jobs. Cross-service contention means slower jobs, not failures. |
| 4 | **Port collisions.** | **None** | `7860`, `8189`, `8000`, `8080`, `3000`, `5000` are all free. `8188` (ComfyUI), `11434` (Ollama), `9000/9443` (Portainer), `11000` (DGX Dashboard), `8765`/`8788` (status/worker) are taken. **Recommend `8189`.** |
| 5 | **Build load.** flash-attn compilation will saturate all 20 cores for 30–60 min. | Low | Cap `MAX_JOBS=8`; build when the content-factory pipeline is idle. |
| 6 | **`dgx-trivy-watchdog`** will CVE-scan our new ~40 GB image. | Low | One-off I/O spike. Harmless. |
| 7 | **Docker disk growth.** Already 309 GB with ~227 GB reclaimable. | Low | 2.5 TB free. `docker system prune` is available if wanted — **not run during this audit**. |
| 8 | **`/srv/ai/models` is mode 777.** | Low | Mount our model dir **read-only** into the container and create `trellis2/` with tighter permissions. |

---

## Appendix — anything abnormal?

**Nothing is broken.** The machine is healthy: 0 % GPU utilization, load average 0.32, no swap in use,
80 GiB RAM available, 2.5 TB disk free, no zombie processes, no OOM events.

Five things worth your attention:

1. **32.1 GiB of RAM is pinned indefinitely by design.** `ollama-warm-qwen3.service` loads
   `qwen3.6:35b-a3b` with a 262 K context and `keep_alive:-1` at every boot. Intentional, but it is
   permanently a quarter of the machine, and it is the single biggest input to our memory budget.
2. **The `ollama-warm-qwen3` unit description is stale** — it says "Keep qwen3:8b resident" while the
   script warms `qwen3.6:35b-a3b`. Cosmetic.
3. **A batch of ~14 containers was stopped at 20:36–20:37**, two minutes before this audit, with no
   cron job, timer, or automation accounting for it. Almost certainly manual. Worth confirming it
   was you.
4. **~227 GB of reclaimable Docker data** (207 GB dangling images + 19.5 GB build cache). Not urgent
   at 31 % disk usage. Untouched.
5. **`nvidia-smi` cannot report GPU memory on this platform.** Expect any tool that autodetects VRAM
   to misbehave. This will most likely surface as a TRELLIS/Gradio memory-management quirk in Step 2.

---

## Sources

- [microsoft/TRELLIS.2](https://github.com/microsoft/TRELLIS.2) — official repo
- [microsoft/TRELLIS.2 README](https://github.com/microsoft/TRELLIS.2/blob/main/README.md) — requirements
- [microsoft/TRELLIS.2-4B](https://huggingface.co/microsoft/TRELLIS.2-4B) — 16.2 GB checkpoint
- [microsoft/TRELLIS.2#32](https://github.com/microsoft/TRELLIS.2/issues/32) — unresolved arm64 platform issue
- [dr-vij/Trellis2-DGX-Spark-Docker](https://github.com/dr-vij/Trellis2-DGX-Spark-Docker) — working Docker port (recommended seed)
- [raziel2001au/dgx-trellis2](https://github.com/raziel2001au/dgx-trellis2) — conda-based DGX OS port
- [TRELLIS.2 on DGX Spark — NVIDIA Developer Forums](https://forums.developer.nvidia.com/t/trellis-2-on-dgx-spark/355816)
- [Trying out 3D model generation with Trellis.2 on DGX Spark (npaka)](https://note.com/npaka/n/n344aae232ac0?hl=en) — hands-on report
- [Support for compute capability 12.1 (sm121) — NVIDIA GB10 · flash-attention#1969](https://github.com/Dao-AILab/flash-attention/issues/1969)
- [FlashAttention — Blackwell GPU Wiki](https://0xsero.github.io/blackwell-gpu-wiki/kernels/flashattention/)
- [bidual/awesome-dgx-spark](https://github.com/bidual/awesome-dgx-spark)

---

**Status: Step 1 complete. No changes made to the system. Awaiting approval before Step 2.**
