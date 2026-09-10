# Step 02 — Dockerized TRELLIS.2 backend

**Host:** `aitopatom-c85a` — GIGABYTE AI TOP ATOM (GB10 Grace-Blackwell, DGX Spark class)
**Scope:** Get TRELLIS.2 running in Docker behind a minimal FastAPI service. No dashboard yet.
**Prerequisite reading:** [step-01-dgx-audit.md](step-01-dgx-audit.md)

---

## Architecture

One container. One process. No broker, no database, no reverse proxy.

```
Browser / curl
      │  127.0.0.1:8189
      ▼
┌──────────────────────────────────────────────────┐
│ container: 3d-generator-trellis                  │
│                                                  │
│  uvicorn → FastAPI (app/main.py)                 │
│     ├── GET  /health                             │
│     ├── GET  /system                             │
│     └── POST /generate   (multipart, synchronous)│
│                    │                             │
│                    ▼                             │
│  app/memory.py   MemAvailable gate (refuse, not  │
│                  evict — never touches Ollama)   │
│                    │                             │
│                    ▼                             │
│  app/engine.py   lazy load + single-flight lock  │
│                    │                             │
│                    ▼                             │
│  TRELLIS.2 (/opt/TRELLIS.2, pinned commit)       │
│  flash-attn · nvdiffrast · nvdiffrec             │
│  CuMesh · FlexGEMM · o-voxel                     │
└──────────────────────────────────────────────────┘
      │                          │
      ▼                          ▼
 ./data (bind)              ./models (bind)
   uploads/<id>/              hf/            model weights
   outputs/<id>/              torch_extensions/  nvdiffrast JIT
     model.glb                triton/
     metadata.json            torch/
```

### Files

```
~/3d-generator/
├── Dockerfile              # the whole GB10/sm_121 build recipe
├── docker-compose.yml      # one service, restart: "no"
├── .env / .env.example     # HF_TOKEN, memory floor, UID/GID
├── app/
│   ├── main.py             # FastAPI: /health /system /generate
│   ├── engine.py           # lazy pipeline load, generation, GLB validation
│   ├── memory.py           # MemAvailable gate
│   └── config.py           # env-driven settings
├── scripts/
│   ├── start.sh  stop.sh  status.sh  generate.sh
│   └── verify_stack.py     # in-container CUDA/EGL verification
├── data/                   # bind mount — uploads/ outputs/ (gitignored)
├── models/                 # bind mount — HF + JIT caches (gitignored)
└── docs/
```

### Design decisions worth stating

- **Lazy loading (option B).** The pipeline loads on the first `/generate`, not at
  container start. `docker compose up -d` therefore costs a few hundred MB, not tens
  of GB. Once loaded the model stays resident for the container's lifetime; that is
  the right trade because reloading a 16 GB checkpoint per request would dominate
  runtime. `/system` reports `model.loaded` so the state is never a guess.
- **Refuse, never evict.** The memory gate returns HTTP 503 with
  `{"error":"insufficient_memory", ...}`. It does not call Ollama's unload endpoint
  and does not stop containers. Freeing someone else's model is a user decision.
- **In-process lock, not a queue.** One GPU means concurrency is 1 by definition.
  `threading.Lock` with a non-blocking acquire returns HTTP 409 if a generation is
  already running. Redis would add a service to solve a problem we do not have.
- **Synchronous `/generate`.** Generation blocks the request. That is acceptable for
  a backend smoke-test milestone; the dashboard milestone adds job IDs and polling.

---

## GPU / GB10 compatibility — what the ARM64 + sm_121 build actually required

GB10 is compute capability **12.1**. Stock PyTorch wheels ship `sm_120` cubins and
`compute_120` PTX only, so anything compiled against them must be told about 12.1
explicitly or it silently falls back to PTX JIT (or fails outright).

