# Step 03 — Forge3D dashboard

> Historical milestone record. Use [the current README](../README.md) and [SETUP.md](SETUP.md) for installation and current behavior.

**Host:** `aitopatom-c85a` — GIGABYTE AI TOP ATOM (GB10 Grace-Blackwell, DGX Spark class)
**Scope:** A browser UI served by the existing FastAPI container. No new services.
**Prerequisites:** [step-01-dgx-audit.md](step-01-dgx-audit.md) · [step-02-trellis-backend.md](step-02-trellis-backend.md)

Open `http://<DGX-IP>:8189` and use Forge3D without touching a terminal.

---

## Architecture

Still exactly one container. The dashboard is static files served by the same
FastAPI app that runs the model — no Node, no bundler, no second image.

```
Browser
  │  http://<DGX-IP>:8189
  ▼
┌──────────────────────────────────────────────────────────────┐
│ container: 3d-generator-trellis   (uvicorn, port 8189)       │
│                                                              │
│  GET /                → app/static/index.html                │
│  GET /static/*        → CSS · JS · vendored three.js         │
│                                                              │
│  POST /jobs ─────────┐   non-blocking: returns 202 + job_id  │
│  GET  /jobs/{id}     │   browser polls every 1.5 s           │
│                      ▼                                       │
│            jobs.py: ThreadPoolExecutor(max_workers=1)        │
│                      │  real state callbacks                 │
│                      ▼                                       │
│            engine.py  (lazy TRELLIS.2, in-process lock)      │
│                      │                                       │
│  /assets*  ◄─────── assets.py: reads data/ directly          │
└──────────────────────────────────────────────────────────────┘
        │                              │
        ▼                              ▼
   data/uploads/<id>/            data/outputs/<id>/
     reference.png                 model.glb
                                   metadata.json   ← name lives here
```

There is **no database**. `data/outputs/` is the source of truth; the library is
rebuilt by reading the directory on every request, which is why assets simply
reappear after a restart with no migration or import step.

### Files added in this milestone

```
app/
├── assets.py          asset library — list / read / rename / delete, path safety
├── jobs.py            in-process job queue and state machine
└── static/
    ├── index.html     the whole dashboard markup
    ├── style.css      dark theme
    ├── app.js         upload, modes, jobs, library, modals
    ├── viewer.js      three.js GLB viewer
    └── vendor/three/  three.js, vendored at build time (see below)
docs/step-03-dashboard.md
```

`app/engine.py` gained one optional `on_state` callback; `app/main.py` gained the
new routes. Nothing from Step 2 changed behaviour.

---

## Frontend structure

Plain ES modules, no build step, no framework. Three files plus vendored three.js.

| Layer | What it does |
| --- | --- |
| `index.html` | Static markup. An import map points `three` and `three/addons/` at `/static/vendor/three/…`. |
| `style.css` | ~190 lines. CSS custom properties, `grid` layout, dark palette with a single warm accent. |
| `app.js` | Drag-and-drop, file validation, mode selection, job polling, asset library, rename/delete modals, toast. |
| `viewer.js` | A `Viewer` class wrapping `WebGLRenderer` + `OrbitControls` + `GLTFLoader`. |

**three.js is vendored, not loaded from a CDN.** The Dockerfile fetches five files
at build time into `/app/app/static/vendor/three/`, preserving the upstream
`examples/jsm/` layout because `GLTFLoader.js` imports
`'../utils/BufferGeometryUtils.js'` relatively. The dashboard therefore works with
no outbound internet access from the browser:

```
build/three.module.min.js                      687 KB
examples/jsm/loaders/GLTFLoader.js             110 KB
examples/jsm/controls/OrbitControls.js          32 KB
examples/jsm/utils/BufferGeometryUtils.js       32 KB
examples/jsm/environments/RoomEnvironment.js     4 KB
```

Pinned by `ARG THREE_VERSION=0.169.0`.

### Layout