| Concern | What was done | Why |
| --- | --- | --- |
| **CUDA arch** | `ENV TORCH_CUDA_ARCH_LIST="12.1+PTX"` set before every source build | Emits native sm_121 cubins; `+PTX` keeps a forward-compatible PTX fallback |
| **Base image** | `nvcr.io/nvidia/cuda:12.9.1-cudnn-devel-ubuntu24.04` (has a real `linux/arm64` manifest) | CUDA 12.9 rather than 13.0 because flash-attn 2.7.x does not build against the CUDA 13 toolchain. A 12.9-compiled binary runs fine on the host's CUDA 13.0 / 580.x driver (backward compatibility). |
| **PyTorch** | `torch==2.9.1` from `download.pytorch.org/whl/cu129` (aarch64 cp312 wheel) | Pinned, stable — not a nightly. `torch` and `torchvision` are resolved in one pip invocation so the resolver cannot silently upgrade torch past the pin. |
| **torchvision** | **Stock wheel, NOT rebuilt from source** | Deviation from the reference implementation. TRELLIS.2 imports `torchvision.transforms` (CPU) and nothing else — no `torchvision.ops`, no NMS/RoI/deform_conv. There is no torchvision CUDA kernel on the inference path, so a source rebuild buys nothing and costs ~20 min of build time. |
| **flash-attention** | `flash-attn==2.7.4.post1`, source build, `--no-build-isolation`, `MAX_JOBS=8` | **Not optional.** TRELLIS.2's *sparse* attention backend accepts only `flash_attn`/`xformers` — unlike the dense path it has no `sdpa`/`naive` fallback (`trellis2/modules/sparse/config.py`). It calls `flash_attn_varlen_qkvpacked_func` and `flash_attn_varlen_kvpacked_func`. No aarch64 wheel exists on PyPI. sm_121 is binary-compatible with sm_120 codegen, so a 12.1 target builds and runs. |
| **nvdiffrast** | v0.4.0, source | Used through `RasterizeCudaContext` (see EGL section) |
| **nvdiffrec** | `renderutils` branch, source | PBR split-sum renderer |
| **CuMesh / FlexGEMM / o-voxel** | source, all with arch 12.1 | FlexGEMM is Triton-based; `TRITON_PTXAS_PATH` is set so Triton finds `ptxas` |
| **flash-attn arch explosion** | `ENV FLASH_ATTN_CUDA_ARCHS=121` | **Found the hard way.** flash-attn's `setup.py` builds its gencode list in `cuda_archs()`, which defaults to `"80;90;100;120"` and **ignores `TORCH_CUDA_ARCH_LIST`** for those. Combined with our 12.1 target that is five architectures per translation unit. The first build attempt spawned 49 concurrent `cicc` processes at 4-6 GB each and drove host `MemAvailable` to **7.7 GiB**, well under the content-factory 40 GiB gate. Setting the arch list to `121` makes all four hardcoded branches miss, leaving only `TORCH_CUDA_ARCH_LIST`'s sm_121. |
| **Build parallelism** | `MAX_JOBS=4`, `NVCC_THREADS=1` | flash-attn defaults `NVCC_THREADS` to 2 and multiplies it by `MAX_JOBS`, so `MAX_JOBS` alone does not bound concurrency. Treat these as memory-safety controls on a shared-memory box, not speed knobs. |
| **o-voxel / Eigen** | `git submodule update --init --recursive` on the TRELLIS.2 clone | o-voxel's C++ sources `#include <Eigen/Dense>` and its `setup.py` adds `third_party/eigen` to `include_dirs`. Eigen is a **git submodule**, so a plain (non-recursive) clone fails with `fatal error: Eigen/Dense: No such file or directory`. The upstream README's `git clone` command does not mention this. |
| **Obsolete deps** | **Not installed:** Kaolin, xformers, spconv, diffoctreerast, mip-splatting, vox2seq | TRELLIS.2 replaced these with CuMesh/FlexGEMM/o-voxel. Kaolin in particular has no aarch64 wheels and would have been the hard blocker on this machine. |
| **pillow-simd** | Replaced with stock `Pillow` | `setup.sh --basic` installs `pillow-simd`, which has no aarch64 build |
| **MAX_JOBS** | Capped at 8 (of 20 cores) | Keeps the flash-attn compile from starving the running ComfyUI / content-factory services |

TRELLIS.2 source is pinned at commit `75fbf0183001ed9876c8dbb35de6b68552ee08bd`.

---

## EGL / NVIDIA graphics

### Is it on the critical path? No — but it is configured and verified anyway.

TRELLIS.2 constructs `dr.RasterizeCudaContext()` in every place it rasterizes
(`trellis2/renderers/mesh_renderer.py`, `trellis2/renderers/pbr_mesh_renderer.py`,
`trellis2/pipelines/trellis2_texturing.py`, `o-voxel/o_voxel/postprocess.py`). It never
uses `RasterizeGLContext`. So image→3D→GLB does **not** depend on EGL at all.

It is still set up correctly, because the Step 1 audit flagged the llvmpipe trap and
because a silent software-rendering fallback is exactly the kind of thing that costs a
day later.

### What was needed

1. **`NVIDIA_DRIVER_CAPABILITIES: compute,utility,graphics`** in `docker-compose.yml`.
   The reference implementation sets only `compute,utility`, which means the container
   runtime never injects `libEGL_nvidia.so.0` and EGL cannot find an NVIDIA ICD.
2. **An explicit NVIDIA EGL ICD manifest, written into the image.** NGC `cuda:*-devel`
   images ship `libglvnd` but no `/usr/share/glvnd/egl_vendor.d/10_nvidia.json`. Without
   it libglvnd enumerates only Mesa and `eglInitialize` quietly yields llvmpipe. The
   Dockerfile writes:

   ```json
   {
       "file_format_version" : "1.0.0",
       "ICD" : { "library_path" : "libEGL_nvidia.so.0" }
   }
   ```

   **Host graphics configuration was not touched.** The fix lives entirely in our image.

### Verification

```bash
docker compose exec trellis python /app/scripts/verify_stack.py
```

Output (2026-09-09, abridged to the graphics section):

```
[graphics / EGL]  (informational: TRELLIS.2 uses the CUDA rasterizer)
  [ OK ] EGL NVIDIA ICD: 2 device(s); NVIDIA ICD active ->
         device0: vendor='NVIDIA' version='1.5'; device2: vendor='Mesa Project' version='1.5'
  [ OK ] EGL GL context (llvmpipe check): GL_RENDERER='NVIDIA GB10/PCIe',
         GL_VENDOR='NVIDIA Corporation', GL_VERSION='4.6.0 NVIDIA 580.173.02'
  [ OK ] nvdiffrast RasterizeGLContext: RasterizeGLContext OK, 20808 px covered
```

**EGL is hardware accelerated.** `GL_RENDERER` is `NVIDIA GB10/PCIe`, not `llvmpipe`, and
nvdiffrast's OpenGL rasterizer produces real coverage through it. Two EGL devices are
enumerated (the NVIDIA ICD and Mesa); the NVIDIA one is selected. Some benign
`libEGL warning: ... failed to create dri2 screen` lines appear on stderr while
libglvnd probes the Mesa vendor first — they do not affect the NVIDIA path.

Full verification run, all components:

```
[core CUDA]
  [ OK ] torch + sm_121: torch 2.9.1+cu129, cuda 12.9, NVIDIA GB10, sm_121,
         arch_list=['sm_80', 'sm_90', 'sm_100', 'sm_120']
  [ OK ] CUDA kernel execution: bf16 matmul 2048^2 OK, mean=0.0174
[TRELLIS.2 native extensions]
  [ OK ] flash-attention (varlen): flash_attn 2.7.4.post1, varlen_qkvpacked OK, out=(256, 8, 64)
  [ OK ] nvdiffrast (CUDA raster): RasterizeCudaContext OK, 20808 px covered
  [ OK ] nvdiffrec / renderutils: nvdiffrec_render imported
  [ OK ] CuMesh: cumesh imported
  [ OK ] FlexGEMM: flex_gemm imported
  [ OK ] o-voxel: o_voxel.postprocess.to_glb present
[SPARSE] Conv backend: flex_gemm; Attention backend: flash_attn
  [ OK ] trellis2 import: trellis2.pipelines imports cleanly
RESULT: all required checks passed
```