```
┌─────────────────────────────────────────────────────────────┐
│ Forge3D                        58.2 / 65 GiB   ● Ready      │
├────────────┬────────────────────────────────────────────────┤
│ Reference  │                                                │
│  [drop]    │                                                │
│            │            3D VIEWPORT                         │
│ Mode       │                            [Reset view]        │
│  ○ Standard│                                                │
│  ○ High Q. │                                                │
│            │                                                │
│ [Generate] │                                                │
│ job status │                                                │
├────────────┴────────────────────────────────────────────────┤
│ Assets 4    [thumb] name · Standard · 38.1 MB  [Open][…]    │
└─────────────────────────────────────────────────────────────┘
```

---

## New API endpoints

Everything from Step 2 (`GET /health`, `GET /system`, `POST /generate`) is
unchanged and still works.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | The dashboard |
| `GET` | `/static/*` | CSS, JS, vendored three.js |
| `POST` | `/jobs` | Start a generation. `202` + job record, or `503` / `409` / `400` |
| `GET` | `/jobs/{job_id}` | Poll job state |
| `GET` | `/assets` | `{count, assets[]}` — newest first |
| `GET` | `/assets/{id}` | One asset summary |
| `GET` | `/assets/{id}/metadata` | The raw `metadata.json` |
| `GET` | `/assets/{id}/model.glb` | Serve the GLB inline (viewer) |
| `GET` | `/assets/{id}/download` | Same bytes as an attachment, named from the asset name |
| `GET` | `/assets/{id}/thumbnail` | The reference image |
| `PATCH` | `/assets/{id}` | `{"name": "..."}` — rename |
| `DELETE` | `/assets/{id}` | Delete this generation |

`/system` gained one field: `generation.job`, the active job record (or `null`).

### Upload validation

Reference images are validated by **decoding the bytes with Pillow**, not by
trusting the filename or the client's content-type. Only `PNG`, `JPEG` and `WEBP`
decode successfully; anything else returns `400 unsupported_image`. A 32 MB cap
returns `413 image_too_large`. The browser does a cheap extension/MIME check first
purely for instant feedback — the server check is the real one.

---

## Job state implementation

`app/jobs.py`: one `ThreadPoolExecutor(max_workers=1)`, a dict of job records, a
lock. No Redis, no Celery, no broker. There is exactly one GPU, so serial
execution is correct rather than a compromise.

States, all real transitions reported by the engine — **no synthetic percentage
is invented**:

```
queued ──► loading_model ──► generating ──► exporting ──► complete
   │            (skipped when TRELLIS is already resident)  │
   └──────────────────────── error ◄────────────────────────┘
```

`engine.generate()` takes an optional `on_state` callback and fires it at the
three real boundaries. `loading_model` is only emitted when the pipeline is not
already resident, so the second and later generations go straight to `generating`.

The UI shows the state name, a live elapsed-seconds counter, and an
indeterminate bar (motion, not a fake percentage).

Job records are in-memory and capped at the 200 most recent. Losing them on
restart costs nothing — the *assets* are on disk and are listed from there.

Memory is checked twice: at `POST /jobs` so the browser gets the real numbers
immediately, and again in the worker in case something else took memory while the
job sat in the queue.

---

## Asset filesystem format

```
data/
├── uploads/<generation-id>/reference.png      the reference image
└── outputs/<generation-id>/
    ├── model.glb                              the mesh
    └── metadata.json                          everything else
```

`<generation-id>` is `YYYYmmdd-HHMMSS-<8 hex>`, minted by `engine.py`.

`metadata.json` carries the Step 2 fields plus one addition, `name`, which is the
**only** thing a rename touches:

```json
{
  "id": "20260910-050734-41b4f4ed",
  "name": "Renamed Crown E2E",
  "created_at": "...", "pipeline_type": "512", "seed": 7,
  "glb_bytes": 39953696, "timings_s": {...}, "validation": {...}
}
```