Note `arch_list` — **PyTorch itself still has no sm_121 cubins** and reaches GB10 through
`compute_120` PTX JIT. Only the extensions we compiled are natively sm_121. This is
expected and matches the Step 1 audit.

---

## Generation modes

Forge3D exposes two modes. Both produce the **same full PBR output** — 4096x4096
base-colour and metallic-roughness textures, WebP-encoded. The difference is
geometry resolution, time, and peak memory, not texture quality.

| Mode | `pipeline_type` | Warm generation | Total (warm) | Peak GPU | `MemAvailable` floor | Notes |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| **Standard** (default) | `512` | ~100 s | ~100 s | 5,034 MiB | **56.5 GiB** | Lower memory use. The interactive default. |
| **High Quality** | `1024_cascade` | ~200 s | ~200 s | 14,714 MiB | **40.1 GiB** | Higher memory use. Denser mesh. |

`512` is the default because it is the mode you can run comfortably alongside the
other services on this box. `1024_cascade` remains fully supported and is selected
per request:

```bash
curl -F "image=@ref.png" -F "pipeline_type=1024_cascade" http://127.0.0.1:8189/generate
```

`1024` and `1536_cascade` are also accepted by the API. `1536_cascade` is untested
here and should be assumed to need materially more headroom than `1024_cascade`.

Change the default for every request with `TRELLIS_PIPELINE_TYPE` in `.env`.

## Build

```bash
cd ~/3d-generator
cp .env.example .env        # then set HF_TOKEN
docker compose build
```

Measured on this host:

| | |
| --- | --- |
| Clean build time | **~38 min** (dominated by the flash-attn compile) |
| Final image size | **19.4 GB** |
| Peak build memory | min `MemAvailable` **58.6 GiB**, max **11** concurrent `cicc` |
| Rebuild after an app-code edit | **~15 s** (only the final `COPY` layers invalidate) |

The layer order puts `COPY app/` last on purpose, so editing the API never re-triggers
any compile. `MAX_JOBS` is a build arg if you need to tune it further:

```bash
docker compose build --build-arg MAX_JOBS=2
```

## Start

```bash
cd ~/3d-generator
docker compose up -d
# or, with a memory pre-flight check:
./scripts/start.sh
```

## Stop

```bash
docker compose down
# or:
./scripts/stop.sh          # also prints the memory that came back
```

`restart: "no"` is set deliberately — the service never comes back on its own after a
reboot, so it cannot silently reclaim unified memory.

## Test

```bash
curl -s http://127.0.0.1:8189/health  | python3 -m json.tool
curl -s http://127.0.0.1:8189/system  | python3 -m json.tool

# full CUDA / graphics stack verification inside the container
docker compose exec trellis python /app/scripts/verify_stack.py

# a real generation
# Standard mode (512) is the default, so pipeline_type may be omitted
curl -sS --max-time 7200 \
  -F "image=@/path/to/reference.png" \
  -F "seed=42" \
  http://127.0.0.1:8189/generate | python3 -m json.tool

# High Quality mode
curl -sS --max-time 7200 \
  -F "image=@/path/to/reference.png" \
  -F "seed=42" \
  -F "pipeline_type=1024_cascade" \
  http://127.0.0.1:8189/generate | python3 -m json.tool

# or: ./scripts/generate.sh /path/to/reference.png [pipeline_type] [seed]
```

### Verified results (2026-09-09)

`/health`:

```json
{ "status": "ok", "model": "TRELLIS.2-4B", "gpu_available": true }
```

`/system` (idle, model not loaded):

```json
{
  "memory": { "total_gb": 121.69, "used_gb": 37.39, "available_gb": 83.12,
              "min_required_gb": 65.0, "sufficient": true },
  "gpu": { "available": true, "name": "NVIDIA GB10", "capability": "12.1",
           "torch": "2.9.1+cu129", "torch_allocated_gb": 0.0, "torch_reserved_gb": 0.0 },
  "model": { "repo": "microsoft/TRELLIS.2-4B", "loaded": false, "load_error": null,
             "default_pipeline_type": "512" },
  "generation": { "active": false, "current": null }
}
```

> Re-captured after the defaults were changed to `TRELLIS_MIN_AVAILABLE_GB=65` and
> `TRELLIS_PIPELINE_TYPE=512`. The generation timings and memory figures below were
> measured under the original settings; the numbers are properties of each mode, not
> of the gate value, so they still stand.

### Real generations

Test input: a 774x774 RGBA render of an ornate crown (from TRELLIS.2's own
`assets/example_image/`), seed 42.

| `pipeline_type` | pipeline load | generation | GLB export | **total** | GLB size | vertices | faces |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `512` | 74.2 s | 71.0 s | 29.0 s | **174.2 s** | **39.55 MB** | 803,278 | 958,254 |
| `1024_cascade` (default) | 0 s (resident) | 161.6 s | 39.2 s | **200.8 s** | **43.07 MB** | 905,007 | 996,059 |

Both produced a valid GLB with a full PBR material — 4096x4096 base-colour and
metallic-roughness textures, WebP-encoded.

### GLB validation

The API validates every export with trimesh and records the result in `metadata.json`:

```json
"validation": {
  "readable": true, "mesh_count": 1, "vertices": 803278, "faces": 958254,
  "materials": [{ "name": "PBRMaterial",
                  "baseColorTexture": [4096, 4096],
                  "metallicRoughnessTexture": [4096, 4096] }],
  "has_texture": true, "has_geometry": true
}
```

Independently re-checked on the host by parsing the glTF container directly:

```
magic: glTF | glTF version: 2 | declared length: 41471468 | actual: 41471468 | match: True
meshes: 1 | materials: 1 | images: 2
  material: baseColorTexture: True | metallicRoughnessTexture: True
image mimeTypes: ['image/webp', 'image/webp']
```

### Error paths (all verified)

| Case | Result |
| --- | --- |
| Second `/generate` while one is running | `409` `{"error":"generation_in_progress","current":{"id":"..."}}` |
| `MemAvailable` below the floor | `503` `{"error":"insufficient_memory","available_gb":83.1,"required_gb":200.0}` |
| Bad `pipeline_type` | `400` `{"error":"invalid_pipeline_type","given":"9999","valid":[...]}` |

### Persistence

```
data/outputs/20260910-045809-5fb913aa/model.glb   41,471,468 bytes
data/outputs/20260910-050228-547a150e/model.glb   45,160,152 bytes
```

Both survive `docker compose down` and image rebuilds. Files are owned by the host
user (via `user: "${UID}:${GID}"`), not root. `models/` holds 17 GB of weights across
four HF repos and is never re-downloaded on rebuild.

---

## Model access (Hugging Face)

`HF_TOKEN` is read from `.env` and passed through compose. It is never baked into the
image and `.env` is gitignored.

| Repo | Gated | Size | Purpose |
| --- | --- | --- | --- |
| `microsoft/TRELLIS.2-4B` | no | ~16 GB | The pipeline and all eight checkpoints |
| `facebook/dinov3-vitl16-pretrain-lvd1689m` | **yes — manual approval** | ~1.1 GB | `image_cond_model` — the image conditioner |
| `briaai/RMBG-2.0` | **yes — auto (accept terms)** | ~0.9 GB | `rembg_model` — background removal |

Both gated repos are named in `pipeline.json` inside `microsoft/TRELLIS.2-4B` and are
constructed **eagerly** by `Trellis2ImageTo3DPipeline.from_pretrained()`, so the pipeline
cannot load at all until access is granted — even for an input image that already has an
alpha channel and would skip background removal at inference time.

**Status: access granted and verified working.** Both gated repos were requested and
approved during this milestone; the pipeline now loads and generates end to end.

To check access for a token without downloading anything (401 = no token, 403 =
authenticated but not approved, 200/206 = granted):

```bash
curl -s -o /dev/null -w "%{http_code}\n" -r 0-100 \
  -H "Authorization: Bearer $HF_TOKEN" \
  https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m/resolve/main/config.json
```

The HF cache on the bind mount ends up holding:

```
models--microsoft--TRELLIS.2-4B
models--facebook--dinov3-vitl16-pretrain-lvd1689m
models--briaai--RMBG-2.0
models--microsoft--TRELLIS-image-large
```

---

## Memory behavior

All figures measured on this host with Ollama holding its usual ~32.9 GB and ComfyUI
running. Host baseline `MemAvailable` was **83.2 GiB**.

| Phase | `MemAvailable` | Container RSS | Our GPU allocation |
| --- | ---: | ---: | ---: |
| Baseline, container not running | 83.2 GiB | — | 0 MiB |
| **Container up, model not loaded** | **83.4 GiB** | **43.7 MiB** | **0 MiB** |
| After first `/health` (torch imported) | 83.1 GiB | 316.4 MiB | 0 MiB |
| Model loaded (resident, idle) | 68.3 GiB | ~16.3 GiB | 0 MiB |
| Generation peak — `512` | 56.5 GiB | 21.8 GiB | 5,034 MiB |
| Generation peak — `1024_cascade` | **40.1 GiB** | 28.8 GiB | 14,714 MiB |
| **After `docker compose down`** | **83.4 GiB** | — | **0 MiB** |

`docker compose down` returns the machine exactly to baseline. Verified immediately
before and after:

```
before:  MemAvailable 42.1 GiB   our GPU proc 13,535 MiB   container 27.96 GiB
after:   MemAvailable 83.4 GiB   our GPU proc gone         container removed
```

Only Ollama (32,888 MiB) and ComfyUI (170 MiB) remain on the GPU, unchanged.

Two things worth internalising:

- **Idle cost is 43.7 MiB and zero GPU.** Lazy loading does what it was meant to.
- **The model sits in host RAM, not "VRAM".** TRELLIS.2 runs with `low_vram=True`, so
  weights live in the unified pool and stages are moved to the GPU on demand. That is
  why container RSS is ~16 GB while the GPU allocation is 0 MiB between stages.

### The gate

`TRELLIS_MIN_AVAILABLE_GB` (default **65**) is checked against `/proc/meminfo`
`MemAvailable` before loading the model and again before each generation. Below the
floor, the API returns HTTP 503:

```json
{ "error": "insufficient_memory", "available_gb": 36.2, "required_gb": 45 }
```

Inside the container `/proc/meminfo` reports the **host's** values, which is what we
want: on GB10 there is no separate VRAM pool, so `MemAvailable` is the single number
that governs safety.

### Why the floor is 65 GiB, not 45

The gate is **pre-flight only** — it runs before a generation starts, and the
generation keeps drawing memory after it passes. The measured drawdown is:

| Mode | Started at | Bottomed out at | Drawdown |
| --- | ---: | ---: | ---: |
| `512` | 83.4 GiB | 56.5 GiB | ~11 GiB (plus ~16 GiB model load) |
| `1024_cascade` | 57.6 GiB | **40.1 GiB** | **~17.5 GiB** |

A 45 GiB floor could therefore be satisfied at launch and still let a
`1024_cascade` run cross content-factory's own 40 GiB threshold mid-flight —
exactly what happened during Step 2 testing, where the run bottomed out level
with that gate.

**65 GiB is chosen so that even the worst-case mode stays clear of 40 GiB for the
whole run**: 65 − 17.5 ≈ 47.5 GiB, a ~7.5 GiB margin over content-factory's gate.
For `512` the margin is larger still.

The floor is deliberately above the content-factory worker's own
`VISUAL_MIN_AVAILABLE_GB=40` so that Forge3D refuses first rather than pushing that
service below its threshold. **`VISUAL_MIN_AVAILABLE_GB` was not modified.**

---

## Known limitations

1. **The memory gate is pre-flight, not continuous.** It checks `MemAvailable`
   before starting; a generation then keeps drawing (~11 GiB for `512`, ~17.5 GiB
   for `1024_cascade`). The 65 GiB floor is sized so that even `1024_cascade`
   stays clear of content-factory's 40 GiB gate for the whole run, but the check
   itself still only happens once, up front. If something else on the box
   allocates heavily *during* a generation, nothing re-checks.
   - Still worth avoiding: a `1024_cascade` run concurrent with a content-factory
     visual job.
   - `VISUAL_MIN_AVAILABLE_GB` was not modified.