- **Rename** rewrites `name` via an atomic `os.replace`. The id, the directory
  names and every URL stay exactly as they were.
- **Delete** removes `outputs/<id>/` and `uploads/<id>/` — the two directories
  keyed by that one generation id. Nothing else is touched.
- A directory without a readable `model.glb` **and** `metadata.json` is skipped by
  the listing. That is not hypothetical: the failed DINOv3 run from Step 2 left
  `20260910-045052-fcf04c5c` behind with neither, and it correctly never appears.

### Path-traversal defence

Two independent layers in `assets.py`:

1. Every id must match `^\d{8}-\d{6}-[0-9a-f]{8}$` exactly. Anything else raises
   before touching the filesystem.
2. The resolved path is then re-checked to confirm it is genuinely inside the data
   directory, which also catches symlinks.

The browser never sends a filesystem path — only an id. See the test table below.

---

## Viewer implementation

`viewer.js`, ~140 lines around three.js:

- `WebGLRenderer` with `ACESFilmicToneMapping`.
- **`RoomEnvironment` through `PMREMGenerator` for neutral IBL.** This matters:
  TRELLIS emits metallic PBR materials, and without an environment map metallic
  surfaces render essentially black. A key light, a fill light and a little
  ambient sit on top of the IBL so form reads clearly.
- `OrbitControls` with damping — orbit, rotate, zoom, pan.
- **Auto-frame**: the model's bounding box is measured, the model is recentred on
  the origin, and the camera is pulled back to `radius / sin(fov/2) * 1.35` so the
  subject fits with a margin regardless of its scale. `near`/`far` are derived
  from that distance. "Reset view" returns to this framing.
- `clear()` disposes geometries, materials and textures before loading the next
  model, so browsing the library does not leak GPU memory.

`EXT_texture_webp` (which our GLBs require) is handled by three's stock
`GLTFLoader` — no extra plugin.

---

## Tests performed

All tests were run against the real container on the DGX Spark. The browser tests
drive a real headless Chromium (Playwright) against the live dashboard, including
one genuine TRELLIS generation — nothing is mocked.

### Browser end-to-end — 19/19 passed

| # | Check | Result |
| --- | --- | --- |
| 1 | `/` dashboard loads | PASS — `title='Forge3D'` |
| 1b | three.js + ES modules initialise | PASS |
| 1c | no JS page errors | PASS |
| 2 | **TRELLIS stays unloaded after opening the dashboard** | PASS — `model.loaded=False` |
| 13a | existing assets listed from disk | PASS — 3 assets |
| 4 | invalid file type fails cleanly | PASS — `Unsupported file type "text/plain". Use PNG, JPG, JPEG or WebP.` and Generate stays disabled |
| 3 | image upload + preview | PASS |
| 3b | Standard (512) selected by default | PASS |
| 5 | **Standard generation completes** | PASS — `queued → loading_model → generating → exporting → complete` |
| 6 | job status updates visibly | PASS — 5 distinct states observed |
| 7 | generated GLB loads in viewer | PASS — 811,111 verts, 968,562 tris, 1 material |
| 7b | viewport renders a non-blank frame | PASS — 1,802 distinct colours |
| 8 | orbit / rotate | PASS |
| 8b | zoom | PASS — distance 1.7688 → 1.3002 |
| 8c | reset camera | PASS — returns to the home framing |
| 9 | download | PASS — `crown.glb`, 41,831,780 bytes, magic `b'glTF'` |
| 10 | rename | PASS — name becomes `Renamed Crown E2E` |
| 10b | rename keeps id + directory stable | PASS |
| 12a | delete asks for confirmation; cancel is safe | PASS |

Observed job timings for the real Standard generation (cold, model not resident):

```
queued          0s
loading_model   1s
generating     73s
exporting     113s
complete      142s
```

### Restart, delete and teardown — all passed