2. **`/generate` is synchronous.** A `1024_cascade` request holds the HTTP connection
   for 3-4 minutes. Fine for curl and for this milestone; the dashboard milestone adds
   job IDs and polling.

3. **`transformers` is pinned to 4.57.1** and cannot currently move to 4.58+ without
   patching TRELLIS.2's `DinoV3FeatureExtractor`, which reaches into model internals.

4. **PyTorch reaches GB10 through PTX JIT**, not native cubins (`arch_list` tops out at
   `sm_120`). Only our compiled extensions are native sm_121. Expect a one-off JIT
   warm-up on the first generation after each container start, and a persistent
   `UserWarning` about capability 12.1 in the logs. Both are cosmetic.

5. **The image is not portable.** flash-attn is compiled for sm_121 only
   (`FLASH_ATTN_CUDA_ARCHS=121`). It will not run on a non-GB10 GPU without a rebuild.

6. **`1536_cascade` is untested.** Given `1024_cascade` already bottoms out at 40 GiB,
   assume it needs materially more headroom before trying it.

7. **No preview render.** `render_utils` / video output is deliberately not wired up;
   only the GLB is produced.

8. **One generation at a time**, by design (single GPU, in-process lock).

---

## Known-good state

Confirmed working at the end of this milestone (2026-09-09):

- `docker compose build` succeeds from scratch on DGX Spark / GB10 / aarch64 (~38 min, 19.4 GB image).
- `docker compose up -d` starts the API in a few seconds at **43.7 MiB and 0 MiB GPU**.
- `GET /health` and `GET /system` respond correctly; `/system` accurately reports memory,
  GPU, model-loaded state and active generation.
- `docker compose exec trellis python /app/scripts/verify_stack.py` -> **all required checks pass**:
  torch on sm_121, real CUDA kernel execution, flash-attn varlen, nvdiffrast (CUDA *and* GL),
  nvdiffrec, CuMesh, FlexGEMM, o-voxel, and a clean `trellis2` import.
- EGL is **hardware accelerated** (`GL_RENDERER='NVIDIA GB10/PCIe'`), not llvmpipe.
- `POST /generate` produces a **real, valid, textured GLB** at both `512` and
  `1024_cascade`; the largest is 43.07 MB with 905,007 vertices, 996,059 faces and
  4096x4096 PBR textures.
- Repeatable: a third generation on a **freshly recreated container** (after a
  `down`/`up` cycle) succeeded at `512`/seed 7 in 163.6 s total - 38.1 MB GLB,
  771,673 vertices, 950,450 faces - with a 72.7 s pipeline load straight from the
  cached weights, confirming nothing is re-downloaded.
- `409` / `503` / `400` error paths all behave as specified.
- Outputs and weights persist across `docker compose down` and across image rebuilds.
- `docker compose down` returns `MemAvailable` to the exact 83.4 GiB baseline and
  releases all GPU memory.
- Ollama, ComfyUI, Portainer and the Trivy watchdog were **untouched throughout**;
  Ollama held a constant 32,888 MiB and no service was stopped or reconfigured.
- `restart: "no"` — the service does not come back by itself after a reboot.

Re-verified after the defaults change (`TRELLIS_MIN_AVAILABLE_GB=65`,
`TRELLIS_PIPELINE_TYPE=512`):

- `/system` reports `min_required_gb: 65.0` and `default_pipeline_type: "512"`.
- The image-baked defaults match, checked with the environment overrides stripped:
  `MIN_AVAILABLE_GB = 65.0`, `DEFAULT_PIPELINE_TYPE = 512`.
- `1024_cascade` is still accepted: with an artificially impossible floor it returns
  `503 insufficient_memory` (i.e. it passed type validation) rather than `400`.
- An unknown mode still returns `400 invalid_pipeline_type` listing all four valid modes.
- PBR / 4096x4096 texture output is unchanged — the mode selects geometry resolution,
  not texture quality.