| # | Check | Result |
| --- | --- | --- |
| 11 | renamed value survives container restart | PASS — `'Renamed Crown E2E'` before and after `down`/`up` |
| 13 | old assets reappear after restart | PASS — 4 → 4 assets |
| 2b | TRELLIS lazy again after restart | PASS — `loaded=False`, idle RSS 306.7 MiB |
| 15 | Step 2 `POST /generate` still works | PASS — still validates, `400` on a bad mode |
| 12 | delete removes the output directory | PASS |
| 12b | delete removes its upload directory | PASS |
| 12c | deleted asset `404`s afterwards | PASS |
| 12d | other assets untouched | PASS — 4 → 3 |
| 16 | `docker compose down` releases GPU | PASS — 0 GPU processes besides ollama/comfyui |
| 16b | `docker compose down` restores memory | PASS — 82.5 GiB available |

### Path traversal — 14 vectors, all rejected, nothing leaked

| Vector | Result |
| --- | --- |
| `../../../../etc/passwd` | 404 |
| `..%2F..%2F..%2Fetc%2Fpasswd` | 404 |
| `%2e%2e%2f..%2fetc%2fpasswd` | 404 |
| `....//....//etc/passwd` | 404 |
| `.%2e/.%2e/etc/passwd` | 404 |
| `..%252f..%252fetc%252fpasswd` (double-encoded) | **400** (regex) |
| `<valid-id>%2F..%2F..%2Fetc` | 404 |
| traversal + `/model.glb`, `/download`, `/thumbnail` | 404 |
| `%00` null byte | **400** |
| `abc` (non-id) | **400** |
| `2026091-050734-41b4f4ed` (malformed date) | **400** |
| `20260910-050734-41B4F4ED` (uppercase hex) | **400** |
| `PATCH`/`DELETE` with a traversal id | 404 |

Response bodies were checked: every attempt returned `{"detail":"Not Found"}` or the
JSON error — no file contents were ever served.

### Upload validation

| Input | Result |
| --- | --- |
| `bad.txt` (plain text) | `400 unsupported_image` |
| `bad.pdf` | `400 unsupported_image` |
| `fakegif.png` — **GIF bytes with a `.png` name** | `400 unsupported_image` |
| 37 MB file | `413 image_too_large` |
| real PNG | accepted |

The disguised-GIF case is the important one: validation decodes the bytes, so a
misleading extension does not get through.

---

## Known-good behaviour

Confirmed on 2026-09-09 against the live container:

- The dashboard is served by the same FastAPI container on port **8189**, reachable at
  `http://<tailscale-ip>:8189` (the host's Tailscale address).
- **Opening the dashboard does not load TRELLIS.** Container RSS is **34.6 MiB** with the
  container up and no requests, and **307 MiB** after serving the UI and browsing the
  whole asset library. GPU allocation stays at **0 MiB** and `model.loaded` stays `false`.
- A real Standard (512) generation runs end to end from the browser, reports five real
  states, and the finished GLB auto-loads into the viewer.
- The viewer orbits, zooms, pans and resets; PBR materials render correctly under the
  `RoomEnvironment` IBL.
- Download, rename and delete all work from the asset cards. Delete requires confirmation.
- Renames and assets survive `docker compose down` / `up`, because both live in
  `metadata.json` on the bind mount.
- `docker compose down` returns the machine to **82.5 GiB** available with **0** of our
  GPU processes remaining.
- Ollama, ComfyUI, Portainer and the Trivy watchdog were untouched throughout.

### One real bug the browser test caught

The first end-to-end run failed at "click Generate" with
`<div hidden id="modal"> intercepts pointer events`.

`.modal { display:grid }` and `.vp-loading { display:flex }` have the same CSS
specificity as the user-agent's `[hidden] { display:none }`, and came later in
source order — so they won. The full-screen modal overlay was therefore *always*
displayed, invisible against the dark background but swallowing every click. The
dashboard would have looked perfect in a screenshot and been completely unusable.

Fixed with an explicit global rule:

```css
[hidden]{display:none !important}
```

This is exactly the class of bug that only a real browser catches.

---

## Regression: `cudaErrorNoKernelImageForDevice` on GB10

A dashboard generation failed with:

```
torch.AcceleratorError: CUDA error: no kernel image is available for execution on the device
```

### Root cause

**`torchvision.ops.deform_conv2d`.** The stock torchvision aarch64 wheel ships CUDA
cubins for `sm_50 … sm_90` and **no PTX at all**, so on GB10 (`sm_121`) there is
nothing the driver can load and nothing it can JIT.

Full call path from the traceback:

```
app/jobs.py                      _run
app/engine.py                    generate
trellis2_image_to_3d.py:537      pipeline.run  -> self.preprocess_image(image)
trellis2_image_to_3d.py:147          output = self.rembg_model(input)
rembg/BiRefNet.py:36                     preds = self.model(input_images)
briaai/RMBG-2.0 birefnet.py:1288             x = deform_conv2d(...)
torchvision/ops/deform_conv.py:92                torch.ops.torchvision.deform_conv2d
                                                 -> AcceleratorError
```

### Why PyTorch itself is fine but torchvision is not

Neither ships an `sm_121` cubin, but only one of them works:

| Library | Cubins present | PTX | Runs on GB10? |
| --- | --- | --- | --- |
| `libtorch_cuda.so` | sm_80, sm_90, sm_100, **sm_120** | none | **yes** |
| `torchvision/_C.so` (stock wheel) | sm_50 … **sm_90** | none | **no** |

`sm_121` is binary-compatible with `sm_120` cubins — same Blackwell 12.x SASS
family. PyTorch has `sm_120`, so it loads. torchvision's newest cubin is `sm_90`
(Hopper, a different major family) and it carries no PTX, so nothing is loadable
and the driver returns `cudaErrorNoKernelImageForDevice`.

For reference, every extension built from source in this image was already correct:

```
flash-attn        ELF:[sm_121]      nvdiffrast     ELF:[sm_121]
cumesh/_C         ELF:[sm_121]      cumesh/_cubvh  ELF:[sm_121]
flex_gemm         ELF:[sm_121]      o_voxel/_C     ELF:[sm_121]
renderutils/_C    ELF:[sm_121]      torchvision    ELF:[sm_50..sm_90]  <-- the one
```

### This was not a Step 3 regression

`git diff 8676139..92c4753 -- Dockerfile` touches **only** the three.js vendoring
block. No CUDA build line, dependency version, or arch flag changed, and no
CUDA layer was invalidated or rebuilt.

The broken binary had been in the image since Step 2, where the Dockerfile
deliberately did **not** rebuild torchvision, on this reasoning:

> TRELLIS.2 imports `torchvision.transforms` (CPU) and nothing else — no
> `torchvision.ops`, no NMS/RoI/deform_conv.

That was true of the TRELLIS.2 repository, which is what was grepped. It was
false for **`briaai/RMBG-2.0`**, whose `birefnet.py` is downloaded at *runtime*
via `trust_remote_code=True` and calls `deform_conv2d`. A static grep of the
TRELLIS.2 source could never have seen it.

### Why every earlier test passed

`Trellis2ImageTo3DPipeline.preprocess_image` short-circuits:

```python
has_alpha = False
if input.mode == 'RGBA':
    alpha = np.array(input)[:, :, 3]
    if not np.all(alpha == 255):
        has_alpha = True
...
if has_alpha:      # use the alpha directly
else:              # <-- only here does rembg / BiRefNet / deform_conv2d run
```

Every test image used in Steps 2 and 3 was the same `crown.png` — RGBA **with a
real alpha channel** — so `has_alpha` was `True` and the background-removal path
was never entered. Confirmed across all reference images on disk:

| Upload | Mode | `has_alpha` | rembg |
| --- | --- | --- | --- |
| 4 assets from Steps 2–3 | RGBA | True | skipped |
| `20260910-060337-8f4e0a4f` (the failure) | **RGB** | False | **runs → deform_conv2d** |

The dashboard did not break anything — it made it trivial to upload an ordinary
opaque photo, which is the normal case for a real user and had simply never been
exercised.

### Fix

Rebuild **only** torchvision from source for `sm_121`, late in the Dockerfile so
the expensive flash-attn / nvdiffrast / CuMesh / FlexGEMM / o-voxel layers stay
cached:

```dockerfile
ARG TORCHVISION_VERSION=0.24.1          # must track torch 2.9.1
RUN git clone --depth 1 --branch "v${TORCHVISION_VERSION}" \
        https://github.com/pytorch/vision.git /tmp/vision && \
    cd /tmp/vision && \
    pip install --no-cache-dir "setuptools<81" && \
    pip uninstall -y torchvision && \
    FORCE_CUDA=1 TORCH_CUDA_ARCH_LIST="12.1+PTX" \
        pip install --no-cache-dir --no-build-isolation . && \
    cd / && rm -rf /tmp/vision
```

Two details:

- `setuptools<81` is needed for the build only — torchvision 0.24.1's `setup.py`
  imports `pkg_resources`, which setuptools removed in 81 (the image had 84).
- A build-time assertion now runs `cuobjdump --list-elf` on the result and
  **fails the build** if `sm_121` is missing, so this cannot regress silently.

The known-good GB10 configuration is unchanged: `TORCH_CUDA_ARCH_LIST=12.1+PTX`,
`FLASH_ATTN_CUDA_ARCHS=121`, `MAX_JOBS=4`, `NVCC_THREADS=1`. Nothing else was
rebuilt and the CUDA major version did not change. Peak build usage: minimum
`MemAvailable` 75.4 GiB, at most 11 concurrent `cicc`.

### Note on the "64.7 / 65 GiB — Insufficient Memory" reading

Not a leak and not a separate fault. The failed generation left the TRELLIS
pipeline resident in the container (the model stays loaded for the container's
lifetime by design). `docker compose down` returned `MemAvailable` from
**64.6 GiB → 82.5 GiB** immediately. The 65 GiB threshold was not lowered.

---

## Known limitations

1. **Job records are in memory.** `docker compose down` loses the status of an
   in-flight generation (and kills the generation itself). Finished assets are
   unaffected — they are on disk. Only the last 200 job records are kept.

2. **One generation at a time.** A second `POST /jobs` while one is running returns
   `409` with the active job. That is deliberate: one GPU.

3. **No authentication.** Anyone who can reach the bind address can generate and
   delete assets. It binds to loopback by default; `FORGE3D_BIND` is set to the
   Tailscale IP on this host, so access is limited to the tailnet. **Do not set it
   to `0.0.0.0`** — Docker publishes ports through its own iptables chain and
   bypasses ufw, so that would expose the UI to the whole LAN.

4. **The memory gate is still pre-flight only** (unchanged from Step 2). The
   dashboard surfaces the real numbers when it refuses, but nothing re-checks
   mid-generation.

5. **No thumbnail generation.** Asset cards show the full-resolution reference
   image scaled down by the browser. Fine at this scale; with hundreds of assets
   it would be worth generating real thumbnails.

6. **The library is read fresh on every `/assets` call.** With a few hundred
   generations that is still trivial; it is a directory scan plus a small JSON
   read per asset, and it is what makes restart-persistence free.

7. **Deleting is permanent.** There is no trash or undo — the confirmation dialog
   is the only guard.

8. **Orphaned directories from failed generations stay on disk.** They are
   correctly hidden from the library (no `model.glb`/`metadata.json`), but nothing
   cleans them up. `20260910-045052-fcf04c5c` from the Step 2 DINOv3 failure is
   still there.

9. **`1024` and `1536_cascade` are reachable through the API but not the UI**, which
   deliberately offers only Standard and High Quality. `1536_cascade` remains
   untested on this hardware.
